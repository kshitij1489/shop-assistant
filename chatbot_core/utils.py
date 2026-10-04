import re
from pathlib import Path
import yaml, json
from chatbot_core.models import TenantInfo
from typing import List, Dict, Any, Optional
import requests

def cors_allow_origin(request_origin: str, request=None) -> bool:
    """
    Allows origin only if it matches allowed_domains of the given tenant (via ?tenant=).
    """
    if not request:
        return False

    try:
        tenant_slug = request.GET.get("tenant")
        if not tenant_slug:
            return False

        origin = request_origin.rstrip("/")
        return TenantInfo.objects.filter(
            slug=tenant_slug,
            allowed_domains__contains=[origin],
            is_active=True
        ).exists()
    except Exception:
        return False



def get_tenant_config(tenant):
    base_path = Path('tenants') / tenant
    with open(base_path / 'config.yaml') as f:
        config = yaml.safe_load(f)
    with open(base_path / 'knowledge_base.json') as f:
        kb = json.load(f)
    return config, kb

# --- Basic lexicons (extend as you like) ---
INDIA_STATES = {
    "haryana", "delhi", "karnataka", "maharashtra", "uttar pradesh",
    "uttarakhand", "punjab", "rajasthan", "gujarat", "tamil nadu",
    "telangana", "andhra pradesh", "west bengal", "bihar", "madhya pradesh",
    "odisha", "kerala", "assam", "chandigarh", "goa", "jharkhand",
    "himachal pradesh", "jammu and kashmir"
}

# Add common Indian cities you expect; this list is small by design—extend in your app.
COMMON_CITIES = {
    "gurugram", "gurgaon", "delhi", "new delhi", "noida", "faridabad",
    "mumbai", "pune", "bengaluru", "bangalore", "chennai", "hyderabad",
    "kolkata", "ahmedabad", "jaipur", "lucknow", "kanpur"
}

# Words that often appear in addresses
ADDRESS_KEYWORDS = r"(?:house|plot|flat|apt|apartment|suite|unit|tower|block|building|villa|sector|street|st\b|road|rd\b|lane|ln\b|avenue|ave\b|marg|nagar|vihar|enclave|phase|society|residency|residential|locality|area|district|tehsil|pincode|pin|postal|zip|near|opposite|opp\.?|behind|beside|next\s*to)"

# Cues that precede addresses in natural language
ADDRESS_CUES = [
    r"\b(address\s*is|my\s*address\s*is|delivery\s*address|shipping\s*address|bill(?:ing)?\s*address)\b",
    r"\b(deliver\s*to|send\s*to|ship\s*to|reach\s*me\s*at|drop\s*to)\b",
    r"\b(located\s*at|located\s*in|find\s*me\s*at)\b",
    r"\b(saved\s*as|noted\s*as|it\s*is)\b"
]

PIN_RE = re.compile(r"\b\d{6}\b")  # Indian PIN
ZIP_RE = re.compile(r"\b\d{5}(-\d{4})?\b")  # US ZIP (optional generic support)

# Components
HOUSE_KEYWORD_RE = re.compile(
    r"\b(?:house|plot|flat|apt|apartment|suite|unit)\s*(?:no\.?|number|\#|:)?\s*([A-Za-z0-9\-\/]+)",
    re.I
)
HOUSE_LEADING_RE = re.compile(
    r"^\s*(\d+[A-Za-z0-9\-\/]*)\b",
    re.I
)

TOWER_BLOCK_RE = re.compile(r"\b(?:tower|block|building|villa)\s*([A-Za-z0-9\-\/ ]+)", re.I)
SECTOR_RE = re.compile(r"\bsector\s*([A-Za-z0-9\-\/ ]+)", re.I)
STREET_RE = re.compile(r"\b(?:street|st\.?|road|rd\.?|lane|ln\.?|avenue|ave\.?|marg|nagar|vihar|enclave|phase)\b[^,.;\n]*", re.I)
LANDMARK_RE = re.compile(r"\b(?:near|opposite|opp\.?|behind|beside|next\s*to)\b[^,.;\n]*", re.I)

# A broad address-chunk pattern: optionally keep a leading house number before the first keyword
CHUNK_RE = re.compile(
    rf"(?P<chunk>(?:\b\d+[A-Za-z0-9\-\/]*\s*,\s*)?(?:{ADDRESS_KEYWORDS})[^.;\n]{{8,}})",
    re.I
)


# Fallback: long comma-separated segments with numbers that "look like" addresses
FALLBACK_RE = re.compile(
    r"(?P<chunk>(?:[A-Za-z0-9#\/\-]+(?:\s+[A-Za-z0-9#\/\-]+)*,?\s*){3,})"
)

SPLIT_SENTENCES_RE = re.compile(r"[.;\n]+")


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip(" ,.;:\t")


def _score_chunk(chunk: str) -> float:
    """Heuristic confidence score in [0, 1]."""
    score = 0.0
    lc = chunk.lower()

    # Signals
    if re.search(ADDRESS_KEYWORDS, lc, re.I):
        score += 0.25
    if any(city in lc for city in COMMON_CITIES):
        score += 0.2
    if any(state in lc for state in INDIA_STATES):
        score += 0.1
    if PIN_RE.search(chunk) or ZIP_RE.search(chunk):
        score += 0.2
    if re.search(r"\d", chunk):  # any number
        score += 0.15
    # length bonus capped
    score += min(len(chunk) / 200.0, 0.1)

    return min(score, 1.0)


def _parse_components(chunk: str) -> Dict[str, Optional[str]]:
    comps = {
        "house_or_flat": None,
        "building_or_block": None,
        "sector_or_phase": None,
        "street_or_locality": None,
        "landmark": None,
        "city": None,
        "state": None,
        "postal_code": None,
    }

    # House/flat: prefer keyworded match anywhere; else a leading number
    m = HOUSE_KEYWORD_RE.search(chunk)
    if not m:
        m = HOUSE_LEADING_RE.search(chunk)
    if m:
        comps["house_or_flat"] = _normalize_ws(m.group(1))

    m = TOWER_BLOCK_RE.search(chunk)
    if m:
        comps["building_or_block"] = _normalize_ws(m.group(0))

    m = SECTOR_RE.search(chunk)
    if m:
        comps["sector_or_phase"] = _normalize_ws(m.group(0))

    m = STREET_RE.search(chunk)
    if m:
        comps["street_or_locality"] = _normalize_ws(m.group(0))

    m = LANDMARK_RE.search(chunk)
    if m:
        comps["landmark"] = _normalize_ws(m.group(0))

    pin = PIN_RE.search(chunk)
    zipc = ZIP_RE.search(chunk)
    comps["postal_code"] = pin.group(0) if pin else (zipc.group(0) if zipc else None)

    lc = chunk.lower()
    for city in COMMON_CITIES:
        if re.search(rf"\b{re.escape(city)}\b", lc):
            comps["city"] = city.title()
            break

    for state in INDIA_STATES:
        if re.search(rf"\b{re.escape(state)}\b", lc):
            comps["state"] = " ".join(w.capitalize() for w in state.split())
            break

    return comps



def _looks_like_address(chunk: str) -> bool:
    """Validation: require at least 2 strong signals or PIN present."""
    signals = 0
    if re.search(ADDRESS_KEYWORDS, chunk, re.I): signals += 1
    if re.search(r"\d", chunk): signals += 1
    if PIN_RE.search(chunk) or ZIP_RE.search(chunk): signals += 2  # strong
    if any(c in chunk.lower() for c in COMMON_CITIES): signals += 1
    if any(s in chunk.lower() for s in INDIA_STATES): signals += 1
    return (signals >= 2) and (len(chunk) >= 12)


def _cued_segments(text: str) -> List[str]:
    """Split text and return segments that appear after address cues."""
    segments = []
    for cue in ADDRESS_CUES:
        for m in re.finditer(cue, text, re.I):
            tail = text[m.end():]
            # take until next sentence boundary
            stop = SPLIT_SENTENCES_RE.search(tail)
            seg = tail[: stop.start()] if stop else tail
            seg = _normalize_ws(seg)
            if seg:
                segments.append(seg)
    return segments


def extract_addresses(query: str, max_candidates: int = 3) -> List[Dict[str, Any]]:
    """
    Return up to `max_candidates` address candidates with components and confidence.
    Each item: {"text": <clean_address>, "components": {...}, "confidence": 0..1}
    """
    text = _normalize_ws(query)

    # 1) Start from cued segments (strong signal)
    candidates: List[str] = []
    for seg in _cued_segments(text):
        candidates.append(seg)

    # 2) Keyword-led chunks (keeps optional leading number now)
    for m in CHUNK_RE.finditer(text):
        candidates.append(_normalize_ws(m.group("chunk")))

    # 3) Always also add fallback candidates (helps keep full strings like "992, ...")
    for m in FALLBACK_RE.finditer(text):
        piece = _normalize_ws(m.group("chunk"))
        # skip super-short or non-informative
        if len(piece) >= 12 and ("," in piece or re.search(r"\d", piece)):
            candidates.append(piece)

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for c in candidates:
        k = c.lower()
        if k not in seen:
            seen.add(k)
            unique.append(c)

    results: List[Dict[str, Any]] = []
    for chunk in unique:
        if not _looks_like_address(chunk):
            continue
        comps = _parse_components(chunk)
        conf = _score_chunk(chunk)
        results.append({
            "text": chunk.rstrip(" ,"),
            "components": comps,
            "confidence": round(conf, 3),
        })
        if len(results) >= max_candidates:
            break

    return results

def best_address(query: str) -> Optional[Dict[str, Any]]:
    """Convenience: return the highest-confidence address or None."""
    cands = extract_addresses(query, max_candidates=5)
    if not cands:
        return None
    return max(cands, key=lambda x: x["confidence"])
