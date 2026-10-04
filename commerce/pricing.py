"""Deterministic minor-unit prices. No binary floats and no provider I/O."""
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from .policy import MAX_ITEM_QUANTITY, MAX_ORDER_MINOR, Policy


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def minor(value, exponent):
    number = Decimal(str(value))
    if not number.is_finite() or number < 0:
        raise ValueError('Money must be finite and nonnegative.')
    return int((number * 10 ** exponent).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def major(value, exponent):
    return Decimal(value) / Decimal(10 ** exponent)


def allocate(amount, weights):
    """Largest remainder allocation; stable input order breaks ties."""
    total = sum(weights)
    if not total:
        return [0] * len(weights)
    result = [amount * w // total for w in weights]
    ranking = sorted(range(len(weights)), key=lambda i: (-(amount * weights[i] % total), i))
    for i in ranking[:amount - sum(result)]:
        result[i] += 1
    return result


def calculate(selections, policy, *, mode, fee='0', discount_code=''):
    policy = Policy.model_validate(policy)
    if not selections:
        raise ValueError('An order needs at least one item.')
    lines = []
    for selection in selections:
        line = deepcopy(selection)
        if type(line['quantity']) is not int or not 1 <= line['quantity'] <= MAX_ITEM_QUANTITY:
            raise ValueError(f'Item quantity must be between 1 and {MAX_ITEM_QUANTITY}.')
        line['unit_minor'] = minor(line['unit_price'], policy.exponent)
        modifier_unit_minor = 0
        for modifier in line.get('modifiers', []):
            if type(modifier['quantity']) is not int or not 1 <= modifier['quantity'] <= MAX_ITEM_QUANTITY:
                raise ValueError(f'Modifier quantity must be between 1 and {MAX_ITEM_QUANTITY}.')
            modifier['unit_minor'] = minor(modifier['unit_price'], policy.exponent)
            modifier['subtotal_minor'] = modifier['unit_minor'] * modifier['quantity'] * line['quantity']
            modifier_unit_minor += modifier['unit_minor'] * modifier['quantity']
        line['subtotal_minor'] = (line['unit_minor'] + modifier_unit_minor) * line['quantity']
        line['discount_minor'] = 0
        lines.append(line)
    subtotal = sum(line['subtotal_minor'] for line in lines)
    if subtotal < policy.minimum_minor:
        raise ValueError('The basket is below the commerce minimum order value.')
    if discount_code:
        discount = next((d for d in policy.discounts if d.code == discount_code), None)
        if not discount or subtotal < discount.minimum_minor or (discount.modes and mode not in discount.modes):
            raise ValueError('This discount is not eligible for this order.')
        weights = [line['subtotal_minor'] if not discount.item_ids or line['item_id'] in discount.item_ids else 0 for line in lines]
        eligible = sum(weights)
        if not eligible:
            raise ValueError('No items qualify for this discount.')
        amount = min(eligible, discount.fixed_minor or minor(Decimal(eligible) * discount.percent / 100, 0))
        for line, share in zip(lines, allocate(amount, weights)):
            line['discount_minor'] = share
    fees = [dict(code='fulfillment', subtotal_minor=minor(fee, policy.exponent), discount_minor=0),
            dict(code='packaging', subtotal_minor=policy.packaging_minor, discount_minor=0)]
    for line in [*lines, *fees]:
        base = line['subtotal_minor'] - line['discount_minor']
        rules = [t for t in policy.taxes if (t.tax_fees if 'code' in line else not t.item_ids or line['item_id'] in t.item_ids)]
        rate = sum((t.rate for t in rules), Decimal(0))
        inclusive = bool(rules and rules[0].inclusive)
        tax = minor(Decimal(base) * rate / (100 + rate if inclusive else 100), 0)
        # Decimal rates converted to integer weights without floating-point loss.
        shares = allocate(tax, [int(t.rate * 10000) for t in rules])
        line['taxes'] = [dict(code=t.code, name=t.name, rate=str(t.rate), inclusive=t.inclusive, amount_minor=share) for t, share in zip(rules, shares)]
        line['tax_minor'] = tax
        line['net_minor'] = base - tax if inclusive else base
        line['total_minor'] = base if inclusive else base + tax
    total = sum(line['total_minor'] for line in [*lines, *fees])
    if total > MAX_ORDER_MINOR:
        raise ValueError('Order total exceeds the supported limit.')
    return dict(schema_version=1, currency=policy.currency, exponent=policy.exponent,
                rounding='HALF_UP_PER_LINE_LARGEST_REMAINDER', policy=policy.model_dump(mode='json'),
                lines=lines, fees=fees, subtotal_minor=subtotal, discount_code=discount_code,
                discount_minor=sum(l['discount_minor'] for l in lines),
                tax_minor=sum(l['tax_minor'] for l in [*lines, *fees]), total_minor=total)
