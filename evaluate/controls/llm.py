"""LangChain callback metadata; never persist messages or raw provider output."""
from threading import Lock
from time import perf_counter
from langchain_core.callbacks import BaseCallbackHandler
from .context import current, activate
from .telemetry import emit


class ModelEvidence(BaseCallbackHandler):
    run_inline = True
    raise_error = True

    def __init__(self):
        self.calls = {}
        self.lock = Lock()

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        ctx = current()
        if ctx is None:
            return
        params = kwargs.get('invocation_params') or {}
        with self.lock:
            self.calls[run_id] = (ctx, perf_counter(), params.get('model_name') or params.get('model'), 0)
        try:
            emit('llm.started', call_id=str(run_id), model=params.get('model_name') or params.get('model'))
        except Exception:
            with self.lock:
                self.calls.pop(run_id, None)
            raise

    def on_retry(self, retry_state, *, run_id, **kwargs):
        with self.lock:
            if run_id in self.calls:
                ctx, start, model, retries = self.calls[run_id]
                self.calls[run_id] = ctx, start, model, retries + 1

    def _finish(self, run_id, response=None, error=None):
        with self.lock:
            call = self.calls.pop(run_id, None)
        if call is None:
            return
        ctx, start, model, retries = call
        output = (response.llm_output or {}) if response else {}
        message = None
        if response and response.generations and response.generations[0]:
            message = getattr(response.generations[0][0], 'message', None)
        metadata = getattr(message, 'response_metadata', None) or {}
        usage = getattr(message, 'usage_metadata', None) or output.get('token_usage') or {}
        # Only extract one allowlisted response header. Never record the container.
        request_id = (metadata.get('headers') or {}).get('x-request-id') or getattr(error, 'request_id', None)
        details = usage.get('input_token_details') or usage.get('prompt_tokens_details') or {}
        with activate(ctx):
            emit('llm.completed', call_id=str(run_id),
                model=metadata.get('model_name') or output.get('model_name') or model,
                elapsed_ms=(perf_counter() - start) * 1000,
                status='failed' if error else 'succeeded',
                error_type=type(error).__name__ if error else None,
                provider_request_id=request_id,
                input_tokens=usage.get('input_tokens', usage.get('prompt_tokens')),
                output_tokens=usage.get('output_tokens', usage.get('completion_tokens')),
                total_tokens=usage.get('total_tokens'), cached_input_tokens=details.get('cache_read', details.get('cached_tokens')),
                retries=None, retry_callbacks=retries)

    def on_llm_end(self, response, *, run_id, **kwargs):
        self._finish(run_id, response=response)

    def on_llm_error(self, error, *, run_id, **kwargs):
        self._finish(run_id, error=error)


callback = ModelEvidence()
