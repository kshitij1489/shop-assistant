import logging
import os, tempfile, requests
from typing import Optional, Dict, Any
from django.conf import settings
from urllib3.util.retry import Retry
from .base import ChannelAdapter
from .registry import register_adapter
from requests.adapters import HTTPAdapter

TELEGRAM_API_URL = "https://api.telegram.org"

log = logging.getLogger(__name__)

class TelegramAdapterImpl:
    name = "telegram"

    def _bot_token(self, payload: Dict[str, Any]) -> str:
        return payload["bot_token"]

    def _chat_id(self, payload: Dict[str, Any]) -> int:
        return payload["chat_id"]

    def _session_with_retries(self) -> requests.Session:
        s = requests.Session()
        retry = Retry(
            total=5,
            connect=5,
            read=5,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods={"GET", "POST"},
            raise_on_status=False,
        )
        s.mount("https://", HTTPAdapter(max_retries=retry))
        s.headers.update({"Connection": "keep-alive"})
        return s

    def fetch_media(self, payload: Dict[str, Any]) -> Optional[str]:
        """
        Download Telegram media to a temp file and return the local path.
        Resilient to transient SSL/HTTP errors via retries & timeouts.
        """
        media = payload.get("media") or {}
        file_id = media.get("id")           # your existing shape
        ftype   = media.get("type")         # "voice" | "video_note" | "video"
        if not file_id:
            return None

        # Decide suffix early (ffmpeg-friendly) and optionally override with Telegram file_path ext
        suffix = ".ogg" if ftype == "voice" else ".mp4"

        bot_token = self._bot_token(payload)
        sess = self._session_with_retries()
        timeouts = (5, 30)  # (connect, read) seconds

        try:
            # 1) Resolve actual file_path via getFile
            meta_resp = sess.get(
                f"{TELEGRAM_API_URL}/bot{bot_token}/getFile",
                params={"file_id": file_id},
                timeout=timeouts,
            )
            meta_resp.raise_for_status()
            meta = meta_resp.json()
            if not meta.get("ok"):
                log.warning("Telegram getFile not ok: %s", {k: meta.get(k) for k in ("ok", "error_code", "description")})
                return None

            file_path = meta["result"]["file_path"]

            # If Telegram gives a useful extension, prefer it (but keep .ogg for voice if unknown)
            _, dot, ext = file_path.rpartition(".")
            if dot and ext:
                guessed = "." + ext.lower()
                # keep .ogg for voice if Telegram returns non-audio ext
                if ftype == "voice":
                    suffix = ".ogg"
                else:
                    suffix = guessed if len(guessed) <= 5 else suffix  # simple sanity check

            # 2) Download the file stream
            url = f"{TELEGRAM_API_URL}/file/bot{bot_token}/{file_path}"
            fd = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
            try:
                with sess.get(url, stream=True, timeout=timeouts) as r:
                    r.raise_for_status()
                    for chunk in r.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            fd.write(chunk)
                fd.flush()
                return fd.name
            finally:
                fd.close()

        except requests.exceptions.SSLError as e:
            # common transient issue: SSL EOF mid-read — log and return None
            log.warning("Telegram media SSL error (likely transient): %s", e, exc_info=True)
            return None
        except requests.exceptions.RequestException as e:
            # any other requests-level error
            log.warning("Telegram media download failed: %s", e, exc_info=True)
            return None
        except Exception:
            log.exception("Unexpected error in fetch_media")
            return None


    def _send(self, payload: Dict[str, Any], method: str, **kwargs) -> None:
        try:
            response = requests.post(
                f"{TELEGRAM_API_URL}/bot{self._bot_token(payload)}/{method}",
                timeout=(5, 30),
                **kwargs,
            )
            response.raise_for_status()
            result = response.json()
        except requests.exceptions.Timeout:
            raise RuntimeError("Telegram send timed out; delivery could not be confirmed.") from None
        except (requests.exceptions.RequestException, ValueError):
            raise RuntimeError("Telegram send failed; delivery could not be confirmed.") from None
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise RuntimeError("Telegram rejected the message.")

    def send_text(self, payload: Dict[str, Any], text: str) -> None:
        self._send(payload, "sendMessage",
                   json={"chat_id": self._chat_id(payload), "text": text or "Thanks!"})

    def send_voice(self, payload: Dict[str, Any], audio_path: str) -> None:
        with open(audio_path, "rb") as f:
            self._send(payload, "sendVoice",
                       data={"chat_id": self._chat_id(payload)}, files={"voice": f})

    def augment_text(self, text: str, payload: Dict[str, Any]) -> str:
        return text or "User sent a media message but it couldn't be transcribed clearly."

# Register on import
register_adapter(TelegramAdapterImpl())
