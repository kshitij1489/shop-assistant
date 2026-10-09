from .menu_source import local_menu_required, menu_context
from django.contrib import messages
from django.db import transaction
from django.db.models.deletion import ProtectedError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods
from orders.models import AddonGroup, MenuItem, ItemAddonGroup
from .decorators import tenant_required
from .modifier_forms import ModifierGroupForm, ModifierOptions, ItemModifierForm
from .utils import publish_menu


@tenant_required
@require_http_methods(['GET', 'POST'])
@local_menu_required
def modifiers(request, group_id=None):
    tenant = request.user.tenantprofile.tenant
    group = get_object_or_404(AddonGroup, tenant=tenant, pk=group_id) if group_id else AddonGroup(tenant=tenant)
    form = ModifierGroupForm(request.POST if request.method == 'POST' else None, instance=group)
    options = ModifierOptions(request.POST if request.method == 'POST' else None, instance=group)
    if request.method == 'POST':
        if request.POST.get('action') == 'delete' and group_id:
            try:
                with transaction.atomic():
                    group.delete()
                    publish_menu(tenant)
                messages.success(request, 'Modifier Group Deleted')
                return redirect('tenant:modifiers')
            except ProtectedError:
                form.add_error(None, 'This group has stock records. Disable its options instead.')
        elif form.is_valid() and options.is_valid():
            try:
                with transaction.atomic():
                    form.save()
                    options.save()
                    publish_menu(tenant)
                messages.success(request, 'Modifier Group Saved')
                return redirect('tenant:modifier_edit', group_id=group.pk)
            except ProtectedError:
                form.add_error(None, 'An option has stock records. Disable it instead of deleting it.')
        messages.error(request, 'Modifier Group Not Deleted' if request.POST.get('action') == 'delete' else 'Modifier Group Not Saved')
    return render(request, 'users/modifiers.html', {'active_page': 'menu', **menu_context(tenant), 'form': form, 'options': options, 'group': group,
        'groups': AddonGroup.objects.filter(tenant=tenant).order_by('name')}, status=400 if form.errors or options.errors else 200)


@tenant_required
@require_http_methods(['GET', 'POST'])
@local_menu_required
def item_modifiers(request, item_id, link_id=None):
    tenant = request.user.tenantprofile.tenant
    item = get_object_or_404(MenuItem, tenant=tenant, pk=item_id)
    link = get_object_or_404(ItemAddonGroup, tenant=tenant, item=item, pk=link_id) if link_id else ItemAddonGroup(item=item, tenant=tenant)
    form = ItemModifierForm(request.POST if request.method == 'POST' else None, instance=link, item=item)
    if request.method == 'POST':
        if request.POST.get('action') == 'delete' and link_id:
            with transaction.atomic():
                link.delete()
                publish_menu(tenant)
            messages.success(request, 'Modifier Group Detached')
            return redirect('tenant:item_modifiers', item_id=item.pk)
        if form.is_valid():
            with transaction.atomic():
                form.save()
                publish_menu(tenant)
            messages.success(request, 'Item Rules Saved')
            return redirect('tenant:item_modifiers', item_id=item.pk)
        messages.error(request, 'Item Rules Not Saved')
    return render(request, 'users/item_modifiers.html', {'active_page': 'menu', **menu_context(tenant), 'item': item, 'form': form, 'link': link,
        'links': item.addon_groups.filter(tenant=tenant).select_related('group')}, status=400 if form.errors else 200)
