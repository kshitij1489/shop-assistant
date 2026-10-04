# orders/management/commands/create_tax.py
from __future__ import annotations

import uuid
from typing import Optional, List
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from chatbot_core.models import TenantInfo
from orders.models import Tax, MenuItemVariant, VariantTaxMap


# -------------------------
# Helpers
# -------------------------
def _normalize_rate_display(rate: str, type_: str) -> str:
    """
    Normalize user input to match Tax.rate_display:
      - Percentage ('P'): ensure trailing '%', e.g. '2.5' -> '2.5%'
      - Fixed ('F'): plain number (strip '%')
    """
    s = (rate or "").strip()
    if not s:
        return s

    # Validate it can be parsed as a number (ignoring %)
    probe = s.rstrip("%")
    try:
        Decimal(probe)
    except InvalidOperation:
        # Let it pass, but usually a user error
        return s

    return f"{probe}%" if type_.upper() == "P" else probe


def _normalize_category_label(raw: Optional[str]) -> Optional[str]:
    return str(raw).strip() if raw else None


def _get_tenant(tenant_id: str) -> TenantInfo:
    try:
        # Most likely UUID PK
        return TenantInfo.objects.get(pk=uuid.UUID(tenant_id))
    except ValueError:
        # If not a UUID, try as plain PK (string/int)
        try:
            return TenantInfo.objects.get(pk=tenant_id)
        except TenantInfo.DoesNotExist:
            pass
    except TenantInfo.DoesNotExist:
        pass
    raise CommandError(f"Tenant not found for id: {tenant_id}")


# -------------------------
# Management Command
# -------------------------
class Command(BaseCommand):
    help = (
        "Create or update a Tax entry for a tenant, and optionally attach it to MenuItemVariants.\n\n"
        "Examples:\n"
        "  python manage.py create_tax --tenant-id <UUID> --title CGST --type P --rate 2.5\n"
        "  python manage.py create_tax --tenant-id <UUID> --title 'Packaging' --type F --rate 10\n"
        "  python manage.py create_tax --tenant-id <UUID> --title CGST --type P --rate 2.5 \\\n"
        "      --attach-variant <VARIANT_UUID> --attach-variant <VARIANT_UUID>\n"
        "  python manage.py create_tax --tenant-id <UUID> --title CGST --type P --rate 2.5 \\\n"
        "      --attach-all --attach-category 'Ice cream' --attach-size regular --only-missing\n"
    )

    def add_arguments(self, parser):
        parser.add_argument("--tenant-id", required=True, help="TenantInfo primary key (UUID or PK).")
        parser.add_argument("--title", required=True, help="Tax title, e.g., 'CGST' or 'SGST'.")
        parser.add_argument(
            "--type",
            choices=["P", "F"],
            default="P",
            help="Tax type: P=Percentage, F=Fixed (default: P)."
        )
        parser.add_argument(
            "--rate",
            required=True,
            help="Rate value; e.g., 2.5 (percent) or 10 (fixed amount). You can also pass '2.5%%' explicitly."
        )
        parser.add_argument(
            "--attach-variant",
            action="append",
            dest="variant_ids",
            default=[],
            help="Optionally attach this tax to a MenuItemVariant by UUID. Can be passed multiple times."
        )
        parser.add_argument(
            "--attach-all",
            action="store_true",
            help="Attach this tax to ALL MenuItemVariants for the tenant."
        )
        parser.add_argument(
            "--attach-category",
            help="Limit attachments to a category (case-insensitive). Use its dashboard display name."
        )
        parser.add_argument(
            "--attach-size",
            action="append",
            dest="attach_sizes",
            default=[],
            help="Limit attachments to specific sizes (repeatable). "
                 "Allowed (case-insensitive): scoop, mini tub, regular, family, per_quantity"
        )
        parser.add_argument(
            "--only-missing",
            action="store_true",
            help="Attach only to variants that currently have NO taxes."
        )
        parser.add_argument(
            "--clear-title",
            action="store_true",
            help="Before attaching, remove any existing VariantTaxMap that uses a Tax with the same title "
                 "for the selected variants."
        )
        parser.add_argument("--dry-run", action="store_true", help="Show what would happen, without writing.")

    @transaction.atomic
    def handle(self, *args, **options):
        tenant_id: str = options["tenant_id"]
        title: str = options["title"].strip()
        type_: str = options["type"].upper()
        rate_input: str = options["rate"]
        variant_ids: List[str] = options.get("variant_ids") or []
        dry_run: bool = options["dry_run"]

        tenant = _get_tenant(tenant_id)
        rate_display = _normalize_rate_display(rate_input, type_)

        # Canonical tax identities are mapped to providers by external adapters.
        tax = Tax.objects.filter(tenant=tenant, title=title, type=type_, rate_display=rate_display).first()
        if tax is None:
            tax = Tax(tenant=tenant, title=title, type=type_, rate_display=rate_display)
            if not dry_run:
                tax.save()

        # --- Optional: attach to explicit variants ---------------------------
        if variant_ids:
            for vid in variant_ids:
                try:
                    variant_uuid = uuid.UUID(vid)
                except ValueError:
                    raise CommandError(f"Invalid variant UUID: {vid}")

                variant = MenuItemVariant.objects.filter(id=variant_uuid, menu_item__tenant=tenant).first()
                if not variant:
                    raise CommandError(f"MenuItemVariant not found: {vid}")

                if dry_run:
                    self.stdout.write(self.style.WARNING(
                        f"[DRY RUN] Would attach Tax {tax.id} to Variant {variant.id}"
                    ))
                else:
                    VariantTaxMap.objects.get_or_create(variant=variant, tax=tax)
                    self.stdout.write(self.style.SUCCESS(
                        f"Attached Tax {tax.id} to Variant {variant.id}"
                    ))

        # --- Bulk attach with filters ----------------------------------------
        attach_all = options["attach_all"]
        attach_category_raw = options.get("attach_category")
        sizes = [s.strip().lower() for s in (options.get("attach_sizes") or [])]
        only_missing = options["only_missing"]
        clear_title = options["clear_title"]

        qs = None
        if attach_all or attach_category_raw or sizes or only_missing:
            qs = MenuItemVariant.objects.filter(menu_item__tenant=tenant)
            # category moved to FK (MenuItem.category_fk)
            if attach_category_raw:
                normalized_cat = _normalize_category_label(attach_category_raw)
                qs = qs.filter(menu_item__category_fk__name__iexact=normalized_cat)

            if sizes:
                from django.db.models import Q
                size_filter = Q()
                for size in sizes:
                    size_filter |= Q(size__iexact=size)
                qs = qs.filter(size_filter)

            if only_missing:
                qs = qs.exclude(
                    id__in=VariantTaxMap.objects
                        .filter(variant__menu_item__tenant=tenant, tax=tax)
                        .values("variant_id")
                )

            if clear_title:
                to_clear = VariantTaxMap.objects.filter(
                    variant_id__in=qs.values("id"),
                    tax__tenant=tenant,
                    tax__title=title,
                )
                cleared = to_clear.count()
                if dry_run:
                    self.stdout.write(self.style.WARNING(
                        f"[DRY RUN] Would clear {cleared} existing maps for title '{title}'."
                    ))
                else:
                    to_clear.delete()
                    self.stdout.write(self.style.NOTICE(
                        f"Cleared {cleared} existing maps for title '{title}'."
                    ))

            attached = 0
            if dry_run:
                count = qs.count()
                attached = count  # simulate
            else:
                for v in qs.only("id"):
                    _, created_map = VariantTaxMap.objects.get_or_create(variant=v, tax=tax)
                    if created_map:
                        attached += 1

            self.stdout.write(self.style.SUCCESS(
                f"{'[DRY RUN] Would attach' if dry_run else 'Attached'} tax '{title}' to {qs.count()} variants "
                f"({attached} new mappings)."
            ))

        self.stdout.write(self.style.SUCCESS("Done."))
