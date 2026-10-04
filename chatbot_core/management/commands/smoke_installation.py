"""Exercise a migrated, seeded installation without paid/provider calls."""
from urllib.parse import urlsplit
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client
from chatbot_core.models import TenantInfo
from chatbot_core.runtime_configuration import get_configuration
from chatbot_core.knowledge_cache import generate_all_menu_payload


class Command(BaseCommand):
    help = 'Check migrations, Redis, demo publication/menu, website and JWT authentication (no LLM calls).'

    def handle(self, *args, **options):
        executor = MigrationExecutor(connection)
        if executor.migration_plan(executor.loader.graph.leaf_nodes()):
            raise CommandError('Unapplied migrations. Run migrate first.')
        tenant = TenantInfo.objects.filter(slug='demo-cafe', approval_status='APPROVED', is_active=True).first()
        if tenant is None:
            raise CommandError('Run seed_cafe_demo first.')
        config = get_configuration(tenant_id=tenant.pk)
        if not config or not config.allows('menu_items', 'pricing'):
            raise CommandError('Demo menu configuration is not published.')
        menu = generate_all_menu_payload(api_key=tenant.api_key).get(tenant.api_key, {})
        if not {'Espresso', 'Cappuccino', 'Butter croissant'} <= menu.keys():
            raise CommandError('Demo menu is incomplete.')
        origin = urlsplit(settings.PUBLIC_URL)
        client = Client(HTTP_HOST=origin.netloc)
        secure = origin.scheme == 'https'
        for path in ('/health', '/accounts/login/', '/chat-page/?tenant=demo-cafe'):
            response = client.get(path, secure=secure)
            if response.status_code != 200:
                raise CommandError(f'{path} returned {response.status_code}, expected 200.')
        response = client.get('/agent_core/token/?tenant=demo-cafe', secure=secure, HTTP_X_API_KEY=tenant.api_key)
        if response.status_code != 200 or not response.json().get('token'):
            raise CommandError('Website token issuance failed.')
        token = response.json()['token']
        # Empty message verifies real JWT admission without invoking an LLM or creating an order.
        response = client.post('/agent_core/chatbot-api/', '{}', content_type='application/json',
                               secure=secure, HTTP_AUTHORIZATION=f'Bearer {token}')
        if response.status_code != 400 or response.json().get('error') != 'Missing message':
            raise CommandError('Website JWT authentication failed.')
        self.stdout.write(self.style.SUCCESS('Installation smoke passed: migrations, Redis, published café, menu, web pages and JWT. No provider calls.'))
