from decimal import Decimal
from django import forms
from .policy import MAX_ITEM_QUANTITY, MAX_ORDER_MINOR, Policy, default_policy, validate_policy

ORDERING_LIMIT_FIELDS = (
    'max_line_quantity', 'max_item_quantity', 'max_basket_units', 'max_basket_lines',
    'max_subtotal_minor', 'max_payable_minor',
)
from .models import Connection, StockItem
from orders.models import MenuItem, MenuItemVariant, AddonItem


class IssueResolutionForm(forms.Form):
    evidence = forms.CharField(max_length=5000, widget=forms.Textarea(attrs={'rows': 4}),
        label='Provider verification and evidence',
        help_text='Record account/environment, provider references, amounts/statuses, verification time and ticket reference. Do not include credentials or card data.')
    note = forms.CharField(max_length=5000, widget=forms.Textarea(attrs={'rows': 4}),
        label='Actions taken and final disposition',
        help_text='Explain replay/refund decisions, event IDs, stock treatment and any residual ledger state. Leave unresolved if the outcome is still uncertain.')
    verified = forms.BooleanField(label='I verified the provider outcome and completed the required recovery actions.')


CURRENCIES = ('INR', 'EUR', 'GBP', 'USD', 'CAD', 'AUD', 'CHF', 'SEK', 'NOK', 'DKK', 'PLN', 'CZK', 'JPY')
MONEY_FIELDS = {'packaging': 'packaging_minor', 'minimum': 'minimum_minor',
                'max_subtotal': 'max_subtotal_minor', 'max_payable': 'max_payable_minor'}


def money_field(label, currency, *, required=True, minimum=0):
    exponent = 0 if currency == 'JPY' else 2
    return forms.DecimalField(label=f'{label} ({"₹ INR" if currency == "INR" else currency})',
        required=required, min_value=minimum, max_value=Decimal(MAX_ORDER_MINOR) / 10 ** exponent,
        decimal_places=exponent, max_digits=12, initial=0)


class RuleForm(forms.Form):
    code = forms.CharField(max_length=64)
    item_ids = forms.ModelMultipleChoiceField(queryset=MenuItem.objects.none(), required=False,
        label='Apply to items', help_text='Leave empty to apply to all items.')

    def __init__(self, *args, tenant=None, currency='INR', **kwargs):
        self.exponent = 0 if currency == 'JPY' else 2
        super().__init__(*args, **kwargs)
        self.fields['item_ids'].queryset = MenuItem.objects.filter(tenant=tenant).order_by('name') if tenant else MenuItem.objects.none()

    def rule(self):
        data = {key: value for key, value in self.cleaned_data.items() if key != 'DELETE'}
        data['item_ids'] = [str(item.pk) for item in data['item_ids']]
        return data


class TaxRuleForm(RuleForm):
    name = forms.CharField(max_length=100, label='Tax name')
    rate = forms.DecimalField(min_value=0, max_value=100, decimal_places=4, label='Rate (%)')
    inclusive = forms.BooleanField(required=False, label='Included in menu prices')
    tax_fees = forms.BooleanField(required=False, label='Also tax fees')


class DiscountRuleForm(RuleForm):
    percent = forms.DecimalField(required=False, min_value=0, max_value=100, decimal_places=4, label='Percentage (%)')
    modes = forms.MultipleChoiceField(required=False,
        choices=[('delivery', 'Delivery'), ('pickup', 'Pickup'), ('dine_in', 'Dine in')],
        widget=forms.CheckboxSelectMultiple, help_text='Leave empty to apply to all fulfillment modes.')

    def __init__(self, *args, currency='INR', **kwargs):
        super().__init__(*args, currency=currency, **kwargs)
        self.fields['fixed'] = money_field('Fixed discount', currency, required=False)
        self.fields['minimum'] = money_field('Minimum basket value', currency, required=False)

    def clean(self):
        data = super().clean()
        if data.get('percent') and data.get('fixed'):
            raise forms.ValidationError('Choose percentage or fixed discount, not both.')
        return data

    def rule(self):
        data = super().rule()
        data['percent'] = data['percent'] or 0
        for name in ('fixed', 'minimum'):
            data[f'{name}_minor'] = int((data.pop(name) or 0) * 10 ** self.exponent)
        return data


TaxRuleFormSet = forms.formset_factory(TaxRuleForm, extra=0, can_delete=True, max_num=100, validate_max=True)
DiscountRuleFormSet = forms.formset_factory(DiscountRuleForm, extra=0, can_delete=True, max_num=100, validate_max=True)


class CommerceSettingsForm(forms.Form):
    currency = forms.ChoiceField(choices=[(c, c) for c in CURRENCIES])
    stock_policy = forms.ChoiceField(choices=[('strict', 'Require numeric stock'), ('availability', 'Allow availability-only stock'), ('untracked', 'Do not check stock')],
        help_text='Tracked stock must be configured in Stock before accepting orders.')
    reservation_seconds = forms.IntegerField(min_value=60, max_value=86400, initial=900, label='Payment reservation duration in seconds')
    stock_max_age_seconds = forms.IntegerField(min_value=1, max_value=86400, initial=300, label='Maximum age of external stock in seconds')
    max_line_quantity = forms.IntegerField(min_value=1, max_value=MAX_ITEM_QUANTITY, label='Maximum quantity on one basket line')
    max_item_quantity = forms.IntegerField(min_value=1, max_value=MAX_ITEM_QUANTITY, label='Maximum quantity of one item across sizes and customizations')
    max_basket_units = forms.IntegerField(min_value=1, max_value=MAX_ITEM_QUANTITY, label='Maximum units in a basket')
    max_basket_lines = forms.IntegerField(min_value=1, max_value=MAX_ITEM_QUANTITY, label='Maximum basket lines')

    def __init__(self, *args, configuration=None, tenant=None, **kwargs):
        policy = Policy.model_validate(configuration.policy if configuration else default_policy()).model_dump(mode='json')
        tenant = tenant or (configuration.tenant if configuration else None)
        limits = policy.get('ordering_limits') or {}
        initial = {**policy, **limits}
        for name, key in MONEY_FIELDS.items():
            value = initial.get(key)
            initial[name] = Decimal(value) / 10 ** policy['exponent'] if value is not None else None
        super().__init__(*args, initial=initial, **kwargs)
        currency = self.data.get('currency', policy['currency']) if self.is_bound else policy['currency']
        self.exponent = 0 if currency == 'JPY' else 2
        for name, label in [('packaging', 'Packaging charge'), ('minimum', 'Minimum basket value'),
                            ('max_subtotal', 'Maximum item subtotal'), ('max_payable', 'Maximum payable amount')]:
            self.fields[name] = money_field(label, currency, minimum=Decimal(1) / 10 ** self.exponent if name.startswith('max_') else 0)
        self.fields['max_payable'].help_text = 'Includes fees and taxes. Must be at least the subtotal limit.'
        discounts = []
        for rule in policy.get('discounts', []):
            discounts.append({**rule, 'fixed': Decimal(rule.get('fixed_minor', 0)) / 10 ** policy['exponent'],
                              'minimum': Decimal(rule.get('minimum_minor', 0)) / 10 ** policy['exponent']})
        options = dict(data=self.data if self.is_bound else None, form_kwargs={'tenant': tenant, 'currency': currency})
        self.tax_rules = TaxRuleFormSet(prefix='taxes', initial=policy.get('taxes', []), **options)
        self.discount_rules = DiscountRuleFormSet(prefix='discounts', initial=discounts, **options)

    @property
    def rule_sets(self):
        return [('Taxes', self.tax_rules), ('Discounts', self.discount_rules)]

    def clean(self):
        data = super().clean()
        taxes_valid = self.tax_rules.is_valid()
        discounts_valid = self.discount_rules.is_valid()
        if not taxes_valid or not discounts_valid:
            self.add_error(None, 'Review the tax and discount rules below.')
        if self.errors:
            return data
        config = {key: data[key] for key in ('currency', 'stock_policy', 'reservation_seconds', 'stock_max_age_seconds')}
        config['exponent'] = self.exponent
        limits = {name: data[name] for name in ORDERING_LIMIT_FIELDS if not name.endswith('_minor')}
        for name, key in MONEY_FIELDS.items():
            (limits if name.startswith('max_') else config)[key] = int(data[name] * 10 ** self.exponent)
        config['ordering_limits'] = limits
        for name, formset in [('taxes', self.tax_rules), ('discounts', self.discount_rules)]:
            config[name] = [form.rule() for form in formset if form.cleaned_data and not form.cleaned_data.get('DELETE')]
        validate_policy(config)
        self.policy = Policy.model_validate(config).model_dump(mode='json')
        return data


class CommerceIntegrationForm(forms.Form):
    enabled = forms.BooleanField(required=False, label='Enable external POS and payment integrations')


CAPABILITIES = {
    'pos': ('order.submit', 'order.reconcile', 'inventory.update', 'catalog.read', 'catalog.write'),
    'payment': ('payment.create', 'payment.reconcile', 'payment.refund'),
}


class ConnectionForm(forms.ModelForm):
    capabilities = forms.MultipleChoiceField(choices=[(c, c) for values in CAPABILITIES.values() for c in values], widget=forms.CheckboxSelectMultiple)

    class Meta:
        model = Connection
        fields = ['provider', 'role', 'account_id', 'environment', 'capabilities', 'active']
        help_texts = {'active': 'Activate after your adapter is deployed and its provider connection has been tested.'}

    def clean(self):
        data = super().clean()
        role, capabilities = data.get('role'), set(data.get('capabilities', []))
        if role and capabilities - set(CAPABILITIES[role]):
            self.add_error('capabilities', 'Choose only capabilities for this connection role.')
        required = {'pos': {'order.submit', 'order.reconcile'}, 'payment': {'payment.create', 'payment.reconcile'}}
        menu_only = role == 'pos' and 'catalog.write' in capabilities and not capabilities & {'order.submit', 'order.reconcile'}
        if role and data.get('active') and not menu_only and not required[role] <= capabilities:
            self.add_error('capabilities', 'Active connections require submission/creation and reconciliation capabilities.')
        if data.get('active') and Connection.objects.filter(location=self.instance.location, role=role, active=True).exclude(pk=self.instance.pk).exists():
            self.add_error('active', 'Deactivate the existing connection for this role first.')
        if not self.instance._state.adding:
            old = Connection.objects.get(pk=self.instance.pk)
            if any(data.get(f) != getattr(old, f) for f in ('provider', 'role', 'account_id', 'environment')):
                self.add_error(None, 'Create a new connection to change provider, role, account or environment; existing payment and order identities must be preserved.')
        return data


class StockForm(forms.ModelForm):
    class Meta:
        model = StockItem
        fields = ['item', 'variant', 'addon', 'authority', 'mode', 'on_hand', 'available']
        labels = {'addon': 'Modifier option', 'on_hand': 'Total stock (including reserved units)'}
        help_texts = {'authority': 'Leave empty for local stock. Provider stock starts at zero until the adapter sends its first stock update.'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        tenant = self.instance.location.tenant
        self.fields['item'].queryset = MenuItem.objects.filter(tenant=tenant)
        self.fields['variant'].queryset = MenuItemVariant.objects.filter(menu_item__tenant=tenant)
        self.fields['addon'].queryset = AddonItem.objects.filter(group__tenant=tenant)
        self.fields['addon'].label_from_instance = lambda option: f'{option.group.name}: {option.name}'
        self.fields['authority'].queryset = Connection.objects.filter(location=self.instance.location, role='pos')
        self.fields['authority'].label_from_instance = lambda connection: f'{connection.provider} ({connection.environment})'
        if not self.instance._state.adding:
            for field in ('item', 'variant', 'addon', 'authority', 'mode'):
                self.fields[field].disabled = True

    def clean(self):
        data = super().clean()
        if sum(bool(data.get(f)) for f in ('item', 'variant', 'addon')) != 1:
            self.add_error(None, 'Choose exactly one item, variant or modifier option.')
        for field in ('item', 'variant', 'addon'):
            if data.get(field) and StockItem.objects.filter(location=self.instance.location, **{field: data[field]}).exclude(pk=self.instance.pk).exists():
                self.add_error(field, 'Stock already exists for this selection.')
        if self.instance.authority_id and not self.instance._state.adding:
            self.add_error(None, 'This stock is managed by its provider; update it through the adapter.')
        if data.get('authority'):
            if 'inventory.update' not in data['authority'].capabilities:
                self.add_error('authority', 'The connection must support inventory updates.')
            if self.instance._state.adding and data.get('on_hand') != 0:
                self.add_error('on_hand', 'Provider stock must start at zero and be populated by the adapter.')
        return data
