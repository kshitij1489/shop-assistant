from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('chatbot_core', '0022_tenantinfo_address'),
    ]

    operations = [
        migrations.AddField(
            model_name='tenantinfo',
            name='geocoding_provider',
            field=models.CharField(
                choices=[('google', 'Google Maps'), ('openstreetmap', 'OpenStreetMap')],
                default='google',
                help_text='Address lookup service for delivery addresses on every channel.',
                max_length=32,
            ),
        ),
        migrations.AddConstraint(
            model_name='tenantinfo',
            constraint=models.CheckConstraint(
                condition=models.Q(geocoding_provider__in=['google', 'openstreetmap']),
                name='tenantinfo_geocoding_provider_known',
            ),
        ),
    ]
