from django import forms
from django.forms import inlineformset_factory, BaseInlineFormSet
from orders.models import AddonGroup, AddonItem, ItemAddonGroup


class ModifierGroupForm(forms.ModelForm):
    class Meta:
        model = AddonGroup
        fields = ['name']


class ModifierOptionForm(forms.ModelForm):
    aliases = forms.CharField(required=False, help_text='Alternate names, one per line.', widget=forms.Textarea(attrs={'rows': 2}))

    class Meta:
        model = AddonItem
        fields = ['name', 'price', 'aliases', 'min_quantity', 'max_quantity', 'is_available']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial['aliases'] = '\n'.join(self.instance.aliases or [])
        self.fields['price'].min_value = 0

    def clean_aliases(self):
        return list(dict.fromkeys(x.strip() for x in self.cleaned_data['aliases'].splitlines() if x.strip()))

    def clean(self):
        data = super().clean()
        if data.get('price') is not None and data['price'] < 0:
            self.add_error('price', 'Price cannot be negative.')
        low, high = data.get('min_quantity'), data.get('max_quantity')
        if low is not None and high is not None and not 1 <= low <= high:
            self.add_error('max_quantity', 'Quantity limits must satisfy 1 ≤ minimum ≤ maximum.')
        return data


class ModifierOptionsFormSet(BaseInlineFormSet):
    def add_fields(self, form, index):
        super().add_fields(form, index)
        form.fields['id'].queryset = AddonItem.objects.filter(group=self.instance) if self.instance.pk else AddonItem.objects.none()

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        available = sum(bool(form.cleaned_data.get('is_available')) for form in self.forms
                        if form.cleaned_data and not form.cleaned_data.get('DELETE'))
        if self.instance.pk and self.instance.items.filter(min_selections__gt=available).exists():
            raise forms.ValidationError('These options would leave an attached item without enough required choices. Lower its minimum selections first.')


ModifierOptions = inlineformset_factory(AddonGroup, AddonItem, form=ModifierOptionForm,
    formset=ModifierOptionsFormSet, extra=1, can_delete=True)


class ItemModifierForm(forms.ModelForm):
    variants = forms.ModelMultipleChoiceField(queryset=None, required=False, help_text='Leave empty to apply to every variant.')

    class Meta:
        model = ItemAddonGroup
        fields = ['group', 'min_selections', 'max_selections']

    def __init__(self, *args, item, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['group'].queryset = AddonGroup.objects.filter(tenant=item.tenant)
        self.fields['group'].label_from_instance = lambda group: group.name
        self.fields['variants'].queryset = item.variants.all()
        self.initial['variants'] = self.instance.variant_ids
        self.instance.item = item
        self.instance.tenant = item.tenant

    def clean(self):
        data = super().clean()
        low, high = data.get('min_selections'), data.get('max_selections')
        if low is not None and high is not None and low > high:
            self.add_error('max_selections', 'Maximum must be at least the minimum.')
        if data.get('group') and low is not None and low > data['group'].addons.filter(is_available=True).count():
            self.add_error('min_selections', 'Add enough available options before requiring this many selections.')
        if data.get('group') and ItemAddonGroup.objects.filter(item=self.instance.item, group=data['group']).exclude(pk=self.instance.pk).exists():
            self.add_error('group', 'This group is already attached to the item; edit its existing rules.')
        return data

    def save(self, commit=True):
        self.instance.variant_ids = [str(v.pk) for v in self.cleaned_data['variants']]
        return super().save(commit=commit)
