"""Git-mode authoring against a real temporary repo with a bare 'origin'."""

import json
import subprocess

import pytest

from adf_mcp.config import GitSettings
from adf_mcp.session import set_session

from .conftest import FakeAdf, FakeSession

REPO_CONFIG = {
    "type": "FactoryVSTSConfiguration",
    "accountName": "acct",
    "projectName": "proj",
    "repositoryName": "adf-mcp-test",
    "collaborationBranch": "main",
    "rootFolder": "/",
}
WAIT = {"activities": [{"name": "w", "type": "Wait", "typeProperties": {"waitTimeInSeconds": 1}}]}


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path, dev_profile):
    origin = tmp_path / "adf-mcp-test.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(origin), str(clone)], check=True, capture_output=True)
    git(clone, "config", "user.name", "test")
    git(clone, "config", "user.email", "test@example.com")
    git(clone, "switch", "-c", "main")
    (clone / "pipeline").mkdir()
    (clone / "pipeline" / "existing.json").write_text(json.dumps({"name": "existing", "properties": WAIT}, indent="\t"))
    git(clone, "add", ".")
    git(clone, "commit", "-m", "init")
    git(clone, "push", "-u", "origin", "main")

    profile = dev_profile.model_copy(update={"git": GitSettings(repo_path=str(clone))})
    set_session(FakeSession(profile, FakeAdf([]), {"name": "adf-dev", "properties": {"repoConfiguration": REPO_CONFIG}}))
    return clone


def test_dry_run_writes_nothing(repo, call):
    result = call("adf_resource_upsert", kind="pipeline", name="p_new", properties=WAIT)
    assert result["mode"] == "git" and result["action"] == "create" and result["dry_run"] is True
    assert result["target"] == "git branch adf-mcp/pipeline-p_new"
    assert "+" in result["diff"]
    assert git(repo, "branch", "--list").split() == ["*", "main"]


def test_apply_commits_to_feature_branch_not_main(repo, call):
    result = call("adf_resource_upsert", kind="pipeline", name="p_new", properties=WAIT, dry_run=False)
    assert result["branch"] == "adf-mcp/pipeline-p_new" and result["branch_created"] is True and result["commit"]
    text = (repo / "pipeline" / "p_new.json").read_text()
    assert text.startswith('{\n\t"name": "p_new"')
    assert "p_new.json" not in git(repo, "ls-tree", "--name-only", "main:pipeline")

    # A second change lands on the same (current) feature branch.
    second = call("adf_resource_upsert", kind="dataset", name="ds1", properties={"type": "Json", "linkedServiceName": {"referenceName": "ls1", "type": "LinkedServiceReference"}}, dry_run=False, force=True)
    assert second["branch"] == "adf-mcp/pipeline-p_new"
    assert second["missing_references"] == ["linkedService:ls1"]

    status = call("adf_git_status")
    assert {(c["kind"], c["name"]) for c in status["changed_resources"]} == {("pipeline", "p_new"), ("dataset", "ds1")}
    assert status["pull_request_url"].startswith("https://dev.azure.com/acct/proj/_git/adf-mcp-test/pullrequestcreate")

    pushed = call("adf_git_push")
    assert pushed["pushed"] == "adf-mcp/pipeline-p_new"
    assert "adf-mcp/pipeline-p_new" in git(repo, "ls-remote", "--heads", "origin")


def test_missing_reference_blocks_without_force(repo, call_error):
    bad = {"activities": [{"name": "run", "type": "ExecutePipeline", "typeProperties": {"pipeline": {"referenceName": "ghost", "type": "PipelineReference"}}}]}
    error = call_error("adf_resource_upsert", kind="pipeline", name="p_bad", properties=bad, dry_run=False)
    assert error["code"] == "validation_failed"


def test_refuses_collaboration_branch(repo, call_error):
    error = call_error("adf_resource_upsert", kind="pipeline", name="p", properties=WAIT, dry_run=False, branch="main")
    assert error["code"] == "git_error"


def test_pipeline_edit_and_unchanged(repo, call):
    edited = call(
        "adf_pipeline_edit",
        pipeline="existing",
        upsert_activities=[{"name": "w2", "type": "Wait", "dependsOn": [{"activity": "w", "dependencyConditions": ["Succeeded"]}], "typeProperties": {"waitTimeInSeconds": 2}}],
        set_parameters={"p": {"type": "string", "defaultValue": "x"}},
        dry_run=False,
    )
    assert edited["action"] == "update"
    saved = json.loads((repo / "pipeline" / "existing.json").read_text())["properties"]
    assert [a["name"] for a in saved["activities"]] == ["w", "w2"]

    removed = call("adf_pipeline_edit", pipeline="existing", remove_activities=["w"], dry_run=False)
    saved = json.loads((repo / "pipeline" / "existing.json").read_text())["properties"]
    assert removed["action"] == "update" and saved["activities"][0].get("dependsOn") == []

    same = call("adf_resource_upsert", kind="pipeline", name="existing", properties=saved)
    assert same["action"] == "no_change"


def test_delete_needs_token_from_dry_run(repo, call, call_error):
    preview = call("adf_resource_delete", kind="pipeline", name="existing")
    assert preview["referenced_by"] == [] and preview["confirm_token"]
    assert call_error("adf_resource_delete", kind="pipeline", name="existing", dry_run=False)["code"] == "confirmation_required"
    done = call("adf_resource_delete", kind="pipeline", name="existing", dry_run=False, confirm_token=preview["confirm_token"])
    assert done["deleted"] is True and done["commit"]
    assert not (repo / "pipeline" / "existing.json").exists()


def test_validate_and_graph_read_git(repo, call):
    call("adf_resource_upsert", kind="trigger", name="t1", dry_run=False, properties={
        "type": "ScheduleTrigger",
        "pipelines": [{"pipelineReference": {"referenceName": "existing", "type": "PipelineReference"}}],
        "typeProperties": {"recurrence": {"frequency": "Day", "interval": 1}},
    })
    graph = call("adf_dependency_graph", kind="pipeline", name="existing")
    assert "trigger:t1 -> pipeline:existing" in graph["edges"]
    assert graph["source"].startswith("git:adf-mcp/trigger-t1")
    validation = call("adf_validate")
    assert validation["valid"] is True
