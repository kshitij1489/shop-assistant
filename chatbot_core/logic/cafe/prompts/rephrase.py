import hashlib
from evaluate.controls.cache import cache
from chatbot_core.llm.chains import text_chain
from chatbot_core.llm.streaming import invoke_reply
from chatbot_core.llm.models import get_model_name
from .utils import normalize
import logging

logger = logging.getLogger(__name__)


SYSTEM_ID = "rephrase-v1"  # bump this when you change system_prompt or behavior

def _sig(model: str, message: str) -> str:
    """
    Create a deterministic signature so repeated messages skip the API call.
    """
    norm = normalize(message)
    m = hashlib.sha256()
    m.update(SYSTEM_ID.encode())
    m.update(model.encode())
    m.update(b"temp0")     # We enforce deterministic rewrite = temperature 0
    m.update(norm.encode())
    return m.hexdigest()


def rephrase_cafe_message(message: str) -> str:
    """
    Takes a café system message and returns a rephrased, friendly,
    human-like version — cached so repeat messages do not cause API calls.
    """

    model = get_model_name()

    # ---- 1) Lookup in Redis Cache (Fast Path) ----
    key = _sig(model, message)
    cached = cache.get(key)
    if cached is not None:
        return cached

    # ---- 2) Miss → Run LLM ----
    system_prompt = (
        "You are a helpful assistant for a café ordering chatbot. "
        "Rephrase system messages to sound more dynamic, friendly, and human-like, "
        "keeping the original meaning and preserving any variables/placeholders. "
        "Always return just ONE alternative sentence."
    )

    try:
        result = invoke_reply(text_chain(
            system_prompt, 'Rephrase this message:\n\n"{message}"', model=model,
        ), {"message": message}).strip()
    except Exception:
        logger.exception("Message rephrasing failed")
        return message

    # ---- 3) Store into Redis Cache ----
    cache.set(key, result, timeout=60 * 60 * 24 * 30)  # 30 days

    return result
