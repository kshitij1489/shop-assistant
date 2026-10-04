"""Explicit, repeatable tenant rollout with a rollback-only preview by default."""
import json
from pathlib import Path
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from chatbot_core.models import TenantInfo
from orders.models import CheckoutSettings
from orders.checkout_config import CheckoutPolicy
from commerce.models import Configuration, Location
from commerce.policy import Policy
from commerce.readiness import readiness_issues


class Command(BaseCommand):
    help = 'Preview or apply explicit checkout/commerce policies to named tenants. Existing policies are preserved unless a file is supplied.'

    def add_arguments(self, parser):
        parser.add_argument('--tenant', type=int, action='append', required=True, help='Tenant ID; repeat for a reviewed batch.')
        parser.add_argument('--checkout-policy', help='Validated checkout JSON file; required for tenants on legacy checkout.')
        parser.add_argument('--commerce-policy', help='Validated commerce JSON file.')
        parser.add_argument('--enable', action='store_true', help='Enable commerce only when all local prerequisites pass.')
        parser.add_argument('--apply', action='store_true', help='Persist changes. Default is a rollback-only preview.')

    def handle(self, *args, **options):
        def policy_file(option, schema):
            path = options[option]
            if not path:
                return None
            try:
                value = json.loads(Path(path).read_text())
                return schema.model_validate(value).model_dump(mode='json')
            except (OSError, ValueError, ValidationError) as exc:
                raise CommandError(f'Invalid {option}: {exc}') from exc

        checkout_policy = policy_file('checkout_policy', CheckoutPolicy)
        commerce_policy = policy_file('commerce_policy', Policy)
        reports = []
        with transaction.atomic():
            for tenant_id in sorted(set(options['tenant'])):
                try:
                    tenant = TenantInfo.objects.select_for_update().get(pk=tenant_id, approval_status='APPROVED')
                except TenantInfo.DoesNotExist as exc:
                    raise CommandError(f'Approved tenant {tenant_id} was not found.') from exc
                checkout = CheckoutSettings.objects.filter(tenant=tenant).first()
                if checkout_policy is not None:
                    CheckoutSettings.objects.update_or_create(tenant=tenant, defaults={'configuration': checkout_policy})
                elif not checkout:
                    raise CommandError(f'Tenant {tenant_id} uses legacy checkout. Supply an explicit --checkout-policy file.')
                config = Configuration.objects.filter(tenant=tenant).first()
                if not config:
                    location, _ = Location.objects.get_or_create(tenant=tenant, code='default', defaults={'name': tenant.display_name})
                    config = Configuration(tenant=tenant, location=location)
                if commerce_policy is not None:
                    config.policy = commerce_policy
                config.save()
                issues = readiness_issues(tenant, configuration=config)
                if (options['enable'] or config.enabled) and issues:
                    raise CommandError(f'Tenant {tenant_id} is not ready: ' + ' '.join(issues))
                if options['enable']:
                    config.enabled = True
                    config.save(update_fields=['enabled'])
                reports.append({'tenant_id': tenant_id, 'checkout': 'configurable', 'commerce_enabled': config.enabled, 'remaining_setup': issues})
            if not options['apply']:
                transaction.set_rollback(True)
        self.stdout.write(json.dumps({'applied': options['apply'], 'tenants': reports}, indent=2))
