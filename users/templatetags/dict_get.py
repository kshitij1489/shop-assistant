from django import template

register = template.Library()

@register.filter
def dict_get(d, key):
    """
    Usage in template: {{ row|dict_get:col }}
    Returns d.get(key) if possible, else empty string.
    """
    try:
        if d is None:
            return ''
        # if it's already a mapping
        return d.get(key, '') if hasattr(d, 'get') else ''
    except Exception:
        return ''