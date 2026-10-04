"""Tenant menu ownership settings and guards for local catalog mutations."""
from functools import wraps
from django import forms
from django.contrib import messages
from django.db import transaction
from django.http import HttpResponseForbidden
from django.shortcuts import render, redirect
from django.views.decorators.http import require_http_methods
from commerce.models import Connection, MenuSource
from commerce.menu_sync import configure_source, source_for, lock_menu, assert_local_menu, assert_menu_fresh
from .decorators import tenant_required


def local_menu_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if request.method != 'POST':
            return view(request, *args, **kwargs)
        with transaction.atomic():
            tenant_id = request.user.tenantprofile.tenant_id
            lock_menu(tenant_id)
            try:
                assert_local_menu(tenant_id)
            except ValueError as exc:
                return HttpResponseForbidden(str(exc))
            return view(request, *args, **kwargs)
    return wrapped


def menu_context(tenant):
    source = source_for(tenant.pk)
    return {'menu_source': source, 'external_menu': bool(source and source.mode == 'external')}


class MenuSourceForm(forms.Form):
    mode = forms.ChoiceField(choices=MenuSource._meta.get_field('mode').choices, label='Menu managed by')
    connection = forms.ModelChoiceField(queryset=Connection.objects.none(), required=False,
        help_text='Choose the external menu connection. Leave empty for a local menu.')
    max_age_seconds = forms.IntegerField(min_value=1, max_value=86400, initial=900,
        label='Maximum external menu age (seconds)', help_text='Ordering pauses when the last source observation is older than this limit.')

    def __init__(self, *args, tenant, **kwargs):
        self.tenant = tenant
        source = source_for(tenant.pk)
        super().__init__(*args, initial={'mode': source.mode if source else 'local',
            'connection': source.connection_id if source else None,
            'max_age_seconds': source.max_age_seconds if source else 900}, **kwargs)
        self.fields['connection'].queryset = Connection.objects.filter(location__tenant=tenant, role='pos', active=True)
        self.fields['connection'].label_from_instance = lambda row: f'{row.provider} — {row.account_id} ({row.location.name})'

    def clean(self):
        data = super().clean()
        connection = data.get('connection')
        if data.get('mode') == 'external' and (not connection or 'catalog.write' not in connection.capabilities):
            self.add_error('connection', 'Choose an active connection supporting catalog.write.')
        if data.get('mode') == 'local' and connection:
            self.add_error('connection', 'Leave the connection empty for a local menu.')
        return data


@tenant_required
@require_http_methods(['GET', 'POST'])
def settings_view(request):
    tenant = request.user.tenantprofile.tenant
    form = MenuSourceForm(request.POST if request.method == 'POST' else None, tenant=tenant)
    if request.method == 'POST' and form.is_valid():
        from django.core.exceptions import ValidationError
        try:
            configure_source(tenant.pk, **form.cleaned_data)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, 'Menu source saved. External menus require a fresh synchronization before ordering.')
            return redirect('tenant:menu_source')
    freshness_error = ''
    try:
        assert_menu_fresh(tenant.pk)
    except ValueError as exc:
        freshness_error = str(exc)
    return render(request, 'users/menu_source.html', {'form': form, **menu_context(tenant),
        'freshness_error': freshness_error}, status=400 if form.errors else 200)
