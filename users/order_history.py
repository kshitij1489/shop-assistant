"""Present saved order facts, preferring immutable accepted-order snapshots."""
from decimal import Decimal
import re

from commerce.models import AcceptedOrder


def money(value, currency, exponent):
    prefix = '₹' if currency == 'INR' else f'{currency} '
    return f'{prefix}{Decimal(value):.{exponent}f}'


def prepare_order_history(orders):
    orders = list(orders)
    for order in orders:
        try:
            record = order.commerce_record
        except AcceptedOrder.DoesNotExist:
            record = None
        if record:
            currency, exponent = record.currency, record.exponent
            order.display_total = money(Decimal(record.total_minor) / 10 ** exponent, currency, exponent)
            order.display_items = [dict(
                name=line['name'], size=line.get('size', ''), quantity=line['quantity'],
                unit_price=money(line['unit_price'], currency, exponent),
                modifiers=[dict(name=choice['name'], quantity=choice['quantity'],
                                unit_price=money(choice['unit_price'], currency, exponent))
                           for choice in line.get('modifiers', [])],
            ) for line in record.snapshot.get('pricing', {}).get('lines', [])]
            continue
        # Legacy orders without accepted pricing used INR unless currency was saved.
        currency = order.meta.get('currency', 'INR')
        exponent = order.meta.get('exponent', 2)
        order.display_total = money(order.total_amount, currency, exponent)
        order.display_items = []
        for item in order.items.all():
            size = item.variant.size if item.variant_id else ''
            # Basket orders already include the variant in their saved item name.
            if re.search(r'\s\([^()]+\)$', item.item_name):
                size = ''
            order.display_items.append(dict(
                name=item.item_name, size=size, quantity=item.quantity,
                unit_price=money(item.unit_price, currency, exponent),
                modifiers=[dict(name=choice.addon_name or 'Customization (original name unavailable)', quantity=choice.quantity,
                                unit_price=money(choice.unit_price, currency, exponent))
                           for choice in item.addons.all()],
            ))
    return orders
