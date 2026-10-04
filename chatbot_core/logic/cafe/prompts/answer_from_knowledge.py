from evaluate.controls.cache import namespace, semantic_lookup, semantic_write
import logging, numpy as np
from chatbot_core.scope import required_identity, normalize_platform, scope_digest
from chatbot_core.runtime_configuration import active_configuration
from typing import Union, List, Dict, Any, Optional
from evaluate.controls.cache import cache
from django.utils import timezone
from chatbot_core.llm.chains import text_chain
from chatbot_core.llm.streaming import invoke_reply
from chatbot_core.llm.models import get_model_name
from rapidfuzz.fuzz import token_set_ratio
from chatbot_core.queues import enqueue_string
from chatbot_core.models import SemanticCacheEntry, FaissVector
from chatbot_core.vector_store.embedding_client import get_embedding
from chatbot_core.vector_store.faiss_index import search, add_vector
from .utils import normalize, kb_fingerprint, exact_sig

logger = logging.getLogger(__name__)

SYSTEM_ID = "cafebot-kb-answer-v3"
SIM, LEX = 0.96, 90  # semantic + lexical gates

@semantic_lookup('knowledge')
def kb_lookup(model: str, scope: str, knowledge: Any, question: str, ttl: int):
    scope = namespace(scope)
    kb_fp = kb_fingerprint(knowledge)
    norm_q = normalize(question)
    sig = exact_sig(SYSTEM_ID, model, scope, kb_fp, norm_q)

    # 1) Exact cache
    hot = cache.get(sig)
    if hot is not None:
        return True, hot, {"sig": sig, "scope": scope, "kb_fp": kb_fp, "norm_q": norm_q}

    # 2) FAISS semantic cache (scoped)
    qvec = np.array(get_embedding(norm_q), dtype="float32")
    ids, sims = search(qvec, k=5)
    for pk, sim in zip(ids, sims):
        if sim < SIM: continue
        e = SemanticCacheEntry.objects.filter(pk=pk, system_id=SYSTEM_ID, model=model).first()
        if not e or e.scope != scope or e.kb_fp != kb_fp: continue
        if token_set_ratio(norm_q, e.normalized_query) < LEX: continue

        e.hit_count += 1; e.last_hit = timezone.now()
        e.save(update_fields=["hit_count","last_hit"])
        cache.set(sig, e.response, timeout=ttl)
        return True, e.response, {"sig": sig, "scope": scope, "kb_fp": kb_fp, "norm_q": norm_q}

    return False, None, {"sig": sig, "scope": scope, "kb_fp": kb_fp, "norm_q": norm_q, "qvec": qvec}

@semantic_write
def kb_create(ctx: Dict[str, Any], response: str, ttl: int, model: str):
    sig, scope, kb_fp, norm_q = ctx["sig"], ctx["scope"], ctx["kb_fp"], ctx["norm_q"]
    entry, made = SemanticCacheEntry.objects.get_or_create(
        scope=scope, kb_fp=kb_fp, normalized_query=norm_q,
        system_id=SYSTEM_ID, model=model,
        defaults=dict(sig=sig, response=response)
    )
    if made:
        vec = ctx.get("qvec")
        if vec is None:
            vec = np.array(get_embedding(norm_q), dtype="float32")
        FaissVector.objects.create(cache_entry=entry, dim=vec.shape[0], vector=vec.tobytes())
        add_vector(vec, entry.pk)
    cache.set(sig, response, timeout=ttl)
    return response

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
    enqueue_string(f"answer_from_knowledge, knowledge: {knowledge}, question: {question}")
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
