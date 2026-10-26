"""Profile configuration.

A profile names one ADF target (tenant + subscription + resource group + factory) and how
to authenticate to it. Profiles live in ``$ADF_MCP_HOME/profiles.yaml`` (default
``~/.adf-mcp``), or in the file named by ``ADF_MCP_PROFILES``. A profile can also be built
purely from ``ADF_MCP_*`` env vars; it is registered under the name ``env``.
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class AuthMethod(StrEnum):
    BROWSER = "browser"
    DEVICE_CODE = "device_code"
    AZURE_CLI = "azure_cli"
    SP_SECRET = "service_principal_secret"
    SP_CERTIFICATE = "service_principal_certificate"
    MANAGED_IDENTITY = "managed_identity"
    WORKLOAD_IDENTITY = "workload_identity"


USER_AUTH_METHODS = {AuthMethod.BROWSER, AuthMethod.DEVICE_CODE}


class AuthoringTarget(StrEnum):
    AUTO = "auto"  # git if the factory has a repo configuration, otherwise live
    GIT = "git"
    LIVE = "live"


class Environment(StrEnum):
    DEV = "dev"
    TEST = "test"
    PROD = "prod"


class GitSettings(BaseModel):
    repo_path: str | None = Field(None, description="Local clone of the factory's Git repo")
    root_folder: str | None = Field(None, description="ADF root folder in the repo; defaults to the factory's rootFolder")
    branch_prefix: str = "adf-mcp/"


class Profile(BaseModel):
    name: str = ""
    tenant_id: str | None = None
    subscription_id: str | None = None
    resource_group: str | None = None
    factory_name: str | None = None
    auth_method: AuthMethod = AuthMethod.AZURE_CLI
    client_id: str | None = None
    client_secret_env: str = "AZURE_CLIENT_SECRET"
    certificate_path: str | None = None
    certificate_password_env: str = "AZURE_CLIENT_CERTIFICATE_PASSWORD"
    environment: Environment = Environment.DEV
    read_only: bool = False
    allow_prod_writes: bool = False
    authoring_target: AuthoringTarget = AuthoringTarget.AUTO
    allow_live_writes_on_git_factory: bool = False
    git: GitSettings = Field(default_factory=GitSettings)

    @property
    def writes_blocked_reason(self) -> str | None:
        if self.read_only:
            return f"profile '{self.name}' is read_only"
        if self.environment == Environment.PROD and not self.allow_prod_writes:
            return f"profile '{self.name}' is prod and allow_prod_writes is false"
        return None


class ProfilesFile(BaseModel):
    default_profile: str | None = None
    profiles: dict[str, Profile] = Field(default_factory=dict)


def home_dir() -> Path:
    path = Path(os.environ.get("ADF_MCP_HOME", Path.home() / ".adf-mcp"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def profiles_path() -> Path:
    return Path(os.environ.get("ADF_MCP_PROFILES", home_dir() / "profiles.yaml"))


def _env_profile() -> Profile | None:
    env = {k: v for k, v in os.environ.items() if k.startswith("ADF_MCP_") and v}
    if "ADF_MCP_FACTORY_NAME" not in env and "ADF_MCP_SUBSCRIPTION_ID" not in env:
        return None
    fields = {
        "tenant_id": "ADF_MCP_TENANT_ID",
        "subscription_id": "ADF_MCP_SUBSCRIPTION_ID",
        "resource_group": "ADF_MCP_RESOURCE_GROUP",
        "factory_name": "ADF_MCP_FACTORY_NAME",
        "auth_method": "ADF_MCP_AUTH_METHOD",
        "client_id": "ADF_MCP_CLIENT_ID",
        "environment": "ADF_MCP_ENVIRONMENT",
        "read_only": "ADF_MCP_READ_ONLY",
    }
    data = {field: env[var] for field, var in fields.items() if var in env}
    return Profile(name="env", **data)


def load_profiles(path: Path | None = None) -> ProfilesFile:
    path = path or profiles_path()
    loaded = ProfilesFile()
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        loaded = ProfilesFile.model_validate(raw)
    for name, profile in loaded.profiles.items():
        profile.name = name
    env_profile = _env_profile()
    if env_profile:
        loaded.profiles["env"] = env_profile
    return loaded


def initial_profile_name(profiles: ProfilesFile) -> str | None:
    """Profile a new session starts on: ADF_MCP_PROFILE, then default_profile, then the only one."""
    chosen = os.environ.get("ADF_MCP_PROFILE") or profiles.default_profile
    if chosen:
        return chosen
    if "env" in profiles.profiles:
        return "env"
    if len(profiles.profiles) == 1:
        return next(iter(profiles.profiles))
    return None
