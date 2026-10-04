"""Bounded reference context shared by factual answer handlers.

Route changes do not start new conversations. Only the immediate prior exchange
in this tenant/chat/channel can supply references; it is never factual evidence
or permission to execute a historical request.
"""
REFERENCE_INTENTS = frozenset({'information_about_the_cafe', 'menu_items', 'placing_order', 'order_enquiry'})
MAX_QUESTION_CHARS = 4000
MAX_REPLY_CHARS = 2000


def previous_knowledge_context(intent, history):
    last = history[-1] if history else None
    previous = last.get('query_obj') if isinstance(last, dict) else None
    if not isinstance(previous, dict) or intent.tenant in (None, '') or not intent.chat_id or not intent.platform or (
        str(previous.get('tenant')) != str(intent.tenant)
        or previous.get('chat_id') != intent.chat_id
        or previous.get('platform') != intent.platform
        or previous.get('intent_type') not in REFERENCE_INTENTS
    ):
        return {}
    result = {}
    for source, target, limit in (
        ('main_query', 'previous_user_message', MAX_QUESTION_CHARS),
        ('response', 'system_log_message', MAX_REPLY_CHARS),
    ):
        value = previous.get(source)
        if isinstance(value, str) and value.strip():
            result[target] = value[:limit]
    return result
