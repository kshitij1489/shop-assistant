"""Lazy, shared model configuration. Retries apply only to provider calls."""

from functools import lru_cache

from django.conf import settings
from langchain_openai import ChatOpenAI


def get_model_name(task: str = "cafe") -> str:
    defaults = {
        "cafe": ("LLM_MODEL", "gpt-6-luna"),
        "translation": ("LLM_TRANSLATE_MODEL", "gpt-6-luna"),
        "analytics": ("LLM_ANALYTICS_MODEL", "gpt-6-luna"),
    }
    setting, default = defaults[task]
    return getattr(settings, setting, default)


def get_chat_model(*, task="cafe", model=None, temperature=0.0,
                   timeout=None, max_tokens=None):
    return _configured_model(
        model or get_model_name(task), temperature,
        timeout if timeout is not None else getattr(settings, "LLM_TIMEOUT", 20.0),
        max_tokens if max_tokens is not None else getattr(settings, "LLM_MAX_TOKENS", 2048),
        getattr(settings, "LLM_MAX_RETRIES", 2), settings.OPENAI_API_KEY,
        _evaluation_model(),
    )


def _evaluation_model():
    from evaluate.controls.context import current
    return current() is not None


@lru_cache(maxsize=32)
def _configured_model(model, temperature, timeout, max_tokens, max_retries, api_key, evaluation=False):
    # Luna otherwise defaults to medium reasoning, which rejects temperature.
    options = {}
    if model == "gpt-6-luna" or model.startswith("gpt-6-luna-"):
        options["reasoning_effort"] = "none"
    if evaluation:
        from evaluate.controls.llm import callback
        options.update(callbacks=[callback], include_response_headers=True, cache=False)
    return ChatOpenAI(
        model=model, api_key=api_key, temperature=temperature,
        timeout=timeout, max_tokens=max_tokens, max_retries=max_retries,
        **options,
    )
