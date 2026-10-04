"""Small wire examples for the offline walkthrough (not a second pricing engine)."""
import hashlib
import uuid
from datetime import datetime, timedelta, timezone

from .storage import encode


def identifier(name):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'commerce-reference/' + name))


def command(kind, data, name=None):
    key = identifier(name or kind)
    return dict(schema_version=1, command_id=key, idempotency_key=key, type=kind,
        lease_token=str(uuid.uuid4()), lease_until=(datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat(),
        attempt=1, data=data)


def payment_command():
    return command('payment.create', dict(payment_id=identifier('payment'), order_id=identifier('order'),
        currency='EUR', exponent=2, amount_minor=1250,
        customer=dict(id=None, name='Demo Guest', phone=''),
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()))


def order_command(payment):
    pricing = dict(schema_version=1, currency='EUR', exponent=2,
        rounding='HALF_UP_PER_LINE_LARGEST_REMAINDER', policy={'currency': 'EUR', 'exponent': 2},
        location_id=identifier('location'), subtotal_minor=1250, discount_code='', discount_minor=0,
        tax_minor=0, total_minor=1250, fees=[], lines=[dict(line_id=identifier('order') + ':1',
            item_id=identifier('coffee'), item_variant_id=identifier('regular'), name='Coffee', quantity=1,
            unit_price='12.50', unit_minor=1250, subtotal_minor=1250, discount_minor=0, tax_minor=0,
            net_minor=1250, total_minor=1250, taxes=[], modifiers=[])])
    snapshot = dict(schema_version=1, order_id=identifier('order'), tenant_id=identifier('tenant'),
        location_id=identifier('location'), source='website', customer=dict(id=None, name='Demo Guest', phone=''),
        fulfillment={'fulfillment_id': identifier('order') + ':fulfillment', 'mode': 'pickup'},
        instructions='', pricing=pricing)
    return command('order.submit', dict(accepted_order_id=identifier('accepted'), order_id=identifier('order'),
        snapshot_hash=hashlib.sha256(encode(snapshot).encode()).hexdigest(), snapshot=snapshot,
        payment_method='online', payments=[dict(payment_id=identifier('payment'), external_id=payment['external_id'],
            provider='custom', currency='EUR', captured_minor=1250, refunded_minor=0)], reservations=[]))
