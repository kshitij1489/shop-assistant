from django.urls import path
from chatbot_core.channels import telegram_webhook, whatsapp, website, voice_assistant

app_name = "chatbot_core"

urlpatterns = [
    path('chatbot-api/', website.chatbot_api, name='chatbot_api'),
    path('telegram-webhook/', telegram_webhook.telegram_webhook, name='telegram_webhook'),
    path('whatsapp-webhook/', whatsapp.whatsapp_webhook, name='whatsapp_webhook'),
    path('token/', website.public_jwt_token, name='public_jwt_token'),

    path('voice/', voice_assistant.voice_api, name='voice_api'),

]
