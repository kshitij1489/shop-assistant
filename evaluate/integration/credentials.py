"""Lease-owned website credentials. The API key never enters a serializable artifact."""
from __future__ import annotations

from evaluate.contracts.interfaces import Blocked, Lease
from evaluate.runner.ports import WebsiteCredential


class LeaseCredentials:
    """`CredentialResolver` over the provisioner's private binding."""

    def __init__(self, provisioner) -> None:
        self.provisioner = provisioner

    def resolve(self, lease: Lease) -> WebsiteCredential:
        tenant = self.provisioner.binding(lease)["tenant"]
        if not tenant.slug or not tenant.api_key:
            raise Blocked("Owned tenant has no website credential")
        return WebsiteCredential(tenant_slug=tenant.slug, api_key=tenant.api_key)

    def session_key(self, lease: Lease) -> str:
        key = self.provisioner.binding(lease)["browser_session"]
        if not isinstance(key, str) or not key:
            raise Blocked("Owned browser session is missing")
        return key
