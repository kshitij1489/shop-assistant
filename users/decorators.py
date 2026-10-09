from functools import wraps
from django.shortcuts import redirect
from django.contrib import messages
from django.http import JsonResponse

def _wants_json(request):
    accept = (request.headers.get('Accept') or '')
    xrw = (request.headers.get('X-Requested-With') or '')
    return 'application/json' in accept or xrw.lower() == 'xmlhttprequest'

def master_required(view_func):
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        profile = getattr(request.user, 'tenantprofile', None)
        is_master = request.user.is_superuser or (profile and profile.is_master)  # ← changed
        if not request.user.is_authenticated or not is_master:
            messages.error(request, "Master Access Required")
            return redirect("dashboard")
        return view_func(request, *args, **kwargs)
    return _wrapped_view

def tenant_required(view_func):
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        profile = getattr(request.user, 'tenantprofile', None)
        if not request.user.is_authenticated or not profile or profile.is_master or not profile.tenant:
            messages.error(request, "Tenant Access Required")
            return redirect("dashboard")

        if not profile.tenant.is_active:
            if _wants_json(request):
                return JsonResponse({'detail': 'This tenant is inactive.'}, status=403)
            messages.error(request, "Tenant Inactive: Contact Support")
            return redirect("pending_review")

        # Approval gate (masters already excluded above)
        status = getattr(profile.tenant, 'approval_status', 'APPROVED')
        if status != 'APPROVED':
            if _wants_json(request):
                return JsonResponse({'detail': 'Your tenant is not approved yet.', 'status': status}, status=403)
            messages.info(request, "Application Under Review")
            return redirect("pending_review")

        return view_func(request, *args, **kwargs)
    return _wrapped_view
