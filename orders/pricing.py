"""Tax-exclusive catalog prices: percentage on item + modifiers, fixed per item.

Commerce policies override these catalog mappings when commerce is enabled.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from orders.models import VariantTaxMap
from chatbot_core.logic.cafe.catalog import selection_unit_total


def catalog_taxes(tenant, entries):
    snapshots = []
    for entry in entries:
        variant_id = entry.get('item_variant_id') or getattr(entry.get('variant'), 'pk', None)
        taxes = []
        for mapping in VariantTaxMap.objects.filter(variant_id=variant_id).select_related('tax', 'variant__menu_item').order_by('tax_id'):
            tax = mapping.tax
            if tax.tenant_id != tenant.pk or mapping.variant.menu_item.tenant_id != tenant.pk:
                raise ValueError('Tax mapping belongs to another tenant.')
            try:
                rate = Decimal(tax.rate_display.strip().rstrip('%'))
            except (InvalidOperation, ValueError):
                raise ValueError('A catalog tax rate is invalid. Please contact the café.')
            if not rate.is_finite() or rate < 0 or tax.type not in {'P', 'F'}:
                raise ValueError('A catalog tax rate is invalid. Please contact the café.')
            amount = (selection_unit_total(entry) * rate / 100 if tax.type == 'P' else rate) * entry['quantity']
            taxes.append({'id': str(tax.pk), 'name': tax.title, 'type': tax.type, 'rate': str(rate),
                          'amount': str(amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))})
        snapshots.append(taxes)
    total = sum((Decimal(t['amount']) for taxes in snapshots for t in taxes), Decimal('0.00'))
    return total, snapshots
