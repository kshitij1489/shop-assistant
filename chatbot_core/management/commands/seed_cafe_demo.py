"""An explicit, repeatable local cafe; never modifies an existing tenant."""
import os
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from chatbot_core.models import TenantInfo, TenantJSONDoc
from chatbot_core.runtime_configuration import publish_default_configuration, publish_configuration
from orders.models import MenuCategory, MenuItem, MenuItemVariant
from orders.onboarding import initialize_ordering_settings, complete_ordering_setup
from commerce.models import StockItem
from users.models import TenantProfile


class Command(BaseCommand):
    help = 'Create the approved demo-cafe tenant, owner, menu and published knowledge (no providers).'

    @transaction.atomic
    def handle(self, *args, **options):
        tenant = TenantInfo.objects.filter(slug='demo-cafe').first()
        if tenant:
            if (tenant.meta or {}).get('installation_demo') != 1:
                raise CommandError('demo-cafe already belongs to a non-demo tenant; refusing to modify it.')
            self.stdout.write('Demo already exists; preserving users, credentials, menu and publication.')
            return
        password = os.environ.get('DEMO_OWNER_PASSWORD', '')
        if len(password) < 12:
            raise CommandError('Set DEMO_OWNER_PASSWORD to at least 12 characters before seeding.')
        User = get_user_model()
        if User.objects.filter(username='demo-owner').exists():
            raise CommandError('demo-owner already exists; refusing to reassign an account.')
        owner = User.objects.create_user(username='demo-owner', password=password)
        tenant = TenantInfo.objects.create(
            slug='demo-cafe', display_name='Demo Café', business_type='cafe',
            approval_status='APPROVED', reviewed_at=timezone.now(),
            review_note='Explicit installation demo seed',
            allowed_domains=[settings.PUBLIC_URL], meta={'installation_demo': 1},
        )
        TenantProfile.objects.create(user=owner, tenant=tenant)
        category = MenuCategory.objects.create(tenant=tenant, name='Café menu')
        menu = {}
        for name, price, description in (
            ('Espresso', '120.00', 'A small black coffee.'),
            ('Cappuccino', '180.00', 'Espresso with steamed milk; contains dairy.'),
            ('Butter croissant', '150.00', 'Contains wheat and dairy.'),
        ):
            item = MenuItem.objects.create(tenant=tenant, category_fk=category, name=name, description=description)
            MenuItemVariant.objects.create(menu_item=item, size='Regular', price=price)
            menu[name] = {'price': price, 'description': description, 'currency': 'INR'}
        checkout, config = initialize_ordering_settings(tenant, demo=True)
        for item in MenuItem.objects.filter(tenant=tenant):
            stock = StockItem.objects.create(location=config.location, item=item, on_hand=100)
            item.quantity = stock.on_hand
            item.save(update_fields=['quantity'])
        publication = publish_default_configuration(tenant.pk)
        for intent, topic, description, knowledge in (
            ('information_about_the_cafe', 'location_and_hours', 'Opening hours and location',
             {'hours': 'Daily 09:00–18:00 Asia/Kolkata', 'address': '1 Example Street (fictional demo location)',
              'delivery_postal_codes': ['560001'], 'delivery_fee': 'INR 30', 'payment': 'Cash at fulfillment'}),
            ('menu_items', 'pricing', 'Menu and prices in INR', menu),
            ('menu_items', 'explore_options', 'Show the café menu', menu),
        ):
            for dtype, payload in (
                ('intent_classification', {'description': description, 'enabled': True}),
                ('response_intents', 'Answer using the supplied demo café knowledge. Prices are INR. Do not invent facts.'),
                ('knowledge', knowledge),
            ):
                TenantJSONDoc.objects.create(tenant=tenant, dtype=dtype, intent=intent, sub_intent=topic, payload=payload)
        publish_configuration(tenant.pk, expected_version=publication.version)
        complete_ordering_setup(tenant, checkout.configuration)
        self.stdout.write(self.style.SUCCESS(
            f'Demo ready. Owner: demo-owner; login: {settings.PUBLIC_URL}/accounts/login/; '
            f'chat: {settings.PUBLIC_URL}/chat-page/?tenant=demo-cafe. Cash checkout is ready; '
            'open daily 09:00–18:00 Asia/Kolkata. Delivery: 560001. External integrations are disabled.'
        ))
