"""Read payment links supplied by the external adapter's signed events."""
from django.utils import timezone
from .models import AcceptedOrder


def initiate_payment(order):
    record = AcceptedOrder.objects.filter(order=order).first()
    if record is None:
        raise ValueError('Online payments require an external payment adapter.')
    payment = record.payments.first()
    if record.state != 'awaiting_payment' or record.expires_at <= timezone.now() or not payment:
        raise ValueError('This order is not awaiting an online payment.')
    return {'payment_url': payment.checkout_url, 'payment_id': str(payment.pk), 'pending': not payment.checkout_url}
