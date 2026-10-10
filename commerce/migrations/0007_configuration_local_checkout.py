from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('commerce', '0006_reconciliation_resolution')]
    operations = [migrations.AddField(
        model_name='configuration', name='local_checkout',
        field=models.BooleanField(default=False),
    )]
