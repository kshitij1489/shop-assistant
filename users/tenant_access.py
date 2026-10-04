"""Resolve tenant authority from the authenticated account, never request data."""
from django.core.exceptions import PermissionDenied


def authenticated_tenant(request):
    user = getattr(request, 'user', None)
    if not user or not user.is_authenticated or not user.is_active:
        raise PermissionDenied('Authentication required.')
    profile = getattr(user, 'tenantprofile', None)
    tenant = profile.tenant if profile and not profile.is_master else None
    if user.is_superuser or (profile and profile.is_master):
        from chatbot_core.models import TenantInfo
        tenant = TenantInfo.objects.filter(pk=request.session.get('impersonated_tenant_id')).first()
    if not tenant or not tenant.is_active or tenant.approval_status != 'APPROVED':
        raise PermissionDenied('An active, approved tenant is required.')
    return tenant
