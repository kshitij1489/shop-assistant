from django.contrib import admin
from .models import TenantInfo

@admin.register(TenantInfo)
class TenantInfoAdmin(admin.ModelAdmin):
    list_display = ('slug', 'display_name', 'business_type', 'created_at')
    search_fields = ('slug', 'display_name')
    list_filter = ('business_type',)
    exclude = ('geocoding_provider',)  # Retained database field; delivery no longer geocodes.
