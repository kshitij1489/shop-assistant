"""Drop legacy tables from the removed core app."""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("chatbot_core", "0016_semanticcacheentry_faissvector_and_more"),
    ]

    operations = [
        migrations.RunSQL(
            sql=[
                "DROP TABLE IF EXISTS core_pagevisit CASCADE;",
                "DROP TABLE IF EXISTS core_contactmessage CASCADE;",
                "DELETE FROM django_migrations WHERE app = 'core';",
            ],
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
