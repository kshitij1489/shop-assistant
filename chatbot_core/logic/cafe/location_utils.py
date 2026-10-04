"""Basic validation and formatting for customer-confirmed text addresses."""
import math
import re
from collections.abc import Mapping


ADDRESS_FIELDS = (
    "street_address",
    "house_or_flat", "building_or_block", "street_or_locality", "sector_or_phase",
    "landmark", "city", "state", "postal_code", "country",
)
STREET_FIELDS = ("house_or_flat", "building_or_block", "street_or_locality", "sector_or_phase", "landmark")
_EMPTY_VALUES = {"", "none", "null", "n/a", "na", "unknown", "not provided"}


def _address_value(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    value = str(value).strip()
    return None if value.casefold() in _EMPTY_VALUES else value


def normalize_address(address):
    """Only copy recognized, nonempty components; never accept IDs from extraction."""
    if not isinstance(address, Mapping):
        return {}
    return {key: value for key in ADDRESS_FIELDS
            if (value := _address_value(address.get(key))) is not None}


def normalize_pincode(value):
    value = _address_value(value)
    return value if value and re.fullmatch(r"[1-9][0-9]{5}", value) else None


def extract_pincode(text):
    matches = re.findall(r"(?<!\w)[1-9][0-9]{5}(?!\w)", text or "")
    unique = set(matches)
    return matches[0] if len(unique) == 1 else None


def get_missing_address_keys(address_dict):
    """Street text is customer-confirmed; only basic postal fields are required."""
    address = normalize_address(address_dict)
    missing = []
    if not street_address(address):
        missing.append("street_address")
    for key in ("city", "state", "country"):
        if not address.get(key) or not any(c.isalpha() for c in address[key]):
            missing.append(key)
    if not normalize_pincode(address.get("postal_code")):
        missing.append("postal_code")
    return missing


def street_address(address):
    """Read free-form text, including components stored by older sessions."""
    address = normalize_address(address)
    return address.get("street_address") or ", ".join(address[k] for k in STREET_FIELDS if address.get(k))


def comparable_address(address):
    address = normalize_address(address)
    values = {"street_address": street_address(address),
              **{k: address.get(k, "") for k in ("city", "state", "country", "postal_code")}}
    return {k: " ".join(v.split()).casefold() for k, v in values.items()}


def format_address(address_dict):
    address = normalize_address(address_dict)
    parts, seen = [], set()
    for key in ADDRESS_FIELDS:
        if address.get("street_address") and key in STREET_FIELDS:
            continue
        value = address.get(key)
        if not value or value.casefold() in seen:
            continue
        seen.add(value.casefold())
        parts.append(value)
    return ", ".join(parts)
