import uuid
from django.db.models.functions import Lower
from django.db import models
from chatbot_core.models import TenantInfo
from django.db.models import Q, UniqueConstraint
from decimal import Decimal
from .checkout_config import default_checkout_config, validate_checkout_config


class Customer(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(TenantInfo, on_delete=models.CASCADE)
    name = models.CharField(max_length=255)
    phone = models.CharField(max_length=20, db_index=True)
    whatsapp_number = models.CharField(max_length=20, blank=True, null=True)
    telegram_id = models.CharField(max_length=50, blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    location_coordinates = models.JSONField(blank=True, null=True)  # {"lat": ..., "lng": ...}
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    class Meta:
        constraints = [
            UniqueConstraint(
                fields=['tenant', 'telegram_id'],
                name='uniq_telegram_per_tenant',
                condition=Q(telegram_id__isnull=False) & ~Q(telegram_id='')
            ),
            UniqueConstraint(  # ← add this
                fields=['tenant', 'phone'],
                name='uniq_phone_per_tenant',
                condition=Q(phone__isnull=False) & ~Q(phone='')
            ),
        ]

class DeliveryPartner(models.Model):
    class PartnerType(models.TextChoices):
        EXTERNAL = 'external'
        INHOUSE = 'inhouse'

    class Status(models.TextChoices):
        REQUESTED = 'requested'
        ASSIGNED = 'assigned'
        IN_TRANSIT = 'in_transit'
        DELIVERED = 'delivered'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(TenantInfo, on_delete=models.CASCADE)
    name = models.CharField(max_length=100)  # 'Pidge', etc.
    partner_type = models.CharField(max_length=10, choices=PartnerType.choices)
    tracking_url = models.URLField(blank=True, null=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.REQUESTED)

    def __str__(self):
        return self.name

class MenuCategory(models.Model):
    tenant = models.ForeignKey(TenantInfo, on_delete=models.CASCADE)
    name = models.CharField(max_length=255)
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name

    class Meta:
        ordering = ["sort_order", "name", "pk"]
        constraints = [
            # one category name per tenant (case-insensitive)
            UniqueConstraint(
                Lower("name"), "tenant",
                name="uniq_category_name_per_tenant_ci",
            ),
        ]


class MenuItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(TenantInfo, on_delete=models.CASCADE)
    name = models.CharField(max_length=255)
    quantity = models.IntegerField(default=10)
    is_available = models.BooleanField(default=True)
    description = models.TextField(blank=True, null=True)
    platform_item_ids = models.JSONField(default=dict)  # {'swiggy': '123', ...}
    category_fk = models.ForeignKey(MenuCategory, on_delete=models.SET_NULL, null=True, blank=True)
    meta = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return self.name

    class Meta:
        constraints = [
        ]

class MenuItemVariant(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    menu_item = models.ForeignKey(MenuItem, on_delete=models.CASCADE, related_name='variants')
    size = models.CharField(max_length=50)
    sort_order = models.PositiveIntegerField(default=0)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    is_available = models.BooleanField(default=True)
    aliases = models.JSONField(default=list, blank=True)
    volume_ml = models.IntegerField(blank=True, null=True)
    weight_grams = models.IntegerField(blank=True, null=True)
    description = models.TextField(blank=True, null=True)
    gst_liability = models.CharField(max_length=16, choices=[("vendor","vendor"),("restaurant","restaurant")], default="restaurant")

    def __str__(self):
        return f"{self.menu_item.name} - {self.size}"

    class Meta:
        ordering = ["sort_order", "size", "pk"]
        constraints = [
            UniqueConstraint(Lower("size"), "menu_item", condition=Q(is_available=True), name="uniq_variant_label_per_item_ci"),
        ]

class MenuCatalogMeta(models.Model):
    menu_item = models.OneToOneField(
        MenuItem, on_delete=models.CASCADE, related_name="catalog_meta"
    )
    dietary_preferences = models.JSONField(default=dict, blank=True)
    allergens = models.JSONField(default=dict, blank=True)
    preparation = models.JSONField(default=dict, blank=True)
    nutrition = models.JSONField(default=dict, blank=True)
    explore_options = models.JSONField(default=dict, blank=True)

    ingredients = models.JSONField(default=list, blank=True)
    recommendations = models.JSONField(default=list, blank=True)
    specialty_items = models.JSONField(default=list, blank=True)
    source_quality = models.JSONField(default=list, blank=True)
    pairings = models.JSONField(default=list, blank=True)
    flavor_profile = models.TextField(blank=True, null=True)

    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"CatalogMeta({self.menu_item.name})"


class Order(models.Model):
    class Source(models.TextChoices):
        ZOMATO = 'zomato'
        SWIGGY = 'swiggy'
        WHATSAPP = 'whatsapp'
        INHOUSE = 'inhouse'

    class Status(models.TextChoices):
        PENDING = 'pending'
        ACCEPTED = 'accepted'
        PREPARING = 'preparing'
        DISPATCHED = 'dispatched'
        DELIVERED = 'delivered'
        CANCELLED = 'cancelled'

    class PaymentStatus(models.TextChoices):
        UNPAID = 'unpaid'
        PAID = 'paid'
        FAILED = 'failed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(TenantInfo, on_delete=models.CASCADE)
    external_order_id = models.CharField(max_length=100, blank=True, null=True)
    source = models.CharField(max_length=20, choices=Source.choices)
    customer = models.ForeignKey(Customer, on_delete=models.SET_NULL, null=True)
    delivery_partner = models.ForeignKey(DeliveryPartner, on_delete=models.SET_NULL, null=True, blank=True)
    order_status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    payment_status = models.CharField(max_length=20, choices=PaymentStatus.choices, default=PaymentStatus.UNPAID)
    payment_mode = models.CharField(max_length=50, blank=True, null=True)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)
    tax_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    meta = models.JSONField(default=dict)
    location_coordinates = models.JSONField(blank=True, null=True)
    order_type = models.CharField(max_length=1, choices=[("H","Home"),("P","Parcel"),("D","Dine-in")], blank=True, null=True)
    advanced_order = models.CharField(max_length=1, choices=[("Y","Yes"),("N","No")], default="N")
    preorder_date = models.DateField(blank=True, null=True)
    preorder_time = models.TimeField(blank=True, null=True)
    payment_type = models.CharField(max_length=10, blank=True, null=True)  # COD/CARD/ONLINE
    packing_charges = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00')) 
    service_charge   = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    delivery_charges = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    enable_delivery = models.BooleanField(default=True)
    urgent_order = models.BooleanField(default=False)
    urgent_time_mins = models.PositiveIntegerField(blank=True, null=True)
    pickup_otp = models.CharField(max_length=10, blank=True, null=True)
    discount_type = models.CharField(max_length=1, choices=[("P","Percent"),("F","Fixed")], blank=True, null=True)
    callback_url = models.URLField(blank=True, null=True)

    def __str__(self):
        return f"{self.source.upper()} Order - {self.id}"

    class Meta:
        get_latest_by = "created_at"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["tenant", "created_at"]),
            models.Index(fields=["tenant", "customer", "created_at"]),
            models.Index(fields=["tenant", "order_status"]),   # ← add
            models.Index(fields=["tenant", "payment_status"]), # ← add
            models.Index(fields=["tenant", "source", "created_at"]), # handy reports
        ]
        constraints = [
            UniqueConstraint(
                fields=['tenant', 'external_order_id'],
                name='uniq_external_order_per_tenant',
                condition=Q(external_order_id__isnull=False) & ~Q(external_order_id='')
            )
        ]

class OrderItem(models.Model):
    # Prices cover the base variant only; modifier charges live on OrderItemAddon.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='items')
    item = models.ForeignKey(MenuItem, on_delete=models.SET_NULL, null=True)
    item_name = models.CharField(max_length=255)
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    total_price = models.DecimalField(max_digits=10, decimal_places=2)
    variant = models.ForeignKey(MenuItemVariant, on_delete=models.SET_NULL, null=True, blank=True)
    gst_liability = models.CharField(max_length=16, choices=[("vendor","vendor"),("restaurant","restaurant")], default="restaurant")
    item_tax_snapshot = models.JSONField(blank=True, null=True)  # store computed CGST/SGST split as sent

    def __str__(self):
        return f"{self.quantity} x {self.item_name}"


class PlatformWebhookLog(models.Model):
    class Source(models.TextChoices):
        ZOMATO = 'zomato'
        SWIGGY = 'swiggy'
        WHATSAPP = 'whatsapp'
        PIDGE = 'pidge'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(TenantInfo, on_delete=models.CASCADE)
    source = models.CharField(max_length=20, choices=Source.choices)
    event_type = models.CharField(max_length=100)
    payload = models.JSONField()
    processed = models.BooleanField(default=False)
    received_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.source} - {self.event_type}"


class ChatSession(models.Model):
    class Platform(models.TextChoices):
        WHATSAPP = 'whatsapp'
        WEBSITE = 'website'
        TELEGRAM = 'telegram'
        INSTAGRAM = 'instagram'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(TenantInfo, on_delete=models.CASCADE)
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE)
    session_id = models.CharField(max_length=100)
    platform = models.CharField(max_length=20, choices=Platform.choices)
    order = models.ForeignKey(Order, on_delete=models.SET_NULL, null=True, blank=True)
    state = models.JSONField(default=dict)  # Chatbot state memory
    language = models.CharField(max_length=10, blank=True, null=True)
    device_info = models.JSONField(blank=True, null=True)  # {"browser": "...", "os": "..."}
    geo_metadata = models.JSONField(blank=True, null=True)  # {"ip": "...", "city": "..."}
    is_completed = models.BooleanField(default=False)
    last_interaction_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.platform.upper()} Session {self.session_id}"

class CustomerAddress(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(TenantInfo, on_delete=models.CASCADE)
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name='addresses')
    label = models.CharField(max_length=100, blank=True, null=True)  # e.g., 'Home', 'Work'
    address_line = models.TextField()
    location_coordinates = models.JSONField(blank=True, null=True)  # {"lat": ..., "lng": ...}
    components = models.JSONField(blank=True, null=True)
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('customer', 'label')  # Optional: Prevent duplicate labels per customer

    def __str__(self):
        return f"{self.label or 'Address'} for {self.customer.name}"

class ItemAddonGroup(models.Model):
    tenant = models.ForeignKey(TenantInfo, on_delete=models.CASCADE)
    item = models.ForeignKey('MenuItem', on_delete=models.CASCADE, related_name='addon_groups')
    group = models.ForeignKey('AddonGroup', on_delete=models.CASCADE, related_name='items')
    min_selections = models.PositiveIntegerField(default=0)
    max_selections = models.PositiveIntegerField(default=1)
    # Empty means every variant of this item.
    variant_ids = models.JSONField(default=list, blank=True)

    class Meta:
        unique_together = ('item', 'group')

class OrderItemAddon(models.Model):
    # Quantity is per purchased item; total_price also includes the parent quantity.
    order_item = models.ForeignKey('OrderItem', on_delete=models.CASCADE, related_name='addons')
    addon = models.ForeignKey('AddonItem', on_delete=models.SET_NULL, null=True)
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    total_price = models.DecimalField(max_digits=10, decimal_places=2)

class AddonGroup(models.Model):
    tenant = models.ForeignKey(TenantInfo, on_delete=models.CASCADE)
    name = models.CharField(max_length=255)

    class Meta:
        constraints = [
        ]

class AddonItem(models.Model):
    aliases = models.JSONField(default=list, blank=True)
    min_quantity = models.PositiveIntegerField(default=1)
    max_quantity = models.PositiveIntegerField(default=1)
    is_available = models.BooleanField(default=True)
    group = models.ForeignKey(AddonGroup, on_delete=models.CASCADE, related_name="addons")
    name = models.CharField(max_length=255)
    price = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        constraints = [
        ]

class Tax(models.Model):
    tenant = models.ForeignKey(TenantInfo, on_delete=models.CASCADE)
    title = models.CharField(max_length=50)  # CGST/SGST etc.
    type = models.CharField(max_length=1, choices=[("P","Percentage"),("F","Fixed")], default="P")
    rate_display = models.CharField(max_length=16)  # "9%", "2.5%" or flat amount

    class Meta:
        constraints = [
        ]

class VariantTaxMap(models.Model):
    variant = models.ForeignKey(MenuItemVariant, on_delete=models.CASCADE, related_name="taxes")
    tax = models.ForeignKey(Tax, on_delete=models.CASCADE)
    class Meta:
        unique_together = ('variant', 'tax')


class CheckoutSettings(models.Model):
    """Tenant checkout settings; online checkout requires a ready external adapter."""
    tenant = models.OneToOneField(TenantInfo, on_delete=models.CASCADE, related_name='checkout_settings')
    configuration = models.JSONField(default=default_checkout_config, validators=[validate_checkout_config])
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        self.full_clean()
        from .checkout_config import validate_online_readiness
        validate_online_readiness(self.configuration, self.tenant)
        return super().save(*args, **kwargs)
