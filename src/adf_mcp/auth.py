"""Credential construction for every supported auth method.

User methods (browser, device code) never prompt implicitly: they are built with
``disable_automatic_authentication`` so a tool call fails fast with ``login_required``
and the user signs in explicitly through ``adf_login``. The resulting authentication
record and the MSAL token cache are persisted per tenant, so later sessions sign in
silently.
"""

from __future__ import annotations

import base64
import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

from azure.core.credentials import TokenCredential
from azure.identity import (
    AuthenticationRecord,
    AzureCliCredential,
    CertificateCredential,
    ClientSecretCredential,
    DeviceCodeCredential,
    InteractiveBrowserCredential,
    ManagedIdentityCredential,
    TokenCachePersistenceOptions,
    WorkloadIdentityCredential,
)

from .config import AuthMethod, Profile, home_dir
from .errors import AdfMcpError

ARM_SCOPE = "https://management.azure.com/.default"
CACHE_NAME = "adf-mcp"


def credential_key(profile: Profile) -> str:
    return f"{profile.auth_method}|{profile.tenant_id or 'organizations'}|{profile.client_id or ''}"


def _record_path(profile: Profile) -> Path:
    directory = home_dir() / "auth"
    directory.mkdir(exist_ok=True)
    safe = credential_key(profile).replace("|", "_").replace("/", "_")
    return directory / f"{safe}.json"


def load_record(profile: Profile) -> AuthenticationRecord | None:
    path = _record_path(profile)
    if not path.exists():
        return None
    return AuthenticationRecord.deserialize(path.read_text(encoding="utf-8"))


def save_record(profile: Profile, record: AuthenticationRecord) -> None:
    _record_path(profile).write_text(record.serialize(), encoding="utf-8")


def delete_record(profile: Profile) -> bool:
    path = _record_path(profile)
    if path.exists():
        path.unlink()
        return True
    return False


def _require(value: str | None, what: str, profile: Profile) -> str:
    if not value:
        raise AdfMcpError("config_error", f"Profile '{profile.name}' needs {what} for auth_method={profile.auth_method}.")
    return value


def build_credential(profile: Profile, device_code_callback=None) -> TokenCredential:
    method = profile.auth_method
    tenant = profile.tenant_id

    if method in (AuthMethod.BROWSER, AuthMethod.DEVICE_CODE):
        common = dict(
            tenant_id=tenant or "organizations",
            client_id=profile.client_id or None,
            cache_persistence_options=TokenCachePersistenceOptions(name=CACHE_NAME),
            authentication_record=load_record(profile),
            disable_automatic_authentication=True,
        )
        common = {k: v for k, v in common.items() if v is not None}
        if method == AuthMethod.BROWSER:
            return InteractiveBrowserCredential(**common)
        return DeviceCodeCredential(prompt_callback=device_code_callback, **common)

    if method == AuthMethod.AZURE_CLI:
        return AzureCliCredential(tenant_id=tenant) if tenant else AzureCliCredential()

    if method == AuthMethod.SP_SECRET:
        secret = os.environ.get(profile.client_secret_env)
        return ClientSecretCredential(
            _require(tenant, "tenant_id", profile),
            _require(profile.client_id, "client_id", profile),
            _require(secret, f"env var {profile.client_secret_env}", profile),
        )

    if method == AuthMethod.SP_CERTIFICATE:
        return CertificateCredential(
            _require(tenant, "tenant_id", profile),
            _require(profile.client_id, "client_id", profile),
            certificate_path=_require(profile.certificate_path, "certificate_path", profile),
            password=os.environ.get(profile.certificate_password_env),
        )

    if method == AuthMethod.MANAGED_IDENTITY:
        return ManagedIdentityCredential(client_id=profile.client_id) if profile.client_id else ManagedIdentityCredential()

    if method == AuthMethod.WORKLOAD_IDENTITY:
        kwargs = {"tenant_id": tenant, "client_id": profile.client_id}
        return WorkloadIdentityCredential(**{k: v for k, v in kwargs.items() if v})

    raise AdfMcpError("config_error", f"Unsupported auth_method {method}")


def token_identity(credential: TokenCredential) -> dict:
    """Fetch an ARM token and return non-secret identity claims from it (never the token)."""
    token = credential.get_token(ARM_SCOPE).token
    payload = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    return {
        "tenant_id": claims.get("tid"),
        "user": claims.get("upn") or claims.get("unique_name") or claims.get("preferred_username"),
        "app_id": claims.get("appid") or claims.get("azp"),
        "object_id": claims.get("oid"),
        "identity_type": "user" if claims.get("scp") else "application",
    }


@dataclass
class DeviceCodeFlow:
    """A device-code sign-in running in a background thread."""

    message: str | None = None
    done: bool = False
    error: str | None = None
    prompted: threading.Event = field(default_factory=threading.Event)
