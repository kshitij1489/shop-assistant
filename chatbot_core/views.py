"""Public-facing chatbot views (embed page, etc.)."""
from __future__ import annotations

from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_GET

from chatbot_core.models import TenantInfo


def extract_subdomain(host: str) -> str | None:
    """Extract subdomain from host string like 'tenant.example.com'."""
    parts = host.split(":")[0].split(".")
    if len(parts) >= 3:
        return parts[0]
    return None


def resolve_tenant_from_request(request) -> str | None:
    return (
        request.META.get("HTTP_X_TENANT")
        or request.GET.get("tenant")
        or extract_subdomain(request.get_host())
    )


@require_GET
def chat_page_view(request):
    """Public embeddable chat UI for a tenant (subdomain, ?tenant=, or X-Tenant header)."""
    tenant_slug = resolve_tenant_from_request(request)
    tenant = TenantInfo.objects.filter(slug=tenant_slug, is_active=True).first()
    if not tenant:
        return HttpResponse("Invalid tenant", status=400)

    context = {
        "tenant_slug": tenant.slug,
        "tenant_api_key": tenant.api_key,
        "token_url": reverse("chatbot_core:public_jwt_token"),
        "chatbot_endpoint": reverse("chatbot_core:chatbot_api"),
    }
    return render(request, "chatbot_core/ai_agent.html", context)
