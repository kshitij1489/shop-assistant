"""Fresh, validated product presets used by models, forms and onboarding."""


def ordering_defaults(*, demo=False):
    from commerce.policy import Policy
    from .checkout_config import CheckoutPolicy, ModePolicy

    # Deliberately adopted product defaults, in INR. Owners can edit every cap.
    limits = dict(max_line_quantity=20, max_item_quantity=30, max_basket_units=60,
                  max_basket_lines=20, max_subtotal_minor=500_000, max_payable_minor=600_000)
    modes = {'pickup': ModePolicy(required_fields=['name', 'phone'])}
    if demo:
        modes.update(
            delivery=ModePolicy(required_fields=['name', 'phone', 'address', 'postal_code'], fee='30'),
            dine_in=ModePolicy(required_fields=['name', 'phone', 'table_id']),
        )
    checkout = CheckoutPolicy(
        modes=modes, opening_hours={str(day): [['09:00', '18:00']] for day in range(7)},
        delivery_postal_codes=['560001'] if demo else [],
    )
    policy = Policy(ordering_limits=limits, stock_policy='strict' if demo else 'untracked')
    return {'checkout': checkout.model_dump(mode='json'), 'policy': policy.model_dump(mode='json')}
