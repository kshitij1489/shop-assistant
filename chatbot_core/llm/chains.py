"""Small chain builders; callers retain their caching and business rules."""

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

from .models import get_chat_model


def _prompt(system, user):
    # Bind system text as a value so JSON examples and tenant content stay literal.
    return ChatPromptTemplate.from_messages([
        ("system", "{system}"), ("human", user),
    ]).partial(system=system)


def text_chain(system, user="{input}", **model_options):
    return _prompt(system, user) | get_chat_model(**model_options) | StrOutputParser()


def structured_chain(schema, system, user="{input}", *, include_raw=False, **model_options):
    model = get_chat_model(**model_options).with_structured_output(
        schema, method="json_schema", strict=True, include_raw=include_raw,
    )
    return _prompt(system, user) | model
