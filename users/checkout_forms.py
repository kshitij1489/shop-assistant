from django import forms
from orders.checkout_config import CheckoutPolicy, FIELDS, MODES, default_checkout_config, validate_checkout_config


class CheckoutSettingsForm(forms.Form):
    modes = forms.MultipleChoiceField(choices=[(m, m.replace('_', ' ').title()) for m in MODES], widget=forms.CheckboxSelectMultiple)
    timezone = forms.CharField(initial='Asia/Kolkata', help_text='Timezone used for opening hours and scheduled orders.')
    always_open = forms.BooleanField(required=False, initial=True, label='Open 24 hours every day',
        help_text='Otherwise enter weekly opening hours below. Leave a day empty when closed.')
    delivery_postal_codes = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 2}),
        help_text='Comma-separated postal codes. Leave empty for unrestricted delivery coverage.')
    online_provider = forms.ChoiceField(required=False, choices=[('', 'No online provider'), ('adapter', 'External payment adapter')],
        help_text='Online payments require an active external payment adapter and enabled commerce settings.')

    def __init__(self, *args, configuration=None, tenant=None, **kwargs):
        self.tenant = tenant
        config = configuration or default_checkout_config()
        initial = {**config, 'modes': list(config['modes']),
                   'delivery_postal_codes': ', '.join(config.get('delivery_postal_codes', []))}
        initial['always_open'] = not config.get('opening_hours')
        for day, intervals in config.get('opening_hours', {}).items():
            initial[f'hours_{day}'] = ', '.join('-'.join(interval) for interval in intervals)
        for mode, policy in config['modes'].items():
            initial.update({f'{mode}_{key}': value for key, value in policy.items()})
        super().__init__(*args, initial=initial, **kwargs)
        for day, label in enumerate(('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')):
            self.fields[f'hours_{day}'] = forms.CharField(required=False, label=label,
                help_text='24-hour times, e.g. 09:00-13:00, 14:00-22:00. Split overnight hours across days.')
        for mode in MODES:
            prefix = mode.replace('_', ' ').title()
            self.fields[f'{mode}_required_fields'] = forms.MultipleChoiceField(required=False,
                choices=[(f, f.replace('_', ' ').title()) for f in FIELDS[mode]], widget=forms.CheckboxSelectMultiple,
                label=f'{prefix}: required fields')
            self.fields[f'{mode}_payment_methods'] = forms.MultipleChoiceField(required=False,
                choices=[('cash', 'Cash at fulfillment'), ('online', 'Online')], widget=forms.CheckboxSelectMultiple,
                initial=['cash'], label=f'{prefix}: payment methods')
            self.fields[f'{mode}_preparation_minutes'] = forms.IntegerField(required=False, initial=20, min_value=0, max_value=1440,
                label=f'{prefix}: preparation minutes')
            self.fields[f'{mode}_scheduling_enabled'] = forms.BooleanField(required=False, label=f'{prefix}: allow scheduling')
            self.fields[f'{mode}_max_advance_days'] = forms.IntegerField(required=False, initial=7, min_value=1, max_value=365,
                label=f'{prefix}: maximum days in advance')
            for key in ('minimum_order', 'fee'):
                self.fields[f'{mode}_{key}'] = forms.DecimalField(required=False, initial=0, min_value=0, max_digits=10, decimal_places=2,
                    label=f'{prefix}: {key.replace("_", " ")}',
                    help_text='In your menu currency. Minimum excludes fees; fee is charged once per order.')

    @property
    def panels(self) -> dict[str, list[tuple[str, list]]]:
        """Group fields into the Checkout and Hours settings tabs.

        Delivery coverage starts with the delivery mode name, so it is excluded
        from the per-mode field lists.
        """
        shared = {'modes', 'timezone', 'always_open', 'delivery_postal_codes', 'online_provider'}
        checkout = [('Ordering', [self[name] for name in ('modes', 'delivery_postal_codes', 'online_provider')])]
        for mode in MODES:
            title = mode.replace('_', ' ').title()
            fields = [self[name] for name in self.fields if name.startswith(f'{mode}_') and name not in shared]
            checkout.append((title, fields))
        hours = [
            ('Schedule', [self['timezone'], self['always_open']]),
            ('Opening hours', [self[f'hours_{day}'] for day in range(7)]),
        ]
        return {'checkout': checkout, 'hours': hours}

    @property
    def error_fields(self):
        return [
            {'tab': tab, 'field': field}
            for tab, sections in self.panels.items()
            for _title, fields in sections
            for field in fields if field.errors
        ]

    def clean(self):
        data = super().clean()
        if self.errors:
            return data
        modes = {}
        for mode in data.get('modes', []):
            policy = {}
            for field in ('required_fields', 'payment_methods', 'preparation_minutes', 'scheduling_enabled',
                          'max_advance_days', 'minimum_order', 'fee'):
                value = data.get(f'{mode}_{field}')
                if value is not None:
                    policy[field] = value
            modes[mode] = policy
        hours = {} if data.get('always_open') else {str(day): [
            [part.strip() for part in interval.strip().split('-')]
            for interval in data.get(f'hours_{day}', '').split(',') if interval.strip()] for day in range(7)}
        config = {'modes': modes, 'timezone': data.get('timezone'), 'opening_hours': hours,
                  'delivery_postal_codes': [c.strip() for c in data.get('delivery_postal_codes', '').split(',') if c.strip()],
                  'online_provider': data.get('online_provider', '')}
        validate_checkout_config(config)
        from orders.checkout_config import validate_online_readiness
        validate_online_readiness(config, self.tenant)
        self.configuration = CheckoutPolicy.model_validate(config).model_dump(mode='json')
        return data
