import os
import re
import json
import uuid
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError
from django.utils.text import slugify
from django.db import transaction

from orders.models import MenuItem, MenuItemVariant, MenuCategory
from chatbot_core.models import TenantInfo


CURRENCY_STRIPPER = re.compile(r"[^\d\.]")  # remove ₹, commas, spaces, etc.


def parse_money(value: str) -> Decimal:
    if value is None:
        return Decimal("0")
    try:
        sanitized = CURRENCY_STRIPPER.sub("", str(value))
        return Decimal(sanitized or "0")
    except (InvalidOperation, ValueError):
        return Decimal("0")


class Command(BaseCommand):
    help = (
        "Load MenuItem objects with variants for a given tenant from "
        "an explicitly selected knowledge JSON file"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant-id",
            type=str,
            required=True,
            help="Tenant ID (UUID or integer) to associate MenuItems with",
        )
        source = parser.add_mutually_exclusive_group(required=True)
        source.add_argument(
            "--file",
            help="Path to a knowledge JSON file containing menu_items",
        )
        source.add_argument(
            "--slug",
            type=str,
            help="Legacy source: tenants/<slug>/knowledge_base.json (operator-owned, not bundled)",
        )

    def _get_tenant(self, raw_id: str):
        # Try UUID first
        try:
            return TenantInfo.objects.get(id=uuid.UUID(raw_id))
        except Exception:
            pass
        # Fall back to integer
        try:
            return TenantInfo.objects.get(id=int(raw_id))
        except Exception:
            return None

    @transaction.atomic
    def handle(self, *args, **options):
        tenant_id = options["tenant_id"]
        tenant_slug = options["slug"]

        tenant = self._get_tenant(tenant_id)
        if not tenant:
            self.stderr.write(self.style.ERROR(f"❌ Invalid or non-existent tenant ID: {tenant_id}"))
            return

        from commerce.menu_sync import lock_menu, assert_local_menu
        lock_menu(tenant.pk)
        try:
            assert_local_menu(tenant.pk)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        # project_root/.../orders/management/commands -> go up 4 levels
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
        knowledge_path = options["file"] or os.path.join(base_dir, "tenants", tenant_slug, "knowledge_base.json")

        if not os.path.exists(knowledge_path):
            raise CommandError(f"File not found: {knowledge_path}")

        with open(knowledge_path, "r", encoding="utf-8") as f:
            knowledge = json.load(f)

        try:
            items = knowledge["menu_items"]["availability"]["all_items"]
        except KeyError:
            self.stderr.write(self.style.ERROR("❌ JSON missing menu_items.availability.all_items"))
            return

        pricing_map = knowledge["menu_items"].get("pricing", {})
        portion_map = knowledge["menu_items"].get("portion_and_size", {})
        cat_map = knowledge["menu_items"].get("menu_category", {})

        created_items = 0
        created_variants = 0
        updated_variants = 0
        warnings = 0

        for name in items:
            name = str(name).strip()
            if not name:
                continue

            price_info = pricing_map.get(name, {})
            if not price_info:
                self.stderr.write(self.style.WARNING(f"⚠️  No pricing found for '{name}'"))
                warnings += 1

            category_name = str(cat_map.get(name) or "").strip()
            category = None
            if category_name:
                category, _ = MenuCategory.objects.get_or_create(
                    tenant=tenant, name__iexact=category_name, defaults={"name": category_name},
                )

            # Basic platform IDs from name slug (change as needed)
            base_slug = slugify(name)[:16] or "item"
            platform_ids = {
                "swiggy": f"sw_{base_slug}",
                "zomato": f"zo_{base_slug}",
            }

            menu_item = MenuItem.objects.filter(tenant=tenant, name=name).order_by('-is_available', 'pk').first()
            created = menu_item is None
            if created:
                menu_item = MenuItem.objects.create(tenant=tenant, name=name, category_fk=category,
                    is_available=True, description='', platform_item_ids=platform_ids)

            # Keep category in sync if you want (optional)
            if not created and menu_item.category_fk != category:
                menu_item.category_fk = category
                menu_item.save(update_fields=["category_fk"])

            if created:
                created_items += 1

            # Build variants
            for position, (raw_size, price_str) in enumerate(price_info.items() if isinstance(price_info, dict) else []):
                size_norm = str(raw_size).strip()
                price = parse_money(price_str)

                volume_ml = None
                weight_grams = None
                desc = None

                item_portions = portion_map.get(name, {})
                size_meta = item_portions.get(raw_size, {})
                if not size_meta and size_norm == "per_quantity":
                    size_meta = item_portions
                if isinstance(size_meta, dict):
                    volume_ml = size_meta.get("volume_ml")
                    weight_grams = size_meta.get("weight_grams")
                    desc = size_meta.get("description")

                variant, var_created = MenuItemVariant.objects.get_or_create(
                    menu_item=menu_item,
                    size__iexact=size_norm,
                    is_available=True,
                    defaults={
                        "size": size_norm,
                        "sort_order": position,
                        "price": price,
                        "volume_ml": volume_ml,
                        "weight_grams": weight_grams,
                        "description": desc,
                    },
                )


                if var_created:
                    created_variants += 1
                else:
                    # Update price/metrics if changed
                    changed = False
                    if variant.price != price:
                        variant.price = price
                        changed = True
                    if volume_ml is not None and variant.volume_ml != volume_ml:
                        variant.volume_ml = volume_ml
                        changed = True
                    if weight_grams is not None and variant.weight_grams != weight_grams:
                        variant.weight_grams = weight_grams
                        changed = True
                    if desc and variant.description != desc:
                        variant.description = desc
                        changed = True
                    if changed:
                        variant.save(update_fields=["price", "volume_ml", "weight_grams", "description"])
                        updated_variants += 1

        from users.utils import publish_menu
        transaction.on_commit(lambda: publish_menu(tenant))

        self.stdout.write(
            self.style.SUCCESS(
                f"✅ Tenant: {tenant.slug} — {created_items} items created, "
                f"{created_variants} variants created, {updated_variants} variants updated. "
                f"{'(' + str(warnings) + ' warnings)' if warnings else ''}"
            )
        )
