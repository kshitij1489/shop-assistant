# chatbot_core/middleware.py

from django.core.cache import cache
from django.http import JsonResponse
import time
import jwt
from django.conf import settings

class ChatbotRateLimitMiddleware:
    RATE_LIMIT = 30  # requests
    TIME_WINDOW = 60  # seconds

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith("/chatbot-api/") and request.method == "POST":
            tenant_slug = None
            auth_header = request.META.get("HTTP_AUTHORIZATION", "")
            if auth_header.startswith("Bearer "):
                try:
                    payload = jwt.decode(auth_header.split(" ")[1], settings.JWT_SECRET, algorithms=["HS256"])
                    tenant_slug = payload.get("tenant_slug")
                except jwt.InvalidTokenError:
                    return JsonResponse({"error": "Invalid token"}, status=401)

            key = f"chatbot_rl:{tenant_slug or request.META.get('REMOTE_ADDR')}"
            history = cache.get(key, [])
            now_ts = int(time.time())

            # Filter old timestamps
            history = [ts for ts in history if now_ts - ts < self.TIME_WINDOW]

            if len(history) >= self.RATE_LIMIT:
                return JsonResponse({"error": "Rate limit exceeded"}, status=429)

            history.append(now_ts)
            cache.set(key, history, timeout=self.TIME_WINDOW)

        return self.get_response(request)
