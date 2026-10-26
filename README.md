# Azure Data Factory MCP Server

An [MCP](https://modelcontextprotocol.io) server that lets Claude (or any MCP client) monitor, debug, author and
operate **Azure Data Factory (ADF v2)** pipelines across multiple tenants and factories. It is built for
Git-connected factories.

It works with classic Azure Data Factory, not Microsoft Fabric Data Factory.

## What it can do

| Area | Tools |
|---|---|
| **Sign-in & targets** | `adf_login`, `adf_logout`, `adf_context`, `adf_profiles_list`, `adf_profile_use`, `adf_context_set`, `adf_factories_list` |
| **Monitoring** | `adf_runs_query`, `adf_run_get`, `adf_activity_runs`, `adf_activity_io`, `adf_trigger_runs`, `adf_failures_summary`, `adf_run_tree`, `adf_run_cancel` |
| **End-to-end health** | `adf_dependency_graph`, `adf_validate`, `adf_drift`, `adf_factory_health` |
| **Authoring** | `adf_resource_list` / `get` / `upsert` / `delete` (pipelines, datasets, linked services, triggers, data flows, integration runtimes, credentials), `adf_pipeline_edit`, `adf_resource_template` |
| **Git** | `adf_git_status`, `adf_git_checkout`, `adf_git_sync`, `adf_git_push` |
| **Triggers** | `adf_triggers_list`, `adf_trigger_start`, `adf_trigger_stop`, `adf_trigger_rerun` |
| **Debugging** | `adf_pipeline_run`, `adf_run_rerun` (from failure / from activity), `adf_run_wait`, `adf_diagnose_failure`, `adf_pipeline_debug_run`, `adf_debug_cleanup`, `adf_dataflow_debug_session`, `adf_dataflow_debug_preview` |

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
| `ADF_MCP_TOOLSETS` | `all` or a comma list of `context,monitor,health,authoring,triggers,debug` |
| `ADF_MCP_HOME` | Config/state folder (default `~/.adf-mcp`: profiles, saved sign-ins, `audit.jsonl`) |
| `ADF_MCP_AZURE_LOG_LEVEL` | Azure SDK log level (default `WARNING`) |

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

## Roadmap

- Admin toolset: integration runtime management, managed VNet and private endpoints, global parameters, credentials
- Cost intelligence (billed units from activity runs + Azure Retail Prices)
- Run history beyond ADF's 45-day retention via Log Analytics
