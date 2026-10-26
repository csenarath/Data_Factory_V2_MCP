"""Shared fixtures: an isolated ADF_MCP_HOME and a Session backed by a fake ADF client."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from azure.mgmt.datafactory import models

from adf_mcp.config import Profile, ProfilesFile
from adf_mcp.server import build_server
from adf_mcp.session import Session, set_session


def page(items: list[dict], model=models.PipelineRun, token: str | None = None):
    return SimpleNamespace(value=[model(item) for item in items], continuation_token=token)


class FakeAdf:
    """Minimal stand-in for DataFactoryManagementClient, keyed by run id."""

    def __init__(self, runs: list[dict], activities: dict[str, list[dict]] | None = None):
        self.runs = {r["runId"]: r for r in runs}
        self.activities = activities or {}
        self.cancelled: list[tuple[str, bool]] = []
        self.queries: list[dict] = []
        self.pipeline_runs = SimpleNamespace(
            get=lambda rg, f, run_id: models.PipelineRun(self.runs[run_id]),
            query_by_factory=self._query_runs,
            cancel=lambda rg, f, run_id, is_recursive=None: self.cancelled.append((run_id, is_recursive)),
        )
        self.activity_runs = SimpleNamespace(query_by_pipeline_run=self._query_activities)

    def _matches(self, item: dict, body: dict, field_map: dict[str, str]) -> bool:
        for f in body.get("filters") or []:
            field = field_map.get(f["operand"])
            if field and item.get(field) not in f["values"]:
                return False
        return True

    def _query_runs(self, rg, factory, body):
        self.queries.append(body)
        items = [r for r in self.runs.values() if self._matches(r, body, {"Status": "status", "PipelineName": "pipelineName"})]
        items.sort(key=lambda r: r.get("runStart") or "", reverse=True)
        return page(items)

    def _query_activities(self, rg, factory, run_id, body):
        fields = {"Status": "status", "ActivityName": "activityName", "ActivityType": "activityType"}
        items = [a for a in self.activities.get(run_id, []) if self._matches(a, body, fields)]
        return page(items, models.ActivityRun)


class FakeSession(Session):
    def __init__(self, profile: Profile, adf: FakeAdf, factory: dict | None = None):
        super().__init__(ProfilesFile(default_profile=profile.name, profiles={profile.name: profile}))
        self.active_name = profile.name
        self.fake_adf = adf
        self.fake_factory = factory or {"name": profile.factory_name, "properties": {}}

    def adf(self, target):
        return self.fake_adf

    def factory(self, target, refresh=False):
        return self.fake_factory


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ADF_MCP_HOME", str(tmp_path))
    monkeypatch.setenv("ADF_MCP_PROFILES", str(tmp_path / "profiles.yaml"))
    for var in ("ADF_MCP_PROFILE", "ADF_MCP_FACTORY_NAME", "ADF_MCP_SUBSCRIPTION_ID"):
        monkeypatch.delenv(var, raising=False)
    yield tmp_path
    set_session(None)


@pytest.fixture
def dev_profile() -> Profile:
    return Profile(name="dev", tenant_id="t", subscription_id="s", resource_group="rg", factory_name="adf-dev")


@pytest.fixture
def call():
    server = build_server()

    def _call(tool: str, /, **arguments):
        result = asyncio.run(server.call_tool(tool, arguments))
        if result.is_error:
            raise AssertionError(result.content[0].text)
        if result.structured_content is not None:
            return result.structured_content
        return json.loads(result.content[0].text)

    return _call


def _error_body(text: str) -> dict:
    # The SDK prefixes tool errors with "Error executing tool <name>: ".
    return json.loads(text[text.index("{") :])


@pytest.fixture
def call_error():
    server = build_server()

    def _call(tool: str, /, **arguments) -> dict:
        from mcp.server.mcpserver.exceptions import ToolError

        try:
            result = asyncio.run(server.call_tool(tool, arguments))
        except ToolError as exc:
            return _error_body(str(exc))
        assert result.is_error, "expected an error"
        return _error_body(result.content[0].text)

    return _call
