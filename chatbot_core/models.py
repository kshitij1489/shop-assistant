import secrets
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.conf import settings
from django.utils.text import slugify

class TenantInfo(models.Model):

    class ApprovalStatus(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        APPROVED = 'APPROVED', 'Approved'
        REJECTED = 'REJECTED', 'Rejected'
        SUSPENDED = 'SUSPENDED', 'Suspended'

    class GeocodingProvider(models.TextChoices):
        GOOGLE = 'google', 'Google Maps'
        OPENSTREETMAP = 'openstreetmap', 'OpenStreetMap'

    ADDRESS_MAX_LENGTH = 500

    slug = models.SlugField(unique=True, blank=True)
    display_name = models.CharField(max_length=100)
    address = models.CharField(
        max_length=ADDRESS_MAX_LENGTH,
        blank=True,
        default='',
        help_text='Optional street address for this store.',
    )
    street_address_1 = models.CharField(max_length=200, blank=True, default='')
    street_address_2 = models.CharField(max_length=200, blank=True, default='')
    city = models.CharField(max_length=100, blank=True, default='')
    state = models.CharField(max_length=100, blank=True, default='')
    country = models.CharField(max_length=100, blank=True, default='')
    country_code = models.CharField(max_length=2, blank=True, default='')
    postal_code = models.CharField(max_length=20, blank=True, default='')
    city_place_id = models.CharField(max_length=255, blank=True, default='')
    # Historical value retained for schema compatibility; no delivery lookup uses it.
    geocoding_provider = models.CharField(
        max_length=32,
        choices=GeocodingProvider.choices,
        default=GeocodingProvider.GOOGLE,
        help_text='Address lookup service for delivery addresses on every channel.',
    )

    BUSINESS_TYPES = [
        ('cafe', 'Café / restaurant'),
    ]
    business_type = models.CharField(max_length=20, choices=BUSINESS_TYPES, default='cafe')
    description = models.TextField(blank=True)
    whatsapp_number = models.CharField(max_length=20, blank=True, null=True)
    telegram_chat_id = models.CharField(max_length=100, blank=True, null=True)
    whatsapp_id = models.CharField(max_length=100, blank=True, null=True, unique=True)
    telegram_bot_token = models.CharField(max_length=200, blank=True, null=True, unique=True)
    api_key = models.CharField(max_length=64, unique=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    allowed_domains = models.JSONField(default=list)
    is_active = models.BooleanField(default=True)
    meta = models.JSONField(blank=True, null=True, default=dict)

    approval_status = models.CharField(max_length=16, choices=ApprovalStatus.choices, default=ApprovalStatus.PENDING)
    reviewed_at = models.DateTimeField(blank=True, null=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='reviewed_tenants')
    review_note = models.TextField(blank=True, null=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(geocoding_provider__in=['google', 'openstreetmap']),
                name='tenantinfo_geocoding_provider_known',
            ),
        ]

    def __str__(self):
        return self.display_name

    def save(self, *args, **kwargs):
        # The slug is the public link used by widgets and JWTs. Assign it once.
        if not self.api_key:
            self.api_key = secrets.token_hex(32)
        if self._state.adding and not self.slug:
            self.slug = next_tenant_slug(self.display_name)
            self._insert_with_unique_slug(*args, **kwargs)
            return
        if not self.slug:
            raise ValidationError("A public link is required.")
        super().save(*args, **kwargs)

    def _insert_with_unique_slug(self, *args, **kwargs) -> None:
        for _attempt in range(5):
            try:
                with transaction.atomic():
                    super().save(*args, **kwargs)
                return
            except IntegrityError as exc:
                if not is_slug_conflict(exc):
                    raise
                self.pk = None
                self._state.adding = True
                self.slug = next_tenant_slug(self.display_name)
        raise ValidationError("A unique public link could not be created.")


def normalize_tenant_slug(value: str) -> str:
    """Turn a name or requested link into a slug, or return an empty string."""
    max_length = TenantInfo._meta.get_field("slug").max_length
    return slugify(value)[:max_length].strip("-")


def next_tenant_slug(display_name: str) -> str:
    """Return an unused slug derived from a business name."""
    base = normalize_tenant_slug(display_name)
    if not base:
        raise ValidationError("Enter a business name that can be used in a web address.")
    max_length = TenantInfo._meta.get_field("slug").max_length
    for suffix_number in range(1, 101):
        suffix = "" if suffix_number == 1 else f"-{suffix_number}"
        trimmed = base[: max_length - len(suffix)].strip("-")
        candidate = f"{trimmed}{suffix}"
        if candidate and not TenantInfo.objects.filter(slug=candidate).exists():
            return candidate
    raise ValidationError("A unique public link could not be created.")


def is_slug_conflict(exc: IntegrityError) -> bool:
    """True when the database rejected a tenant insert because the slug was taken."""
    cause = getattr(exc, "__cause__", None)
    constraint = getattr(getattr(cause, "diag", None), "constraint_name", "") or ""
    if "slug" in constraint:
        return True
    return "slug" in str(exc).lower()


class TenantJSONDoc(models.Model):
    class DocType(models.TextChoices):
        KNOWLEDGE = "knowledge", "Knowledge"
        RESPONSE_INTENTS = "response_intents", "Response Intents"
        INTENT_CLASSIFICATION = "intent_classification", "Intent Classification"

    tenant = models.ForeignKey(
        'chatbot_core.TenantInfo',
        on_delete=models.CASCADE,
        related_name='json_docs'
    )
    intent = models.CharField(max_length=120, db_index=True)
    sub_intent = models.CharField(max_length=120, db_index=True)
    dtype = models.CharField(max_length=32, choices=DocType.choices)
    payload = models.JSONField(default=dict)  # <- use built-in JSONField if possible

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("tenant", "dtype", "intent", "sub_intent"),
                name="uniq_tenant_dtype_intent_subintent"
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "dtype"]),
            models.Index(fields=["tenant", "dtype", "intent", "sub_intent"]),
        ]

    def __str__(self):
        return f"{self.tenant_id}:{self.dtype}:{self.intent}:{self.sub_intent}"

class TenantRuntimeConfiguration(models.Model):
    """Latest published bundle; TenantJSONDoc rows are the editable draft."""
    tenant = models.OneToOneField(TenantInfo, on_delete=models.CASCADE, related_name="runtime_configuration")
    version = models.PositiveBigIntegerField(default=0)
    documents = models.JSONField(default=list)
    published_at = models.DateTimeField(null=True, blank=True)


class SemanticCacheEntry(models.Model):
    sig = models.CharField(max_length=64, db_index=True)
    scope = models.CharField(max_length=128, db_index=True)   # e.g., "user:123" or "tenant:abc:intent"
    kb_fp = models.CharField(max_length=64, db_index=True)    # fingerprint of knowledge payload
    normalized_query = models.TextField()
    response = models.JSONField()  # or TextField if you prefer markdown
    system_id = models.CharField(max_length=64)
    model = models.CharField(max_length=64)
    params_hash = models.CharField(max_length=128)
    language = models.CharField(max_length=8, default="en")
    domain = models.CharField(max_length=32, default="static")  # static/menu/events/now
    created_at = models.DateTimeField(auto_now_add=True)
    last_hit = models.DateTimeField(auto_now=True)
    hit_count = models.IntegerField(default=0)

    class Meta:
        indexes = [
            models.Index(fields=["sig"]),
            models.Index(fields=["domain", "created_at"]),
        ]

class FaissVector(models.Model):
    cache_entry = models.OneToOneField(
        SemanticCacheEntry, on_delete=models.CASCADE, primary_key=True
    )
    dim = models.IntegerField(default=384)
    # raw float32 bytes (np.ndarray.tobytes())
    vector = models.BinaryField()
