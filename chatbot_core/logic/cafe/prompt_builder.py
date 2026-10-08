from functools import lru_cache
from typing import List
import logging

from chatbot_core.llm.chains import structured_chain
from chatbot_core.llm.schemas import AddressComponents
from chatbot_core.logic.cafe.location_utils import normalize_address

logger = logging.getLogger(__name__)


def extract_address_with_gpt(text: str, *, original_text=None, pending=None, rephrased_sentence=None):
    """Return supplied address fields; {} is a no-op and None is extraction failure."""
    system = (
        "Extract every address field supplied in the ORIGINAL user text of this turn. "
        "Read the entire message before deciding whether it supplies address details. "
        "Return supplied fields even when they repeat pending values. "
        "An acknowledgment, confirmation, or request to wait returns all nulls ONLY when the "
        "entire message contains no address details. If it also supplies details, extract them. "
        "Words such as 'ya', 'yes', 'ok', or 'found it' before an address are conversational filler, "
        "not a reason to ignore the rest of the message. A previous pause does not apply to this turn. "
        "The resolved request and English interpretation are context, never evidence of field values. "
        "Postal fields: extract city, state, country and postal_code independently, including mixed labelled and "
        "unlabelled text. A city immediately followed by a postal code supplies both fields. "
        "Preserve invalid postal codes for downstream validation. "
        "Understand the original message in its language, using the English interpretation to resolve its meaning. "
        "Copy postal values from the original message, never translate or infer missing postal fields. "
        "Return null for each postal field absent from the original message, even if pending contains it. "
        "Street field: if the original message supplies no street details, return street_address as null. "
        "City, state, country and postal code are not street details. For example, pending 'Tower B' "
        "plus original 'found it, Delhi 110001' yields city 'Delhi', postal_code '110001', and null "
        "street_address, state and country. "
        "If it supplies street details, merge them with the pending street into a complete updated street_address. "
        "For this merge, pending street details ARE a source of values: retain every previous house, flat, "
        "tower, building, sector, street, society and landmark detail except what the user explicitly replaces. "
        "For example, pending 'Tower B' plus original 'found it, flat 7, Main Road' yields "
        "street_address 'Tower B, flat 7, Main Road'. A flat number does not replace a tower or landmark. "
        "Street text is free-form: preserve its original language, do not translate, validate or require "
        "individual components. Legacy pending street components can be combined when street details are supplied. "
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
