"""Cancellation of an explicitly selected pending request."""

STOPPED_REQUEST = "Stopped the current request. Your basket and any placed order are unchanged."


def stop_pending_request(pending, previous, basket, customer, checklist):
    """Stop only the displayed request (or its explicitly selected original)."""
    if previous not in pending:
        previous = None
    if previous is not None and previous.basket_item.get('clarify_cancel_target'):
        pending.remove(previous)
        previous.is_complete = True
        previous.follow_up_question.clear()
        previous = next((item for item in pending
                         if item.query_id == previous.basket_item.get('cancel_task_id')), None)
    if previous is None:
        return "There is no pending request to cancel."
    # Checkout also has a durable draft. Its cleanup must finish before the
    # question is removed, or recovery could resurrect an abandoned checkout.
    if previous.basket_item.get('checkout'):
        previous.resolved_action = None
        previous.main_query = 'cancel checkout'
        reply = previous.configured_checkout(basket, customer, checklist)
        if not previous.is_complete:
            return reply
    else:
        reply = STOPPED_REQUEST
    pending.remove(previous)
    previous.is_complete = True
    previous.follow_up_question.clear()
    previous.basket_item = {}
    previous.handoff_to = None
    previous.handoff_overrides = {}
    return reply
