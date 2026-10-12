"""LangChain boundaries preserve assembled prompts, raw responses and parsing."""
from langchain_core.callbacks import BaseCallbackHandler
from .capture import current


class ModelCapture(BaseCallbackHandler):
    run_inline = True

    def __init__(self, recorder):
        self.recorder = recorder

    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, **kwargs):
        self.recorder.emit('model.started', call_id=str(run_id), parent_call_id=str(parent_run_id),
                           messages=messages, settings=kwargs.get('invocation_params', {}))

    def on_llm_end(self, response, *, run_id, **kwargs):
        self.recorder.emit('model.completed', call_id=str(run_id), response=response)

    def on_llm_error(self, error, *, run_id, **kwargs):
        self.recorder.emit('model.failed', call_id=str(run_id), error=error,
                           partial_response=kwargs.get('response'),
                           completion=getattr(error, 'completion', None))

    def on_retry(self, retry_state, *, run_id, **kwargs):
        self.recorder.emit('model.retry', call_id=str(run_id), attempt=retry_state.attempt_number)


def capture_chain(chain, *, schema=None, options=None, model=None):
    recorder = current()
    if recorder is None:
        return chain

    def start(run):
        recorder.emit('model_chain.started', call_id=str(run.id), inputs=run.inputs,
                      schema=schema.model_json_schema() if schema else None, options=options or {},
                      effective_settings={key: getattr(model, key, None) for key in (
                          'model_name', 'temperature', 'request_timeout', 'max_tokens', 'max_retries', 'reasoning_effort')})

    def end(run):
        recorder.emit('model_chain.completed', call_id=str(run.id), output=run.outputs)

    def error(run):
        recorder.emit('model_chain.failed', call_id=str(run.id), error=run.error)

    return chain.with_config(callbacks=[ModelCapture(recorder)]).with_listeners(
        on_start=start, on_end=end, on_error=error)
