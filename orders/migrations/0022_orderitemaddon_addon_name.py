from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('orders', '0021_remove_menuitemvariant_uniq_variant_label_per_item_ci_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='orderitemaddon',
            name='addon_name',
            field=models.CharField(blank=True, default='', editable=False, max_length=255),
        ),
    ]
