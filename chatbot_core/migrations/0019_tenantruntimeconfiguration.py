from django.db import migrations, models
import django.db.models.deletion
from django.utils import timezone


def bootstrap_published_documents(apps, schema_editor):
    from django.core.exceptions import ValidationError
    from chatbot_core.runtime_configuration import validate_documents
    Tenant = apps.get_model('chatbot_core', 'TenantInfo')
    Document = apps.get_model('chatbot_core', 'TenantJSONDoc')
    Configuration = apps.get_model('chatbot_core', 'TenantRuntimeConfiguration')
    alias = schema_editor.connection.alias
    for tenant in Tenant.objects.using(alias).iterator():
        documents = list(Document.objects.using(alias).filter(tenant_id=tenant.pk)
                         .values('dtype', 'intent', 'sub_intent', 'payload'))
        try:
            validate_documents(tenant, documents, registry=apps, using=alias)
        except ValidationError:
            # Keep invalid legacy documents as drafts for correction and review.
            Configuration.objects.using(alias).create(tenant_id=tenant.pk)
            continue
        if not documents:
            Configuration.objects.using(alias).create(tenant_id=tenant.pk)
            continue
        Configuration.objects.using(alias).create(tenant_id=tenant.pk, version=1,
                                                  documents=documents, published_at=timezone.now())


class Migration(migrations.Migration):
    dependencies = [
        ('chatbot_core', '0017_rename_business_types_bookings_informational'),
        ('chatbot_core', '0018_drop_core_legacy_tables'),
        ('orders', '0018_checkout_settings'),
    ]
    operations = [
        migrations.CreateModel(
            name='TenantRuntimeConfiguration',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('version', models.PositiveBigIntegerField(default=0)),
                ('documents', models.JSONField(default=list)),
                ('published_at', models.DateTimeField(blank=True, null=True)),
                ('tenant', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE,
                    related_name='runtime_configuration', to='chatbot_core.tenantinfo')),
            ],
        ),
        migrations.RunPython(bootstrap_published_documents, migrations.RunPython.noop),
    ]
