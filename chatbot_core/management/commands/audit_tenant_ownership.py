"""Read-only deployment inventory. No automatic attribution of historical data."""
import json
from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db.models import F, Q


class Command(BaseCommand):
    help = 'Report unowned records and conflicting ownership paths; --fail exits nonzero when issues remain.'

    def add_arguments(self, parser):
        parser.add_argument('--fail', action='store_true')

    def handle(self, *args, **options):
        issues = {}

        def record(label, qs):
            count = qs.count()
            if count:
                issues[label] = {'count': count, 'sample_ids': list(map(str, qs.values_list('pk', flat=True)[:20]))}

        for model_name, owner, related in [
            ('orders.Order', 'tenant_id', 'customer__tenant_id'),
            ('orders.Order', 'tenant_id', 'delivery_partner__tenant_id'),
            ('orders.CustomerAddress', 'tenant_id', 'customer__tenant_id'),
            ('orders.ChatSession', 'tenant_id', 'customer__tenant_id'),
            ('orders.ChatSession', 'tenant_id', 'order__tenant_id'),
            ('orders.MenuItem', 'tenant_id', 'category_fk__tenant_id'),
            ('orders.OrderItem', 'order__tenant_id', 'item__tenant_id'),
            ('orders.OrderItem', 'order__tenant_id', 'variant__menu_item__tenant_id'),
            ('orders.ItemAddonGroup', 'tenant_id', 'item__tenant_id'),
            ('orders.ItemAddonGroup', 'tenant_id', 'group__tenant_id'),
            ('orders.OrderItemAddon', 'order_item__order__tenant_id', 'addon__group__tenant_id'),
            ('orders.VariantTaxMap', 'variant__menu_item__tenant_id', 'tax__tenant_id'),
            ('commerce.Configuration', 'tenant_id', 'location__tenant_id'),
            ('commerce.AcceptedOrder', 'order__tenant_id', 'location__tenant_id'),
            ('commerce.StockItem', 'location__tenant_id', 'authority__location__tenant_id'),
            ('commerce.StockItem', 'location__tenant_id', 'item__tenant_id'),
            ('commerce.StockItem', 'location__tenant_id', 'variant__menu_item__tenant_id'),
            ('commerce.StockItem', 'location__tenant_id', 'addon__group__tenant_id'),
            ('commerce.Payment', 'accepted_order__location__tenant_id', 'connection__location__tenant_id'),
            ('commerce.Command', 'accepted_order__location__tenant_id', 'connection__location__tenant_id'),
            ('commerce.Reservation', 'accepted_order__location__tenant_id', 'stock__location__tenant_id'),
        ]:
            model = apps.get_model(model_name)
            record(f'{model_name}:{related}', model.objects.filter(**{related + '__isnull': False}).exclude(**{owner: F(related)}))
        # Report identifiers only; never include secret material in an audit.
        from commerce.credentials import adapter_secret, configured_secret
        grouped = {}
        invalid = []
        for adapter in apps.get_model('commerce.Connection').objects.filter(active=True).select_related('location'):
            secret = configured_secret(adapter)
            if not adapter_secret(adapter):
                invalid.append(str(adapter.pk))
            if secret:
                grouped.setdefault(secret, []).append((adapter.pk, adapter.location.tenant_id))
        if invalid:
            issues['commerce.unregistered_credentials'] = {'count': len(invalid), 'sample_ids': invalid[:20]}
        ambiguous = [str(pk) for group in grouped.values() if len(group) > 1 for pk, _ in group]
        if ambiguous:
            issues['commerce.shared_credentials'] = {'count': len(ambiguous), 'sample_ids': ambiguous[:20]}
        self.stdout.write(json.dumps(issues, indent=2))
        if issues and options['fail']:
            raise CommandError('Ownership issues require explicit review; no records were changed.')
