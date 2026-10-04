"""Register existing unique credentials; ambiguous/missing credentials stay disabled."""
import hashlib
from django.conf import settings
from django.db import migrations
from django.utils.crypto import salted_hmac


def register(apps, schema_editor):
    Connection = apps.get_model('commerce', 'Connection')
    grouped = {}
    for connection in Connection.objects.using(schema_editor.connection.alias).all():
        if connection.secret_ref.startswith('managed:'):
            secret = salted_hmac('commerce.adapter.v1',
                f'{connection.pk}:{connection.location_id}:{connection.secret_ref}', algorithm='sha256').hexdigest()
        else:
            secret = getattr(settings, 'COMMERCE_ADAPTER_SECRETS', {}).get(connection.secret_ref)
        key = hashlib.sha256(secret.encode()).hexdigest() if secret else None
        grouped.setdefault(key, []).append(connection.pk)
    for key, ids in grouped.items():
        rows = Connection.objects.using(schema_editor.connection.alias).filter(pk__in=ids)
        if key and len(ids) == 1:
            rows.update(secret_fingerprint=key)
        else:
            rows.update(active=False, secret_fingerprint=None)


class Migration(migrations.Migration):
    dependencies = [('commerce', '0003_connection_secret_fingerprint')]
    operations = [migrations.RunPython(register, migrations.RunPython.noop)]
