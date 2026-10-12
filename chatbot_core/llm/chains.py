"""Small chain builders; callers retain their caching and business rules."""

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableLambda
from openai import pydantic_function_tool

from .models import get_chat_model
from chatbot_core.recording.llm import capture_chain


def _prompt(system, user):
    # Bind system text as a value so JSON examples and tenant content stay literal.
    return ChatPromptTemplate.from_messages([
        ("system", "{system}"), ("human", user),
    ]).partial(system=system)


def text_chain(system, user="{input}", **model_options):
    model = get_chat_model(**model_options)
    chain = _prompt(system, user) | model | StrOutputParser()
    return capture_chain(chain, options=model_options, model=model)


def structured_chain(schema, system, user="{input}", *, include_raw=False, **model_options):
    # Keep the provider response before local validation. Passing the Pydantic
    # class to the SDK parses inside the provider call and loses malformed raw
    # responses before LangChain can emit its completion callback.
    response_format = {'type': 'json_schema', 'json_schema': {
        'name': schema.__name__, 'strict': True,
        'schema': pydantic_function_tool(schema)['function']['parameters'],
    }}
    configured_model = get_chat_model(**model_options)
    model = configured_model.bind(response_format=response_format)

    def parse(raw):
        result = {'raw': raw, 'parsed': None, 'parsing_error': None}
        try:
            if raw.additional_kwargs.get('refusal'):
                raise ValueError('Model refused structured response')
            # Providers can omit this metadata or use their own success reason.
            # Only explicit truncation/filtering/failure overrides a valid body.
            if (raw.response_metadata.get('finish_reason') in {'length', 'content_filter'}
                    or raw.response_metadata.get('status') in {'incomplete', 'failed'}):
                raise ValueError('Incomplete structured response')
            result['parsed'] = schema.model_validate_json(raw.content if isinstance(raw.content, str) else raw.text)
        except Exception as exc:
            result['parsing_error'] = exc
            if not include_raw:
                raise
        return result if include_raw else result['parsed']

    return capture_chain(_prompt(system, user) | model | RunnableLambda(parse),
                         schema=schema, options=model_options, model=configured_model)
