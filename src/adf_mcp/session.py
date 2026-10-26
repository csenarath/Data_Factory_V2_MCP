"""Per-process session state.

In stdio mode every Claude session spawns its own server process, so one module-level
``Session`` is exactly one Claude session: switching profile here never affects another
session.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from azure.core.credentials import TokenCredential
from azure.identity import DeviceCodeCredential
from azure.mgmt.datafactory import DataFactoryManagementClient

from .auth import ARM_SCOPE, DeviceCodeFlow, build_credential, credential_key, delete_record, save_record
from .config import AuthMethod, Profile, ProfilesFile, initial_profile_name, load_profiles
from .errors import AdfMcpError


@dataclass(frozen=True)
class Target:
    profile: Profile
    subscription_id: str
    resource_group: str
    factory_name: str

    def describe(self) -> dict:
        return {
            "profile": self.profile.name,
            "tenant_id": self.profile.tenant_id,
            "subscription_id": self.subscription_id,
            "resource_group": self.resource_group,
            "factory_name": self.factory_name,
            "environment": self.profile.environment,
        }


class Session:
    def __init__(self, profiles: ProfilesFile | None = None):
        self.profiles = profiles or load_profiles()
        self.active_name = initial_profile_name(self.profiles)
        # Ad hoc target overrides set via adf_context_set; cleared on profile switch.
        self.overrides: dict[str, str] = {}
        self._credentials: dict[str, TokenCredential] = {}
        self._clients: dict[tuple[str, str], DataFactoryManagementClient] = {}
        self._factories: dict[tuple[str, str, str], dict] = {}
        self.device_flows: dict[str, DeviceCodeFlow] = {}
        # Misc per-session objects (Git repos, ...), keyed by the module that owns them.
        self.cache: dict = {}
        # Re-entrant: client() takes the lock and then calls credential(), which takes it again.
        self._lock = threading.RLock()

    # -- profiles -------------------------------------------------------------------

    def reload(self) -> None:
        self.profiles = load_profiles()
        if self.active_name not in self.profiles.profiles:
            self.active_name = initial_profile_name(self.profiles)

    @property
    def profile(self) -> Profile:
        if not self.active_name or self.active_name not in self.profiles.profiles:
            names = ", ".join(self.profiles.profiles) or "none configured"
            raise AdfMcpError(
                "no_profile",
                "No active profile.",
                f"Call adf_profile_use with one of: {names}. Profiles are defined in ~/.adf-mcp/profiles.yaml.",
            )
        return self.profiles.profiles[self.active_name]

    def use(self, name: str) -> Profile:
        if name not in self.profiles.profiles:
            self.reload()
        if name not in self.profiles.profiles:
            raise AdfMcpError("unknown_profile", f"Profile '{name}' not found.", "Call adf_profiles_list.")
        self.active_name = name
        self.overrides = {}
        return self.profile

    def target(
        self,
        factory_name: str | None = None,
        resource_group: str | None = None,
        subscription_id: str | None = None,
    ) -> Target:
        profile = self.profile
        sub = subscription_id or self.overrides.get("subscription_id") or profile.subscription_id
        rg = resource_group or self.overrides.get("resource_group") or profile.resource_group
        factory = factory_name or self.overrides.get("factory_name") or profile.factory_name
        missing = [n for n, v in (("subscription_id", sub), ("resource_group", rg), ("factory_name", factory)) if not v]
        if missing:
            raise AdfMcpError(
                "target_incomplete",
                f"No {', '.join(missing)} for the active profile '{profile.name}'.",
                "Pass them to the tool, or call adf_factories_list then adf_context_set.",
            )
        return Target(profile, sub, rg, factory)  # type: ignore[arg-type]

    # -- credentials & clients -------------------------------------------------------

    def credential(self, profile: Profile | None = None) -> TokenCredential:
        profile = profile or self.profile
        key = credential_key(profile)
        with self._lock:
            if key not in self._credentials:
                self._credentials[key] = build_credential(profile, self._device_code_callback(key))
            return self._credentials[key]

    def client(self, subscription_id: str, profile: Profile | None = None) -> DataFactoryManagementClient:
        profile = profile or self.profile
        key = (credential_key(profile), subscription_id)
        with self._lock:
            if key not in self._clients:
                self._clients[key] = DataFactoryManagementClient(self.credential(profile), subscription_id)
            return self._clients[key]

    def adf(self, target: Target) -> DataFactoryManagementClient:
        return self.client(target.subscription_id, target.profile)

    def factory(self, target: Target, refresh: bool = False) -> dict:
        """The factory resource as REST JSON, cached for the session (used for Git mode checks)."""
        key = (target.subscription_id, target.resource_group, target.factory_name)
        if refresh or key not in self._factories:
            self._factories[key] = self.adf(target).factories.get(target.resource_group, target.factory_name).as_dict()
        return self._factories[key]

    def forget(self, profile: Profile) -> None:
        key = credential_key(profile)
        with self._lock:
            self._credentials.pop(key, None)
            for client_key in [k for k in self._clients if k[0] == key]:
                self._clients.pop(client_key)
            self.device_flows.pop(key, None)

    # -- sign-in ---------------------------------------------------------------------

    def _device_code_callback(self, key: str):
        def callback(verification_uri: str, user_code: str, expires_on) -> None:
            flow = self.device_flows.setdefault(key, DeviceCodeFlow())
            flow.message = f"Open {verification_uri} and enter code {user_code} (expires {expires_on:%H:%M:%S} UTC)."
            flow.prompted.set()

        return callback

    def login(self, profile: Profile, wait_seconds: float = 30) -> dict:
        """Sign in for user auth methods; returns a status dict for the tool to relay."""
        credential = self.credential(profile)
        key = credential_key(profile)

        if profile.auth_method == AuthMethod.DEVICE_CODE:
            flow = self.device_flows.get(key)
            if flow and not flow.done and flow.error is None:
                return {"status": "pending", "instructions": flow.message}
            flow = self.device_flows[key] = DeviceCodeFlow()

            def run() -> None:
                try:
                    assert isinstance(credential, DeviceCodeCredential)
                    save_record(profile, credential.authenticate(scopes=[ARM_SCOPE]))
                    flow.done = True
                except Exception as exc:  # surfaced through the next adf_login / adf_context call
                    flow.error = str(exc).splitlines()[0]
                finally:
                    flow.prompted.set()

            threading.Thread(target=run, daemon=True).start()
            flow.prompted.wait(wait_seconds)
            if flow.error:
                raise AdfMcpError("auth_failed", flow.error)
            return {
                "status": "pending",
                "instructions": flow.message,
                "next": "Show the instructions to the user; after they finish, call adf_context to confirm.",
            }

        if profile.auth_method == AuthMethod.BROWSER:
            record = credential.authenticate(scopes=[ARM_SCOPE])  # type: ignore[attr-defined]
            save_record(profile, record)
            return {"status": "signed_in", "user": record.username, "tenant_id": record.tenant_id}

        # Non-interactive methods: nothing to prompt, just prove the credential works.
        credential.get_token(ARM_SCOPE)
        return {"status": "signed_in"}

    def logout(self, profile: Profile) -> dict:
        removed = delete_record(profile)
        self.forget(profile)
        return {"status": "signed_out", "removed_saved_sign_in": removed}


_session: Session | None = None


def get_session() -> Session:
    global _session
    if _session is None:
        _session = Session()
    return _session


def set_session(session: Session | None) -> None:
    """Replace the process session (tests)."""
    global _session
    _session = session
