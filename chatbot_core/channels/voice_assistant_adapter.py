# chatbot_core/channels/adapters/voiceassistant.py
import logging
from typing import Dict, Any
from .registry import register_adapter

log = logging.getLogger(__name__)

class VoiceAssistantAdapter:
    """
    Text-only adapter for the Voice Assistant channel.
    """
    name = "voiceassistant"

    def _chat_id(self, payload: Dict[str, Any]) -> str:
        return str(payload.get("chat_id") or "")

    # No fetch_media: this channel is text-only.

    def fetch_text(self, payload: Dict[str, Any]) -> str:
        text = payload.get("text")
        if not text:
            log.warning("[VoiceAssistant] Empty text in payload: %s", payload)
        return text or ""

    def send_text(self, payload: Dict[str, Any], text: str) -> None:
        """
        Currently logs the outgoing text. If you later add a websocket or
        SSE push to your frontend, hook it up here.
        """
        chat_id = self._chat_id(payload) or "unknown"
        log.info("[VoiceAssistant] → chat_id=%s: %s", chat_id, (text or ""))

    def augment_text(self, text: str, payload: Dict[str, Any]) -> str:
        return text or "..."

register_adapter(VoiceAssistantAdapter())
