from functools import lru_cache
from typing import List
import logging

from chatbot_core.llm.chains import structured_chain
from chatbot_core.llm.schemas import AddressComponents
from chatbot_core.logic.cafe.location_utils import normalize_address

logger = logging.getLogger(__name__)


def extract_address_with_gpt(text: str, *, original_text=None, pending=None, rephrased_sentence=None):
    """Return explicit address changes; {} is a no-op and None is extraction failure."""
    system = (
        "Extract address changes from the ORIGINAL user text. Return null for unchanged or absent fields. "
        "A confirmation or acknowledgment without new address details returns all nulls. "
        "The resolved request is context, never evidence of new values. Extract city, state, "
        "country and postal_code if they are present in the text. Preserve invalid postal codes for downstream validation. "
        "Understand the original message in its language, using the English interpretation to resolve its meaning. "
        "Copy field values from the original message, never translate or infer missing postal fields. "
        "Conversational introductions and filler do not invalidate the address that follows. "
        "A request to wait, search for details, or confirm the current address supplies no new fields. "
        "street_address is free-form user text: keep all house, flat, tower, building, sector, street, "
        "society and landmark details exactly as supplied, in their original language. Do not validate, "
        "translate, discard or require individual street components. "
        "When the user supplies a street correction or addition, return the complete updated street_address, "
        "retaining all previously supplied street details except what the user explicitly replaces. "
        "Legacy pending street components can be combined when a street change is supplied. "
        "Do not turn GPS coordinates or map links into an address."
    )
    user = f"""Original user text (evidence):
{original_text if original_text is not None else text}
Pending address fields:
{pending or {}}
Resolved request:
{text}
English interpretation (context only):
{rephrased_sentence or ''}
Return street_address, city, state, country, postal_code."""
    try:
        data = structured_chain(AddressComponents, system).invoke({"input": user}).model_dump()
    except Exception:
        logger.exception("Address extraction failed")
        return None
    return normalize_address(data)


@lru_cache(maxsize=1)
def _sentence_pipeline():
    import spacy
    return spacy.load("en_core_web_sm")


def query_splitter(query: str) -> List[str]:
    return [sent.text.strip() for sent in _sentence_pipeline()(query).sents]


def generate_response_from_knowledge_deprecated(
    api_key, sub_intent, user_input, system_log_message=None,
):
    """Compatibility wrapper for the cached knowledge response chain."""
    from .prompts.generate_response_from_knowledge import generate_response_from_knowledge
    return generate_response_from_knowledge(
        api_key, sub_intent, user_input, system_log_message=system_log_message,
    )
