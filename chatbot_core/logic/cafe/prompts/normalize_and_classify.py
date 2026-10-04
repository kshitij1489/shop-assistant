"""One contextual routing proposal; no business actions or semantic cache reuse."""
import hashlib
import json
import logging
from copy import deepcopy

from chatbot_core.capabilities import CONTROL_ROUTES
from chatbot_core.knowledge_cache import get_intent_classification_cache
from chatbot_core.llm.chains import structured_chain
from chatbot_core.llm.models import get_model_name
from chatbot_core.llm.schemas import NormalizedClassifiedMessages
from chatbot_core.scope import required_identity
from evaluate.controls.cache import cache
from evaluate.controls.context import fault_active
from evaluate.controls.telemetry import observed
from .normalize_and_classify_prompt import SYSTEM_PROMPT

logger = logging.getLogger(__name__)
SYSTEM_ID = "cafe-normalize-and-classify-v18-interpreted-actions"
FAILURE_REPLY = "Something wrong happened with your query, please ask again"


class NormalizationClassificationError(RuntimeError):
    """No routing proposal is safe to execute or cache."""


def _cached_proposal(key):
    """Operation-local lookup boundary for evaluation fault injection."""
    return cache.get(key)


def _validate(payload, schema, context=None):
    result = NormalizedClassifiedMessages.model_validate(payload)
    if not result.classifications:
        raise ValueError("Empty classifications")
    if any(not constraint.strip() for constraint in result.declared_constraints):
        raise ValueError("Empty declared constraint")
    open_ids = {str(request['id']) for request in (context or {}).get('open_requests', [])}
    for row in result.classifications:
        if row.reply_to is not None and row.reply_to not in open_ids:
            raise ValueError("Unknown pending request ID")
        if row.clarification is not None and not row.clarification.strip():
            raise ValueError("Empty clarification")
        if not row.query.strip():
            raise ValueError("Empty query")
        if not row.rephrased_sentence.strip():
            raise ValueError("Empty English rewrite")
        if ((row.intent, row.sub_intent) not in CONTROL_ROUTES
                and row.sub_intent not in schema.get(row.intent, {})):
            raise ValueError("Unknown tenant intent/sub-intent")
    return result


@observed("normalize_and_classify")
def normalize_and_classify(user_message, prev_system_message="", prev_user_sentence="", *,
                           tenant_key, model=None, conversation_context=None):
    """Return validated classifications and newly declared constraints, or fail atomically.

    Exact cache identity includes every model input, tenant and schema version.
    Queries are deliberately neither case-folded nor stripped for cache lookup.
    """
    tenant_key = required_identity(tenant_key, "tenant_key")
    model = model or get_model_name()
    if fault_active("classification", tenant_key):
        raise NormalizationClassificationError("Injected classification timeout")
    try:
        schema = deepcopy(get_intent_classification_cache(tenant_key))
        # RuntimeConfiguration always permits these controls, even without
        # tenant documents. Supply the same routes to the model and validator.
        # Keep existing descriptions and the schema's publication version.
        for intent, topic in CONTROL_ROUTES:
            schema.setdefault(intent, {}).setdefault(topic, {
                "description": topic.replace("_", " "), "examples": [],
            })
        payload = {
            "new_user_message": user_message,
            "prev_system_message": prev_system_message or "",
            "prev_user_sentence": prev_user_sentence or "",
        }
        if conversation_context:
            payload['conversation_context'] = deepcopy(conversation_context)
        system = SYSTEM_PROMPT + json.dumps(schema, ensure_ascii=False, sort_keys=True)
        identity = [SYSTEM_ID, tenant_key, model, getattr(schema, "version", 0), system, payload]
        key = SYSTEM_ID + ":" + hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        try:
            cached = _cached_proposal(key)
        except Exception:
            logger.warning("Combined classification cache lookup failed", exc_info=True)
            cached = None
        if cached is not None:
            try:
                return _validate(cached, schema, conversation_context)
            except (ValueError, TypeError):
                # Invalid stored data must never become executable proposals.
                logger.warning("Ignoring invalid combined classification cache entry")
        response = structured_chain(
            NormalizedClassifiedMessages, system, model=model, include_raw=True,
        ).invoke({"input": json.dumps(payload, ensure_ascii=False)})
        raw = response["raw"]
        metadata = raw.response_metadata
        if (response.get("parsing_error") or response.get("parsed") is None
                or raw.additional_kwargs.get("refusal")
                or metadata.get("finish_reason", "stop") != "stop"
                or metadata.get("status") in {"incomplete", "failed"}):
            raise ValueError("Incomplete or refused classification")
        result = response["parsed"].model_dump()
        proposal = _validate(result, schema, conversation_context)
    except Exception as exc:
        logger.exception("Combined normalization and classification failed")
        raise NormalizationClassificationError("Unable to classify this turn") from exc
    # Validate every row before writing anything; cache outages do not lose a
    # valid proposal or cause a second provider invocation.
    try:
        cache.set(key, result, timeout=6 * 60 * 60)
    except Exception:
        logger.warning("Combined classification cache write failed", exc_info=True)
    return proposal
