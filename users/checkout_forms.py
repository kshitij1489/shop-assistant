from django import forms
from orders.checkout_config import FIELDS, MODES, CheckoutPolicy, default_checkout_config


class CheckoutSettingsForm(forms.Form):
    modes = forms.MultipleChoiceField(choices=[(m, m.replace('_', ' ').title()) for m in MODES], widget=forms.CheckboxSelectMultiple)
    timezone = forms.CharField(initial='Asia/Kolkata', help_text='Timezone used for opening hours and scheduled orders.')
    always_open = forms.BooleanField(required=False, initial=False, label='Open 24 hours every day',
        help_text='Otherwise enter weekly opening hours below. Leave a day empty when closed.')
    delivery_postal_codes = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 2}),
        help_text='Comma-separated postal codes. Leave empty for unrestricted delivery coverage.')
    online_provider = forms.ChoiceField(required=False, choices=[('', 'No online provider'), ('adapter', 'External payment adapter')],
        help_text='Online payments require an active external payment adapter and enabled commerce settings.')

    def __init__(self, *args, configuration=None, tenant=None, currency='INR', **kwargs):
        self.tenant = tenant
        config = CheckoutPolicy.model_validate(configuration or default_checkout_config()).model_dump(mode='json')
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
                self.fields[f'{mode}_{key}'] = forms.DecimalField(required=False, initial=0, min_value=0, max_digits=10, decimal_places=0 if currency == 'JPY' else 2,
                    label=f'{prefix}: {key.replace("_", " ")} ({"₹ INR" if currency == "INR" else currency})',
                    help_text='Minimum excludes fees; fee is charged once per order.')
        # Enabling another mode starts with complete contact/payment defaults.
        for mode in MODES:
            initial.setdefault(f'{mode}_required_fields', list(FIELDS[mode]) if mode != 'pickup' else ['name', 'phone'])

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
        from chatbot_core.configuration_imports import validated_checkout
        self.configuration = validated_checkout(config, self.tenant)
        return data


class OrderingSetupForm(forms.Form):
    modes = forms.MultipleChoiceField(choices=[(m, m.replace('_', ' ').title()) for m in MODES],
        widget=forms.CheckboxSelectMultiple, initial=['pickup'])
    timezone = forms.CharField(initial='Asia/Kolkata')
    days = forms.MultipleChoiceField(choices=list(enumerate(('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'))),
        widget=forms.CheckboxSelectMultiple, label='Open on', initial=[str(day) for day in range(7)])
    opens = forms.TimeField(widget=forms.TimeInput(attrs={'type': 'time'}), initial='09:00', label='Opens at')
    closes = forms.TimeField(widget=forms.TimeInput(attrs={'type': 'time'}), initial='18:00', label='Closes at')
    delivery_postal_codes = forms.CharField(required=False, label='Delivery postal codes',
        help_text='Required when delivery is selected. Separate postal codes with commas.')
    confirmed = forms.BooleanField(label='These hours and fulfillment modes are correct for my business.')

    def __init__(self, *args, configuration=None, tenant=None, **kwargs):
        self.tenant = tenant
        self.configuration = CheckoutPolicy.model_validate(configuration or default_checkout_config()).model_dump(mode='json')
        config = self.configuration
        initial = dict(modes=list(config['modes']), timezone=config['timezone'],
                       delivery_postal_codes=', '.join(config.get('delivery_postal_codes', [])))
        hours = config.get('opening_hours', {})
        intervals = [interval for day in hours.values() for interval in day]
        self.preserve_opening_hours = not intervals or any(len(day) > 1 for day in hours.values()) or any(
            interval != intervals[0] for interval in intervals)
        if not self.preserve_opening_hours:
            initial.update(days=[day for day, values in hours.items() if values], opens=intervals[0][0], closes=intervals[0][1])
        super().__init__(*args, initial=initial, **kwargs)
        if self.preserve_opening_hours:
            for name in ('days', 'opens', 'closes'):
                del self.fields[name]
            self.fields['confirmed'].label = 'The saved opening hours and fulfillment modes are correct for my business.'

    def clean(self):
        from copy import deepcopy
        from orders.checkout_config import ModePolicy
        from chatbot_core.configuration_imports import validated_checkout
        data = super().clean()
        if self.errors:
            return data
        if not self.preserve_opening_hours and data['opens'] >= data['closes']:
            self.add_error('closes', 'Closing time must be later than opening time. You can set split or overnight hours in Opening hours after setup.')
        if 'delivery' in data['modes'] and not data['delivery_postal_codes'].strip():
            self.add_error('delivery_postal_codes', 'Enter the postal codes you deliver to.')
        if self.errors:
            return data
        config = deepcopy(self.configuration)
        config['timezone'] = data['timezone']
        if not self.preserve_opening_hours:
            config['opening_hours'] = {str(day): [
                [data['opens'].strftime('%H:%M'), data['closes'].strftime('%H:%M')]] if str(day) in data['days'] else [] for day in range(7)}
        config['delivery_postal_codes'] = [code.strip() for code in data['delivery_postal_codes'].split(',') if code.strip()] if 'delivery' in data['modes'] else []
        config['modes'] = {mode: config['modes'].get(mode) or ModePolicy(required_fields=[
            f for f in FIELDS[mode] if f != 'scheduled_at']).model_dump(mode='json') for mode in data['modes']}
        self.configuration = validated_checkout(config, self.tenant)
        return data
