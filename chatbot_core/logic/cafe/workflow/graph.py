"""Conversation routing only. Nodes never access the session store or retry work."""
from chatbot_core.llm.streaming import final_reply, publish_reply
from chatbot_core.llm.replies import join_replies
from ..reply_renderer import render_reply
from ..reply_evidence import ordering_reply_evidence
import logging
from copy import deepcopy
from functools import lru_cache
from typing import Literal

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from chatbot_core.logic.cafe.intent_handler.base import get_intent
from chatbot_core.logic.cafe.prompts.normalize_and_classify import (
    normalize_and_classify, NormalizationClassificationError, FAILURE_REPLY,
    SYSTEM_ID,
)
from chatbot_core.queues import enqueue_string
from chatbot_core.runtime_configuration import active_configuration
from chatbot_core.capabilities import CONTROL_ROUTES
from .state import ConversationContext, ConversationState
from .lifecycle import pending_address_question
from .cancellation import stop_pending_request
from .order_context import classification_context
from chatbot_core.logic.action_resolver import NeedsClarification, TerminalRejection
from chatbot_core.logic.outcomes import TaskOutcome
from chatbot_core.logic.cafe.catalog import catalog_names, load_catalog
from .actions import (CLARIFICATION_ROUTES, action_route, bind_action, compatible_followup,
                      ordering_evidence, required_routes, requires_action, unresolved_item_answer)
from .pending import (CLARIFICATION_LIMIT_REASON, MAX_CLARIFICATION_QUESTIONS, allowed_intent,
                      resumable_pending, restore_clarification_budget, spend_clarification, valid_pending)
from .task_matching import explicit_classification, redundant_address_selection, unsupported_size_answer


logger = logging.getLogger(__name__)


def create_intent(context, counter, query, main_intent, sub_intent, response=""):
    intent = get_intent(main_intent)(
        main_query=query, sub_intent=sub_intent, tenant=context.tenant.id,
        chat_id=context.user_id, query_id=counter, response=response,
    )
    intent.platform = context.platform
    return intent


def prepare_followup(state: ConversationState):
    index = state["awaiting_followup_index"]
    original = state["pending_queries"]
    # Scope redirects are terminal, including entries saved by older handlers.
    # Remove them before normalization can use a stale reply as pending context.
    pending_queries = valid_pending(original, state)
    for request in pending_queries:
        restore_clarification_budget(request)
    previous = original[index] if index is not None and 0 <= index < len(original) else None
    index = next((i for i, intent in enumerate(pending_queries) if intent is previous), None)
    question = main_query = ""
    awaiting = index
    pending = None
    if index is not None:
        pending = pending_queries[index]
        question, main_query = pending.get_followup_question(), pending.main_query
        awaiting = None
    return {"previous_followup_index": index, "previous_followup": pending,
            "paused_intent": None, "awaiting_followup_index": awaiting,
            "followup_question": question, "followup_main_query": main_query,
            "pending_queries": pending_queries}


def classify_intents(state: ConversationState, runtime: Runtime[ConversationContext]):
    context = runtime.context
    try:
        payload = classification_context(state['history'], state['checklist'],
            tenant_id=context.tenant.id, chat_id=context.user_id, platform=context.platform,
            query=state['query'], basket=state['basket'], pending=state['pending_queries'],
            tenant=context.tenant, customer=context.customer, active_pending=state['previous_followup'],
            delivery_address=state['delivery_address'])
        explicit = explicit_classification(state['query'], state['pending_queries'])
        proposal = explicit
        if proposal is None:
            proposal = normalize_and_classify(state['query'],
                payload['last_assistant_question'], state['followup_main_query'],
                tenant_key=str(context.tenant.id), conversation_context=payload)
    except NormalizationClassificationError:
        return {'classifications': [], 'persist': False, 'include_basket': False,
                'response': FAILURE_REPLY}
    if not proposal.classifications:
        return {'classifications': [], 'persist': False, 'include_basket': False, 'response': FAILURE_REPLY}
    from evaluate.controls.telemetry import emit_classification
    emit_classification(proposal, prompt_version='cafe-explicit-commands-v1' if explicit else SYSTEM_ID,
                        catalog_version=payload.get('catalog_version'))
    # Apply explicit requirements before dispatch, including requirements stated
    # after an ordering clause. No state changes until the whole turn validates.
    constraints = state['checklist'].setdefault('declared_constraints', [])
    if proposal.response_language:
        state['checklist']['response_language'] = proposal.response_language
    for constraint in proposal.declared_constraints:
        if constraint not in constraints:
            constraints.append(constraint)
    intents = [(row.query, row.intent, row.sub_intent, row.reply_to, row.clarification)
               for row in proposal.classifications]
    return {'classifications': intents, 'checklist': state['checklist'],
            'actions': [row.action for row in proposal.classifications],
            'rephrased_sentences': [row.rephrased_sentence for row in proposal.classifications],
            'saved_addresses': payload.get('saved_addresses', []),
            'offered_quote_fingerprint': (state['checklist'].get('checkout', {}).get('quote') or {}).get('fingerprint')}


def after_classification(state: ConversationState) -> Literal["resolve_intent", "__end__"]:
    if not state["persist"]:
        return END
    return "resolve_intent"


def resolve_intent(state: ConversationState, runtime: Runtime[ConversationContext]):
    # Earlier clauses can remove a target or finish checkout in this same turn.
    state['pending_queries'][:] = valid_pending(state['pending_queries'], state)
    sentence, main_intent, sub_intent, reply_to, clarification = state['classifications'][state['intent_index']]
    rewrite = state['rephrased_sentences'][state['intent_index']]
    classified_route = (main_intent, sub_intent)
    proposal = state['actions'][state['intent_index']]
    if (classified_route not in {('placing_order', 'special_requests'), ('placing_order', 'order_scheduling')}
            and proposal is not None and proposal.kind == 'SET_CHECKOUT_FIELD' and proposal.field == 'scheduled_at'):
        main_intent, sub_intent = 'placing_order', 'order_scheduling'
    # Store-only requests must not be converted into mutations or checkout replies
    # by an action/clarification proposed by the classifier.
    if main_intent == 'placing_order' and sub_intent in {'special_requests', 'order_scheduling'}:
        proposal = None
        reply_to = None
        clarification = None
    if reply_to is None and proposal is not None and proposal.kind == 'RECOVER_PAYMENT':
        retries = [p for p in state['pending_queries'] if p.basket_item.get('payment_recovery')]
        if len(retries) == 1:
            reply_to = str(retries[0].query_id)
    resolved = None
    rejection = None
    index = next((i for i, pending in enumerate(state['pending_queries'])
                  if str(pending.query_id) == reply_to), None)
    replied = state['pending_queries'][index] if index is not None else None
    # Naming a saved address for deletion/default management is not a request to
    # select it for delivery. Let the classified handler resolve its own target.
    if (classified_route[0] == 'location_based'
            and classified_route[1] in {'delete_delivery_address', 'set_default_delivery_address'}
            and proposal is not None and proposal.kind == 'SELECT_ADDRESS'):
        proposal = None
    if redundant_address_selection(proposal, classified_route, state):
        proposal = None
    if unresolved_item_answer(proposal, classified_route, replied) and not clarification:
        # The customer answered the task's question with nothing usable. Repeat
        # that question through the clarify path so its budget is counted once.
        clarification = replied.get_followup_question() or 'Please specify the item, size and quantity.'
    clarification_only = bool(clarification and classified_route in CLARIFICATION_ROUTES
                              and (proposal is None or requires_action(*action_route(proposal))))
    if proposal is not None and not clarification_only:
        main_intent, sub_intent = action_route(proposal)
    catalog = (catalog_names(runtime.context.tenant.api_key)
               if proposal and proposal.kind == 'CHANGE_BASKET' and not clarification_only else ())
    if replied is not None and not compatible_followup(proposal, (main_intent, sub_intent), replied, catalog=catalog):
        index = None
    try:
        if proposal is not None and not clarification_only:
            resolved = bind_action(proposal, state, catalog=catalog,
                                   text=ordering_evidence(replied if index is not None else None,
                                                          state['query'], rewrite))
        elif not clarification and requires_action(main_intent, sub_intent):
            raise NeedsClarification('Please specify the action and its details.')
    except NeedsClarification as exc:
        clarification = clarification or str(exc)
    except TerminalRejection as exc:
        rejection = str(exc)
    configuration = active_configuration()
    routes = {(main_intent, sub_intent)} | (required_routes(proposal) if proposal and not clarification_only else set())
    unavailable = {route for route in routes if
                   (configuration is None and route not in CONTROL_ROUTES)
                   or (configuration is not None and not configuration.allows(*route))}
    from evaluate.controls.telemetry import emit_capability_check
    emit_capability_check(classification_index=state['intent_index'],
        classified_route=classified_route, effective_route=(main_intent, sub_intent),
        action_kind=proposal.kind if proposal else None,
        configuration_version=configuration.version if configuration else None,
        required_routes=routes, unavailable_routes=unavailable)
    proposed_routes = required_routes(proposal) if proposal else set()
    described_action = resolved.proposal if resolved is not None else proposal
    response_context = {
        'query': sentence,
        'rephrased_sentence': rewrite,
        'classified_route': classified_route,
        'effective_route': (main_intent, sub_intent),
        'proposed_action': proposal.kind if proposal else None,
        'requested_item_ids': list(dict.fromkeys(
            line.item_id for line in described_action.basket.lines
            if line.item_id and line.action != 'remove'))
            if described_action and described_action.basket else [],
        'clarification_only': clarification_only,
        'capabilities': {
            'required_routes': sorted(routes), 'unavailable_routes': sorted(unavailable),
            'proposed_action_routes': sorted(proposed_routes),
            'proposed_action_allowed': (not any(
                (configuration is None and route not in CONTROL_ROUTES)
                or (configuration is not None and not configuration.allows(*route))
                for route in proposed_routes)) if proposal else None,
        },
    }

    def result(**values):
        return {'current_response_context': response_context,
                'basket_before_intent': deepcopy(state['basket'].items), **values}

    if unavailable:
        # An answered conversational clarification is no longer pending when its
        # now-specific request is unavailable. Do not repeat its old question.
        if index is not None and (replied.intent_type, replied.sub_intent) in CLARIFICATION_ROUTES:
            state['pending_queries'].pop(index)
        return result(resolution='unavailable', current_reply='That service is currently unavailable at this café.')
    if reply_to is not None and replied is None:
        # An earlier unit may have completed this request. Never replay it.
        if not (state['checklist'].get('order_id') and proposal and proposal.kind in {
                'CONTINUE_CHECKOUT', 'CONFIRM_ORDER', 'RECOVER_PAYMENT'}):
            response_context['reason'] = 'request_already_finished'
            return result(resolution='unavailable', current_reply='That request has already finished. Please start a new request.')
    if (not rejection and index is not None and proposal is not None and proposal.kind == 'CHANGE_BASKET'
            and unsupported_size_answer(proposal, replied, state['query'],
                                        load_catalog(runtime.context.tenant.api_key))):
        # Do not replace the saved proposal or spend its clarification budget on
        # unrelated text. The customer can still answer after this turn.
        response_context['reason'] = 'answer_does_not_resolve_pending_choice'
        response_context['task_id'] = str(replied.query_id)
        return result(resolution='unavailable', current_reply='', question_intent=replied)
    if rejection:
        rejected = (state['pending_queries'].pop(index) if index is not None else
                    create_intent(runtime.context, f"{state['counter']}:{state['intent_index']}",
                                  sentence, main_intent, sub_intent))
        rejected.set_outcome(TaskOutcome.TERMINAL_REJECTION, rejection)
        response_context['outcome'] = TaskOutcome.TERMINAL_REJECTION.value
        return result(resolution='unavailable', current_reply=rejection,
                      history=(state['history'] + [{'query_obj': deepcopy(rejected.to_dict())}])[-200:])
    intent = create_intent(runtime.context, f"{state['counter']}:{state['intent_index']}",
                           sentence, main_intent, sub_intent)
    intent.original_query = state['query']
    intent.rephrased_sentence = rewrite
    intent.response_language = state['checklist'].get('response_language')
    intent.resolved_action = resolved
    if clarification:
        if main_intent == 'location_based' and sub_intent in {'add_delivery_address', 'update_delivery_address'}:
            intent.preserve_draft(sentence, state['delivery_address'], state['checklist'],
                                  new=sub_intent == 'add_delivery_address' and index is None)
            intent._sync_checkout_address(runtime.context.customer, state['delivery_address'], state['checklist'])
        pending = state['pending_queries'][index] if index is not None else intent
        pending.rephrased_sentence = rewrite
        pending.response_language = intent.response_language
        # Count only the final selected question, alongside handler questions.
        pending.set_outcome(TaskOutcome.NEEDS_CLARIFICATION, clarification)
        pending.missing_fields = ['clarification']
        pending.basket_item.setdefault('original_request', pending.original_query)
        if proposal is not None:
            pending.basket_item['action_proposal'] = proposal.model_dump()
            if index is not None and proposal.kind == 'CHANGE_BASKET':
                # Keep each answer as evidence for the next product ambiguity check.
                pending.basket_item.setdefault('replies', []).append(state['query'])
        if not resumable_pending(pending, state):
            if index is not None:
                state['pending_queries'].pop(index)
            response_context['outcome'] = pending.outcome.value
            return result(resolution='unavailable', current_reply=pending.response,
                          history=(state['history'] + [{'query_obj': deepcopy(pending.to_dict())}])[-200:])
        # Repeated clarification is still unfinished work, not a rejection.
        if index is None:
            state['pending_queries'].append(pending)
        return result(resolution='clarify', current_reply='', question_intent=pending)
    if (main_intent, sub_intent) == ('general', 'cancel_and_abort'):
        return result(resolution='cancel', active_intent=intent, matched_followup_index=index)
    resolution = 'pause' if (main_intent, sub_intent) == ('general', 'wait') else 'new'
    return result(resolution=resolution, active_intent=intent, matched_followup_index=index,
                  paused_intent=None)


def after_resolution(state: ConversationState) -> Literal["cancel_pending", "pause_pending", "match_followup", "collect_reply"]:
    if state["resolution"] in {"unavailable", "clarify"}:
        return "collect_reply"
    if state["resolution"] == "cancel":
        return "cancel_pending"
    return "pause_pending" if state["resolution"] == "pause" else "match_followup"


def pause_pending(state: ConversationState):
    # A wait is an explicit control, not a probabilistic follow-up match. Keep
    # the original object reference because earlier clauses may move the queue.
    pending = state["pending_queries"]
    paused = state["next_intent"] or (pending[-1] if pending else None)
    state["active_intent"].promp_restriction = True
    return {"pending_queries": pending, "paused_intent": paused,
            "active_intent": state["active_intent"]}


def cancel_pending(state: ConversationState, runtime: Runtime[ConversationContext]):
    with final_reply(stream_original_reply(state), state["replies"]):
        pending = state['pending_queries']
        index = state['matched_followup_index']
        selected = pending[index] if index is not None else None
        reply = stop_pending_request(pending, selected, state['basket'], runtime.context.customer, state['checklist'])
        return {'pending_queries': pending, 'current_reply': reply, 'checklist': state['checklist']}


def match_followup(state: ConversationState, runtime: Runtime[ConversationContext]):
    incoming = state['active_intent']
    pending = state['pending_queries']
    question = pending_address_question(pending, incoming)
    if incoming.intent_type == 'placing_order' and incoming.sub_intent in {'order_confirmation', 'order_payment'}:
        blocker = next((p for p in pending if p.intent_type == 'placing_order'
                        and p.sub_intent in p.ITEM_ACTIONS), None)
        if blocker:
            question = 'Please finish or cancel the pending basket change before checkout.'
            if incoming.resolved_action and incoming.resolved_action.proposal.kind in {
                    'SET_FULFILLMENT', 'SET_PAYMENT_METHOD', 'SET_CHECKOUT_FIELD', 'CLEAR_CHECKOUT_FIELD'}:
                incoming.checkout_blocker = question
                question = None
    if question:
        return {'resolution': 'unavailable', 'current_reply': question}
    index = state['matched_followup_index']
    if index is not None:
        target = pending[index]
        if (incoming.resolved_action and incoming.resolved_action.proposal.kind in {
                'SELECT_ADDRESS', 'CHANGE_BASKET', 'SHOW_CART'}
                and target.basket_item.get('checkout')):
            # Basket/address work is separate from collecting checkout fields.
            # Retain the checkout so its next continuation validates a new quote.
            return {'matched_followup_index': None}
        return {'resolution': 'followup'}
    # An independent request resolves only a generic clarification, not business work.
    if incoming.intent_type != 'out_of_context' and incoming.sub_intent != 'wait':
        pending = [p for p in pending if p.intent_type != 'insufficient_information']
    if pending:
        incoming.promp_restriction = True
    return {'pending_queries': pending}


def after_match(state: ConversationState) -> Literal["process_followup", "process_new_query", "collect_reply"]:
    if state['resolution'] == 'unavailable':
        return 'collect_reply'
    return "process_followup" if state["resolution"] == "followup" else "process_new_query"


def _business_arguments(state, context):
    return (state["basket"], state["delivery_address"], state["checklist"],
            state["history"], context.tenant.api_key, context.customer)


def _business_update(state, reply):
    # Business handlers mutate these objects; publish each change to graph state.
    return {"current_reply": reply, "basket": state["basket"],
            "delivery_address": state["delivery_address"], "checklist": state["checklist"],
            "active_intent": state["active_intent"], "pending_queries": state["pending_queries"]}


def stream_original_reply(state):
    # Handler text is evidence for the final composer, not the delivered wording.
    return False


def process_followup(state: ConversationState, runtime: Runtime[ConversationContext]):
    with final_reply(stream_original_reply(state), state["replies"]):
        pending = state["pending_queries"][state["matched_followup_index"]]
        pending.original_query = state['query']
        pending.rephrased_sentence = state['active_intent'].rephrased_sentence
        pending.response_language = state['active_intent'].response_language
        pending.resolved_action = state['active_intent'].resolved_action
        pending.checkout_blocker = getattr(state['active_intent'], 'checkout_blocker', None)
        reply, _ = pending.process_followup(state["active_intent"], *_business_arguments(state, runtime.context))
        return _business_update(state, reply)


def process_new_query(state: ConversationState, runtime: Runtime[ConversationContext]):
    with final_reply(stream_original_reply(state), state["replies"]):
        reply, _ = state["active_intent"].process_query(*_business_arguments(state, runtime.context))
        return _business_update(state, reply)


def collect_reply(state: ConversationState):
    replies = list(state["replies"])
    pending = state["pending_queries"]
    history = state["history"]
    next_intent = state["next_intent"]
    fact = dict(state.get('current_response_context', {}))
    fact.update(verified_result=state['current_reply'],
                outcome=fact.get('outcome', 'needs_clarification' if state['resolution'] == 'clarify'
                                 else state['resolution']),
                business_handler_ran=state['resolution'] not in {'unavailable', 'clarify'},
                basket_changed=state['basket'].items != state.get('basket_before_intent', state['basket'].items))
    if state["resolution"] in {"cancel", "unavailable", "clarify"}:
        if state['current_reply']:
            replies.append(state["current_reply"])
        if state['resolution'] == 'clarify':
            fact['task_id'] = str(state['question_intent'].query_id)
            history = (history + [{'query_obj': deepcopy(state['question_intent'].to_dict())}])[-200:]
    else:
        incoming = state["active_intent"]
        is_followup = state["resolution"] == "followup"
        index = state["matched_followup_index"]
        processed = pending[index] if is_followup else incoming
        fact['task_id'] = str(processed.query_id)
        fact['outcome'] = processed.outcome.value
        if processed.handoff_to:
            # Preserve the existing rule: the last requested handoff is queued.
            target = processed.build_handoff_intent()
            if allowed_intent(target):
                next_intent = target
            else:
                replies.append("That service is currently unavailable at this café.")
                fact['handoff'] = {'outcome': 'unavailable',
                                   'route': (target.intent_type, target.sub_intent)}
        # Retain resolved answers for the next history-based question,
        # including tasks restored from older sessions.
        recorded = processed if processed.intent_type in {
            "insufficient_information", "information_about_the_cafe", "menu_items", "order_enquiry", "placing_order",
        } else incoming
        history = (history + [{"query_obj": deepcopy(recorded.to_dict()),
                               "func": "is_not_followup" if is_followup else "not_intent"}])[-200:]
        if is_followup:
            pending.pop(index)
            if not processed.is_complete:
                pending.append(processed)
            # choose_followup emits the pending question once. Some handlers
            # return that same question as their entire reply.
            if processed.is_complete or state["current_reply"] != processed.get_followup_question():
                replies.append(state["current_reply"])
        elif processed.is_complete:
            # Repeated read-only answers can arise from separate knowledge clauses.
            # Never suppress confirmations of separate business mutations.
            read_only = processed.intent_type in {
                'information_about_the_cafe', 'menu_items', 'general', 'out_of_context', 'order_enquiry'}
            if not read_only or state['current_reply'].strip() not in {r.strip() for r in replies}:
                replies.append(state["current_reply"])
        else:
            pending.append(processed)
    question_intent = state.get('question_intent')
    if state['resolution'] not in {'cancel', 'unavailable', 'clarify'} and not processed.is_complete:
        question_intent = processed
    return {"question_intent": question_intent, "replies": replies, "pending_queries": pending, "history": history,
            "response_facts": [*state.get('response_facts', []), fact],
            "next_intent": next_intent, "intent_index": state["intent_index"] + 1}


def after_collection(state: ConversationState) -> Literal["resolve_intent", "choose_followup"]:
    return "resolve_intent" if state["intent_index"] < len(state["classifications"]) else "choose_followup"


def choose_followup(state: ConversationState):
    pending = list(state["pending_queries"])
    if state["next_intent"]:
        pending.append(state["next_intent"])
    # Apply the budget once, only to a question selected for this reply.
    pending = valid_pending(pending, state)
    response = join_replies(state["replies"])
    awaiting = None
    delivered_question = ''
    facts = list(state.get('response_facts', []))
    history = list(state['history'])
    candidate = state.get('question_intent') or state.get('next_intent')
    if candidate in pending and candidate is not state.get('paused_intent'):
        delivered_question = candidate.get_followup_question()
        if delivered_question:
            # A guessed size rejected for lack of user evidence must not spend
            # the task's budget. Use its latest result so a later, valid unit in
            # the same turn can still advance the task normally.
            latest = next((fact for fact in reversed(facts)
                           if fact.get('task_id') == str(candidate.query_id)), {})
            preserve_budget = latest.get('reason') == 'answer_does_not_resolve_pending_choice'
            if preserve_budget or spend_clarification(candidate):
                if not response.endswith(delivered_question):
                    response = join_replies([response, delivered_question])
                awaiting = pending.index(candidate)
            else:
                # Some handlers include their question in the returned result.
                # Remove it before replacing this task's next step with closure.
                if response.endswith(delivered_question):
                    response = response[:-len(delivered_question)].rstrip()
                response = join_replies([response, candidate.response])
                pending.remove(candidate)
                delivered_question = ''
                closure = {
                    'task_id': str(candidate.query_id),
                    'query': candidate.main_query, 'outcome': candidate.outcome.value,
                    'reason': CLARIFICATION_LIMIT_REASON,
                    'clarifications_delivered': MAX_CLARIFICATION_QUESTIONS,
                    'verified_result': candidate.response,
                    'business_handler_ran': False, 'basket_changed': False,
                }
                # Replace stale question instructions for this task. Other
                # operations in the same turn retain their own execution facts.
                facts = [fact for fact in facts if fact.get('task_id') != str(candidate.query_id)]
                facts.append(closure)
            # Save the delivered count/terminal outcome, not the earlier draft.
            record = next((i for i in range(len(history) - 1, -1, -1)
                           if str(history[i].get('query_obj', {}).get('query_id')) == str(candidate.query_id)), None)
            if record is not None:
                history[record] = {**history[record], 'query_obj': deepcopy(candidate.to_dict())}
            else:
                history = (history + [{'query_obj': deepcopy(candidate.to_dict())}])[-200:]
    elif candidate in pending:
        awaiting = pending.index(candidate)
    elif state.get('previous_followup') in pending:
        awaiting = pending.index(state['previous_followup'])
    return {'pending_queries': pending, 'awaiting_followup_index': awaiting,
            'response_facts': facts, 'history': history,
            'response': response, 'delivered_question': delivered_question}


def render_response(state: ConversationState, runtime: Runtime[ConversationContext]):
    index = state['awaiting_followup_index']
    pending = state['pending_queries'][index] if index is not None else None
    question = state.get('delivered_question', '')
    response, delivered_question = render_reply(
        query=state['query'],
        previous_message=state['checklist'].get('last_assistant_message', ''),
        previous_question=state['checklist'].get('last_assistant_question', ''),
        facts=state.get('response_facts', []), response=state['response'], question=question,
        followup={'outcome': pending.outcome.value, 'missing_fields': pending.missing_fields,
                  'route': (pending.intent_type, pending.sub_intent)} if pending else {},
        evidence=ordering_reply_evidence(runtime.context.tenant.api_key, state.get('response_facts', [])),
        language=state['checklist'].get('response_language', 'en'))
    if pending is not None and delivered_question:
        # Next-turn classification and task restoration see the question actually sent.
        if pending.response == question:
            pending.response = delivered_question
        pending.follow_up_question[-1:] = [delivered_question]
    publish_reply(response)
    return {'response': response, 'delivered_question': delivered_question,
            'pending_queries': state['pending_queries']}


@lru_cache(maxsize=1)
def get_conversation_graph():
    builder = StateGraph(ConversationState, context_schema=ConversationContext)
    for node in (prepare_followup, classify_intents, resolve_intent,
                 cancel_pending, pause_pending, match_followup, process_followup, process_new_query,
                 collect_reply, choose_followup, render_response):
        builder.add_node(node.__name__, node, retry_policy=None)
    builder.add_edge(START, "prepare_followup")
    builder.add_edge("prepare_followup", "classify_intents")
    builder.add_conditional_edges("classify_intents", after_classification)
    builder.add_conditional_edges("resolve_intent", after_resolution)
    builder.add_edge("pause_pending", "process_new_query")
    builder.add_conditional_edges("match_followup", after_match)
    for node in ("cancel_pending", "process_followup", "process_new_query"):
        builder.add_edge(node, "collect_reply")
    builder.add_conditional_edges("collect_reply", after_collection)
    builder.add_edge("choose_followup", "render_response")
    builder.add_edge("render_response", END)
    # Redis owns persistence. No checkpoint/resume or graph-level retry policy.
    return builder.compile(checkpointer=False)
