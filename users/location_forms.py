from django import forms

from chatbot_core.models import TenantInfo
from . import site_location


class SiteLocationForm(forms.Form):
    city = forms.CharField(max_length=100, widget=forms.TextInput(attrs={
        'role': 'combobox', 'aria-autocomplete': 'list', 'aria-expanded': 'false',
        'aria-controls': 'city-suggestions', 'aria-describedby': 'city-status',
        'autocomplete': 'off', 'placeholder': 'Search by city and country',
    }))
    state = forms.CharField(max_length=100, required=False, widget=forms.TextInput(attrs={'readonly': True}))
    country = forms.CharField(max_length=100, widget=forms.TextInput(attrs={'readonly': True}))
    city_place_id = forms.CharField(max_length=255, widget=forms.HiddenInput())
    street_address_1 = forms.CharField(max_length=200, label='Street address 1',
                                     widget=forms.TextInput(attrs={'autocomplete': 'address-line1'}))
    street_address_2 = forms.CharField(max_length=200, required=False, label='Street address 2 (optional)',
                                     widget=forms.TextInput(attrs={'autocomplete': 'address-line2'}))
    postal_code = forms.CharField(max_length=20, label='Pincode / postal code',
                                 widget=forms.TextInput(attrs={'autocomplete': 'postal-code'}))

    def __init__(self, *args, tenant, **kwargs):
        self.tenant = tenant
        kwargs['initial'] = {name: getattr(tenant, name) for name in self.base_fields}
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned = super().clean()
        if self.errors or not self.has_changed():
            return cleaned
        try:
            city = site_location.get_city(cleaned['city_place_id'])
            if any(cleaned[field] != city[field] for field in ('city', 'state', 'country')):
                self.add_error('city', 'Select a city from the suggestions to validate city, state and country.')
                return cleaned
            if not site_location.valid_postal_code(cleaned['postal_code'], city):
                self.add_error('postal_code', 'Enter a valid postal code for the city.')
                return cleaned
        except site_location.InvalidCity as exc:
            self.add_error('city', str(exc))
            return cleaned
        except site_location.PostalCodeUnverified as exc:
            self.add_error('postal_code', str(exc))
            return cleaned
        except site_location.LocationUnavailable as exc:
            self.add_error(None, str(exc))
            return cleaned
        cleaned['country_code'] = city['country_code']
        cleaned['address'] = ', '.join(cleaned[k] for k in (
            'street_address_1', 'street_address_2', 'city', 'state', 'country', 'postal_code') if cleaned[k])
        if len(cleaned['address']) > TenantInfo.ADDRESS_MAX_LENGTH:
            self.add_error(None, 'The complete address must be 500 characters or fewer.')
        return cleaned

    def save(self):
        if not self.has_changed():
            return
        for name, value in self.cleaned_data.items():
            setattr(self.tenant, name, value)
        self.tenant.save(update_fields=list(self.cleaned_data))


class WhatsAppContactForm(forms.Form):
    whatsapp_number = forms.CharField(max_length=20, required=False, label='WhatsApp number',
                                     help_text='Saves your business contact number. WhatsApp chatbot connection is coming soon.',
                                     widget=forms.TextInput(attrs={'type': 'tel', 'placeholder': 'with country code'}))

    def __init__(self, *args, tenant, **kwargs):
        self.tenant = tenant
        kwargs['initial'] = {'whatsapp_number': tenant.whatsapp_number}
        super().__init__(*args, **kwargs)

    def save(self):
        if self.has_changed():
            self.tenant.whatsapp_number = self.cleaned_data['whatsapp_number']
            self.tenant.save(update_fields=['whatsapp_number'])
