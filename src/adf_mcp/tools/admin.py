"""Toolset `admin`: factory settings, access check, diagnostics, integration runtimes, managed network.

Definitions of IRs and credentials are authored with adf_resource_upsert (kinds integrationRuntime,
credential); these tools cover the runtime/infrastructure side.
"""

from __future__ import annotations

import fnmatch

from azure.core.rest import HttpRequest
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..errors import AdfMcpError, handle_errors
from ..guards import audit, check_write, require_confirm_token
from ..guards import confirm_token as make_confirm_token
from ..jobs import run_job
from ..session import Target
from ._common import ctx

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)

ARM = "https://management.azure.com"

# What each capability needs, as Azure RBAC actions.
CAPABILITIES = {
    "read_definitions_and_monitor": "Microsoft.DataFactory/factories/read",
    "author_pipelines": "Microsoft.DataFactory/factories/pipelines/write",
    "delete_pipelines": "Microsoft.DataFactory/factories/pipelines/delete",
    "run_pipelines": "Microsoft.DataFactory/factories/pipelines/createrun/action",
    "cancel_runs": "Microsoft.DataFactory/factories/pipelineruns/cancel/action",
    "start_stop_triggers": "Microsoft.DataFactory/factories/triggers/start/action",
    "manage_linked_services": "Microsoft.DataFactory/factories/linkedServices/write",
    "manage_integration_runtimes": "Microsoft.DataFactory/factories/integrationruntimes/write",
    "read_ir_auth_keys": "Microsoft.DataFactory/factories/integrationruntimes/listauthkeys/action",
    "change_factory_settings": "Microsoft.DataFactory/factories/write",
    "manage_private_endpoints": "Microsoft.DataFactory/factories/managedVirtualNetworks/managedPrivateEndpoints/write",
    "assign_roles": "Microsoft.Authorization/roleAssignments/write",
}


def _factory_id(target: Target) -> str:
    return f"/subscriptions/{target.subscription_id}/resourceGroups/{target.resource_group}/providers/Microsoft.DataFactory/factories/{target.factory_name}"


def _arm_get(client, path: str, api_version: str) -> dict:
    response = client.send_request(HttpRequest("GET", f"{ARM}{path}?api-version={api_version}"))
    if response.status_code >= 400:
        raise AdfMcpError(f"http_{response.status_code}", response.text()[:500])
    return response.json()


def _allowed(action: str, permissions: list[dict]) -> bool:
    action = action.lower()
    allowed = any(fnmatch.fnmatch(action, a.lower()) for p in permissions for a in p.get("actions", []))
    denied = any(fnmatch.fnmatch(action, a.lower()) for p in permissions for a in p.get("notActions", []))
    return allowed and not denied


def _mask(value: str | None) -> str | None:
    return f"{value[:12]}…{value[-4:]}" if value and len(value) > 20 else ("***" if value else None)


def _ir_view(status: dict) -> dict:
    props = status.get("properties") or {}
    view = {
        "name": status.get("name"),
        "type": props.get("type"),
        "state": props.get("state"),
    }
    if props.get("type") == "SelfHosted":
        view.update(
            version=props.get("version"),
            latest_version=props.get("latestVersion"),
            auto_update=props.get("autoUpdate"),
            nodes=[
                {
                    "node": n.get("nodeName"),
                    "machine": n.get("machineName"),
                    "status": n.get("status"),
                    "version": n.get("version"),
                    "last_connect": n.get("lastConnectTime"),
                    "concurrent_jobs": f"{n.get('concurrentJobsRunning', 0)}/{n.get('maxConcurrentJobs', '?')}",
                }
                for n in props.get("nodes") or []
            ],
        )
    elif props.get("type") == "Managed":
        tp = props.get("typeProperties") or {}
        view.update(create_time=props.get("createTime"), nodes=tp.get("nodes"), other_errors=tp.get("otherErrors"))
    return {k: v for k, v in view.items() if v not in (None, [], {})}


def register(mcp: MCPServer) -> None:
    @mcp.tool(annotations=READ)
    @handle_errors
    def adf_factory_info(factory_name: str | None = None, resource_group: str | None = None) -> dict:
        """Factory settings: location, managed identity, public network access, encryption, Git configuration,
        global parameter count, tags."""
        session, target, client = ctx(factory_name, resource_group)
        factory = session.factory(target, refresh=True)
        props = factory.get("properties") or {}
        repo = props.get("repoConfiguration") or {}
        globals_ = [g.as_dict() for g in client.global_parameters.list_by_factory(target.resource_group, target.factory_name)]
        return {
            "name": factory.get("name"),
            "id": factory.get("id"),
            "location": factory.get("location"),
            "identity": factory.get("identity"),
            "public_network_access": props.get("publicNetworkAccess") or "Enabled",
            "encryption": props.get("encryption"),
            "created": props.get("createTime"),
            "git": {k: repo.get(k) for k in ("type", "accountName", "projectName", "repositoryName", "collaborationBranch", "rootFolder", "lastCommitId", "disablePublish")} if repo else None,
            "global_parameters": sorted(((globals_[0].get("properties") or {}) if globals_ else {}).keys()),
            "tags": factory.get("tags"),
        }

    @mcp.tool(annotations=DESTRUCTIVE)
    @handle_errors
    def adf_factory_update(
        public_network_access: str | None = None,
        tags: dict | None = None,
        dry_run: bool = True,
        confirm_token: str | None = None,
        factory_name: str | None = None,
        resource_group: str | None = None,
    ) -> dict:
        """Change factory settings: public_network_access (Enabled | Disabled) and/or tags (replaces all tags).
        dry_run=true shows the change and a confirm_token; apply with dry_run=false and the token."""
        session, target, client = ctx(factory_name, resource_group)
        factory = session.factory(target, refresh=True)
        body: dict = {}
        if public_network_access is not None:
            if public_network_access not in ("Enabled", "Disabled"):
                raise AdfMcpError("bad_argument", "public_network_access must be Enabled or Disabled.")
            body["publicNetworkAccess"] = public_network_access
        if tags is not None:
            body["tags"] = tags
        if not body:
            raise AdfMcpError("bad_argument", "Nothing to change: pass public_network_access and/or tags.")
        current = {
            "publicNetworkAccess": (factory.get("properties") or {}).get("publicNetworkAccess") or "Enabled",
            "tags": factory.get("tags"),
        }
        parts = ("factory_update", sorted(body.items(), key=str))
        if dry_run:
            return {"current": {k: current[k] for k in body}, "proposed": body, "confirm_token": make_confirm_token(target, *parts),
                    "warning": "Disabling public network access cuts off ADF Studio and the REST API except through private endpoints." if body.get("publicNetworkAccess") == "Disabled" else None}
        require_confirm_token(target, confirm_token, *parts)
        check_write(target, "Update factory")
        client.factories.update(target.resource_group, target.factory_name, body)
        session.factory(target, refresh=True)
        audit(target, "factory_update", f"factory/{target.factory_name}", body)
        return {"updated": body}

    @mcp.tool(annotations=READ)
    @handle_errors
    def adf_access_check(factory_name: str | None = None, resource_group: str | None = None) -> dict:
        """What the signed-in identity may do on this factory (from its effective Azure RBAC permissions):
        read/monitor, author, run, triggers, IRs, factory settings, role assignment."""
        session, target, client = ctx(factory_name, resource_group)
        permissions = _arm_get(client, f"{_factory_id(target)}/providers/Microsoft.Authorization/permissions", "2022-04-01").get("value", [])
        capabilities = {name: _allowed(action, permissions) for name, action in CAPABILITIES.items()}
        missing = [n for n, ok in capabilities.items() if not ok and n != "assign_roles"]
        return {
            "factory": target.factory_name,
            "capabilities": capabilities,
            "hint": None if not missing else (
                "Reader is enough for monitoring; Data Factory Contributor on the factory covers authoring, runs and triggers."
            ),
        }

    @mcp.tool(annotations=READ)
    @handle_errors
    def adf_diagnostic_settings(factory_name: str | None = None, resource_group: str | None = None) -> dict:
        """Where the factory sends logs/metrics (Log Analytics, storage, Event Hub). Run history in ADF is kept 45 days;
        long-term history needs a Log Analytics destination with ActivityRuns / PipelineRuns / TriggerRuns enabled."""
        _, target, client = ctx(factory_name, resource_group)
        settings = _arm_get(client, f"{_factory_id(target)}/providers/Microsoft.Insights/diagnosticSettings", "2021-05-01-preview").get("value", [])
        rows = []
        for s in settings:
            p = s.get("properties") or {}
            rows.append({
                "name": s.get("name"),
                "log_analytics_workspace": p.get("workspaceId"),
                "destination_table_type": p.get("logAnalyticsDestinationType"),
                "storage_account": p.get("storageAccountId"),
                "event_hub": p.get("eventHubAuthorizationRuleId"),
                "logs_enabled": sorted(l.get("category") or l.get("categoryGroup") for l in p.get("logs") or [] if l.get("enabled")),
            })
        long_term = any(r["log_analytics_workspace"] and {"PipelineRuns", "ActivityRuns"} <= set(r["logs_enabled"]) for r in rows)
        return {
            "settings": rows,
            "long_term_run_history": long_term,
            "hint": None if long_term else "Add a diagnostic setting to a Log Analytics workspace with PipelineRuns, ActivityRuns and TriggerRuns (resource-specific tables) to keep run history beyond 45 days.",
        }

    @mcp.tool(annotations=READ)
    @handle_errors
    def adf_integration_runtimes(name: str | None = None, factory_name: str | None = None, resource_group: str | None = None) -> dict:
        """Integration runtime status. Without name: every IR with type and state. With name: details — for
        self-hosted IRs the nodes (machine, status, version, last connect, concurrent jobs) and update state."""
        _, target, client = ctx(factory_name, resource_group)
        rg, factory = target.resource_group, target.factory_name
        names = [name] if name else [ir.as_dict()["name"] for ir in client.integration_runtimes.list_by_factory(rg, factory)]
        views = [_ir_view(client.integration_runtimes.get_status(rg, factory, n).as_dict()) for n in names]
        if not name:
            views = [{k: v for k, v in view.items() if k in ("name", "type", "state", "version")} for view in views]
        return {"factory": factory, "integration_runtimes": views or "only AutoResolveIntegrationRuntime (built in, always available)"}

    @mcp.tool(annotations=WRITE)
    @handle_errors
    def adf_integration_runtime_control(
        name: str, action: str, wait_seconds: int = 45, factory_name: str | None = None, resource_group: str | None = None
    ) -> dict:
        """Operate an integration runtime. action: start | stop (Azure-SSIS / managed IRs; takes minutes, runs as a
        job) | sync_credentials | upgrade (self-hosted)."""
        session, target, client = ctx(factory_name, resource_group)
        check_write(target, f"IR {action}")
        rg, factory = target.resource_group, target.factory_name
        if action in ("start", "stop"):
            def job(j):
                j.log(f"{action} {name}")
                poller = (client.integration_runtimes.begin_start if action == "start" else client.integration_runtimes.begin_stop)(rg, factory, name)
                poller.result()
                audit(target, f"ir_{action}", f"integrationRuntime/{name}")
                return {"integration_runtime": name, "action": action, "state": _ir_view(client.integration_runtimes.get_status(rg, factory, name).as_dict()).get("state")}

            return run_job(session.jobs, f"ir{action}", f"{action} IR {name}", job, wait_seconds)
        if action == "sync_credentials":
            client.integration_runtimes.sync_credentials(rg, factory, name)
        elif action == "upgrade":
            client.integration_runtimes.upgrade(rg, factory, name)
        else:
            raise AdfMcpError("bad_argument", "action must be start, stop, sync_credentials or upgrade.")
        audit(target, f"ir_{action}", f"integrationRuntime/{name}")
        return {"integration_runtime": name, "action": action, "status": "requested"}

    @mcp.tool(annotations=DESTRUCTIVE)
    @handle_errors
    def adf_integration_runtime_keys(
        name: str,
        reveal: bool = False,
        regenerate: str | None = None,
        dry_run: bool = True,
        confirm_token: str | None = None,
        factory_name: str | None = None,
        resource_group: str | None = None,
    ) -> dict:
        """Self-hosted IR authentication keys (used to register nodes). Masked unless reveal=true.
        regenerate=authKey1|authKey2 invalidates that key (nodes registered with it must re-register):
        needs dry_run=false and the confirm_token from a dry run."""
        session, target, client = ctx(factory_name, resource_group)
        rg, factory = target.resource_group, target.factory_name
        if regenerate:
            if regenerate not in ("authKey1", "authKey2"):
                raise AdfMcpError("bad_argument", "regenerate must be authKey1 or authKey2.")
            parts = ("ir_regenerate_key", name, regenerate)
            if dry_run:
                return {"integration_runtime": name, "would_regenerate": regenerate,
                        "warning": f"Nodes registered with {regenerate} stop working until re-registered with the new key.",
                        "confirm_token": make_confirm_token(target, *parts)}
            require_confirm_token(target, confirm_token, *parts)
            check_write(target, "Regenerate IR key")
            keys = client.integration_runtimes.regenerate_auth_key(rg, factory, name, {"keyName": regenerate}).as_dict()
            audit(target, "ir_regenerate_key", f"integrationRuntime/{name}", {"key": regenerate})
        else:
            if reveal:
                check_write(target, "Reveal IR keys")
            keys = client.integration_runtimes.list_auth_keys(rg, factory, name).as_dict()
            if reveal:
                audit(target, "ir_reveal_keys", f"integrationRuntime/{name}")
        show = (lambda v: v) if reveal else _mask
        return {"integration_runtime": name, "authKey1": show(keys.get("authKey1")), "authKey2": show(keys.get("authKey2")), "revealed": reveal}

    @mcp.tool(annotations=WRITE)
    @handle_errors
    def adf_managed_network(action: str = "list", factory_name: str | None = None, resource_group: str | None = None) -> dict:
        """Managed virtual network and private endpoints. action=list shows the managed VNet, its managed private
        endpoints (target, group, approval state, FQDNs) and private endpoint connections into the factory.
        action=enable creates the 'default' managed VNet."""
        _, target, client = ctx(factory_name, resource_group)
        rg, factory = target.resource_group, target.factory_name
        if action == "enable":
            check_write(target, "Enable managed VNet")
            client.managed_virtual_networks.create_or_update(rg, factory, "default", {"properties": {}})
            audit(target, "managed_vnet_enable", "managedVirtualNetwork/default")
            return {"managed_virtual_network": "default", "status": "enabled", "next": "Create an Azure IR with managedVirtualNetwork 'default' to use it."}
        if action != "list":
            raise AdfMcpError("bad_argument", "action must be list or enable.")
        vnets = [v.as_dict() for v in client.managed_virtual_networks.list_by_factory(rg, factory)]
        out: dict = {"managed_virtual_networks": [], "factory_private_endpoint_connections": []}
        for vnet in vnets:
            endpoints = []
            for pe in client.managed_private_endpoints.list_by_factory(rg, factory, vnet["name"]):
                p = pe.as_dict().get("properties") or {}
                endpoints.append({
                    "name": pe.as_dict()["name"],
                    "target": p.get("privateLinkResourceId"),
                    "group": p.get("groupId"),
                    "approval": (p.get("connectionState") or {}).get("status"),
                    "provisioning": p.get("provisioningState"),
                    "fqdns": p.get("fqdns"),
                })
            out["managed_virtual_networks"].append({"name": vnet["name"], "private_endpoints": endpoints})
        for conn in client.private_end_point_connections.list_by_factory(rg, factory):
            p = conn.as_dict().get("properties") or {}
            out["factory_private_endpoint_connections"].append({
                "name": conn.as_dict().get("name"),
                "state": (p.get("privateLinkServiceConnectionState") or {}).get("status"),
                "private_endpoint": (p.get("privateEndpoint") or {}).get("id"),
            })
        if not vnets:
            out["note"] = "No managed VNet. adf_managed_network action=enable creates one."
        return out

    @mcp.tool(annotations=DESTRUCTIVE)
    @handle_errors
    def adf_managed_private_endpoint(
        name: str,
        action: str = "upsert",
        private_link_resource_id: str | None = None,
        group_id: str | None = None,
        fqdns: list[str] | None = None,
        dry_run: bool = True,
        confirm_token: str | None = None,
        managed_virtual_network: str = "default",
        factory_name: str | None = None,
        resource_group: str | None = None,
    ) -> dict:
        """Create/update (action=upsert) or delete a managed private endpoint in the factory's managed VNet.
        upsert needs private_link_resource_id (target resource ID) and group_id (e.g. blob, dfs, sqlServer, vault).
        The target resource's owner must approve the connection afterwards. dry_run first; apply with the token."""
        _, target, client = ctx(factory_name, resource_group)
        rg, factory = target.resource_group, target.factory_name
        if action == "upsert":
            if not private_link_resource_id or not group_id:
                raise AdfMcpError("bad_argument", "upsert needs private_link_resource_id and group_id.")
            body = {"properties": {"privateLinkResourceId": private_link_resource_id, "groupId": group_id, **({"fqdns": fqdns} if fqdns else {})}}
        elif action == "delete":
            body = None
        else:
            raise AdfMcpError("bad_argument", "action must be upsert or delete.")
        parts = ("managed_private_endpoint", action, managed_virtual_network, name, body)
        if dry_run:
            return {"action": action, "managed_virtual_network": managed_virtual_network, "name": name, "body": body,
                    "confirm_token": make_confirm_token(target, *parts),
                    "note": "After creation the target resource owner must approve the private endpoint connection." if body else None}
        require_confirm_token(target, confirm_token, *parts)
        check_write(target, f"{action} managed private endpoint")
        if body:
            result = client.managed_private_endpoints.create_or_update(rg, factory, managed_virtual_network, name, body).as_dict()
            state = ((result.get("properties") or {}).get("connectionState") or {}).get("status")
        else:
            client.managed_private_endpoints.delete(rg, factory, managed_virtual_network, name)
            state = "deleted"
        audit(target, f"managed_private_endpoint_{action}", f"managedPrivateEndpoint/{name}", {"target": private_link_resource_id, "group": group_id})
        return {"name": name, "action": action, "state": state}
