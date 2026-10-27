# Azure Data Factory MCP Server

An [MCP](https://modelcontextprotocol.io) server that lets Claude (or any MCP client) monitor, debug, author and
operate **Azure Data Factory (ADF v2)** pipelines across multiple tenants and factories. It is built for
Git-connected factories.

It works with classic Azure Data Factory, not Microsoft Fabric Data Factory.

## What it can do

| Area | Tools |
|---|---|
| **Sign-in, targets & jobs** | `adf_login`, `adf_logout`, `adf_context`, `adf_profiles_list`, `adf_profile_use`, `adf_context_set`, `adf_factories_list`, `adf_job_status`, `adf_jobs_list` |
| **Monitoring** | `adf_runs_query`, `adf_run_get`, `adf_activity_runs`, `adf_activity_io`, `adf_trigger_runs`, `adf_failures_summary`, `adf_run_tree`, `adf_run_cancel` |
| **End-to-end health** | `adf_dependency_graph`, `adf_validate`, `adf_drift`, `adf_factory_health` |
| **Authoring** | `adf_resource_list` / `get` / `upsert` / `delete` (pipelines, datasets, linked services, triggers, data flows, integration runtimes, credentials), `adf_pipeline_edit`, `adf_resource_template` |
| **Git & publish** | `adf_git_status`, `adf_git_checkout`, `adf_git_sync`, `adf_git_push`, `adf_publish`, `adf_global_parameters` |
| **Triggers** | `adf_triggers_list`, `adf_trigger_start`, `adf_trigger_stop`, `adf_trigger_rerun` |
| **Debugging** | `adf_pipeline_run`, `adf_run_rerun` (from failure / from activity), `adf_run_wait`, `adf_diagnose_failure`, `adf_pipeline_debug_run`, `adf_linked_service_test`, `adf_debug_cleanup`, `adf_dataflow_debug_session`, `adf_dataflow_debug_preview` |
| **Admin** | `adf_factory_info`, `adf_factory_update`, `adf_access_check`, `adf_diagnostic_settings`, `adf_integration_runtimes`, `adf_integration_runtime_control`, `adf_integration_runtime_keys`, `adf_managed_network`, `adf_managed_private_endpoint` |
| **Cost** | `adf_cost_report` |

Some highlights:

- **One sign-in per session, many tenants.** Each Claude session runs its own server process and picks a
  *profile* (tenant + subscription + factory + auth method). Sessions never affect each other, and sign-ins are
  cached per tenant.
- **User and machine authentication.** Browser, device code and Azure CLI for people. Service principal (secret
  or certificate), managed identity and workload identity for automation.
- **Git-first authoring.** On a Git-connected factory, changes are committed to a feature branch of your local
  clone, in exactly the JSON format ADF Studio writes, never to the collaboration branch. `adf_git_push` returns
  the pull-request link. Live writes to Git-connected factories are refused, because the next publish would
  overwrite them.
- **Debug unpublished changes.** ADF can only run published pipelines. `adf_pipeline_debug_run` publishes a
  temporary copy of the branch version (with `with_dependencies`, also copies of the changed datasets, linked
  services and child pipelines), runs it and cleans up.
- **Publish without ADF Studio.** `adf_publish` deploys the collaboration branch to the live factory in dependency
  order, stops and restarts affected triggers, publishes global parameters, and can delete resources removed from
  Git. It shows the plan first and needs a confirm token to apply.
- **Test connection for linked services.** `adf_linked_service_test` runs a tiny probe (`SELECT 1`,
  GetMetadata(exists), an authenticated GET) as a temporary debug run, including against unpublished Git versions.
- **No blocking calls.** Long operations (debug runs, publish, connection tests, IR start/stop, cost reports) run as
  background jobs: a call returns within ~50 s with the result or a `job_id` for `adf_job_status`.
- **Cost intelligence.** `adf_cost_report` prices the billed units ADF reports per activity run with Azure retail
  prices: spend per pipeline and meter, cost of failed runs, 30-day projection and recommendations.
- **Root-cause analysis.** `adf_run_tree` and `adf_diagnose_failure` follow Execute Pipeline chains to the activity
  that actually failed. They classify the error (network, auth, permission, not found, throttling, timeout,
  schema, expression, IR, Databricks) and list the linked services and integration runtime involved.
- **Safe by default.**
  - Updates are dry runs that return a diff until you pass `dry_run=false`.
  - Deletes need a confirmation token from their dry run.
  - Production or `read_only` profiles block all writes.
  - Every write goes to an audit log.
- **Compact output.** Runs and definitions are summarized to save tokens, and large payloads are truncated.

## Setup

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/) and git.

```bash
git clone https://github.com/csenarath/Data_Factory_V2_MCP.git
cd Data_Factory_V2_MCP
uv sync
```

> `pyproject.toml` points uv at a PyPI mirror. If you can reach pypi.org, delete the `[[tool.uv.index]]` block.

### Profiles

Copy [`profiles.example.yaml`](profiles.example.yaml) to `~/.adf-mcp/profiles.yaml` and fill in your factories:

```yaml
default_profile: dev
profiles:
  dev:
    tenant_id: <tenant-guid>
    subscription_id: <subscription-guid>
    resource_group: rg-data-dev
    factory_name: adf-dev
    auth_method: browser          # browser | device_code | azure_cli | service_principal_secret |
                                  # service_principal_certificate | managed_identity | workload_identity
    environment: dev              # prod blocks writes unless allow_prod_writes: true
    git:
      repo_path: C:/repos/adf-dev # local clone of the factory's Git repo (Git-connected factories)
```

Secrets never go in this file. Service principal secrets are read from the environment variable named by
`client_secret_env` (default `AZURE_CLIENT_SECRET`).

### Register with Claude Code

The repo includes a project-level [`.mcp.json`](.mcp.json). To register it globally instead:

```bash
claude mcp add adf -- uv run --directory /path/to/Data_Factory_V2_MCP python -m adf_mcp
```

| Environment variable | Purpose |
|---|---|
| `ADF_MCP_PROFILE` | Profile a session starts on (otherwise `default_profile`) |
| `ADF_MCP_TOOLSETS` | `all` or a comma list of `context,monitor,health,authoring,triggers,debug,admin,cost` |
| `ADF_MCP_HOME` | Config/state folder (default `~/.adf-mcp`: profiles, saved sign-ins, `audit.jsonl`) |
| `ADF_MCP_AZURE_LOG_LEVEL` | Azure SDK log level (default `WARNING`) |
| `ADF_MCP_MAX_WAIT_SECONDS` | Longest a single tool call waits before handing back a job id (default `50`) |

Then, in Claude: *"Show me what failed in adf-dev in the last 24 hours and why"*, or *"Add a Copy activity to
pl_load_sales on a new branch and debug-run it"*.

## Permissions

| Task | Minimum Azure role on the factory |
|---|---|
| Monitoring, health, reading definitions | Reader |
| Runs, reruns, cancel, triggers, live authoring, debug runs | Data Factory Contributor |
| Git-mode authoring | Repo write access (plus Reader on the factory) |

## Development

```bash
uv run python -m pytest -q      # unit tests: fake ADF client + real temporary Git repos, no Azure needed
uv run python -m adf_mcp        # run the server over stdio
npx @modelcontextprotocol/inspector uv run python -m adf_mcp
```

See [CLAUDE.md](CLAUDE.md) for the architecture, conventions and how to add a tool.

## Known limitations

| Limitation | Why | Workaround / alternative |
|---|---|---|
| No true ADF Studio **Debug** button | It uses a private API | `adf_pipeline_debug_run` publishes temporary `zz_mcp_debug_*` copies, runs them and deletes them. These copies appear briefly in the live factory and its run history, and need Data Factory Contributor. For zero footprint, point a profile at a separate sandbox factory and debug there. |
| Debug runs can't copy integration runtimes or credentials | They are infrastructure, not definitions | Publish IR / credential changes first, or test in a sandbox factory |
| Runs always use the **published** definition | ADF design | Debug-run the branch version, then merge it and run `adf_publish` |
| `adf_publish` doesn't regenerate the `adf_publish` branch (ARM templates) | It deploys through the REST API directly, like an incremental ARM deployment | If CI deploys from ARM templates, run [`@microsoft/azure-data-factory-utilities`](https://www.npmjs.com/package/@microsoft/azure-data-factory-utilities) `export` in that pipeline |
| Background jobs live in the server process | No persistent job store | If the session ends mid-job, check the factory with `adf_runs_query` / `adf_drift` and run `adf_debug_cleanup` |
| `adf_linked_service_test` for Key Vault / Databricks tests the factory's managed identity from the Azure IR | Those probes are Web activities | For self-hosted / private-only targets, probe through a dataset that uses the linked service |
| Cost is an estimate from list prices | Billed units come from ADF, prices from the public retail API | Cross-check with Azure Cost Management for the actual invoice |
| Pull requests aren't created, only linked | Needs Azure DevOps / GitHub credentials beyond Git | Open the returned `pull_request_url`, or use `az repos pr create` / `gh pr create` |
| Static validation is a subset of Studio's | Connector-specific `typeProperties` schemas aren't checked | Run `azure-data-factory-utilities validate` in CI. A debug run is the final check. |
| Expressions aren't evaluated, only their references checked | No offline ADF expression engine | Debug-run the pipeline with real parameters, then inspect results with `adf_activity_io` |
| Run history only 45 days | ADF retention | Enable diagnostic settings to Log Analytics (`ADFPipelineRun` etc.). Log Analytics tools are on the roadmap. |
| Secrets come back masked from live | ADF security | Use Key Vault references / managed identity. Read definitions from Git. |
| Data flow debug previews give no error details on failure; sessions take 3–6 min to start and are billed | ADF API | Find the real error with a probe pipeline. Keep `time_to_live_minutes` low and stop sessions when done. |
| `adf_pipeline_edit` edits top-level activities only | Kept simple | For activities nested in ForEach / If / Switch, send the whole pipeline with `adf_resource_upsert` |
| Trigger rerun works for tumbling-window triggers only | ADF only supports rerun for those | For schedule / event triggers, use `adf_pipeline_run` with the same parameters |
| A Git clone is shared by every session that uses it | One working tree | Use one clone (or `git worktree`) per concurrent session |
| Large factories: validation and graph load every definition per call | No cache yet | Filter by `kind`, or validate a branch `ref` in CI |
| Local only: browser sign-in opens on the machine running the server | stdio design | Use `device_code` for remote shells, `service_principal_*` / `managed_identity` for automation |

## Roadmap

- Run history beyond ADF's 45-day retention via Log Analytics (`adf_diagnostic_settings` already checks the setup)
- Pull request creation (Azure DevOps / GitHub)
- ARM template export for CI-based deployments
