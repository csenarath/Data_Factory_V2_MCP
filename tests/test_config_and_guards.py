import json

import pytest

from adf_mcp.config import AuthMethod, AuthoringTarget, Environment, Profile, initial_profile_name, load_profiles
from adf_mcp.errors import AdfMcpError
from adf_mcp.guards import audit, authoring_mode, check_write

from .conftest import FakeAdf, FakeSession

PROFILES_YAML = """
default_profile: team-dev
profiles:
  team-dev:
    tenant_id: 11111111-1111-1111-1111-111111111111
    subscription_id: sub-dev
    resource_group: rg-data
    factory_name: adf-dev
    auth_method: browser
  team-prod:
    tenant_id: 11111111-1111-1111-1111-111111111111
    subscription_id: sub-prod
    resource_group: rg-data
    factory_name: adf-prod
    auth_method: service_principal_secret
    client_id: app-id
    environment: prod
"""

GIT_FACTORY = {
    "name": "adf-dev",
    "properties": {
        "repoConfiguration": {
            "type": "FactoryVSTSConfiguration",
            "repositoryName": "data-platform",
            "collaborationBranch": "main",
            "rootFolder": "/adf",
        }
    },
}


def test_load_profiles_names_and_defaults(isolated_home):
    (isolated_home / "profiles.yaml").write_text(PROFILES_YAML)
    profiles = load_profiles()
    assert set(profiles.profiles) == {"team-dev", "team-prod"}
    assert profiles.profiles["team-dev"].name == "team-dev"
    assert profiles.profiles["team-dev"].auth_method == AuthMethod.BROWSER
    assert profiles.profiles["team-prod"].environment == Environment.PROD
    assert initial_profile_name(profiles) == "team-dev"


def test_env_var_selects_profile_and_env_profile(isolated_home, monkeypatch):
    (isolated_home / "profiles.yaml").write_text(PROFILES_YAML)
    monkeypatch.setenv("ADF_MCP_PROFILE", "team-prod")
    assert initial_profile_name(load_profiles()) == "team-prod"

    monkeypatch.delenv("ADF_MCP_PROFILE")
    (isolated_home / "profiles.yaml").unlink()
    monkeypatch.setenv("ADF_MCP_SUBSCRIPTION_ID", "sub-x")
    monkeypatch.setenv("ADF_MCP_FACTORY_NAME", "adf-x")
    profiles = load_profiles()
    assert profiles.profiles["env"].factory_name == "adf-x"
    assert initial_profile_name(profiles) == "env"


def test_prod_and_read_only_block_writes(dev_profile):
    session = FakeSession(dev_profile, FakeAdf([]))
    check_write(session.target(), "op")  # dev: allowed

    for blocked in (Profile(name="p", environment="prod"), Profile(name="r", read_only=True)):
        blocked = blocked.model_copy(update={"subscription_id": "s", "resource_group": "rg", "factory_name": "f"})
        with pytest.raises(AdfMcpError) as exc:
            check_write(FakeSession(blocked, FakeAdf([])).target(), "op")
        assert exc.value.code == "write_blocked"


def test_authoring_mode_follows_git_configuration(dev_profile):
    git = FakeSession(dev_profile, FakeAdf([]), GIT_FACTORY)
    assert authoring_mode(git, git.target()) == "git"
    live = FakeSession(dev_profile, FakeAdf([]))
    assert authoring_mode(live, live.target()) == "live"

    forced_live = dev_profile.model_copy(update={"authoring_target": AuthoringTarget.LIVE})
    session = FakeSession(forced_live, FakeAdf([]), GIT_FACTORY)
    with pytest.raises(AdfMcpError) as exc:
        authoring_mode(session, session.target())
    assert exc.value.code == "live_write_on_git_factory"

    opted_in = forced_live.model_copy(update={"allow_live_writes_on_git_factory": True})
    session = FakeSession(opted_in, FakeAdf([]), GIT_FACTORY)
    assert authoring_mode(session, session.target()) == "live"


def test_target_overrides_and_missing_fields(dev_profile):
    session = FakeSession(dev_profile, FakeAdf([]))
    assert session.target(factory_name="other").factory_name == "other"
    session.overrides["resource_group"] = "rg2"
    assert session.target().resource_group == "rg2"

    bare = FakeSession(Profile(name="bare"), FakeAdf([]))
    with pytest.raises(AdfMcpError) as exc:
        bare.target()
    assert exc.value.code == "target_incomplete"


def test_real_client_is_built_and_cached_without_deadlock(dev_profile):
    from adf_mcp.session import Session
    from adf_mcp.config import ProfilesFile

    session = Session(ProfilesFile(default_profile="dev", profiles={"dev": dev_profile}))
    client = session.client("s")  # builds an AzureCliCredential; no network until a call is made
    assert session.client("s") is client
    session.forget(dev_profile)
    assert session.client("s") is not client


def test_audit_appends_jsonl(isolated_home, dev_profile):
    target = FakeSession(dev_profile, FakeAdf([])).target()
    audit(target, "run_cancel", "pipelineRun/1")
    audit(target, "run_cancel", "pipelineRun/2")
    lines = (isolated_home / "audit.jsonl").read_text().splitlines()
    assert [json.loads(line)["resource"] for line in lines] == ["pipelineRun/1", "pipelineRun/2"]
    assert json.loads(lines[0])["factory_name"] == "adf-dev"
