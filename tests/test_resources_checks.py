from adf_mcp.checks import FactoryModel, pipeline_issues, trigger_issues
from adf_mcp.diagnose import classify
from adf_mcp.resources import KINDS, git_document, kind_of, normalize, references, same_definition


class DictStore:
    def __init__(self, data: dict[str, dict[str, dict]]):
        self.data = data
        self.label = "dict"

    def list_definitions(self, kind):
        return self.data.get(kind.name, {})

    def get(self, kind, name):
        return self.data.get(kind.name, {}).get(name)


def act(name, type_="Wait", depends=(), **type_properties):
    return {
        "name": name,
        "type": type_,
        "dependsOn": [{"activity": d, "dependencyConditions": ["Succeeded"]} for d in depends],
        "typeProperties": type_properties,
    }


def test_kind_aliases():
    assert kind_of("linked_service").name == "linkedService"
    assert kind_of("Pipelines").name == "pipeline"
    assert kind_of("ir").name == "integrationRuntime"


def test_references_walks_nested_and_skips_expressions():
    pipeline = {
        "activities": [
            {
                "name": "loop",
                "type": "ForEach",
                "typeProperties": {
                    "activities": [
                        {"name": "copy", "type": "Copy", "inputs": [{"referenceName": "ds_in", "type": "DatasetReference"}]},
                        {"name": "dyn", "type": "Copy", "inputs": [{"referenceName": "@item().ds", "type": "DatasetReference"}]},
                    ]
                },
            }
        ]
    }
    assert references(pipeline) == {("dataset", "ds_in")}


def test_normalize_treats_studio_and_rest_forms_as_equal():
    studio = {"activities": [{"name": "a", "dependsOn": [], "userProperties": [], "policy": {"secureInput": False},
                              "typeProperties": {"value": {"value": "@x", "type": "Expression"}}}]}
    rest = {"activities": [{"name": "a", "typeProperties": {"value": "@x"}}]}
    assert same_definition(studio, rest)
    assert normalize(studio) == rest
    assert same_definition({**rest, "lastPublishTime": "2026-10-26T20:03:44Z"}, rest)


def test_git_document_matches_adf_format():
    text = git_document(KINDS["pipeline"], "p", {"activities": []})
    assert text.startswith('{\n\t"name": "p",') and text.endswith("}")
    assert '"type": "Microsoft.DataFactory/factories/pipelines"' in text


def test_pipeline_issues():
    pipeline = {
        "parameters": {"p1": {"type": "string"}},
        "variables": {"v1": {"type": "String"}},
        "activities": [
            act("a"),
            act("a"),
            act("b", depends=["ghost"]),
            act("c", "SetVariable", variableName="nope", value="@concat(pipeline().parameters.p2, variables('v1'), activity('zzz').output)"),
            act("x", depends=["y"]),
            act("y", depends=["x"]),
        ],
    }
    messages = " | ".join(i["message"] for i in pipeline_issues("p", pipeline))
    assert "'a' is used more than once" in messages
    assert "unknown activity 'ghost'" in messages
    assert "undeclared variable 'nope'" in messages
    assert "undeclared parameter 'p2'" in messages
    assert "unknown activity 'zzz'" in messages
    assert "cycle" in messages
    assert "p1" not in messages and "'v1'" not in messages


def test_trigger_must_supply_required_parameters():
    pipelines = {"p": {"parameters": {"need": {"type": "string"}, "opt": {"type": "string", "defaultValue": "x"}}}}
    trigger = {"pipelines": [{"pipelineReference": {"referenceName": "p", "type": "PipelineReference"}, "parameters": {}}]}
    issues = trigger_issues("t", trigger, pipelines)
    assert len(issues) == 1 and "'need'" in issues[0]["message"]


def test_factory_model_missing_unused_and_subgraph():
    store = DictStore(
        {
            "pipeline": {"p": {"activities": [{"name": "c", "type": "Copy", "inputs": [{"referenceName": "ds", "type": "DatasetReference"}]}]}},
            "dataset": {"ds": {"linkedServiceName": {"referenceName": "ls_missing", "type": "LinkedServiceReference"}}},
            "linkedService": {
                "ls_unused": {"type": "AzureSqlDatabase", "typeProperties": {"password": {"type": "SecureString", "value": "x"}}}
            },
            "trigger": {"t": {"pipelines": [{"pipelineReference": {"referenceName": "p", "type": "PipelineReference"}}]}},
        }
    )
    model = FactoryModel.load(store)
    issues = model.issues()
    text = " | ".join(f"{i['severity']} {i['resource']} {i['message']}" for i in issues)
    assert "error dataset:ds References missing linkedService 'ls_missing'" in text
    assert "info linkedService:ls_unused Not referenced" in text
    assert "warning linkedService:ls_unused" in text
    assert "pipeline:p No trigger" not in text  # the trigger runs it
    assert issues[0]["severity"] == "error"
    up = model.subgraph(("dataset", "ds"), "up")
    assert (("pipeline", "p"), ("dataset", "ds")) in up and (("trigger", "t"), ("pipeline", "p")) in up
    down = model.subgraph(("pipeline", "p"), "down")
    assert (("dataset", "ds"), ("linkedService", "ls_missing")) in down


def test_classify_errors():
    assert classify("Fail", "X1", "boom")[0] == "intentional_fail_activity"
    assert classify("Copy", "2200", "The remote name could not be resolved: 'x.dfs.core.windows.net'")[0] == "dns_or_network"
    assert classify("Copy", "2200", "Login failed for user 'etl'")[0] == "authentication"
    assert classify("Copy", "2200", "This request is not authorized to perform this operation. 403")[0] == "permission"
    assert classify("Lookup", "2100", "Invalid object name 'dbo.x'")[0] == "not_found"
    assert classify("Copy", "9999", "something odd")[0] == "unclassified"


def test_debug_copies_include_changed_dependencies_and_their_dependents():
    from adf_mcp.tempdeploy import debug_copies as _debug_copies, rewrite as _rewrite

    ls = {"type": "AzureBlobFS", "typeProperties": {"url": "https://new/"}}
    ds = {"type": "Parquet", "linkedServiceName": {"referenceName": "ls", "type": "LinkedServiceReference"}}
    pl = {"activities": [
        {"name": "meta", "type": "GetMetadata", "typeProperties": {"dataset": {"referenceName": "ds", "type": "DatasetReference"}}},
        {"name": "child", "type": "ExecutePipeline", "typeProperties": {"pipeline": {"referenceName": "same", "type": "PipelineReference"}}},
    ]}
    same = {"activities": [{"name": "w", "type": "Wait"}]}
    git = DictStore({"pipeline": {"p": pl, "same": same}, "dataset": {"ds": ds}, "linkedService": {"ls": ls}})
    # Live: ls has an old URL, ds is identical to Git, child pipeline identical, p missing.
    live = DictStore({"pipeline": {"same": same}, "dataset": {"ds": ds}, "linkedService": {"ls": {**ls, "typeProperties": {"url": "https://old/"}}}})
    order, unresolved = _debug_copies(git, live, ("pipeline", "p"))
    # ds is unchanged but must be copied to point at the copied ls; the child pipeline is untouched.
    assert order == [("linkedService", "ls"), ("dataset", "ds"), ("pipeline", "p")]
    assert unresolved == []

    renames = {r: f"tmp_{r[1]}" for r in order}
    rewritten = _rewrite(pl, renames)
    assert rewritten["activities"][0]["typeProperties"]["dataset"]["referenceName"] == "tmp_ds"
    assert rewritten["activities"][1]["typeProperties"]["pipeline"]["referenceName"] == "same"


def test_trigger_start_time_must_match_time_zone():
    def trig(start, zone):
        return {"typeProperties": {"recurrence": {"frequency": "Day", "interval": 1, "startTime": start, "timeZone": zone}}}

    assert trigger_issues("t", trig("2030-01-01T02:00:00", "UTC"), {})
    assert not trigger_issues("t", trig("2030-01-01T02:00:00Z", "UTC"), {})
    assert trigger_issues("t", trig("2030-01-01T02:00:00Z", "Sri Lanka Standard Time"), {})
    assert not trigger_issues("t", trig("2030-01-01T02:00:00", "Sri Lanka Standard Time"), {})
