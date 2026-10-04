"""Canonical commerce records. Provider secrets stay in the deployment secret store."""
import uuid
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q, F
from .policy import default_policy, validate_policy


class UUIDModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True


class Location(UUIDModel):
    tenant = models.ForeignKey('chatbot_core.TenantInfo', on_delete=models.PROTECT)
    code = models.CharField(max_length=64)
    name = models.CharField(max_length=200)
    timezone = models.CharField(max_length=64, default='Asia/Kolkata')
    address = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['tenant', 'code'], name='commerce_location_code')]


class Configuration(models.Model):
    tenant = models.OneToOneField('chatbot_core.TenantInfo', on_delete=models.PROTECT)
    location = models.ForeignKey(Location, on_delete=models.PROTECT)
    enabled = models.BooleanField(default=False)
    policy = models.JSONField(default=default_policy, validators=[validate_policy])
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        if self.location_id and self.location.tenant_id != self.tenant_id:
            raise ValidationError('Location belongs to another tenant.')

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class Connection(UUIDModel):
    location = models.ForeignKey(Location, on_delete=models.PROTECT)
    provider = models.CharField(max_length=64)  # square, clover, custom, etc.
    role = models.CharField(max_length=16, choices=[('pos', 'POS'), ('payment', 'Payment')])
    account_id = models.CharField(max_length=200)
    environment = models.CharField(max_length=16, choices=[('test', 'Test'), ('live', 'Live')], default='test')
    active = models.BooleanField(default=False)
    # Explicit subset of order.submit, order.reconcile, payment.create,
    # payment.reconcile, payment.refund, inventory.update, catalog.read, catalog.write.
    capabilities = models.JSONField(default=list)
    secret_ref = models.CharField(max_length=200, help_text='Key in settings.COMMERCE_ADAPTER_SECRETS; never the secret itself.')
    secret_fingerprint = models.CharField(max_length=64, null=True, blank=True, unique=True, editable=False)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['location', 'role'], condition=Q(active=True), name='commerce_active_role')]


    def save(self, *args, **kwargs):
        from .credentials import configured_secret, fingerprint
        secret = configured_secret(self)
        self.secret_fingerprint = fingerprint(secret) if secret else None
        if kwargs.get('update_fields') is not None:
            kwargs['update_fields'] = set(kwargs['update_fields']) | {'secret_fingerprint'}
        return super().save(*args, **kwargs)


class ExternalMapping(UUIDModel):
    connection = models.ForeignKey(Connection, on_delete=models.PROTECT)
    kind = models.CharField(max_length=32)
    canonical_id = models.CharField(max_length=100)
    external_id = models.CharField(max_length=200)
    # Distinguishes menu contexts, checks, modifier parents, etc.
    scope = models.CharField(max_length=200, blank=True, default='')
    revision = models.CharField(max_length=100, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['connection', 'kind', 'scope', 'canonical_id'], name='commerce_mapping_canonical'),
            models.UniqueConstraint(fields=['connection', 'kind', 'scope', 'external_id'], name='commerce_mapping_external'),
        ]


class MenuSource(models.Model):
    """One catalog authority per tenant; absent rows retain local management."""
    tenant = models.OneToOneField('chatbot_core.TenantInfo', on_delete=models.CASCADE)
    mode = models.CharField(max_length=16, choices=[('local', 'Local menu'), ('external', 'External menu')], default='local')
    connection = models.ForeignKey(Connection, on_delete=models.PROTECT, null=True, blank=True)
    generation = models.UUIDField(default=uuid.uuid4, editable=False)
    max_age_seconds = models.PositiveIntegerField(default=900)
    sequence = models.PositiveBigIntegerField(default=0)
    revision = models.CharField(max_length=100, blank=True)
    payload_hash = models.CharField(max_length=64, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    observed_at = models.DateTimeField(null=True, blank=True)
    synced_at = models.DateTimeField(null=True, blank=True)

    def clean(self):
        if not 1 <= self.max_age_seconds <= 86400:
            raise ValidationError('Menu freshness must be between 1 and 86400 seconds.')
        if self.mode == 'external':
            if not self.connection_id or self.connection.location.tenant_id != self.tenant_id:
                raise ValidationError('Choose a menu connection belonging to this tenant.')
            if self.connection.role != 'pos' or not self.connection.active or 'catalog.write' not in self.connection.capabilities:
                raise ValidationError('Choose an active POS/menu connection with catalog.write capability.')
        elif self.mode != 'local' or self.connection_id:
            raise ValidationError('Local menus must not have an external connection.')


class StockItem(UUIDModel):
    location = models.ForeignKey(Location, on_delete=models.PROTECT)
    item = models.ForeignKey('orders.MenuItem', null=True, blank=True, on_delete=models.PROTECT)
    variant = models.ForeignKey('orders.MenuItemVariant', null=True, blank=True, on_delete=models.PROTECT)
    addon = models.ForeignKey('orders.AddonItem', null=True, blank=True, on_delete=models.PROTECT)
    authority = models.ForeignKey(Connection, null=True, blank=True, on_delete=models.PROTECT)
    mode = models.CharField(max_length=16, choices=[('quantity', 'Quantity'), ('availability', 'Availability')], default='quantity')
    on_hand = models.PositiveIntegerField(default=0)
    reserved = models.PositiveIntegerField(default=0)
    pending_consumed = models.PositiveIntegerField(default=0)
    available = models.BooleanField(default=True)
    sequence = models.PositiveBigIntegerField(default=0)
    observed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(check=(Q(item__isnull=False, variant__isnull=True, addon__isnull=True) | Q(item__isnull=True, variant__isnull=False, addon__isnull=True) | Q(item__isnull=True, variant__isnull=True, addon__isnull=False)), name='commerce_stock_one_subject'),
            *[models.UniqueConstraint(fields=['location', field], name=f'commerce_stock_{field}') for field in ('item', 'variant', 'addon')],
        ]

    def clean(self):
        subject = self.item if self.item_id else self.variant.menu_item if self.variant_id else self.addon.group if self.addon_id else None
        if subject and subject.tenant_id != self.location.tenant_id:
            raise ValidationError('Stock subject belongs to another tenant.')
        if not self.authority_id and self.mode == 'quantity' and self.on_hand < self.reserved + self.pending_consumed:
            raise ValidationError('Local stock cannot be reduced below committed and reserved quantities.')
        if self.authority_id and self.authority.location_id != self.location_id:
            raise ValidationError('Stock authority belongs to another location.')


class AcceptedOrder(UUIDModel):
    order = models.OneToOneField('orders.Order', on_delete=models.PROTECT, related_name='commerce_record')
    location = models.ForeignKey(Location, on_delete=models.PROTECT)
    currency = models.CharField(max_length=3)
    exponent = models.PositiveSmallIntegerField(default=2)
    total_minor = models.PositiveBigIntegerField()
    snapshot = models.JSONField()
    snapshot_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    # These workflow fields may change; snapshot and accepted money may not.
    state = models.CharField(max_length=32, default='awaiting_payment')
    pos_state = models.CharField(max_length=32, default='not_requested')

    def save(self, *args, **kwargs):
        if not self._state.adding:
            previous = type(self).objects.get(pk=self.pk)
            if any(getattr(previous, f) != getattr(self, f) for f in ('order_id', 'location_id', 'currency', 'exponent', 'total_minor', 'snapshot', 'snapshot_hash')):
                raise ValidationError('Accepted order snapshots are immutable.')
        return super().save(*args, **kwargs)


class Reservation(UUIDModel):
    accepted_order = models.ForeignKey(AcceptedOrder, on_delete=models.PROTECT, related_name='reservations')
    stock = models.ForeignKey(StockItem, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    state = models.CharField(max_length=16, default='held')
    expires_at = models.DateTimeField(db_index=True)
    acknowledged = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['accepted_order', 'stock'], name='commerce_reservation_stock'), models.CheckConstraint(check=Q(quantity__gt=0), name='commerce_reservation_positive')]


class Payment(UUIDModel):
    accepted_order = models.ForeignKey(AcceptedOrder, on_delete=models.PROTECT, related_name='payments')
    connection = models.ForeignKey(Connection, on_delete=models.PROTECT)
    requested_minor = models.PositiveBigIntegerField()
    captured_minor = models.PositiveBigIntegerField(default=0)
    refunded_minor = models.PositiveBigIntegerField(default=0)
    currency = models.CharField(max_length=3)
    status = models.CharField(max_length=32, default='pending')
    external_id = models.CharField(max_length=200, null=True, blank=True)
    sequence = models.PositiveBigIntegerField(default=0)
    checkout_url = models.URLField(max_length=2000, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['connection', 'external_id'], condition=Q(external_id__isnull=False), name='commerce_payment_external'),
            models.UniqueConstraint(fields=['accepted_order'], name='commerce_one_payment_attempt'),
            models.CheckConstraint(check=Q(refunded_minor__lte=F('captured_minor')), name='commerce_refund_lte_capture'),
        ]


class Inbox(UUIDModel):
    connection = models.ForeignKey(Connection, on_delete=models.PROTECT)
    event_id = models.CharField(max_length=200)
    event_type = models.CharField(max_length=64)
    payload = models.JSONField()
    payload_hash = models.CharField(max_length=64)
    status = models.CharField(max_length=16, default='pending')
    attempts = models.PositiveIntegerField(default=0)
    error = models.CharField(max_length=300, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['connection', 'event_id'], name='commerce_inbox_event')]


class Command(UUIDModel):
    connection = models.ForeignKey(Connection, on_delete=models.PROTECT)
    accepted_order = models.ForeignKey(AcceptedOrder, null=True, on_delete=models.PROTECT, related_name='commands')
    kind = models.CharField(max_length=64)
    dedupe_key = models.CharField(max_length=200)
    payload = models.JSONField()
    status = models.CharField(max_length=16, default='pending', db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    available_at = models.DateTimeField(db_index=True)
    lease_token = models.UUIDField(null=True)
    lease_until = models.DateTimeField(null=True)
    error = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['connection', 'dedupe_key'], name='commerce_command_dedupe')]


class ReconciliationIssue(UUIDModel):
    accepted_order = models.ForeignKey(AcceptedOrder, on_delete=models.PROTECT, related_name='issues')
    code = models.CharField(max_length=64)
    detail = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.CharField(max_length=254, blank=True, editable=False)
    resolution_evidence = models.TextField(blank=True, editable=False)
    resolution_note = models.TextField(blank=True, editable=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['accepted_order', 'code'], condition=Q(resolved_at__isnull=True), name='commerce_open_issue')]
