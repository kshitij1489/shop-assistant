"""Join completed reply fragments without changing their facts or formatting."""


def reply_separator(text):
    return ' ' if text[-1] in '.!?。！？।:;…' else '. '


def join_replies(parts):
    result = ''
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if result:
            # Preserve paragraphs, URLs, decimals and existing sentence endings.
            result += reply_separator(result)
        result += part
    return result
