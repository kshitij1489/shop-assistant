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


class CommerceSettingsForm(forms.Form):
    enabled = forms.BooleanField(required=False, label='Enable commerce for configurable checkout')
    currency = forms.ChoiceField(choices=[(c, c) for c in ('INR', 'EUR', 'GBP', 'USD', 'CAD', 'AUD', 'CHF', 'SEK', 'NOK', 'DKK', 'PLN', 'CZK', 'JPY')])
    packaging_minor = forms.IntegerField(min_value=0, initial=0, label='Packaging charge in minor units', help_text='100 minor units = 1.00 for INR/EUR/GBP; 1 minor unit = 1 JPY.')
    minimum_minor = forms.IntegerField(min_value=0, initial=0, label='Minimum basket value in minor units')
    stock_policy = forms.ChoiceField(choices=[('strict', 'Require fresh numeric stock'), ('availability', 'Allow availability-only stock'), ('untracked', 'Do not check stock')])
    reservation_seconds = forms.IntegerField(min_value=60, max_value=86400, initial=900, label='Payment reservation duration in seconds')
    stock_max_age_seconds = forms.IntegerField(min_value=1, max_value=86400, initial=300, label='Maximum age of external stock in seconds')
    taxes = forms.JSONField(required=False, initial=list, widget=forms.Textarea(attrs={'rows': 5}), help_text='Tax rules: code, name, rate, inclusive, optional item_ids and tax_fees. Example: [{"code":"VAT","name":"VAT","rate":"20","inclusive":true}]')
    discounts = forms.JSONField(required=False, initial=list, widget=forms.Textarea(attrs={'rows': 5}), help_text='Discount rules: code, percent or fixed_minor, optional minimum_minor, item_ids and modes. Example: [{"code":"WELCOME","percent":"10"}]')
    max_line_quantity = forms.IntegerField(required=False, min_value=1, max_value=MAX_ITEM_QUANTITY, label='Maximum quantity on one basket line')
    max_item_quantity = forms.IntegerField(required=False, min_value=1, max_value=MAX_ITEM_QUANTITY, label='Maximum quantity of one item across sizes and customizations')
    max_basket_units = forms.IntegerField(required=False, min_value=1, max_value=MAX_ITEM_QUANTITY, label='Maximum units in a basket')
    max_basket_lines = forms.IntegerField(required=False, min_value=1, max_value=MAX_ITEM_QUANTITY, label='Maximum basket lines')
    max_subtotal_minor = forms.IntegerField(required=False, min_value=1, max_value=MAX_ORDER_MINOR, label='Maximum item subtotal in minor units')
    max_payable_minor = forms.IntegerField(required=False, min_value=1, max_value=MAX_ORDER_MINOR, label='Maximum payable amount in minor units')

    def __init__(self, *args, configuration=None, **kwargs):
        initial = configuration.policy if configuration else default_policy()
        limits = initial.get('ordering_limits') or {}
        super().__init__(*args, initial={**initial, **limits, 'enabled': bool(configuration and configuration.enabled)}, **kwargs)
        self.fields['max_subtotal_minor'].help_text = 'Required together with the other ordering limits before ordering can be published. Minor units use this policy currency and exponent. Leave all six empty to keep ordering unavailable.'
        self.fields['max_payable_minor'].help_text = 'Checked again after fees and taxes once fulfillment is known. Must be at least the subtotal limit.'

    def clean(self):
        data = super().clean()
        if self.errors:
            return data
        limits = {name: data.get(name) for name in ORDERING_LIMIT_FIELDS}
        config = {k: data[k] for k in self.fields if k != 'enabled' and k not in ORDERING_LIMIT_FIELDS}
        config.update(exponent=0 if config['currency'] == 'JPY' else 2, taxes=config['taxes'] or [], discounts=config['discounts'] or [])
        if any(value is not None for value in limits.values()):
            if any(value is None for value in limits.values()):
                self.add_error(None, 'Enter every ordering limit, or leave them all empty. Ordering stays unavailable until the full set is saved.')
                return data
            config['ordering_limits'] = limits
        else:
            config['ordering_limits'] = None
        validate_policy(config)
        self.policy = Policy.model_validate(config).model_dump(mode='json')
        return data


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
