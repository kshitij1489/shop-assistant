# processor.py
import os
import logging
import inspect
from typing import Dict, Any, Optional

from django.conf import settings
from redis.exceptions import RedisError

from .active_chats import (
    touch_active_chat,
    append_message,
    is_agent_enabled,
    is_global_agent_enabled, set_latest_meta
)
from chatbot_core.models import TenantInfo
from chatbot_core.logic.cafe.db_utils import create_or_get_customer
from chatbot_core.logic.cafe.session.redis_session import RedisSessionStore
from chatbot_core.channels.utils import route_message_for_tenant
from .channels.registry import get_adapter

from chatbot_core.language_utils import (
    translate, to_wav, transcribe_and_detect,
    translate_with_detection,
    choose_voice_for_language,
    tts_generate_to_file,
)

log = logging.getLogger(__name__)

# Default user-facing fallback message
DEFAULT_ERROR_MSG = "Oops something happened. Please try again later."

def _record_message(*args, **kwargs):
    """Transcript outages must not resend a reply already delivered by an adapter."""
    try:
        append_message(*args, **kwargs)
    except RedisError:
        log.exception('Could not save chat transcript message')

def _safe_send_text(adapter, payload, message: str) -> None:
    """Best-effort send; never raises back to caller."""
    if not adapter:
        log.warning("No adapter available to send message: %r", message)
        return
    try:
        adapter.send_text(payload, message)
    except Exception:
        log.exception("Adapter send_text failed")

def _normalize_channel(raw: Optional[str]) -> str:
    return (raw or "").strip().lower()


def _extract_identity(channel: str, payload: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    """
    Produce a normalized identity dict used by create_or_get_customer.
    """
    display_name = payload.get("user_name") or payload.get("name")
    phone = payload.get("phone") or payload.get("whatsapp_number")

    channel_specific: Dict[str, Any] = {}
    external_id: Optional[str] = None

    if channel == "whatsapp":
        external_id = (
            payload.get("wa_id")
            or payload.get("from")
            or payload.get("whatsapp_id")
            or user_id
        )
        channel_specific["whatsapp_id"] = external_id
    elif channel == "telegram":
        external_id = (
            payload.get("telegram_user_id")
            or payload.get("from_id")
            or payload.get("chat_id")
            or payload.get("telegram_chat_id")
            or user_id
        )
        channel_specific["telegram_id"] = external_id
    elif channel in ("web", "website"):
        external_id = payload.get("session_id") or payload.get("web_user_id") or user_id
        channel_specific["web_user_id"] = external_id
    else:
        external_id = payload.get("user_id") or payload.get("id") or user_id
        channel_specific["external_id"] = external_id

    return {
        "external_id": str(external_id) if external_id is not None else None,
        "display_name": display_name,
        "phone": phone,
        "channel_specific": channel_specific,
    }


def _call_create_or_get_customer_safe(
    tenant: TenantInfo,
    channel: str,
    ident: Dict[str, Any],
) -> Any:
    """
    Call create_or_get_customer with the intersection of candidate kwargs and the function signature.
    """
    sig = inspect.signature(create_or_get_customer)

    candidates: Dict[str, Any] = {
        "tenant": tenant,
        "tenant_id": getattr(tenant, "id", None),
        "platform": channel,
        "channel": channel,
        "external_id": ident.get("external_id"),
        "name": ident.get("display_name"),
        "phone": ident.get("phone"),
        "meta": {
            "raw_payload_keys": list(ident.get("raw_payload_keys", [])),
            "source": "processor.process_payload",
        },
        **ident.get("channel_specific", {}),
    }

    candidates = {k: v for k, v in candidates.items() if v is not None}
    accepted = {k: v for k, v in candidates.items() if k in sig.parameters}
    minimal_order = ["tenant", "tenant_id", "platform", "channel", "external_id", "name"]
    minimal = {k: v for k, v in accepted.items() if k in minimal_order}

    try:
        return create_or_get_customer(**accepted)
    except TypeError as e:
        log.warning("create_or_get_customer signature mismatch, retrying with minimal args: %s", e)
        return create_or_get_customer(**minimal)

def process_payload(tenant_id: str, user_id: str, payload: Dict[str, Any]) -> None:
    """
    Channel-agnostic pipeline:
      - get adapter
      - fetch media (if any), transcribe & detect language
      - optional channel text fallback
      - translate inbound to English if needed
      - route to CafeHandler (English-only)
      - translate reply back to user's language
      - send reply via adapter (text or voice)
      - on ANY exception: send DEFAULT_ERROR_MSG (best-effort)
    """
    from .tasks import validate_payload_scope
    channel = validate_payload_scope(tenant_id, user_id, payload)
    tenant = TenantInfo.objects.get(id=tenant_id, is_active=True, approval_status='APPROVED')
    if channel == 'telegram':
        if not tenant.telegram_bot_token or payload.get('bot_token') != tenant.telegram_bot_token:
            raise ValueError('Telegram credential does not match the queued tenant.')
    adapter = None

    # original incoming text (may be empty if media present)
    text = payload.get("text") or ""
    media_path = wav_path = tts_path = None

    # detection defaults (safe guards)
    detected_lang = payload.get("language") or None
    lang_conf = payload.get("lang_confidence") or 0.0
    transcribed = False

    try:
        adapter = get_adapter(channel)

        # 1) Media -> local file (channel-specific), then ffmpeg -> whisper transcription
        media = payload.get("media")
        if media:
            try:
                media_path = adapter.fetch_media(payload)
            except Exception:
                log.exception("adapter.fetch_media failed")
                media_path = None

            if media_path:
                try:
                    wav_path = to_wav(media_path)
                except Exception:
                    log.exception("ffmpeg conversion failed for %s", media_path)
                    wav_path = None

                if wav_path:
                    original_text, detected_lang, lang_conf = transcribe_and_detect(wav_path)
                    text = original_text or ""
                    payload["language"] = detected_lang or "en"
                    payload["lang_confidence"] = lang_conf or 0.0
                    transcribed = True

        if transcribed:
            log.info(
                "Transcribed: lang=%s conf=%s transcript_len=%d",
                detected_lang,
                lang_conf,
                len(text or ""),
            )

        # 2) Channel-specific text fallback
        try:
            text = adapter.augment_text(text, payload)
        except Exception:
            log.exception("adapter.augment_text failed; proceeding with original text")

        # 2a) If no language present (text-only), detect it
        #routing_text, detected_lang, conf = translate_with_detection(text, target_lang="en")
        #routing_text = routing_text[:8000]
        #payload["language"] = detected_lang
        #payload["lang_confidence"] = conf
        routing_text = text[:8000]
        payload["language"] = "en"
        payload["lang_confidence"] = 1.0

        # store local vars from payload
        detected_lang = payload.get("language", "en")
        lang_conf = payload.get("lang_confidence", 0.0)

        # log low confidence
        if lang_conf < 0.6:
            log.warning("Low language detection confidence=%s for tenant=%s chat=%s", lang_conf, tenant_id, payload.get("chat_id"))

        # 4) Resolve customer (platform = channel), safely
        ident = _extract_identity(channel, payload, user_id)
        ident["raw_payload_keys"] = list(payload.keys())
        customer = _call_create_or_get_customer_safe(tenant, channel, ident)

        # dashboard-friendly values
        chat_id = str(payload.get("chat_id") or ident.get("external_id") or user_id)
        display_name = getattr(customer, "name", None) or ident.get("display_name") or "Guest"
        phone = getattr(customer, "phone", None) or ident.get("phone")

        # Record inbound message best-effort (does not raise)
        _record_message(str(tenant.id), channel, chat_id, direction="in", text=text or "")
        touch_active_chat(
            str(tenant.id),
            channel,
            chat_id,
            user_id=str(user_id),
            customer_id=str(getattr(customer, "id", "")),
            display_name=display_name,
            phone=phone,
        )

        # Gate the bot by the Agent toggle
        try:
            global_enabled = is_global_agent_enabled(str(tenant.id), channel)
        except RedisError:
            log.exception('Agent status unavailable; skipping bot routing')
            return
        if not global_enabled:
            log.info("Global agent disabled for tenant=%s channel=%s; skipping bot routing", tenant_id, channel)
            return

        if not is_agent_enabled(str(tenant.id), channel, chat_id):
            log.info("Agent disabled for %s/%s via %s; skipping bot routing", tenant_id, user_id, channel)
            return

        # 5) Route (English-only handler)
        try:
            session_store = RedisSessionStore(chat_id, tenant_id=tenant.id, platform=channel)
            reply_en, meta_data = route_message_for_tenant(
                tenant, routing_text, session_store, customer=customer,
            )
            meta_data = meta_data if isinstance(meta_data, list) else None
        except Exception:
            log.exception("route_message_for_tenant failed")
            reply_en = ""
            meta_data = None

        # 6) Translate reply back to user's language (if needed)
        #ui_lang = (payload.get("ui_lang") or "").strip()
        #preferred_lang = (ui_lang.split("-")[0].lower() if ui_lang else None)
        #detected_lang = (payload.get("language") or "en").lower()
        out_lang = "en" #preferred_lang or detected_lang or "en"
        reply_local = reply_en or ""
        #if out_lang != "en" and (reply_en or "").strip():
        #    try:
        #        reply_local, _, _ = translate_with_detection(reply_en, target_lang=out_lang)
        #    except Exception:
        #        log.exception("Reply translation failed; falling back to English")
        #        reply_local = reply_en

        log.debug("Reply local len=%d out_lang=%s", len(reply_local or ""), out_lang)

        # 7) Send response in same mode as incoming (voice -> TTS, otherwise text)
        if media and media.get("type") in {"voice", "video_note", "video"}:
            try:
                voice_hint = choose_voice_for_language(out_lang)
                tts_path = tts_generate_to_file(reply_local or "Thanks for your message!", lang=out_lang, voice_hint=voice_hint)
                if tts_path:
                    try:
                        adapter.send_voice(payload, tts_path)
                    except Exception:
                        log.exception("adapter.send_voice failed, falling back to text")
                        _safe_send_text(adapter, payload, reply_local or reply_en or DEFAULT_ERROR_MSG)
                else:
                    # fallback to text if TTS generation failed
                    _safe_send_text(adapter, payload, reply_local or reply_en or DEFAULT_ERROR_MSG)

                # record textual reply (store local-language text)
                _record_message(str(tenant.id), channel, chat_id, direction="out", text=reply_local or reply_en or "")
            except Exception:
                log.exception("TTS or send_voice failed; falling back to text")
                _safe_send_text(adapter, payload, reply_local or reply_en or DEFAULT_ERROR_MSG)
                _record_message(str(tenant.id), channel, chat_id, direction="out", text=reply_local or reply_en or "")
        else:
            _safe_send_text(adapter, payload, reply_local or reply_en or "Thanks for your message!")
            _record_message(str(tenant.id), channel, chat_id, direction="out", text=reply_local or reply_en or "")
        if isinstance(meta_data, list):
            set_latest_meta(str(tenant.id), channel, chat_id, meta_data)
        log.info("Replied to %s/%s via %s", tenant_id, user_id, channel)

    except TenantInfo.DoesNotExist:
        log.warning("Tenant not found for id=%s", tenant_id)
        _safe_send_text(adapter, payload, DEFAULT_ERROR_MSG)
    except Exception:
        log.exception("processor.process_payload error")
        _safe_send_text(adapter, payload, DEFAULT_ERROR_MSG)
    finally:
        # clean up temporary files
        for p in (media_path, wav_path, tts_path):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    log.warning("Temp cleanup failed: %s", p)
