# chatbot_core/channels/registry.py
from typing import Dict
from .base import ChannelAdapter

_adapters: Dict[str, ChannelAdapter] = {}

def register_adapter(adapter: ChannelAdapter) -> None:
    key = adapter.name.strip().lower()
    _adapters[key] = adapter

def get_adapter(channel: str) -> ChannelAdapter:
    key = (channel or "").strip().lower()
    if key not in _adapters:
        known = ", ".join(sorted(_adapters.keys())) or "<none>"
        raise ValueError(f"No adapter registered for channel: {channel}. Known: {known}")
    return _adapters[key]
