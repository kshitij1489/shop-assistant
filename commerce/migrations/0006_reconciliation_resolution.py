from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('commerce', '0005_menusource')]

    operations = [
        migrations.AddField(model_name='reconciliationissue', name='resolved_by',
            field=models.CharField(blank=True, editable=False, max_length=254)),
        migrations.AddField(model_name='reconciliationissue', name='resolution_evidence',
            field=models.TextField(blank=True, editable=False)),
        migrations.AddField(model_name='reconciliationissue', name='resolution_note',
            field=models.TextField(blank=True, editable=False)),
    ]
