from .menu_source import local_menu_required, menu_context
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login
from django.contrib.auth.views import LogoutView as DjangoLogoutView, LoginView
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST, require_http_methods, require_GET
from django.utils.decorators import method_decorator
from django.http import HttpRequest, HttpResponseBadRequest, HttpResponseForbidden, HttpResponseRedirect, JsonResponse
from chatbot_core.channels.utils import generate_tenant_jwt
import os, json
from datetime import datetime, timezone as dt_timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from orders.models import Customer, Order, MenuItem, MenuItemVariant, MenuCatalogMeta, MenuCategory, ChatSession
from orders.models import ChatSession, PlatformWebhookLog
from django.db.models import Count, Avg, Max, Avg, F
from django.utils.timezone import now
from datetime import timedelta
from .forms import (
    SignUpForm, CreateTenantForm, TenantProfileForm, TenantDirectoryForm,
    MenuCategoryForm, MenuVariantForm,
)
from .models import TenantProfile
from .location_forms import SiteLocationForm, WhatsAppContactForm
from chatbot_core.models import TenantInfo
from .decorators import master_required, tenant_required
from chatbot_core.models import TenantJSONDoc, TenantRuntimeConfiguration
from chatbot_core.capabilities import CAPABILITIES
from chatbot_core.runtime_configuration import publish_configuration
from users.runtime_forms import CapabilityTopicForm
from django_ratelimit.decorators import ratelimit
import requests
from django.db import transaction, IntegrityError
from django.core.exceptions import ValidationError
import datetime
from datetime import timezone
from .utils import ExtractEpoch, publish_menu as _publish_menu
from orders.tasks import task_sync_tenant_from_folder
from chatbot_core.active_chats import list_active_chats, set_agent_enabled, get_messages, append_message, is_global_agent_enabled, set_global_agent_enabled, get_latest_meta
from chatbot_core.channels.registry import get_adapter
from chatbot_core.knowledge_cache import initialize_caches
from django.core.mail import send_mail
from django.urls import reverse
from types import SimpleNamespace
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone
from django.db.models import Avg, Count, Max
import datetime
from users.analytics.db_utils import execute_db_query
from users.analytics.prompt_builder import create_db_query
from django.db import connection

import logging

logger = logging.getLogger(__name__)

# -------------------------------
# 🔧 Helpers
# -------------------------------
def get_current_tenant(request):
    profile = getattr(request.user, 'tenantprofile', None)
    if request.user.is_superuser or (profile and profile.is_master):  # ← changed
        tenant_id = request.session.get('impersonated_tenant_id')
        if tenant_id:
            return TenantInfo.objects.filter(id=tenant_id).first()
    return profile.tenant if profile else None  # ← safe if no profile

# -------------------------------
# 🔐 Authentication Views
# -------------------------------
def _register_tenant_account(request, form, *, created_by_master=False):
    """Create an owner account and its tenant together, without changing the session."""
    with transaction.atomic():
        user = form.save(commit=False)
        user.set_password(form.cleaned_data['password'])
        user.save()

        business_name = form.cleaned_data['business_name']
        business_type = form.cleaned_data['business_type']
        tenant_fields = {
            'display_name': business_name,
            'business_type': business_type,
            'address': form.cleaned_data.get('address', ''),
            'whatsapp_number': form.cleaned_data.get('whatsapp_number', ''),
            'telegram_bot_token': form.cleaned_data.get('telegram_bot_token') or None,
        }
        if form.cleaned_data.get('slug'):
            tenant_fields['slug'] = form.cleaned_data['slug']
        tenant = TenantInfo.objects.create(**tenant_fields)

        TenantProfile.objects.create(user=user, tenant=tenant, is_master=False)
        if business_type == 'cafe':
            from chatbot_core.runtime_configuration import publish_default_configuration
            publish_default_configuration(tenant.pk)

        # Schedule the sync AFTER commit to avoid race conditions.
        if getattr(settings, 'LEGACY_TENANT_SYNC_ENABLED', False):
            transaction.on_commit(lambda: task_sync_tenant_from_folder.delay(tenant.id))

        # Notify the master only after the account and tenant have committed.
        def _notify_master():
            if not settings.SIGNUP_ALERT_EMAIL:
                return
            alert_type = "Master-created tenant" if created_by_master else "New signup"
            subject = f"{alert_type}: {business_name} ({user.username})"
            introduction = (
                f"A master created this tenant (user ID: {request.user.pk})."
                if created_by_master else "A new user signed up."
            )
            link = request.build_absolute_uri(reverse('master_dashboard'))
            msg = (
                f"{introduction}\n\n"
                f"User: {user.username} <{user.email}>\n"
                f"Business: {business_name}\n"
                f"Type: {business_type}\n"
                f"Address: {tenant.address or 'not provided'}\n"
                f"Tenant slug: {tenant.slug}\n"
                f"Status: {tenant.approval_status}\n\n"
                f"Review/approve here: {link}\n"
            )
            try:
                # In dev: raise errors so you notice; in prod: don’t break signup
                fs = not settings.DEBUG
                sent = send_mail(
                    subject=subject,
                    message=msg,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[settings.SIGNUP_ALERT_EMAIL],
                    fail_silently=fs,
                )
                # If fail_silently=True, a failure returns 0 (no exception), so log it:
                if sent != 1:
                    logger.error(
                        "Signup alert email NOT sent (sent=%s).",
                        sent,
                        extra={"username": user.username, "tenant_slug": tenant.slug},
                    )
            except Exception:
                logger.exception(
                    "Signup alert email failed with exception.",
                    extra={"username": user.username, "tenant_slug": tenant.slug},
                )
        transaction.on_commit(_notify_master)
    return user


@csrf_protect
def signup_view(request):
    if request.method == 'POST':
        form = SignUpForm(request.POST)
        if form.is_valid():
            user = _register_tenant_account(request, form)
            login(request, user)
            messages.success(
                request,
                'Your account has been created successfully. '
                'Your café is awaiting administrator approval.',
            )
            return redirect('dashboard')
    else:
        form = SignUpForm()
    return render(request, 'users/signup.html', {'form': form})


class SignInView(LoginView):
    template_name = 'users/login.html'


@method_decorator(csrf_protect, name='dispatch')
@method_decorator(require_POST, name='dispatch')
class LogoutView(DjangoLogoutView):
    def dispatch(self, request, *args, **kwargs):
        request.session.pop('impersonated_tenant_id', None)
        messages.success(request, "You have been securely logged out.")
        return super().dispatch(request, *args, **kwargs)

# -------------------------------
# 🔁 Dashboard Redirect & Views
# -------------------------------
@login_required
def dashboard_redirect_view(request):
    profile = getattr(request.user, 'tenantprofile', None)  # ← new
    if request.user.is_superuser or (profile and profile.is_master):  # ← changed
        return redirect('master_dashboard')
    if profile and profile.tenant:  # ← guard
        if getattr(profile.tenant, 'approval_status', 'APPROVED') != 'APPROVED':
            return redirect('pending_review')
        return redirect('tenant:tenant_dashboard')
    messages.error(request, "Your account is not linked to a tenant yet.")
    return redirect('login')


@login_required
@tenant_required
def tenant_dashboard_view(request):
    return render(request, "users/dashboard_tenant.html", {
        "is_master": False,
        "tenant": request.user.tenantprofile.tenant,
    })

# -------------------------------
# 🧑‍💼 Master Actions
# -------------------------------
@login_required
@require_POST
@csrf_protect
def impersonate_tenant_view(request, tenant_id):
    profile = getattr(request.user, 'tenantprofile', None)
    if not (request.user.is_superuser or (profile and profile.is_master)):
        return HttpResponseForbidden("Unauthorized")

    try:
        tenant = TenantInfo.objects.get(id=tenant_id)
        request.session['impersonated_tenant_id'] = tenant.id
        messages.success(request, f"Now impersonating {tenant.display_name}")
    except TenantInfo.DoesNotExist:
        return HttpResponseBadRequest("Invalid tenant ID")

    return redirect('master_dashboard')


@login_required
@require_POST
@csrf_protect
def stop_impersonation_view(request):
    request.session.pop('impersonated_tenant_id', None)
    messages.info(request, "Stopped impersonating.")
    return redirect('master_dashboard')


def _requested_edit_form(request: HttpRequest):
    raw_tenant_id = request.GET.get('edit', '')
    if not raw_tenant_id.isdigit():
        return None
    try:
        tenant_id = int(raw_tenant_id)
    except ValueError:
        return None
    tenant = TenantInfo.objects.filter(pk=tenant_id).prefetch_related('users__user').first()
    if tenant is None:
        return None
    return TenantDirectoryForm(tenant=tenant)


def _render_master_tenants(request, form, edit_form=None, status=200):
    return render(request, 'users/master_tenants.html', {
        'form': form,
        'edit_form': edit_form,
        'pending_tenants': TenantInfo.objects.filter(approval_status=TenantInfo.ApprovalStatus.PENDING),
        'tenants': TenantInfo.objects.order_by('-created_at', '-pk').prefetch_related('users__user'),
    }, status=status)


@login_required
@master_required
@require_GET
def master_tenants_view(request):
    return _render_master_tenants(request, CreateTenantForm(), edit_form=_requested_edit_form(request))


@require_POST
@login_required
@master_required
@csrf_protect
def create_tenant_view(request):
    form = CreateTenantForm(request.POST)
    if form.is_valid():
        _register_tenant_account(request, form, created_by_master=True)
        messages.success(request, 'Tenant and login account created successfully. Awaiting administrator approval.')
        return redirect('master_tenants')
    return _render_master_tenants(request, form)


@require_POST
@login_required
@master_required
@csrf_protect
def set_tenant_active_view(request, tenant_id):
    tenant = get_object_or_404(TenantInfo, pk=tenant_id)
    active = request.POST.get('is_active')
    if active not in ('true', 'false'):
        return HttpResponseBadRequest('An explicit activation state is required.')
    tenant.is_active = active == 'true'
    tenant.save(update_fields=['is_active'])
    logger.info(
        "Tenant activation changed: user_id=%s tenant_id=%s is_active=%s",
        request.user.pk, tenant.pk, tenant.is_active,
    )
    action = 'Activated' if tenant.is_active else 'Deactivated'
    messages.success(request, f'{action} {tenant.display_name}.')
    return redirect('master_tenants')


@require_POST
@login_required
@master_required
@csrf_protect
def update_tenant_details_view(request: HttpRequest, tenant_id: int):
    """Save the business name, login usernames, and address for one tenant.

    The public slug stays as it was when the tenant was created.
    """
    tenant = get_object_or_404(
        TenantInfo.objects.prefetch_related('users__user'),
        pk=tenant_id,
    )
    form = TenantDirectoryForm(request.POST, tenant=tenant)
    if form.is_valid():
        saved = _save_tenant_directory(request, tenant, form)
        if saved:
            return redirect('master_tenants')
    messages.error(request, f'Changes for {tenant.display_name} were not saved.')
    return _render_master_tenants(request, CreateTenantForm(), edit_form=form, status=400)


def _save_tenant_directory(request: HttpRequest, tenant: TenantInfo, form: TenantDirectoryForm) -> bool:
    try:
        changed = form.save()
    except IntegrityError:
        logger.warning(
            "Tenant directory update conflict: user_id=%s tenant_id=%s",
            request.user.pk, tenant.pk,
        )
        tenant.refresh_from_db()
        for profile in form.profiles:
            profile.user.refresh_from_db(fields=['username'])
        form.add_error(None, 'A user with that username already exists.')
        return False
    logger.info(
        "Tenant directory updated: user_id=%s tenant_id=%s changed=%s",
        request.user.pk, tenant.pk, ','.join(changed) or 'none',
    )
    messages.success(request, f'Updated {form.cleaned_data["display_name"]}.')
    return True


@require_http_methods(["POST"])
@login_required
def delete_tenant_view(request, tenant_id):
    if not (request.user.is_superuser or (getattr(request.user, 'tenantprofile', None) and request.user.tenantprofile.is_master)):
        return HttpResponseForbidden()
    try:
        TenantInfo.objects.get(id=tenant_id).delete()
        messages.success(request, "Tenant deleted.")
    except TenantInfo.DoesNotExist:
        messages.error(request, "Tenant not found.")
    return redirect('master_dashboard')

# -------------------------------
# ⚙️ Tenant Settings
# -------------------------------
SETTINGS_TABS = ('checkout', 'hours', 'contact', 'integrations')
CHECKOUT_SETTINGS_TABS = ('checkout', 'hours')


def _requested_settings_tab(request: HttpRequest) -> str:
    tab = request.GET.get('tab', '')
    if tab in SETTINGS_TABS:
        return tab
    return 'checkout'


def _posted_checkout_tab(request: HttpRequest) -> str:
    tab = request.POST.get('settings_tab', '')
    if tab in CHECKOUT_SETTINGS_TABS:
        return tab
    return 'checkout'


def _redirect_settings(tab: str) -> HttpResponseRedirect:
    if tab not in SETTINGS_TABS:
        tab = 'checkout'
    return redirect(f"{reverse('tenant:tenant_settings')}?tab={tab}")


@login_required
@tenant_required
def tenant_settings_view(request):
    tenant = request.user.tenantprofile.tenant

    from orders.models import CheckoutSettings
    from .checkout_forms import CheckoutSettingsForm
    checkout_settings = CheckoutSettings.objects.filter(tenant=tenant).first()
    checkout_form = CheckoutSettingsForm(
        request.POST if request.method == 'POST' and request.POST.get('section') == 'checkout' else None,
        configuration=checkout_settings.configuration if checkout_settings else None, tenant=tenant,
    )
    contact_post = request.method == 'POST' and (
        request.POST.get('section') == 'contact'
        or not request.POST.get('section') and 'address' in request.POST
    )
    location_form = SiteLocationForm(request.POST if contact_post else None, tenant=tenant)
    whatsapp_post = request.method == 'POST' and request.POST.get('section') == 'whatsapp'
    whatsapp_form = WhatsAppContactForm(request.POST if whatsapp_post else None, tenant=tenant)
    profile_form = TenantProfileForm(
        request.POST if request.method == 'POST' and request.POST.get('section') == 'profile' else None,
        tenant=tenant,
        initial={'display_name': tenant.display_name},
    )
    settings_context = {
        'tenant': tenant,
        'checkout_form': checkout_form,
        'profile_form': profile_form,
        'location_form': location_form,
        'whatsapp_form': whatsapp_form,
        'active_tab': _requested_settings_tab(request),
    }
    if whatsapp_post:
        if whatsapp_form.is_valid():
            whatsapp_form.save()
            messages.success(request, 'WhatsApp number updated.')
            return _redirect_settings('contact')
        settings_context['active_tab'] = 'contact'
        return render(request, 'users/tenant_settings.html', settings_context, status=400)
    if contact_post:
        if location_form.is_valid():
            location_form.save()
            messages.success(request, 'Store address updated.')
            return _redirect_settings('contact')
        settings_context['active_tab'] = 'contact'
        return render(request, 'users/tenant_settings.html', settings_context, status=400)
    if request.method == 'POST' and request.POST.get('section') == 'profile':
        if profile_form.is_valid():
            new_name = profile_form.cleaned_data['display_name']
            if new_name != tenant.display_name:
                tenant.display_name = new_name
                tenant.save(update_fields=['display_name'])
                logger.info(
                    "Tenant name updated: user_id=%s tenant_id=%s",
                    request.user.pk, tenant.pk,
                )
            messages.success(request, 'Business name updated.')
            return _redirect_settings('contact')
        settings_context['active_tab'] = 'contact'
        return render(request, 'users/tenant_settings.html', settings_context, status=400)

    if request.method == 'POST' and request.POST.get('section') == 'checkout':
        if checkout_form.is_valid():
            from chatbot_core.configuration_imports import import_checkout
            import_checkout(tenant, checkout_form.configuration)
            messages.success(request, 'Checkout and opening hours updated.')
            return _redirect_settings(_posted_checkout_tab(request))
        errors = checkout_form.error_fields
        settings_context['active_tab'] = errors[0]['tab'] if errors else _posted_checkout_tab(request)
        return render(request, 'users/tenant_settings.html', settings_context, status=400)

    if request.method == 'POST':
        section = request.POST.get('section', '')
        section_fields = {
            'integrations': ('telegram_bot_token',),
            # Accept the combined form from pages opened before the tab split.
            '': ('whatsapp_number', 'telegram_bot_token'),
        }
        if section not in section_fields:
            return HttpResponseBadRequest('Unknown settings section.')
        fields = section_fields[section]
        updates = {field: (request.POST.get(field) or '').strip() for field in fields}

        def _set_telegram_webhook(new_token: str):
            """
            Calls Telegram setWebhook with the new token.
            Mirrors:
              curl -X POST "https://api.telegram.org/bot<token>/setWebhook" \
                   -d "url=https://myip/agent_core/telegram-webhook/?token=<token>"
            """
            if not new_token:
                return

            if not settings.PUBLIC_URL.startswith('https://'):
                messages.error(request, 'Telegram requires an HTTPS PUBLIC_URL. Configure it before registering the bot.')
                return

            endpoint = f"https://api.telegram.org/bot{new_token}/setWebhook"
            payload = {
                "url": f"{settings.PUBLIC_URL}/agent_core/telegram-webhook/?token={new_token}"
            }
            try:
                resp = requests.post(endpoint, data=payload, timeout=10)
                data = {}
                try:
                    data = resp.json()
                except Exception:
                    pass

                if resp.ok and data.get("ok") is True:
                    messages.success(request, "Telegram webhook set successfully.")
                else:
                    # Surface some context to help debug
                    reason = data.get("description") or resp.text
                    messages.error(
                        request,
                        f"Failed to set Telegram webhook: {reason}"
                    )
            except requests.RequestException as e:
                messages.error(request, f"Error calling Telegram API: {e}")

        with transaction.atomic():
            for field, value in updates.items():
                setattr(tenant, field, value)
            tenant.save(update_fields=list(updates))

            # Saving again also re-registers after a PUBLIC_URL change.
            telegram_bot_token = updates.get('telegram_bot_token')
            if telegram_bot_token:
                transaction.on_commit(lambda: _set_telegram_webhook(telegram_bot_token))

        messages.success(request, "Tenant settings updated.")
        return _redirect_settings('integrations' if section == 'integrations' else 'contact')

    return render(request, "users/tenant_settings.html", settings_context)

@login_required
@tenant_required
@require_POST
@csrf_protect
@ratelimit(key='user', rate='5/m', method='POST', block=True)
def generate_jwt_token_view(request):
    tenant = request.user.tenantprofile.tenant
    profile = request.user.tenantprofile

    # Reuse token if not expired (within 10 minutes)
    if profile.last_jwt_token and profile.last_token_generated_at:
        if now() - profile.last_token_generated_at < timedelta(minutes=10):
            logger.info(f"[JWT] Reused token for tenant '{tenant.slug}' by user '{request.user.username}'")
            return JsonResponse({"token": profile.last_jwt_token})

    # Generate new token
    token = generate_tenant_jwt(tenant.slug)

    # Save to profile for reuse
    profile.last_jwt_token = token
    profile.last_token_generated_at = now()
    profile.save()

    logger.info(f"[JWT] Generated new token for tenant '{tenant.slug}' by user '{request.user.username}'")

    return JsonResponse({"token": token})

# -------------------------------
# 📊 Tenant Dashboard Sub-Pages
# -------------------------------
@login_required
@tenant_required
def tenant_analytics_view(request):
    tenant = get_current_tenant(request)

    # Base stats
    sessions = ChatSession.objects.filter(tenant=tenant)
    total_sessions = sessions.count()
    unique_customers = sessions.values('customer').distinct().count()

    # Fix: compute avg of timestamp by extracting epoch seconds first
    avg_ts_seconds = sessions.aggregate(avg_ts=Avg(ExtractEpoch('last_interaction_at')))['avg_ts']
    avg_duration = (
        datetime.datetime.fromtimestamp(float(avg_ts_seconds), tz=dt_timezone.utc) if avg_ts_seconds else None
    )

    # Platform stats from PlatformWebhookLog
    platform_data = (
        PlatformWebhookLog.objects.filter(tenant=tenant)
        .values('source')
        .annotate(count=Count('id'), last_seen=Max('received_at'))
    )
    platform_stats = [
        {"source": entry["source"], "count": entry["count"], "last_seen": entry["last_seen"]}
        for entry in platform_data
    ]

    context = {
        "active_page": "analytics",
        "summary": {
            "total_sessions": total_sessions,
            "unique_customers": unique_customers,
            "avg_duration": avg_duration,
            "positive_feedback": None  # Placeholder
        },
        "platform_stats": platform_stats,
        # default empty values for template safety
        "query": "",
        "db_result": None,
    }

    # If a search query present, call create_db_query and execute_db_query
    q = request.GET.get("q", "").strip()
    if q:
        context["query"] = q
        try:
            # 1) ask your model/utility to produce SQL (create_db_query)
            model_output = create_db_query(user_text=q, tenant_id=str(tenant.id), examples=None)

            # 2) execute the result using your provided executor
            db_result = execute_db_query(
                model_query_output=model_output,
                tenant_id=str(tenant.id),
                enforce_tenant=True,
                mask_sensitive=True,
            )

            # db_result is expected to be:
            # { "sql": ..., "params": [...], "columns": [...], "rows": [ {col: val}, ... ], ... }
            context["db_result"] = db_result

            # optionally add lightweight metadata for debugging in template
            context["db_sql_preview"] = db_result.get("sql")
            context["db_warnings"] = db_result.get("warnings", [])

        except Exception as exc:
            # If execution error, show warning in page (don't leak raw SQL in production)
            context["db_result_error"] = str(exc)

    return render(request, "users/dashboard_analytics.html", context)

@login_required
@tenant_required
def tenant_orders_view(request):
    tenant = get_current_tenant(request)
    orders = Order.objects.filter(tenant=tenant).prefetch_related('items', 'delivery_partner', 'customer').order_by('-created_at')

    return render(request, "users/dashboard_orders.html", {
        "active_page": "orders",
        "orders": orders
    })

@login_required
@tenant_required
def tenant_campaigns_view(request):
    return render(request, "users/dashboard_campaigns.html", {
        "active_page": "campaigns"
    })

@login_required
@tenant_required
def tenant_users_view(request):
    tenant = get_current_tenant(request)
    customers = Customer.objects.filter(tenant=tenant).order_by('-created_at')

    return render(request, "users/dashboard_users.html", {
        "active_page": "users",
        "customers": customers
    })

@login_required
@tenant_required
def tenant_billing_view(request):
    return render(request, "users/dashboard_billing.html", {
        "active_page": "billing"
    })

@login_required
@tenant_required
def tenant_activity_view(request):
    tenant = get_current_tenant(request)
    logs = PlatformWebhookLog.objects.filter(tenant=tenant).order_by('-received_at')[:100]
    sessions = ChatSession.objects.filter(tenant=tenant).select_related('customer').order_by('-last_interaction_at')[:50]

    return render(request, "users/dashboard_activity.html", {
        "active_page": "activity",
        "logs": logs,
        "sessions": sessions
    })

@login_required
@tenant_required
def tenant_chats_view(request):
    tenant = get_current_tenant(request)
    return render(request, 'users/tenant_chats.html', {
        'active_page': 'chats',
        'tenant': tenant,
    })

@login_required
@tenant_required
@require_POST
def tenant_chats_toggle_api(request):
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return HttpResponseBadRequest('invalid json')

    tenant = get_current_tenant(request)
    channel = (data.get('channel') or 'telegram').strip().lower()
    chat_id = str(data.get('chat_id') or '').strip()
    enabled = bool(data.get('enabled', True))

    if not chat_id:
        return HttpResponseBadRequest('chat_id required')
    if channel not in ('telegram',):
        return HttpResponseBadRequest('unsupported channel')

    set_agent_enabled(str(tenant.id), channel, chat_id, enabled)
    return JsonResponse({'ok': True, 'enabled': enabled})

@login_required
@tenant_required
@require_GET
def tenant_chats_messages_api(request):
    tenant = get_current_tenant(request)
    channel = (request.GET.get('channel') or 'telegram').strip().lower()
    chat_id = str(request.GET.get('chat_id') or '').strip()
    if not chat_id:
        return HttpResponseBadRequest('chat_id required')
    if channel not in ('telegram',):
        return HttpResponseBadRequest('unsupported channel')

    msgs = get_messages(str(tenant.id), channel, chat_id, limit=200)
    return JsonResponse({'messages': msgs})

@login_required
@tenant_required
@require_POST
def tenant_chats_send_api(request):
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return HttpResponseBadRequest('invalid json')

    tenant = get_current_tenant(request)
    channel = (data.get('channel') or 'telegram').strip().lower()
    chat_id = str(data.get('chat_id') or '').strip()
    text = (data.get('text') or '').strip()

    if not chat_id or not text:
        return HttpResponseBadRequest('chat_id and text required')
    if channel != 'telegram':
        return HttpResponseBadRequest('only telegram supported in v0')

    bot_token = tenant.telegram_bot_token
    if not bot_token:
        return JsonResponse({'ok': False, 'error': 'Telegram bot token not configured.'}, status=400)

    # Use existing adapter to send
    try:
        adapter = get_adapter('telegram')
        payload = {'chat_id': int(chat_id), 'bot_token': bot_token}
        adapter.send_text(payload, text)
    except Exception as e:
        return JsonResponse({'ok': False, 'error': f'send failed: {e}'}, status=500)

    # Mirror to transcript
    append_message(str(tenant.id), channel, chat_id, direction='owner', text=text)

    return JsonResponse({'ok': True})


@login_required
@tenant_required
@require_GET
def tenant_chats_list_api(request):
    tenant = get_current_tenant(request)
    channel = (request.GET.get('channel') or 'telegram').strip().lower()
    if channel not in ('telegram',):
        return JsonResponse({'chats': [], 'global_agent_enabled': True})

    chats = list_active_chats(str(tenant.id), channel, limit=100)
    return JsonResponse({
        'chats': chats,
        'global_agent_enabled': is_global_agent_enabled(str(tenant.id), channel),  # NEW
    })

@login_required
@tenant_required
@require_GET
def tenant_chats_global_status_api(request):
    tenant = get_current_tenant(request)
    channel = (request.GET.get('channel') or 'telegram').strip().lower()
    enabled = is_global_agent_enabled(str(tenant.id), channel)
    return JsonResponse({'enabled': enabled})

@login_required
@tenant_required
@require_POST
def tenant_chats_toggle_global_api(request):
    data = json.loads(request.body or '{}')
    tenant = get_current_tenant(request)
    channel = (data.get('channel') or 'telegram').strip().lower()

    enabled = bool(data.get('enabled', True))
    set_global_agent_enabled(str(tenant.id), channel, enabled)
    return JsonResponse({'ok': True, 'enabled': enabled})

@login_required
def pending_review_view(request):
    profile = request.user.tenantprofile
    if profile.is_master:
        return redirect('master_dashboard')
    tenant = profile.tenant
    status = getattr(tenant, 'approval_status', 'APPROVED')
    if status == 'APPROVED' and tenant.is_active:
        return redirect('tenant:tenant_dashboard')
    return render(request, "users/pending_review.html", {
        "tenant": tenant,
        "status": status,
    })

# --- Master dashboard: pass pending list (tiny extension) ---
@login_required
@master_required
def master_dashboard_view(request):
    tenants = TenantInfo.objects.all().prefetch_related('users__user')
    impersonating = request.session.get('impersonated_tenant_id')

    tenant = None
    if impersonating:
        tenant = TenantInfo.objects.filter(id=impersonating).first()

    # NEW:
    pending_tenants = TenantInfo.objects.filter(approval_status=TenantInfo.ApprovalStatus.PENDING)

    return render(request, "users/dashboard_master.html", {
        "tenants": tenants,
        "is_master": True,
        "impersonating": bool(impersonating),
        "tenant": tenant,
        "pending_tenants": pending_tenants,  # NEW
    })

# --- NEW: Approve / Reject actions ---
@require_POST
@login_required
@master_required
def approve_tenant_view(request, tenant_id):
    return_to = 'master_tenants' if request.POST.get('return_to') == 'master_tenants' else 'master_dashboard'
    try:
        tenant = TenantInfo.objects.get(id=tenant_id)
    except TenantInfo.DoesNotExist:
        messages.error(request, "Tenant not found.")
        return redirect(return_to)

    tenant.approval_status = TenantInfo.ApprovalStatus.APPROVED
    tenant.reviewed_at = now()
    tenant.reviewed_by = request.user
    note = (request.POST.get("note") or "").strip()
    if note:
        tenant.review_note = note
    tenant.save(update_fields=['approval_status', 'reviewed_at', 'reviewed_by', 'review_note'])
    messages.success(request, f"Approved {tenant.display_name}.")
    return redirect(return_to)

@require_POST
@login_required
@master_required
def reject_tenant_view(request, tenant_id):
    return_to = 'master_tenants' if request.POST.get('return_to') == 'master_tenants' else 'master_dashboard'
    try:
        tenant = TenantInfo.objects.get(id=tenant_id)
    except TenantInfo.DoesNotExist:
        messages.error(request, "Tenant not found.")
        return redirect(return_to)

    tenant.approval_status = TenantInfo.ApprovalStatus.REJECTED
    tenant.reviewed_at = now()
    tenant.reviewed_by = request.user
    tenant.review_note = (request.POST.get("note") or "").strip()
    tenant.save(update_fields=['approval_status', 'reviewed_at', 'reviewed_by', 'review_note'])
    messages.success(request, f"Rejected {tenant.display_name}.")
    return redirect(return_to)

# --- helper: load existing or create in-memory doc (saved on POST) ---
def _load_doc(tenant, dtype, default_payload=None, name=None):
    doc = TenantJSONDoc.objects.filter(tenant=tenant, dtype=dtype).first()
    if doc:
        return doc
    return TenantJSONDoc(
        tenant=tenant,
        dtype=dtype,
        name=name or dtype.title(),
        payload=default_payload or {},
    )

@login_required
@tenant_required
@require_http_methods(["GET", "POST"])
@transaction.atomic
def tenant_knowledge_view(request):
    from .tenant_access import authenticated_tenant
    tenant = authenticated_tenant(request)

    if not tenant:
        if request.method == "GET":
            messages.error(request, "No tenant associated with your account.")
            return render(request, "users/tenant_knowledge.html", {
                "docs": [],
                "docs_payload": [],          # <-- provide for template
                "dtypes": TenantJSONDoc.DocType.choices,
                "selected_dtype": request.GET.get("dtype", ""),
            })
        messages.error(request, "No tenant associated with your account.")
        return redirect("tenant:tenant_knowledge")

    publication = TenantRuntimeConfiguration.objects.filter(tenant=tenant).first()
    topic_form = CapabilityTopicForm()
    if request.method == "POST":
        tenant = TenantInfo.objects.select_for_update().get(pk=tenant.pk)
        action = request.POST.get("action")
        if action == "publish":
            try:
                version = int(request.POST.get("version", ""))
                publication = publish_configuration(tenant.pk, expected_version=version)
                messages.success(request, f"Published configuration version {publication.version}. Ongoing conversations will use it on their next message.")
            except (ValueError, ValidationError) as exc:
                for message in exc.messages if isinstance(exc, ValidationError) else ["Reload the page to obtain a valid configuration version."]:
                    messages.error(request, message)
            return redirect("tenant:tenant_knowledge")
        if action == "save_topic":
            topic_form = CapabilityTopicForm(request.POST)
            if topic_form.is_valid():
                data = topic_form.cleaned_data
                old = TenantJSONDoc.objects.filter(tenant=tenant, dtype="intent_classification",
                    intent=data["intent"], sub_intent=data["sub_intent"]).first()
                options = dict(old.payload) if old and isinstance(old.payload, dict) else {}
                options.update(enabled=data["enabled"], description=data["description"],
                               examples=[line.strip() for line in data["examples"].splitlines() if line.strip()])
                for dtype, payload in (("intent_classification", options), ("response_intents", data["instructions"]),
                                       ("knowledge", data["knowledge"] if data["knowledge"] is not None else {})):
                    TenantJSONDoc.objects.update_or_create(tenant=tenant, dtype=dtype, intent=data["intent"],
                        sub_intent=data["sub_intent"], defaults={"payload": payload})
                messages.success(request, "Topic saved to draft. Publish when your changes are ready.")
                return redirect("tenant:tenant_knowledge")

    if request.method == "GET" or request.POST.get("action") == "save_topic":
        selected_dtype = request.GET.get("dtype") or ""
        # Always load ALL docs for the tenant for client-side switching:
        qs_all = TenantJSONDoc.objects.filter(tenant=tenant).order_by("dtype", "intent", "sub_intent")
        docs_payload = list(qs_all.values("dtype", "intent", "sub_intent", "payload"))
        # (Optional) keep 'docs' if you still render any server-side table (else can pass [])
        return render(request, "users/tenant_knowledge.html", {
            "docs": qs_all,                             # or []
            "docs_payload": docs_payload,               # <-- unfiltered, full dataset
            "dtypes": TenantJSONDoc.DocType.choices,
            "selected_dtype": selected_dtype,
            "publication": publication, "topic_form": topic_form,
            "capabilities": CAPABILITIES.items(),
        })

    # POST
    action = request.POST.get("action")
    dtype = (request.POST.get("dtype") or "").strip()
    intent = (request.POST.get("intent") or "").strip()
    sub_intent = (request.POST.get("sub_intent") or "").strip()
    payload_raw = request.POST.get("payload") or "{}"

    def parse_payload(raw):
        try:
            return json.loads(raw)
        except Exception as e:
            raise ValueError(f"Invalid JSON: {e}")

    try:
        if action == "add":
            data = parse_payload(payload_raw)
            with transaction.atomic():
                _, created = TenantJSONDoc.objects.get_or_create(
                    tenant=tenant, dtype=dtype, intent=intent, sub_intent=sub_intent,
                    defaults={"payload": data},
                )
                if created:
                    messages.success(request, "Added successfully.")
                else:
                    messages.warning(request, "Row already exists. Use Update instead.")

        elif action == "update":
            data = parse_payload(payload_raw)
            updated = TenantJSONDoc.objects.filter(
                tenant=tenant, dtype=dtype, intent=intent, sub_intent=sub_intent
            ).update(payload=data)
            if updated:
                messages.success(request, "Updated successfully.")
            else:
                TenantJSONDoc.objects.create(
                    tenant=tenant, dtype=dtype, intent=intent, sub_intent=sub_intent, payload=data
                )
                messages.success(request, "Row not found, so it was created.")

        elif action == "delete":
            deleted, _ = TenantJSONDoc.objects.filter(
                tenant=tenant, dtype=dtype, intent=intent, sub_intent=sub_intent
            ).delete()
            if deleted:
                messages.success(request, "Deleted successfully.")
            else:
                messages.warning(request, "Nothing to delete.")
        else:
            messages.error(request, "Unknown action.")
    except ValueError as ve:
        messages.error(request, str(ve))
    except IntegrityError:
        messages.error(request, "Constraint error. Check uniqueness of (tenant, dtype, intent, sub_intent).")
    messages.info(request, "Changes are saved as a draft. Publish to apply them to conversations.")
    # Preserve selected dtype on redirect (nice for UX)
    redirect_url = reverse("tenant:tenant_knowledge")
    if dtype:
        redirect_url += f"?dtype={dtype}"
    return redirect(redirect_url)

@login_required
@tenant_required
@require_http_methods(["GET", "POST"])
@transaction.atomic
def upload_knowledge_prompt_view(request):
    tenant = request.user.tenantprofile.tenant

    if request.method == "GET":
        selected_dtype = request.GET.get("dtype", "")
        return render(request, "users/upload_knowledge_prompt.html", {
            "dtypes": [*TenantJSONDoc.DocType.choices, ("checkout", "Checkout settings"), ("commerce_policy", "Ordering policy")],
            "selected_dtype": selected_dtype,
        })

    # POST
    tenant = TenantInfo.objects.select_for_update().get(pk=tenant.pk)
    dtype = (request.POST.get("dtype") or "").strip()
    blob = request.POST.get("json_blob") or "{}"

    from chatbot_core.configuration_imports import import_configuration
    try:
        import_configuration(tenant, dtype, blob)
    except (ValueError, ValidationError, IntegrityError) as exc:
        messages.error(request, f"Import failed: {exc}")
        return redirect(reverse("tenant:upload_knowledge_prompt") + (f"?dtype={dtype}" if dtype else ""))
    if dtype == 'checkout':
        messages.success(request, "Checkout settings imported.")
        return _redirect_settings('checkout')
    if dtype == 'commerce_policy':
        messages.success(request, "Ordering policy imported.")
        return redirect(reverse('tenant:upload_knowledge_prompt') + '?dtype=commerce_policy')
    messages.success(request, "Configuration imported. Publish from the Knowledge page to apply draft documents.")
    # Redirect to your main page to inspect results, preselecting dtype:
    return redirect(reverse("tenant:tenant_knowledge") + (f"?dtype={dtype}" if dtype else ""))

@login_required
@tenant_required
@require_http_methods(["POST"])
@local_menu_required
def tenant_menu_variant_add_view(request, item_id):
    tenant = get_current_tenant(request)
    item = get_object_or_404(MenuItem, tenant=tenant, id=item_id)
    return _save_menu_variant(request, MenuItemVariant(menu_item=item))


def _save_menu_variant(request, variant):
    data = request.POST.copy()
    if 'variant_availability_present' not in data:
        data['is_available'] = 'on' if variant.is_available else ''
    data["description"] = data.get("variant_description", "")
    form = MenuVariantForm(data, instance=variant)
    if form.is_valid():
        form.save()
        _publish_menu(variant.menu_item.tenant)
        messages.success(request, "Variant saved.")
    else:
        messages.error(request, form.errors.as_text())
    return redirect("tenant:tenant_menu_item_detail", item_id=variant.menu_item_id)


@login_required
@tenant_required
@require_http_methods(["POST"])
@local_menu_required
def tenant_menu_variant_update_view(request, variant_id):
    tenant = get_current_tenant(request)
    variant = get_object_or_404(MenuItemVariant, id=variant_id, menu_item__tenant=tenant)
    return _save_menu_variant(request, variant)


@login_required
@tenant_required
@require_http_methods(["POST"])
@local_menu_required
def tenant_menu_variant_delete_view(request, variant_id):
    tenant = get_current_tenant(request)
    v = get_object_or_404(MenuItemVariant, id=variant_id)
    if v.menu_item.tenant_id != tenant.id:
        return HttpResponseForbidden()
    item_id = v.menu_item.id
    v.delete()
    _publish_menu(tenant)
    messages.success(request, "Variant deleted.")
    return redirect("tenant:tenant_menu_item_detail", item_id=item_id)

@login_required
@tenant_required
@require_POST
@local_menu_required
def tenant_menu_category_save_view(request, category_id=None):
    tenant = get_current_tenant(request)
    category = (get_object_or_404(MenuCategory, pk=category_id, tenant=tenant)
                if category_id is not None else MenuCategory(tenant=tenant))
    form = MenuCategoryForm(request.POST, instance=category)
    if form.is_valid():
        form.save()
        _publish_menu(tenant)
        messages.success(request, "Category saved.")
    else:
        messages.error(request, form.errors.as_text())
    return redirect("tenant:tenant_menu")


@login_required
@tenant_required
@require_POST
@local_menu_required
def tenant_menu_category_delete_view(request, category_id):
    tenant = get_current_tenant(request)
    category = get_object_or_404(MenuCategory, pk=category_id, tenant=tenant)
    category.delete()
    _publish_menu(tenant)
    messages.success(request, "Category deleted. Its products are now uncategorized.")
    return redirect("tenant:tenant_menu")


# ---------- UPDATE: list view (drop tenant-wide menu_category key; add sample) ----------
@login_required
@tenant_required
def tenant_menu_view(request):
    tenant = get_current_tenant(request)
    items = (
        MenuItem.objects
        .filter(tenant=tenant)
        .select_related("category_fk", "catalog_meta")   # pull per-item meta if it exists
        .prefetch_related("variants")
        .order_by("category_fk__sort_order", "category_fk__name", "name")
    )

    # Build a per-item meta dict keyed by item id (or slug/name if you prefer)
    per_item_meta = {}
    for mi in items:
        cm = getattr(mi, "catalog_meta", None)
        per_item_meta[str(mi.id)] = {
            "dietary_preferences": cm.dietary_preferences if cm else {},
            "allergens":           cm.allergens if cm else {},
            "preparation":         cm.preparation if cm else {},
            "nutrition":           cm.nutrition if cm else {},
            "explore_options":     cm.explore_options if cm else {},
        }

    return render(request, "users/dashboard_menu.html", {
        "active_page": "menu",
        "menu_items": items,
        **menu_context(tenant),
        "categories": MenuCategory.objects.filter(tenant=tenant),
        "catalog_json_text": json.dumps(per_item_meta, indent=2, ensure_ascii=False),
        "sample_menu_json": "",
    })

# assuming you already have tenant_required and get_current_tenant imported
# from .models import MenuItem, MenuItemVariant, MenuCategory, MenuCatalogMeta

@login_required
@tenant_required
@require_POST
@local_menu_required
def tenant_menu_ingest_json_view(request):
    from orders.catalog_imports import import_catalog
    tenant = get_current_tenant(request)
    try:
        created, updated, v_added, v_updated = import_catalog(tenant, request.POST.get('menu_items_json', ''))
    except (ValidationError, ValueError, TypeError, AttributeError, IntegrityError) as exc:
        messages.error(request, f"Menu import failed: {exc}")
    else:
        messages.success(request, f"Imported menu: items created={created}, updated={updated}; variants added={v_added}, updated={v_updated}. Knowledge saved to draft.")
    return redirect('tenant:tenant_menu')


# ---------- UPDATE: save item basics (now handles quantity and category fix) ----------
@login_required
@tenant_required
@require_http_methods(["POST"])
@local_menu_required
def tenant_menu_item_update_view(request, item_id):
    tenant = get_current_tenant(request)
    item = get_object_or_404(MenuItem, tenant=tenant, id=item_id)

    name = (request.POST.get("name") or "").strip()
    category_id = (request.POST.get("category_id") or "").strip()
    if category_id and not category_id.isdecimal():
        return HttpResponseBadRequest("Invalid category ID")
    category = get_object_or_404(MenuCategory, tenant=tenant, pk=category_id) if category_id else None
    description = (request.POST.get("description") or "").strip()
    is_available = request.POST.get("is_available") == "on"
    quantity_raw = request.POST.get("quantity") or ""
    meta_raw = request.POST.get("meta_json") or "{}"

    try:
        meta_obj = json.loads(meta_raw)
        if not isinstance(meta_obj, dict):
            raise ValueError("meta_json must be a JSON object")
    except Exception as e:
        messages.error(request, f"Meta JSON invalid: {e}")
        return redirect("tenant:tenant_menu_item_detail", item_id=item.id)

    try:
        qty_val = int(quantity_raw) if quantity_raw != "" else item.quantity
        if qty_val < 0: qty_val = 0
    except:
        qty_val = item.quantity

    with transaction.atomic():
        if name:
            item.name = name

        item.category_fk = category

        item.description = description
        item.is_available = is_available
        item.quantity = qty_val
        item.meta = meta_obj
        item.save()

    messages.success(request, f"Updated: {item.name}")
    _publish_menu(tenant)
    return redirect("tenant:tenant_menu_item_detail", item_id=item.id)

# ---------- NEW: save per-item catalog meta ----------
@login_required
@tenant_required
@require_http_methods(["POST"])
def tenant_menu_item_catalog_update_view(request, item_id):
    tenant = get_current_tenant(request)
    item = get_object_or_404(MenuItem, tenant=tenant, id=item_id)
    catmeta, _ = MenuCatalogMeta.objects.get_or_create(menu_item=item)

    def _parse_json_field(name, default):
        raw = request.POST.get(name, "")
        if not raw.strip():
            return default
        try:
            val = json.loads(raw)
            return val
        except Exception:
            # For fields that can be a single text (like preparation), accept as {"text": "..."}
            if name == "preparation":
                return {"text": raw}
            return default

    catmeta.dietary_preferences = _parse_json_field("dietary_preferences", {})
    catmeta.allergens = _parse_json_field("allergens", {})
    catmeta.preparation = _parse_json_field("preparation", {})
    catmeta.nutrition = _parse_json_field("nutrition", {})
    catmeta.explore_options = _parse_json_field("explore_options", {})
    catmeta.ingredients = _parse_json_field("ingredients", [])
    catmeta.recommendations = _parse_json_field("recommendations", [])
    catmeta.specialty_items = _parse_json_field("specialty_items", [])
    catmeta.source_quality = _parse_json_field("source_quality", [])
    catmeta.pairings = _parse_json_field("pairings", [])
    catmeta.flavor_profile = request.POST.get("flavor_profile", "").strip() or None

    catmeta.save()
    messages.success(request, "Item catalog meta saved.")
    _publish_menu(tenant)
    return redirect("tenant:tenant_menu_item_detail", item_id=item.id)

# ---------- UPDATE: detail view (fetch catmeta) ----------
@login_required
@tenant_required
def tenant_menu_item_detail_view(request, item_id):
    tenant = get_current_tenant(request)

    # Get the item for THIS tenant
    item = get_object_or_404(
        MenuItem.objects.select_related("category_fk").prefetch_related("variants"),
        id=item_id,
        tenant=tenant,
    )

    # Ensure the O2O meta exists; this returns (obj, created) — so unpack here is correct
    catmeta, _ = MenuCatalogMeta.objects.get_or_create(menu_item=item)

    # Render
    ctx = {
        "item": item,
        **menu_context(tenant),
        "catmeta": catmeta,                # <- matches your template variable
        "variants": list(item.variants.all()),
        "categories": MenuCategory.objects.filter(tenant=tenant),
    }

    initialize_caches()
    return render(request, "users/menu_item_detail.html", ctx)


@login_required
@tenant_required
def voice_assistant(request):
    tenant = get_current_tenant(request)

    # Persist a chat_id per-tenant in the user's session (so page reloads keep the thread)
    sess_key = f"va_chat_id:{tenant.id}"
    chat_id = request.session.get(sess_key)
    if not chat_id:
        import uuid
        chat_id = str(uuid.uuid4())
        request.session[sess_key] = chat_id

    # If your webhook URL is routed in urls.py, prefer reverse().
    # If it's fixed at /agent_core/voice/ keep that literal.
    webhook_url = "/agent_core/voice/"  # or reverse("voice_webhook")

    # Messages polling API (defined just below)
    poll_url = reverse("tenant:voice_messages_api")

    ctx = {
        "tenant_id": str(tenant.id),
        "chat_id": chat_id,
        "webhook_url": webhook_url,
        "poll_url": poll_url,
    }
    return render(request, "users/voice_assistant.html", ctx)


# a tiny messages API to fetch the transcript for this chat ---
@login_required
@tenant_required
@require_GET
def voice_messages_api(request):
    tenant = get_current_tenant(request)
    chat_id = (request.GET.get("chat_id") or "").strip()
    limit = int(request.GET.get("limit") or 100)

    if not chat_id:
        return HttpResponseBadRequest("chat_id required")

    # Reuse the transcript store used everywhere else
    msgs = get_messages(str(tenant.id), "voiceassistant", chat_id, limit=limit)
    metadata = get_latest_meta(str(tenant.id), "voiceassistant", chat_id)
    return JsonResponse({"messages": msgs, "metadata": metadata})
