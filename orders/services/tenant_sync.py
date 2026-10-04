import os
import json
import yaml
import logging
from typing import Optional, Dict, Any, Tuple

from django.conf import settings
from django.utils.text import slugify

from mongoengine import connect
from mongoengine.connection import get_connection

from chatbot_core.models import TenantInfo
from chatbot_core.mongo_models import IntentPrompt, KnowledgeBase

logger = logging.getLogger(__name__)

TENANTS_DIR_DEFAULT = os.path.join(settings.BASE_DIR, "tenants")


def _ensure_mongo_connected():
    """
    Ensure MongoEngine has a live connection. Safe to call multiple times.
    """
    if not getattr(settings, 'MONGO_DB_URL', None):
        raise ValueError('Legacy sync requires MONGO_DB_URL; it does not publish PostgreSQL runtime configuration.')
    try:
        # Will raise if no default connection exists
        get_connection()
    except Exception:
        connect(host=settings.MONGO_DB_URL)


def _tenant_dir(tenant_slug: str, tenants_dir: Optional[str] = None) -> str:
    base = tenants_dir or getattr(settings, "TENANTS_FS_ROOT", TENANTS_DIR_DEFAULT)
    return os.path.join(base, tenant_slug)


def _load_json(path: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not os.path.exists(path):
        return None, f"File not found: {path}"
    try:
        with open(path) as f:
            return json.load(f), None
    except json.JSONDecodeError as e:
        return None, f"Failed to parse JSON at {path}: {e}"


def sync_tenant_for(
    tenant: TenantInfo,
    tenants_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Idempotent sync for a single tenant object.
    - Reads {tenants_dir}/{tenant.slug}/config.yaml
    - Upserts TenantInfo fields (display_name, business_type, description)
    - Loads prompt_intent.json -> IntentPrompt (by api_key + sub_intent)
    - Loads knowledge_base.json -> KnowledgeBase (by api_key + sub_intent)
    Returns a summary dict.
    """
    _ensure_mongo_connected()

    summary = {
        "tenant_id": tenant.id,
        "tenant_slug": tenant.slug,
        "tenants_dir": tenants_dir or getattr(settings, "TENANTS_FS_ROOT", TENANTS_DIR_DEFAULT),
        "updated_tenant": False,
        "intents_loaded": 0,
        "knowledge_loaded": 0,
        "warnings": [],
        "errors": [],
    }

    if not tenant.slug:
        summary["warnings"].append("Tenant has no slug; deriving from display_name.")
        tenant.slug = slugify(tenant.display_name or f"tenant-{tenant.pk}")
        tenant.save(update_fields=["slug"])

    folder = _tenant_dir(tenant.slug, tenants_dir)
    if not os.path.isdir(folder):
        msg = f"Tenant folder not found: {folder}"
        logger.warning(msg)
        summary["warnings"].append(msg)
        return summary

    # ---- Sync tenant metadata (config.yaml) ----
    config_path = os.path.join(folder, "config.yaml")
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                config = yaml.safe_load(f) or {}
            # Keep your DB as source of truth for api_key; only update whitelisted fields.
            updated = False
            new_display = config.get("display_name")
            new_type = config.get("business_type")
            if new_type and new_type != 'cafe':
                raise ValueError('Only café/restaurant tenants are supported.')
            new_desc = config.get("description")

            fields_to_update = {}
            if new_display and new_display != tenant.display_name:
                fields_to_update["display_name"] = new_display
            if new_type and new_type != tenant.business_type:
                fields_to_update["business_type"] = new_type
            if new_desc is not None and new_desc != tenant.description:
                fields_to_update["description"] = new_desc

            if fields_to_update:
                for k, v in fields_to_update.items():
                    setattr(tenant, k, v)
                tenant.save(update_fields=list(fields_to_update.keys()))
                updated = True

            summary["updated_tenant"] = updated
            logger.info("Tenant %s metadata synced (updated=%s)", tenant.slug, updated)
        except Exception as e:
            msg = f"Failed to sync tenant metadata for {tenant.slug}: {e}"
            logger.exception(msg)
            summary["errors"].append(msg)
    else:
        summary["warnings"].append(f"No config.yaml found at {config_path}; skipping metadata sync.")

    # ---- Sync intents (prompt_intent.json) ----
    intents_path = os.path.join(folder, "prompt_intent.json")
    intents_json, err = _load_json(intents_path)
    if err:
        summary["warnings"].append(f"Intents: {err}")
    else:
        api_key = tenant.api_key
        slug = tenant.slug
        count = 0
        # expects: { intent: { sub_intent: prompt_str, ... }, ... }
        for intent, sub_intents in (intents_json or {}).items():
            if not isinstance(sub_intents, dict):
                logger.warning("Skipping non-dict sub_intents for intent=%s", intent)
                continue
            for sub_intent, prompt in sub_intents.items():
                if not prompt or not isinstance(prompt, str):
                    logger.warning("Skipping empty/non-string prompt for %s.%s", intent, sub_intent)
                    continue
                try:
                    IntentPrompt.objects(api_key=api_key, sub_intent=sub_intent).update_one(
                        set__slug=slug,
                        set__intent=intent,
                        set__prompt=prompt,
                        upsert=True,
                    )
                    count += 1
                except Exception as ex:
                    msg = f"Error saving intent for {intent}.{sub_intent}: {ex}"
                    logger.exception(msg)
                    summary["errors"].append(msg)
        summary["intents_loaded"] = count
        logger.info("Loaded %d intents for tenant '%s'", count, tenant.slug)

    # ---- Sync knowledge (knowledge_base.json) ----
    knowledge_path = os.path.join(folder, "knowledge_base.json")
    knowledge_json, err = _load_json(knowledge_path)
    if err:
        summary["warnings"].append(f"Knowledge: {err}")
    else:
        api_key = tenant.api_key
        slug = tenant.slug
        count = 0
        # expects: { intent: { sub_intent: dict, ... }, ... }
        for intent, sub_intents in (knowledge_json or {}).items():
            if not isinstance(sub_intents, dict):
                logger.warning("Skipping non-dict sub_intents for knowledge intent=%s", intent)
                continue
            for sub_intent, data in sub_intents.items():
                if not isinstance(data, dict):
                    data = {}
                try:
                    KnowledgeBase.objects(api_key=api_key, sub_intent=sub_intent).update_one(
                        set__slug=slug,
                        set__intent=intent,
                        set__data=data,
                        upsert=True,
                    )
                    count += 1
                except Exception as ex:
                    msg = f"Error saving knowledge for {intent}.{sub_intent}: {ex}"
                    logger.exception(msg)
                    summary["errors"].append(msg)
        summary["knowledge_loaded"] = count
        logger.info("Loaded %d knowledge items for tenant '%s'", count, tenant.slug)

    return summary


def sync_tenant_by_slug(tenant_slug: str, tenants_dir: Optional[str] = None) -> Dict[str, Any]:
    """
    Convenience entrypoint: fetch TenantInfo by slug, then sync.
    """
    tenant = TenantInfo.objects.filter(slug=tenant_slug).first()
    if not tenant:
        msg = f"No TenantInfo found with slug '{tenant_slug}'"
        logger.warning(msg)
        return {"tenant_slug": tenant_slug, "errors": [msg]}
    return sync_tenant_for(tenant, tenants_dir=tenants_dir)
