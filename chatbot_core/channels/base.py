from typing import Optional, Dict, Any, Tuple, Protocol

class ChannelAdapter(Protocol):
    name: str  # "telegram" | "whatsapp" | "web"

    # Return a local file path for the media (downloaded), or None if not applicable
    def fetch_media(self, payload: Dict[str, Any]) -> Optional[str]:
        ...

    # Send plain text back to the user
    def send_text(self, payload: Dict[str, Any], text: str) -> None:
        ...

    # Send voice/audio back to the user (mp3/wav path provided)
    def send_voice(self, payload: Dict[str, Any], audio_path: str) -> None:
        ...

    # Optional: channel-specific text augmentation (e.g., add location phrasing)
    def augment_text(self, text: str, payload: Dict[str, Any]) -> str:
        return text
