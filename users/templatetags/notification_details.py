from django import template

register = template.Library()


@register.simple_tag(takes_context=True)
def consume_notification_details(context):
    request = context.get('request')
    if request is None:
        return []
    return request.session.pop('action_error_details', [])
