"""Connection-bound adapter credentials; provider credentials stay in the adapter."""
import secrets
import hashlib
import hmac
from django.conf import settings
from django.utils.crypto import salted_hmac


def configured_secret(connection):
    if connection.secret_ref.startswith('managed:'):
        return salted_hmac('commerce.adapter.v1',
            f'{connection.pk}:{connection.location_id}:{connection.secret_ref}',
            algorithm='sha256').hexdigest()
    return getattr(settings, 'COMMERCE_ADAPTER_SECRETS', {}).get(connection.secret_ref)


def fingerprint(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def adapter_secret(connection):
    secret = configured_secret(connection)
    # Environment/key changes require explicit credential refresh. Never accept
    # unregistered material or bypass the database uniqueness constraint.
    if secret and connection.secret_fingerprint and hmac.compare_digest(fingerprint(secret), connection.secret_fingerprint):
        return secret
    return None


def rotate_credentials(connection):
    connection.secret_ref = 'managed:' + secrets.token_hex(32)
    connection.save(update_fields=['secret_ref'])
    return adapter_secret(connection)
