from django.contrib import messages
from django.shortcuts import render, redirect, get_object_or_404
from django.db import transaction, IntegrityError
from django.views.decorators.http import require_http_methods
from django.views.decorators.cache import never_cache
from django.core.paginator import Paginator
from django.utils import timezone
from users.decorators import tenant_required
from orders.models import CheckoutSettings
from .models import Configuration, Location, ReconciliationIssue, Connection, StockItem, Command, Inbox, ExternalMapping
from .forms import CommerceSettingsForm, ConnectionForm, StockForm, IssueResolutionForm
from .credentials import rotate_credentials
from .readiness import readiness_issues


@tenant_required
@never_cache
@require_http_methods(['GET'])
def operations_view(request):
    tenant = request.user.tenantprofile.tenant
    issues = ReconciliationIssue.objects.filter(accepted_order__location__tenant=tenant)
    if request.GET.get('status') != 'resolved':
        issues = issues.filter(resolved_at__isnull=True)
    else:
        issues = issues.filter(resolved_at__isnull=False)
    return render(request, 'commerce/operations.html', {
        'issues': Paginator(issues.select_related('accepted_order').order_by('-created_at', '-pk'), 50).get_page(request.GET.get('page')),
        'show_resolved': request.GET.get('status') == 'resolved',
        'commands': Paginator(Command.objects.filter(connection__location__tenant=tenant,
            status__in=['unknown', 'failed']).select_related('connection').order_by('-created_at', '-pk'), 50).get_page(request.GET.get('commands_page')),
        'inbox': Paginator(Inbox.objects.filter(connection__location__tenant=tenant).exclude(status='processed')
            .select_related('connection').order_by('-received_at', '-pk'), 50).get_page(request.GET.get('inbox_page')),
    })


@tenant_required
@never_cache
@require_http_methods(['GET', 'POST'])
def issue_view(request, issue_id):
    tenant = request.user.tenantprofile.tenant
    form = IssueResolutionForm(request.POST if request.method == 'POST' else None)
    with transaction.atomic():
        issues = ReconciliationIssue.objects.filter(accepted_order__location__tenant=tenant)
        if request.method == 'POST':
            issues = issues.select_for_update()
        issue = get_object_or_404(issues, pk=issue_id)
        if request.method == 'POST':
            if issue.resolved_at:
                form.add_error(None, 'This issue is already resolved. Its resolution cannot be overwritten.')
            elif form.is_valid():
                issue.resolved_at = timezone.now()
                issue.resolved_by = f'user:{request.user.pk}:{request.user.get_username()}'
                issue.resolution_evidence = form.cleaned_data['evidence']
                issue.resolution_note = form.cleaned_data['note']
                issue.save(update_fields=['resolved_at', 'resolved_by', 'resolution_evidence', 'resolution_note'])
                messages.success(request, 'Resolution Recorded')
                return redirect('commerce:issue', issue_id=issue.pk)
    record = issue.accepted_order
    if request.method == 'POST':
        messages.error(request, 'Resolution Not Recorded')
    return render(request, 'commerce/issue.html', {'issue': issue, 'record': record, 'form': form,
        'commands': record.commands.select_related('connection').order_by('created_at'),
        'payments': record.payments.select_related('connection').all(),
        'reservations': record.reservations.select_related('stock').all(),
        'mappings': ExternalMapping.objects.filter(connection__location=record.location,
            kind='order', canonical_id=str(record.order_id)).select_related('connection'),
    }, status=400 if form.errors else 200)


@tenant_required
@require_http_methods(['GET', 'POST'])
def settings_view(request):
    tenant = request.user.tenantprofile.tenant
    config = Configuration.objects.filter(tenant=tenant).first()
    form = CommerceSettingsForm(request.POST if request.method == 'POST' else None, configuration=config)
    if request.method == 'POST' and form.is_valid():
        issues = readiness_issues(tenant, configuration=config, policy=form.policy) if form.cleaned_data['enabled'] else []
        if issues:
            for issue in issues:
                form.add_error(None, issue)
        else:
            with transaction.atomic():
                location, _ = Location.objects.get_or_create(tenant=tenant, code='default', defaults={'name': tenant.display_name})
                Configuration.objects.update_or_create(tenant=tenant, defaults={'location': config.location if config else location,
                    'enabled': form.cleaned_data['enabled'], 'policy': form.policy})
            messages.success(request, 'Commerce Settings Saved')
            return redirect('commerce:settings')
    if request.method == 'POST':
        messages.error(request, 'Commerce Settings Not Saved')
    issues = ReconciliationIssue.objects.filter(accepted_order__location__tenant=tenant, resolved_at__isnull=True).order_by('-created_at')[:50]
    return render(request, 'commerce/settings.html', {'tenant': tenant, 'form': form, 'issues': issues,
        'config': config, 'readiness': readiness_issues(tenant, configuration=config),
        'checkout_enabled': CheckoutSettings.objects.filter(tenant=tenant).exists()}, status=400 if form.errors else 200)


@tenant_required
@never_cache
@require_http_methods(['GET', 'POST'])
def connections_view(request, connection_id=None):
    tenant = request.user.tenantprofile.tenant
    config = get_object_or_404(Configuration, tenant=tenant)
    secret = None
    with transaction.atomic():
        # Serialize role activation and changes in this location.
        if request.method == 'POST':
            Location.objects.select_for_update().get(pk=config.location_id)
        connection = get_object_or_404(Connection, location=config.location, pk=connection_id) if connection_id else Connection(location=config.location)
        form = ConnectionForm(request.POST if request.method == 'POST' else None, instance=connection)
        if request.method == 'POST':
            if request.POST.get('action') == 'rotate' and connection_id:
                secret = rotate_credentials(connection)
                form = ConnectionForm(instance=connection)
                messages.success(request, 'Signing Secret Rotated')
            elif form.is_valid():
                creating = connection._state.adding
                try:
                    with transaction.atomic():
                        form.save()
                        if creating:
                            secret = rotate_credentials(connection)
                except IntegrityError:
                    form.add_error(None, 'Another active connection already uses this role. Reload and try again.')
                else:
                    messages.success(request, 'Connection Saved')
                    if not creating:
                        return redirect('commerce:connections')
    if request.method == 'POST' and form.errors:
        messages.error(request, 'Connection Not Saved')
    return render(request, 'commerce/connections.html', {'form': form, 'connection': connection,
        'adapter_secret': secret, 'connections': Connection.objects.filter(location=config.location)}, status=400 if form.errors else 200)


@tenant_required
@require_http_methods(['GET', 'POST'])
def stock_view(request, stock_id=None):
    config = get_object_or_404(Configuration, tenant=request.user.tenantprofile.tenant)
    with transaction.atomic():
        stocks = StockItem.objects.select_for_update() if request.method == 'POST' else StockItem.objects.all()
        stock = get_object_or_404(stocks, location=config.location, pk=stock_id) if stock_id else StockItem(location=config.location)
        form = StockForm(request.POST if request.method == 'POST' else None, instance=stock)
        if request.method == 'POST' and form.is_valid():
            try:
                with transaction.atomic():
                    form.save()
            except IntegrityError:
                form.add_error(None, 'Stock already exists for this selection. Reload and edit that record.')
            else:
                messages.success(request, 'Stock Saved')
                return redirect('commerce:stock')
    if request.method == 'POST':
        messages.error(request, 'Stock Not Saved')
    return render(request, 'commerce/stock.html', {'form': form,
        'stocks': StockItem.objects.filter(location=config.location).select_related('item', 'variant__menu_item', 'addon')}, status=400 if form.errors else 200)
