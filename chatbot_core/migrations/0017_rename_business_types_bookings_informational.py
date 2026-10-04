from django.db import migrations, models


def remap_business_types_forward(apps, schema_editor):
    TenantInfo = apps.get_model("chatbot_core", "TenantInfo")
    TenantInfo.objects.filter(business_type="studio").update(business_type="bookings")
    TenantInfo.objects.filter(business_type="solutions").update(business_type="informational")


def remap_business_types_reverse(apps, schema_editor):
    TenantInfo = apps.get_model("chatbot_core", "TenantInfo")
    TenantInfo.objects.filter(business_type="bookings").update(business_type="studio")
    TenantInfo.objects.filter(business_type="informational").update(business_type="solutions")


class Migration(migrations.Migration):

    dependencies = [
        ("chatbot_core", "0016_semanticcacheentry_faissvector_and_more"),
    ]

    operations = [
        migrations.RunPython(
            remap_business_types_forward,
            remap_business_types_reverse,
        ),
        migrations.AlterField(
            model_name="tenantinfo",
            name="business_type",
            field=models.CharField(
                choices=[
                    ("cafe", "Cafe"),
                    ("bookings", "Classes & bookings"),
                    ("retail", "Retail Store"),
                    ("informational", "Informational website"),
                ],
                default="cafe",
                max_length=20,
            ),
        ),
    ]
