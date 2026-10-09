"""Keep action notifications brief and validation details available after redirects."""
from django.contrib import messages


def action_error(request, message, details):
    messages.error(request, message)
    if hasattr(details, 'messages'):
        details = details.messages
    elif isinstance(details, str):
        details = [details]
    else:
        details = [str(details)]
    request.session['action_error_details'] = details
