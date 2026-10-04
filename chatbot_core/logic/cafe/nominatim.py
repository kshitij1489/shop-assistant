"""City and postal directory lookups for cafe site settings.

The base URL comes from ``NOMINATIM_BASE_URL``. An empty setting fails closed so
this process never falls through to the public Nominatim endpoint.
"""
import logging
import math
import hashlib
import json
from typing import Optional
from urllib.parse import urlsplit

import requests
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured

from . import nominatim_limit

logger = logging.getLogger(__name__)
DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_USER_AGENT = "StudioDesk/1.0 (site postal directory)"
def lookup_osm_object(osm_id: str) -> Optional[object]:
    """Raw address details for a selected OSM object, using the shared transport."""
    return _get('/lookup', {
        'osm_ids': osm_id, 'format': 'jsonv2', 'addressdetails': 1,
        'namedetails': 1, 'accept-language': 'en',
    })


def search_postal_code(postal_code: str, country_code: str) -> Optional[object]:
    """Look up postal evidence without sending a tenant's street address."""
    return _get('/search', {
        'postalcode': postal_code, 'countrycodes': country_code.lower(),
        'format': 'jsonv2', 'addressdetails': 1, 'accept-language': 'en', 'limit': 40,
    })


def _get(path: str, params: dict) -> Optional[object]:
    base = _base_url()
    if base is None:
        return None
    # Public Nominatim requires repeated queries to be cached. Cache the wire
    # result (including empty searches), so parsing and ambiguity checks still run.
    public = urlsplit(base).hostname == nominatim_limit.PUBLIC_HOST
    cache_key = 'nominatim:response:' + hashlib.sha256(
        json.dumps([base, path, _with_key(params)], sort_keys=True).encode()).hexdigest()
    try:
        if public:
            cached = cache.get(cache_key)
            if cached is not None:
                return cached['result']
        with nominatim_limit.dispatch(base) as permit:
            if permit is None:
                return None
            if public:
                cached = cache.get(cache_key)
                if cached is not None:
                    return cached['result']
            response = _request(base, path, params, permit)
            if response is None:
                return None
            result = response.json()
            if public:
                cache.set(cache_key, {'result': result}, timeout=86400)
            return result
    except (requests.RequestException, ValueError) as exc:
        logger.warning("OpenStreetMap directory request failed",
                       extra={"path": path, "error": type(exc).__name__})
        return None


def _request(base: str, path: str, params: dict, permit: nominatim_limit.Dispatch):
    response = requests.get(
        f"{base}{path}", params=_with_key(params), timeout=_timeout_seconds(),
        headers={"User-Agent": _user_agent(), "Accept": "application/json"},
        allow_redirects=False,
    )
    if getattr(response, "status_code", None) == 429:
        permit.rate_limited(response.headers.get("Retry-After"), response.headers.get("Date"))
        logger.warning("OpenStreetMap directory request was rate limited", extra={"path": path})
        return None
    if 300 <= response.status_code < 400:
        logger.warning("OpenStreetMap directory redirect refused", extra={"path": path})
        return None
    response.raise_for_status()
    return response


def _base_url() -> Optional[str]:
    raw = str(getattr(settings, "NOMINATIM_BASE_URL", "") or "").strip()
    if not raw:
        logger.warning("OpenStreetMap directory selected without NOMINATIM_BASE_URL")
        return None
    parsed = urlsplit(raw)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise ImproperlyConfigured(
            "NOMINATIM_BASE_URL must be an http(s) origin without credentials, a query, or a fragment.")
    return raw.rstrip("/")


def _with_key(params: dict) -> dict:
    key = str(getattr(settings, "NOMINATIM_API_KEY", "") or "").strip()
    if not key:
        return params
    if "\n" in key or "\r" in key:
        raise ImproperlyConfigured("NOMINATIM_API_KEY must be a single line.")
    return {**params, "key": key}


def _timeout_seconds() -> float:
    raw = getattr(settings, "NOMINATIM_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
    try:
        timeout = float(raw)
    except (TypeError, ValueError) as exc:
        raise ImproperlyConfigured("NOMINATIM_TIMEOUT_SECONDS must be positive.") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ImproperlyConfigured("NOMINATIM_TIMEOUT_SECONDS must be positive.")
    return timeout


def _user_agent() -> str:
    agent = str(getattr(settings, "NOMINATIM_USER_AGENT", "") or "").strip()
    if "\n" in agent or "\r" in agent:
        raise ImproperlyConfigured("NOMINATIM_USER_AGENT must be a single line.")
    return agent or DEFAULT_USER_AGENT
