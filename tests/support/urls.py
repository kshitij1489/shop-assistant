"""Application routes used by the isolated integration profile."""
from django.contrib import admin
from django.urls import include, path

from chatbot_core.channels.voice_assistant import voice_api
from chatbot_core.views import chat_page_view
from studio_desk.health import health

urlpatterns = [
    path("health", health, name="health"),
    path("chat-page/", chat_page_view, name="chat_page"),
    path("accounts/", include("users.urls")),
    path("commerce/", include("commerce.urls")),
    path("agent_core/", include("chatbot_core.urls")),
    path("voice/", voice_api),
    path("admin/", admin.site.urls),
]
