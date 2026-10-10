import logging
from chatbot_core.scope import required_identity, normalize_platform, scope_digest
from chatbot_core.runtime_configuration import active_configuration
from typing import Union, List, Dict, Any, Optional
from chatbot_core.llm.chains import text_chain
from chatbot_core.llm.streaming import invoke_reply
from chatbot_core.llm.models import get_model_name
from chatbot_core.queues import enqueue_string
from chatbot_core.vector_store.semantic_cache import lookup as kb_lookup, store as kb_create

logger = logging.getLogger(__name__)

def answer_from_knowledge(
    knowledge: Union[str, List[Any], Dict[str, Any]],
    question: str,
    *,
    tenant_key: str,
    user_id: str = "",
    platform: Optional[str] = None,
    sub_intent: Optional[str] = None,
    main_intent: str = "general",
    model: Optional[str] = None,
    ttl_sec: int = 30*60,
) -> str:
    tenant_key = required_identity(tenant_key, "tenant_key")
    if user_id:
        platform = normalize_platform(platform)
    model = model or get_model_name()
    # Scope rules
    enqueue_string("answer_from_knowledge")
    configuration = active_configuration()
    version = configuration.version if configuration and configuration.tenant_id == tenant_key else 0
    scope = scope_digest(tenant_key, "knowledge", main_intent, sub_intent or "", str(version),
                         platform or "", str(user_id))

    # Lookup (exact → FAISS)
    hit, cached, ctx = kb_lookup(model, scope, knowledge, question, ttl_sec)
    if hit:
        return cached

    # Render context (compact)
    if isinstance(knowledge, list):
        context = "\n".join(f"- {item}" for item in knowledge)
    elif isinstance(knowledge, dict):
        context = "\n".join(f"- {k}: {v}" for k, v in knowledge.items())
    else:
        context = str(knowledge)

    system = (
        "You are CafeBot. Answer ONLY using the provided context. "
        "Reply in the user's language and script. Answer all supported parts and explicitly "
        "say which requested facts cannot be verified. Missing evidence is not a negative fact. "
        "Do not claim an account lookup, sent message, complaint ticket, refund or payment "
        "unless the context contains explicit execution evidence. Be concise and do not repeat yourself."
    )

    try:
        answer = invoke_reply(text_chain(
            system, "CONTEXT:\n{context}\n\nQUESTION: {question}\n\nAnswer:",
            model=model, temperature=0.2, max_tokens=200,
        ), {"context": context, "question": question}).strip()
    except Exception:
        logger.exception("LLM error in answer_from_knowledge")
        return "Oops! Something went wrong while processing your request."

    return kb_create(ctx, answer, ttl_sec, model)
