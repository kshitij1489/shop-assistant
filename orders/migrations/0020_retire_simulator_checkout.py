"""Retired simulator selections fall back to cash; accepted orders remain unchanged."""
from django.db import migrations


def retire_simulator(apps, schema_editor):
    CheckoutSettings = apps.get_model('orders', 'CheckoutSettings')
    for row in CheckoutSettings.objects.using(schema_editor.connection.alias).all():
        config = row.configuration
        if config.get('online_provider') == 'dummy':
            config['online_provider'] = ''
            for mode in config.get('modes', {}).values():
                mode['payment_methods'] = ['cash']
            row.configuration = config
            row.save(update_fields=['configuration'])


class Migration(migrations.Migration):
    dependencies = [('orders', '0019_remove_tenantplatformconfig_tenant_and_more')]
    operations = [migrations.RunPython(retire_simulator, migrations.RunPython.noop)]
