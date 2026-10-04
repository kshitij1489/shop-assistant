from django.contrib.admin import AdminSite


class OperationsAdminSite(AdminSite):
    """Global database administration is reserved for platform superusers."""
    def has_permission(self, request):
        return super().has_permission(request) and request.user.is_superuser
