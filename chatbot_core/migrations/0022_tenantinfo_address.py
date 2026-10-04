from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('chatbot_core', '0021_alter_tenantinfo_business_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='tenantinfo',
            name='address',
            field=models.CharField(
                blank=True,
                default='',
                help_text='Optional street address for this store.',
                max_length=500,
            ),
        ),
    ]
