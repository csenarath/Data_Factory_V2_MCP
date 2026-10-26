# CLAUDE.md — ADF MCP Server

Guidance for Claude Code when working in this repository.

## Objective

An MCP server for **classic Azure Data Factory (ADF v2)**. It must:

- Let each Claude session sign in to a **different tenant / subscription / factory**, independently of other sessions.
- Support **user (interactive)** and **machine (non-interactive)** authentication.
- Cover the ADF lifecycle: monitoring, end-to-end checks, pipeline authoring, triggers, debugging,
  linked services, datasets, and admin setup (integration runtimes, managed VNet / private endpoints,
  global parameters, credentials, Git configuration).

Out of scope: **Microsoft Fabric** Data Factory (different API, `api.fabric.microsoft.com`), Synapse pipelines.

## Decisions (agreed 2026-10-27)

| Topic | Decision |
|---|---|
| Stack | Python 3.12, official `mcp` SDK (v2: FastMCP is now `MCPServer`, `from mcp.server.mcpserver import MCPServer`), `azure-identity`, `azure-mgmt-datafactory` (v10) |
| Hosting | **Local only**, stdio. One server process per Claude session. No remote / HTTP / OBO. |
| Git | Target factories are **Git-connected**. Definition changes go to Git (a local clone, on a feature branch); live writes to Git-connected factories are refused unless the profile opts in. |
| v1 scope | context + auth, monitor, end-to-end health, debug, authoring (pipelines, triggers, datasets, linked services, data flows). v2: admin, cost intelligence, Log Analytics history. |

## Status

| Toolset | State |
|---|---|
41 tools in 6 toolsets, all verified live against `mcp-adf-oct-test` + its Azure DevOps repo (2026-10-27).

| Toolset | Tools |
|---|---|
| `context` | profiles_list, profile_use, context, context_set, login, logout, factories_list |
| `monitor` | runs_query, run_get, activity_runs, activity_io, trigger_runs, failures_summary, run_tree, run_cancel |
| `health` | dependency_graph, validate, drift, factory_health |
| `authoring` | resource_list/get/upsert/delete (all kinds), pipeline_edit, resource_template, git_status/checkout/sync/push |
| `triggers` | triggers_list, trigger_start, trigger_stop, trigger_rerun |
| `debug` | pipeline_run, run_rerun, run_wait, diagnose_failure, pipeline_debug_run, debug_cleanup, dataflow_debug_session, dataflow_debug_preview |
| v2 | admin (IR management, managed VNet / private endpoints, global parameters, credentials), cost, Log Analytics |

## Commands

The project uses a PyPI **mirror** (`pypi.tuna.tsinghua.edu.cn`, configured in `pyproject.toml`) because
pypi.org is blocked on the development network. Remove the `[[tool.uv.index]]` block if you can reach PyPI.

```
uv sync
.venv/Scripts/python.exe -m pytest -q          # unit tests, fake ADF client, no Azure needed
.venv/Scripts/python.exe -m pytest -m live     # live tests (none yet)
uv run python -m adf_mcp                       # run the server (stdio)
uv run python -m adf_mcp --toolsets monitor --profile oct-test
npx @modelcontextprotocol/inspector uv run python -m adf_mcp
```

On machines where running `.exe` files from `.venv` is blocked (`adf-mcp.exe`, `pytest.exe`), use
`python -m ...`. `.mcp.json` registers the server for Claude Code the same way.
Set `ADF_MCP_AZURE_LOG_LEVEL=INFO` to see Azure SDK HTTP logs (default WARNING).

## Live testing

Live tests run against a sandbox factory described in `CLAUDE.local.md` (gitignored: it holds tenant,
subscription and repo identifiers that must not be pushed). Test resources are prefixed `mcp_test_` / `*_mcp_*`.

## Layout

```
src/adf_mcp/
  server.py        # MCPServer assembly, instructions, --toolsets / ADF_MCP_TOOLSETS selection
  config.py        # Profile model, profiles.yaml + ADF_MCP_* env loading
  auth.py          # credential per auth_method, persisted auth records + MSAL cache, token identity claims
  session.py       # per-process Session: active profile, overrides, cached credentials/clients, login flows
  errors.py        # AdfMcpError + @handle_errors -> ToolError JSON {code, message, hint}
  guards.py        # check_write (read_only / prod), authoring_mode (git vs live), confirm tokens, audit log
  compact.py       # compact views, truncation, time windows, run-query pagination
  resources.py     # resource kinds, reference extraction, normalize() for Git-vs-live comparison, Git JSON format
  gitrepo.py       # GitRepo over the git CLI: read any ref, write/commit on feature branches, push, PR URLs
  stores.py        # LiveStore / GitStore (same interface) + resolve_store() picking one per factory mode
  checks.py        # static validation rules + FactoryModel (reference graph)
  diagnose.py      # error classification rules for adf_diagnose_failure
  templates.py     # starter definitions for adf_resource_template
  tools/__init__.py  # TOOLSETS registry
  tools/_common.py   # ctx(), run queries, wait_for_run
  tools/{context,monitor,health,authoring,triggers,debug}.py
tests/             # conftest.py has FakeAdf / FakeSession; tests call tools through MCPServer.call_tool;
                   # test_git_authoring.py uses a real temp repo with a bare origin
profiles.example.yaml
```

## How to add a tool

1. Put it in the toolset module's `register(mcp)` as a **sync** function. The SDK runs sync tools in
   a worker thread, which suits the blocking Azure SDK.
2. Decorate with `@mcp.tool(annotations=...)` then `@handle_errors` (in that order). Always set
   `readOnlyHint` / `destructiveHint` / `idempotentHint`.
3. Resolve the target with `ctx(factory_name, resource_group)` from `tools/_common.py`. Accept those two optional
   args on factory-scoped tools. Read definitions through `resolve_store()`, never the SDK directly, so Git and
   live both work.
4. Any write: `check_write(target, "...")` first, `audit(...)` after success. Definition writes must also go
   through `authoring_mode()`.
5. Return compact dicts (see `compact.py`), never raw ARM lists. The SDK models' `.as_dict()` gives REST-shaped
   camelCase JSON; request bodies can be plain dicts in REST shape.
6. Add a test using `FakeAdf` / `FakeSession`. A new toolset module must be added to `tools/__init__.py`.

## ADF facts that drive the design

- REST: `.../providers/Microsoft.DataFactory/factories/{factory}/...?api-version=2018-06-01`; scope `https://management.azure.com/.default`.
- **Run history is kept 45 days** (`time_window` caps queries). Older history needs Log Analytics (`ADFPipelineRun`, `ADFActivityRun`, `ADFTriggerRun`).
- Run queries are POST with `lastUpdatedAfter/Before`, `filters`, `orderBy` and `continuationToken`. Use `paginate()`.
- Child pipeline runs: an `ExecutePipeline` activity's output has `pipelineRunId`; a child run's `invokedBy.pipelineRunId` points to its parent.
- `Pipelines.CreateRun` supports `referencePipelineRunId`, `isRecovery`, `startActivityName`, `startFromFailure`. This is how "debug" works.
- **There is no public API for ADF Studio's Debug button.** Runs always execute the **published** definition.
  `adf_pipeline_debug_run` works around it by publishing temporary `zz_mcp_debug_*` copies (folder `_mcp_debug`),
  running them and deleting them. ADF rejects a pipeline whose references don't exist live (HTTP 400 "invalid
  reference"), hence `with_dependencies`.
- Triggers must be **stopped before update** (`LiveStore._put_trigger` does stop → update → start). Storage event
  triggers also need `subscribeToEvents` (`adf_trigger_start` handles it).
- Linked service secrets come back masked. Prefer Key Vault references, and never echo secrets.

## Git mode

- Git is detected from the factory's `repoConfiguration` (type, repositoryName, collaborationBranch, rootFolder).
  `git_repo()` checks that the clone's origin matches `repositoryName`.
- Resource JSON lives at `<rootFolder>/<kind>/<name>.json` as `{"name", "properties", "type"}`. ADF Studio writes it
  tab-indented, with LF in the index, and no trailing newline; `git_document()` matches that.
- ADF Studio stores expressions as `{"value": "@..", "type": "Expression"}`, adds empty defaults and `lastPublishTime`.
  Always compare definitions with `same_definition()` / `normalize()`, never with `==`.
- Writes go to a feature branch (`branch`, else the current non-collaboration branch, else
  `<branch_prefix><kind>-<name>`) and are committed immediately. Committing to the collaboration branch is refused.
  `adf_git_push` pushes and returns the PR URL; merging and publishing stay with the team (ADF Studio / CI).
- Reads with `source=auto` use the working tree; `ref` reads any branch or commit without checking it out.
- Runtime operations (run, rerun, cancel, trigger start/stop) are not definition changes and always act on live.

## Safety conventions

- Profiles with `read_only: true`, or `environment: prod` without `allow_prod_writes`, block all writes (`check_write`).
- Upserts default to `dry_run=true` (diff + validation). Validation errors or missing references block the write
  unless `force=true`. Deletes need the `confirm_token` from their dry run (HMAC, valid only in this process).
- Every write is appended to `~/.adf-mcp/audit.jsonl`.
- User auth never prompts implicitly (`disable_automatic_authentication`). Tools fail with `login_required`,
  and the user signs in through `adf_login`.
- Never log or return tokens or secrets. `adf_context` returns identity claims only.

## References

- ADF REST v2: https://learn.microsoft.com/en-us/rest/api/datafactory/v2
- microsoft/DataFactory.MCP (Fabric, C#): source of the auth tool shape and skill-pack idea.
- harinarayn/adf-cost-intelligence-mcp: for v2 cost work, read billed units from `billingReference` in activity
  output, use the Azure Retail Prices API with a 24h cache, and attach savings estimates to recommendations.
- ADF Master skill: validate before publish, ARM diff, deprecation checks.
