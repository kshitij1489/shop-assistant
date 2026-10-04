from django.db import migrations, models
import django.db.models.deletion
import orders.checkout_config


class Migration(migrations.Migration):
    dependencies = [('orders', '0017_catalog_ordering_rules')]

    operations = [migrations.CreateModel(
        name='CheckoutSettings',
        fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('configuration', models.JSONField(default=orders.checkout_config.default_checkout_config,
                                              validators=[orders.checkout_config.validate_checkout_config])),
            ('updated_at', models.DateTimeField(auto_now=True)),
            ('tenant', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE,
                                           related_name='checkout_settings', to='chatbot_core.tenantinfo')),
        ],
    )]
