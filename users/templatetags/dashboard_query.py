from django import template

register = template.Library()


@register.simple_tag(takes_context=True)
def query_update(context, **changes):
    """Change one paginator while retaining the other filters and pages."""
    query = context['request'].GET.copy()
    for name, value in changes.items():
        query[name] = value
    return '?' + query.urlencode()
