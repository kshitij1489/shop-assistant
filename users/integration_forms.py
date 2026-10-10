from django import forms

from chatbot_core.models import TenantInfo


class TelegramSettingsForm(forms.Form):
    telegram_bot_token = forms.CharField(required=False, max_length=200, label='Telegram Bot Token',
        help_text='Paste your token from BotFather. Saving registers the Telegram webhook.',
        widget=forms.TextInput(attrs={'id': 'telegram_bot_token', 'placeholder': 'e.g. 123456:ABC-DEF...',
                                     'aria-describedby': 'telegram_bot_token_help'}))

    def __init__(self, *args, tenant, **kwargs):
        self.tenant = tenant
        super().__init__(*args, initial={'telegram_bot_token': tenant.telegram_bot_token}, **kwargs)
        if tenant.telegram_bot_token:
            self.fields['telegram_bot_token'].widget.attrs['readonly'] = True
            self.fields['telegram_bot_token'].help_text = (
                'Saving registers the webhook again. Changing or disconnecting this bot is coming soon.')

    def clean_telegram_bot_token(self):
        token = self.cleaned_data['telegram_bot_token']
        if self.tenant.telegram_bot_token and token != self.tenant.telegram_bot_token:
            raise forms.ValidationError(
                'Changing or disconnecting Telegram is coming soon. Keep the current bot token to register its webhook again.')
        if token and TenantInfo.objects.filter(telegram_bot_token=token).exclude(pk=self.tenant.pk).exists():
            raise forms.ValidationError('This Telegram bot is already connected to another business.')
        return token or None
