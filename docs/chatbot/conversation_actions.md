# Conversation actions

The conversation graph uses one understanding call for natural-language requests.
Whole-message labelled checkout fields, mode/payment tokens, `checkout`, and
`show basket`/`show cart` use deterministic typed actions instead. Compound requests
still use the understanding call. `logic/action_resolver.py` binds each proposal to live state;
the store adapter in `logic/cafe/workflow/actions.py` selects the business handler.
Handlers execute validated actions and return the result.

An explicit `insufficient_information_order` or generic insufficient-information
classification with a clarification question stays on its clarification route.
An attached proposal requiring action details remains speculative context; it
does not replace the clarification route or execute during that turn. Capability
admission checks the clarification route, while response facts separately record
whether the proposed action is available. Thus an allowed “Which item?” question
survives even when `add_to_basket` is not published. The task and proposed details
are retained for the answer; executable actions are rebound and capability-checked
on that later turn. A matched clarification is retired if its now-specific request
is unavailable, so the old question is not repeated.

The final response renderer uses these facts and the permitted follow-up to write
the customer-facing wording. Static handler messages remain verified result text
and failure fallbacks. Rendering does not determine task outcomes or mutate the
basket, checkout, or order.

Identified item clarifications also receive published menu evidence for price,
currency and serving-size context. Asking for a deferred quantity must not discard
these useful facts or infer a portion from an operational variant label. Successful
add/update results include the validated unit price with currency; the composer
preserves that price instead of deriving it from the previous conversation.

```text
User → contextual understanding → action resolver → business validation/execution
                                    ↑                         ↓
                              current session state ← updated state
```

The resolver has no database, model, store-name, or product-name dependencies.
The existing basket, scoped saved addresses, pending requests, and durable
checkout remain authoritative. `basket_focus` records the last uniquely changed
entry number; `fulfillment_preference` records a mode chosen before checkout. No second basket or checkout
state machine is maintained.

Action kinds cover basket changes, cart display, address selection,
fulfillment, payment method, checkout fields, continuation, confirmation, payment recovery, and
pending-task cancellation. A basket change contains add/update/remove/replace
lines so related changes validate and commit atomically. Information queries and
other address operations continue through their existing handlers.
Deleting a saved address and setting its default use their location routes with
no structured action. If classification incorrectly attaches `SELECT_ADDRESS`
to either route, the workflow discards that action before routing and capability
checks. Deletion returns app guidance; setting a default resolves the customer's
saved address through its existing handler. Neither requires the delivery-address
selection capability.

References use an explicit ID, a name, or conversational focus. Name matches must
be unique; a model's guessed `target_number` is ignored. Focus can use a sole
entry when no focus exists, but a deleted focus never falls back to another row.
Multiple candidates produce clarification. For an edit, removal or replacement, a
basket reference with no matching entry is a terminal rejection, including a stale
focus or an empty basket. An addition never depends on a basket row: "add that
dessert" with nothing matching leaves the product unidentified and asks which menu
item to add, because the customer can still name one. Pending proposals
survive session serialization; executable resolved actions do not.

Tasks persist an `outcome`: `needs_clarification`, `completed`,
`terminal_rejection`, or `temporarily_blocked`. The legacy `is_complete` field
remains compatible; old snapshots derive an outcome from it. The shared policy
in `workflow/pending.py` validates restored tasks, tasks between clauses, and
tasks selected for the next question. Impossible removals cannot block checkout.
Clarifications and temporary failures survive session reloads and unrelated
questions. Detours and pauses never expire valid work. Item and generic clarification
tasks may deliver two questions without progress; the next unresolved reply ends
the task as a terminal rejection. `choose_followup` counts the selected question
once, whether it came from classification, the resolver, or a handler. The persisted
`basket_item.clarification_budget` stores the delivered count and structured choices;
`ignored_count` mirrors that count. Filling missing choices or reducing unresolved
choices resets the budget, while a reworded question or merely invoking a handler
does not. Older saved counters are migrated before the next answer is processed.
Generic vague replies retain the same ordering task and budget, including the
`insufficient_information_order` route. Checkout field collection and temporary
failures do not use this item-clarification limit.
The workflow records `clarification_limit_reached` and the number of
questions delivered in the turn facts, removes the task, and the verified reply
states that nothing changed and invites a new, specific request. The renderer
phrases that outcome; it never decides it.

Checkout quote/acceptance failures remain retryable, and payment-link failures
retain a separate payment recovery task after placement. Completing checkout
retires basket and checkout tasks while preserving durable order/payment records.

A proposed catalog product is checked by `logic/catalog_match.py` against the
classification's contextual English rewrite. This translates product words for
lexical matching while preserving partial names, qualifiers and unresolved choices.
The rewrite restates the complete unapplied request after each clarification;
it must not expand a partial phrase into the model's chosen full catalog name.
Original messages remain stored evidence, and historical decisions without a
rewrite fall back to the original request plus clarification replies.
Words are compared after catalog-notation normalization: number words become
digits, piece/pieces/pc become `pcs`, digits are split from letters and `&`
reads as `and`, so "the brownie, two pieces" fits "Fudgy Chocolate Brownie (2pcs)".
An explicit count and piece unit form one token and must sit beside the product
wording to serve as package evidence. "Two of the brownie" or "the brownie and
two coffees" still requires clarification between the brownie products.
A partial name that fits several products, such as "chocolate ice cream" against
three chocolate flavours, asks which of those products is meant instead of adding
the model's pick. A product name has a head segment (the words before a connector
such as "with" or "and") naming what it is, and a component segment listing what
comes with it. The model's choice stands undisputed when every matched word names
the chosen product's head and reaches the rivals only through their component
words: "fig orange" selects "Fig Orange Ice Cream" without asking about "Dates
with Fig & Orange", whereas a rival whose head also carries the words keeps the
question open. A full product name competes only with an identical alias or a
longer name containing it. Wording with no overlap with catalog names leaves the
model's choice undisputed. Every action is rebound before execution, including
later actions in the same user turn.

Basket execution no longer calls the order interpreter or resolves targets from
prose. It checks current catalog selections, quantities, modifiers, dietary
constraints, and ordering limits on an isolated basket. Replacement preserves
the entry number and defaults to its existing quantity, but does not carry
modifiers to a different product. Mixed actions require every applicable tenant
capability.

Before checkout, `SET_FULFILLMENT` validates and saves a preference through
`order_channels_and_modes` without opening a draft, asking for contact details or
creating a quote. An explicit checkout request reuses this preference. During
checkout the same action changes the existing draft and invalidates its quote.
Compound requests that explicitly select a mode and start checkout carry both actions.

Payment continuation collects missing checkout details; it does not imply a
payment method or order confirmation. Confirmation binds to the reviewed quote's
fingerprint and still requires the same quote after policy and price validation.
The quote must also predate the current turn; a newly computed total cannot be
accepted by a later clause in the same message.
Address selection rechecks customer ownership and retains address confirmation.
Selecting the address that is already drafted carries no operation of its own:
the classified location route (typically the confirmation or denial the customer
actually gave) executes alone and does not require the `choose_delivery_address`
capability. An explicit choice keeps its action and its capability check.

Typed address classification leaves missing-field validation to the address handler.
Extraction receives the original message, pending fields and contextual English
interpretation; field values come from the original message and preserve its script.
A pause keeps the draft and open request intact without running extraction.

An address row created by the conversation is provisional until the customer
confirms it. Denying it, or starting a fresh address while it is drafted, keeps
the row replaceable so the next save overwrites it rather than leaving a rejected
address in the customer's address book. Established saved addresses are never
replaced this way; declining one simply clears the draft. A complete draft that
was never written (for example after a coverage outage) is resubmitted as-is by a
follow-up with no new details, re-verifying coverage. While an address awaits
confirmation, an address-shaped reply is judged by the confirmation: a changed
detail is saved and re-confirmed, an unchanged restatement confirms.

`RECOVER_PAYMENT` retrieves payment for the current chat's existing order through
the `placing_order/order_payment` capability. It does not create an order, replay
checkout, or issue another provider payment. Its successful response includes the
exact validated HTTPS checkout URL. Pending, paid, cancelled and review states
retain their existing responses; an expired or invalid link is reported as
unavailable. Recovery checks the durable chat, tenant and customer binding,
so it also works after conversational checkout state is lost.

This is an execution contract, not a claim of improved measured model accuracy.
Understanding must still preserve negations, conditions, and intended operations.
Candidate retrieval remains bounded; missing catalog choices clarify rather than
guess. Live language evaluation and adaptation of older
scripted fixtures are separate work.

## Task matching and draft restart

A proposed `reply_to` identifies a candidate, not permission to consume its task.
Checkout fields, basket operations and address collection have distinct continuation
rules. Incompatible requests execute independently and leave the original pending
task and its clarification budget intact. Task compatibility is checked before the
resolver borrows wording from the original request. Informational detours do not
clear unfinished business work.

Labelled address and postcode values belong to a single pending address-collection
task before checkout. Postcodes use the original typed value and normal address
validation; they do not require model extraction. The address handler preserves
the other components, requests confirmation, and synchronizes an existing checkout
draft. If several address tasks could own a value, classification must disambiguate.

An addition cannot consume a pending addition for different known catalog products.
It executes independently even if the classifier attaches the older task ID, leaving
that task resumable. An unresolved product choice may still acquire its first item;
size and quantity answers for the existing products continue the original task.

A reply the classifier addresses to an item task's question but cannot turn into
an action ("the nice one", "you decide") is an unresolved answer to that task. It
repeats the task's question (or the classifier's sharper one) through the
clarification path and spends that task's budget, rather than opening a parallel
generic clarification that would leave the item request pending indefinitely.
A vague message with no task ID still becomes its own generic clarification and
leaves the item task untouched.

For a pending basket proposal, a changed or repaired size must appear in the current
customer message as a catalog size or alias. A valid guessed variant ID alone is
insufficient. Unrelated text repeats the pending question without overwriting the
saved proposal or spending its clarification budget. The latest accepted proposal
is saved so a size answer followed by a quantity answer does not reopen the size
question. This is a conservative size-evidence check, not a general language parser;
other semantic interpretation still belongs to classification and business validation.

`new order`, `start a new order`, and polite variants explicitly restart an unfinished
draft: clear its basket, checkout fields/quote, selected delivery address, pending tasks
and conversational history. Persist a reset marker first so cache recovery cannot
restore the discarded draft. Standalone commands acknowledge the restart and ask what
to add. A command followed by `with`, `and`, or a colon/comma/semicolon can include
items for the fresh basket. Mere prefix matches such as `new order status?` do not reset
anything. Active placed orders remain protected; terminal-order rollover preserves
the old durable order and payment records.

Focused verification:

```sh
python manage.py test tests.unit.test_action_resolver tests.unit.test_task_outcomes tests.integration.test_conversation_actions \
  tests.integration.test_payment_recovery tests.integration.test_task_matching \
  --settings=tests.settings.integration
```

An all-add proposal is detached from a pending edit, removal, or customization before it is saved or executed, so it cannot inherit that task's clarification budget even when the model supplies the old reply id. Explicitly abandoning that task emits `CANCEL_PENDING_ACTION`, and the following operation uses a null reply id. A correction inside the same pending operation stays one row.

An empty-basket edit, removal or replacement is a terminal rejection and cannot create pending work, even when the model also supplies a clarification. An addition is never rejected for an empty basket; an unidentified one asks for the menu item.

Explicit labelled checkout fields and mode or payment tokens keep their meaning and are not reused as catalog or size answers. A missing postcode does not discard an explicitly supplied address. Those values can be saved while basket work is still pending, but they cannot produce a quote until that work finishes.

A quantity default applies only when the customer omits quantity without deferring or questioning it. A deferred quantity stays unset. Removing a quantity decrements that line; removing more than the line holds is rejected and leaves the basket unchanged. A numeric basket reply is checked against the pending request and the current basket.

Failed address validation cannot confirm a previously saved address. A declared dietary requirement blocks an unverified addition or update; a removal stays allowed. Payment recovery does not fall back to another order, and it compares modifier selections before it returns a link. A bare cancellation while a basket task is pending asks which target is meant and does not cancel a placed order. Completion is not inferred from the wording of the reply.
