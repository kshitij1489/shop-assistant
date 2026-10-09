import uuid

from django import forms
from django.contrib.auth.forms import UsernameField
from django.contrib.auth.models import User
from django.db import transaction
from chatbot_core.models import TenantInfo, normalize_tenant_slug

class SignUpForm(forms.ModelForm):
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={'placeholder': 'Create a secure password'}),
        label="Password"
    )
    password2 = forms.CharField(
        widget=forms.PasswordInput(attrs={'placeholder': 'Confirm your password'}),
        label="Confirm Password"
    )
    business_type = forms.ChoiceField(choices=TenantInfo.BUSINESS_TYPES)
    business_name = forms.CharField(
        max_length=100,
        widget=forms.TextInput(attrs={'placeholder': 'Your business name'}),
        label="Business Name"
    )
    address = forms.CharField(
        required=False,
        max_length=TenantInfo.ADDRESS_MAX_LENGTH,
        label="Address",
        help_text="Optional.",
        widget=forms.Textarea(attrs={
            'rows': 3,
            'placeholder': 'Street, city, postal code',
        }),
    )
    slug = forms.CharField(
        required=False,
        max_length=100,
        label="Public link",
        help_text=(
            "Letters, numbers, and hyphens. Leave blank to create one from the business name. "
            "If that link is taken, a number is added. This link stays the same if the business is renamed."
        ),
        widget=forms.TextInput(attrs={'placeholder': 'Leave blank to use the business name'}),
    )
    username = forms.CharField(
        widget=forms.TextInput(attrs={'placeholder': 'Choose a username'}),
        label="Username"
    )
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={'placeholder': 'you@example.com'}),
        label="Email"
    )

    class Meta:
        model = User
        fields = ['username', 'email', 'password', 'password2', 'business_type', 'business_name', 'address']

    def clean_business_name(self):
        business_name = self.cleaned_data['business_name']
        if TenantInfo.objects.filter(display_name__iexact=business_name).exists():
            raise forms.ValidationError("A business with this name already exists.")
        return business_name

    def clean_slug(self):
        slug = normalize_tenant_slug(self.cleaned_data.get('slug') or '')
        if self.cleaned_data.get('slug') and not slug:
            raise forms.ValidationError("Use letters or numbers in the public link.")
        if slug and TenantInfo.objects.filter(slug=slug).exists():
            raise forms.ValidationError("This public link is already in use.")
        return slug

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        password2 = cleaned_data.get("password2")

        if password and password2 and password != password2:
            self.add_error('password2', "Passwords do not match.")
        name = cleaned_data.get("business_name")
        if name and not cleaned_data.get("slug") and not normalize_tenant_slug(name):
            self.add_error("business_name", "Enter a business name that can be used in a web address.")
        return cleaned_data


class CreateTenantForm(SignUpForm):
    """Master-created tenants require a new owner account, just like signup."""
    whatsapp_number = forms.CharField(max_length=20, required=False, label="WhatsApp number")
    telegram_bot_token = forms.CharField(
        max_length=200, required=False, label="Telegram Bot Token",
        widget=forms.PasswordInput(attrs={'placeholder': 'e.g. 123456:ABC-DEF...'}),
    )
    field_order = ['username', 'email', 'password', 'password2', 'business_name', 'slug', 'business_type',
                   'address', 'whatsapp_number', 'telegram_bot_token']

    def clean_telegram_bot_token(self):
        token = self.cleaned_data['telegram_bot_token']
        if token and TenantInfo.objects.filter(telegram_bot_token=token).exists():
            raise forms.ValidationError("This Telegram bot token is already in use.")
        return token


class TenantProfileForm(forms.Form):
    """Editable business name. The public slug is intentionally absent."""

    display_name = forms.CharField(max_length=100, label="Business name")

    def __init__(self, *args, tenant: TenantInfo, **kwargs):
        self.tenant = tenant
        super().__init__(*args, **kwargs)

    def clean_display_name(self):
        display_name = self.cleaned_data['display_name']
        if TenantInfo.objects.filter(display_name__iexact=display_name).exclude(pk=self.tenant.pk).exists():
            raise forms.ValidationError("A business with this name already exists.")
        return display_name


class TenantDirectoryForm(forms.Form):
    """Business name, existing login usernames, and address for one tenant.

    The public slug is not a field. Usernames are limited to accounts already
    linked to this tenant.
    """

    display_name = forms.CharField(
        max_length=100,
        label="Business name",
        widget=forms.TextInput(attrs={'autocomplete': 'off'}),
    )
    address = forms.CharField(
        required=False,
        max_length=TenantInfo.ADDRESS_MAX_LENGTH,
        label="Address",
        widget=forms.Textarea(attrs={'rows': 3, 'autocomplete': 'off'}),
        error_messages={'max_length': 'Address must be 500 characters or fewer.'},
    )

    def __init__(self, *args, tenant: TenantInfo, **kwargs):
        self.tenant = tenant
        self.profiles = sorted(tenant.users.all(), key=lambda profile: profile.pk)
        kwargs['prefix'] = f'tenant-{tenant.pk}'
        kwargs.setdefault('initial', {})
        kwargs['initial'].setdefault('display_name', tenant.display_name)
        kwargs['initial'].setdefault('address', tenant.address)
        super().__init__(*args, **kwargs)
        username_field = User._meta.get_field('username')
        for profile in self.profiles:
            self.fields[f'username_{profile.pk}'] = UsernameField(
                max_length=username_field.max_length,
                label="Username",
                validators=list(username_field.validators),
                initial=profile.user.username,
                widget=forms.TextInput(attrs={'autocomplete': 'off'}),
            )
        self.order_fields(
            ['display_name', *[f'username_{profile.pk}' for profile in self.profiles], 'address']
        )

    def username_fields(self) -> list:
        return [self[f'username_{profile.pk}'] for profile in self.profiles]

    def saved_values(self) -> dict[str, str]:
        """Values currently stored, keyed by the form field names."""
        values = {
            self.add_prefix('display_name'): self.tenant.display_name,
            self.add_prefix('address'): self.tenant.address,
        }
        for profile in self.profiles:
            values[self.add_prefix(f'username_{profile.pk}')] = profile.user.username
        return values

    def clean_display_name(self):
        display_name = self.cleaned_data['display_name']
        if TenantInfo.objects.filter(display_name__iexact=display_name).exclude(pk=self.tenant.pk).exists():
            raise forms.ValidationError("A business with this name already exists.")
        return display_name

    def clean(self):
        cleaned = super().clean()
        proposed = self._proposed_usernames(cleaned)
        counts: dict[str, int] = {}
        for username in proposed.values():
            counts[username] = counts.get(username, 0) + 1
        taken = self._usernames_taken_by_others(proposed)
        for profile in self.profiles:
            username = proposed.get(profile.user_id)
            if username and (counts[username] > 1 or username in taken):
                self.add_error(f'username_{profile.pk}', "A user with that username already exists.")
        return cleaned

    def _proposed_usernames(self, cleaned: dict) -> dict[int, str]:
        proposed = {}
        for profile in self.profiles:
            username = cleaned.get(f'username_{profile.pk}')
            if username:
                proposed[profile.user_id] = username
        return proposed

    def _usernames_taken_by_others(self, proposed: dict[int, str]) -> set[str]:
        if not proposed:
            return set()
        return set(
            User.objects.filter(username__in=list(proposed.values()))
            .exclude(pk__in=list(proposed.keys()))
            .values_list('username', flat=True)
        )

    def save(self) -> list[str]:
        """Store edited fields. Returns the names of the fields that changed."""
        with transaction.atomic():
            changed = self._apply_tenant_fields()
            if self._apply_usernames():
                changed.append('username')
        return changed

    def _apply_tenant_fields(self) -> list[str]:
        changed = []
        display_name = self.cleaned_data['display_name']
        address = self.cleaned_data['address']
        if display_name != self.tenant.display_name:
            self.tenant.display_name = display_name
            changed.append('display_name')
        if address != self.tenant.address:
            self.tenant.address = address
            changed.append('address')
            # A free-text edit in the master directory supersedes the previously
            # validated components; do not show those as the new address.
            for field in ('street_address_1', 'street_address_2', 'city', 'state',
                          'country', 'country_code', 'postal_code', 'city_place_id'):
                if getattr(self.tenant, field):
                    setattr(self.tenant, field, '')
                    changed.append(field)
        if changed:
            self.tenant.save(update_fields=changed)
        return changed

    def _apply_usernames(self) -> bool:
        pending = [
            profile for profile in self.profiles
            if self.cleaned_data[f'username_{profile.pk}'] != profile.user.username
        ]
        if not pending:
            return False
        finals = {
            profile.user_id: self.cleaned_data[f'username_{profile.pk}']
            for profile in pending
        }
        current = {profile.user_id: profile.user.username for profile in pending}
        if set(finals.values()) & set(current.values()):
            self._park_usernames(pending)
        for profile in pending:
            profile.user.username = finals[profile.user_id]
            profile.user.save(update_fields=['username'])
        return True

    def _park_usernames(self, pending: list) -> None:
        """Move logins aside so two accounts can exchange usernames."""
        token = uuid.uuid4().hex
        for profile in pending:
            profile.user.username = f'_{profile.user_id}_{token}'[:150]
            profile.user.save(update_fields=['username'])


from orders.models import MenuCategory, MenuItemVariant


class MenuCategoryForm(forms.ModelForm):
    class Meta:
        model = MenuCategory
        fields = ["name", "sort_order", "is_active"]

    def clean_name(self):
        name = self.cleaned_data["name"]
        if MenuCategory.objects.filter(tenant=self.instance.tenant, name__iexact=name).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("A category with this name already exists.")
        return name


class MenuVariantForm(forms.ModelForm):
    aliases = forms.CharField(
        required=False,
        label="Aliases (comma separated)",
        widget=forms.TextInput(attrs={"placeholder": "e.g. Grande, 12 oz"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["price"].widget.attrs["min"] = "0"
        if self.instance.pk:
            self.initial["aliases"] = ", ".join(self.instance.aliases or [])

    def clean_aliases(self):
        return list(dict.fromkeys(value.strip() for value in self.cleaned_data["aliases"].split(",") if value.strip()))

    class Meta:
        model = MenuItemVariant
        fields = ["is_available", "size", "sort_order", "price", "volume_ml", "weight_grams", "aliases", "description"]
        labels = {
            "is_available": "Available",
            "size": "Variant name",
            "sort_order": "Display order",
            "volume_ml": "Volume (ml)",
            "weight_grams": "Weight (g)",
        }
        widgets = {
            "size": forms.TextInput(attrs={"placeholder": "e.g. Small or Half"}),
            "description": forms.TextInput(),
        }

    def clean_size(self):
        size = self.cleaned_data["size"]
        available = self.data.get('is_available') not in (None, '', False)
        if available and MenuItemVariant.objects.filter(menu_item=self.instance.menu_item, size__iexact=size, is_available=True).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("A variant with this name already exists for this item.")
        return size

    def clean_price(self):
        price = self.cleaned_data["price"]
        if price < 0:
            raise forms.ValidationError("Price cannot be negative.")
        return price
