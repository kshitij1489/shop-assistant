from django.contrib import admin
from . import models


@admin.register(models.AcceptedOrder, models.Reservation, models.Payment, models.Inbox, models.Command, models.ExternalMapping, models.ReconciliationIssue)
class LedgerAdmin(admin.ModelAdmin):
    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(models.Location)
admin.site.register(models.Configuration)
admin.site.register(models.Connection)
@admin.register(models.StockItem)
class StockAdmin(admin.ModelAdmin):
    readonly_fields = ['reserved', 'pending_consumed', 'sequence', 'observed_at']

    def get_readonly_fields(self, request, obj=None):
        if obj and models.Reservation.objects.filter(stock=obj).exists():
            return [*self.readonly_fields, 'location', 'item', 'variant', 'addon', 'authority', 'mode']
        return self.readonly_fields
