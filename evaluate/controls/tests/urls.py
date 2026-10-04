from django.urls import path
from chatbot_core.channels.website import chatbot_api

urlpatterns = [path('agent_core/chatbot-api/', chatbot_api)]
