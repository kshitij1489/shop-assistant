from django.db import migrations, models
from django.db.models.functions import Lower


class Migration(migrations.Migration):
    dependencies = [("orders", "0015_remove_chatsession_uniq_session_per_tenant")]

    operations = [
        migrations.RemoveConstraint(model_name="menucategory", name="menucategory_name_allowed"),
        migrations.AddField(model_name="menucategory", name="sort_order", field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="menucategory", name="is_active", field=models.BooleanField(default=True)),
        migrations.AlterModelOptions(name="menucategory", options={"ordering": ["sort_order", "name", "pk"]}),
        migrations.AlterField(model_name="menuitemvariant", name="size", field=models.CharField(max_length=50)),
        migrations.AddField(model_name="menuitemvariant", name="sort_order", field=models.PositiveIntegerField(default=0)),
        migrations.AlterModelOptions(name="menuitemvariant", options={"ordering": ["sort_order", "size", "pk"]}),
        migrations.AddConstraint(
            model_name="menuitemvariant",
            constraint=models.UniqueConstraint(Lower("size"), "menu_item", name="uniq_variant_label_per_item_ci"),
        ),
    ]
