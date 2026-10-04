from django.contrib.auth.models import User
from django.db import models
from chatbot_core.models import TenantInfo

class TenantProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="tenantprofile")
    tenant = models.ForeignKey(TenantInfo, on_delete=models.CASCADE, related_name='users', null=True, blank=True)
    last_jwt_token = models.TextField(blank=True, null=True)
    last_token_generated_at = models.DateTimeField(blank=True, null=True)
    is_master = models.BooleanField(default=False)

    def __str__(self):
        # Display username and tenant slug nicely, or 'Master' if no tenant
        tenant_slug = self.tenant.slug if self.tenant else 'Master'
        return f"{self.user.username} ({tenant_slug})"