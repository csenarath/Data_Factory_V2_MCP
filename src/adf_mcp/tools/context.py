"""Toolset `context`: profiles, sign-in, target selection and factory discovery."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..auth import credential_key, token_identity
from ..config import profiles_path
from ..errors import AdfMcpError, handle_errors
from ..guards import authoring_mode, git_repo_configuration
from ..jobs import bounded
from ..session import get_session

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
LOCAL = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)


def _git_summary(repo: dict | None) -> dict | None:
    if not repo:
        return None
    return {
        "type": "AzureDevOps" if repo.get("type") == "FactoryVSTSConfiguration" else "GitHub",
        "account": repo.get("accountName"),
        "project": repo.get("projectName"),
        "repository": repo.get("repositoryName"),
        "collaboration_branch": repo.get("collaborationBranch"),
        "root_folder": repo.get("rootFolder"),
    }


def _resource_group_of(resource_id: str) -> str | None:
    parts = resource_id.split("/")
    lowered = [p.lower() for p in parts]
    return parts[lowered.index("resourcegroups") + 1] if "resourcegroups" in lowered else None


def register(mcp: MCPServer) -> None:
    @mcp.tool(annotations=READ)
    @handle_errors
    def adf_profiles_list() -> dict:
        """List configured ADF profiles (tenant/subscription/factory targets) and which one this session uses."""
        session = get_session()
        session.reload()
        return {
            "active_profile": session.active_name,
            "profiles_file": str(profiles_path()),
            "profiles": [
                {
                    "name": p.name,
                    "tenant_id": p.tenant_id,
                    "subscription_id": p.subscription_id,
                    "resource_group": p.resource_group,
                    "factory_name": p.factory_name,
                    "auth_method": p.auth_method,
                    "environment": p.environment,
                    "read_only": p.read_only,
                }
                for p in session.profiles.profiles.values()
            ],
        }

    @mcp.tool(annotations=LOCAL)
    @handle_errors
    def adf_profile_use(name: str) -> dict:
        """Switch this Claude session to another profile (tenant / subscription / factory). Other sessions are unaffected."""
        profile = get_session().use(name)
        return {"active_profile": profile.name, "auth_method": profile.auth_method, "environment": profile.environment}

    @mcp.tool(annotations=LOCAL)
    @handle_errors
    def adf_context_set(
        factory_name: str | None = None, resource_group: str | None = None, subscription_id: str | None = None
    ) -> dict:
        """Point this session at a different factory within the active profile's tenant (e.g. one found by adf_factories_list)."""
        session = get_session()
        updates = {"factory_name": factory_name, "resource_group": resource_group, "subscription_id": subscription_id}
        session.overrides.update({k: v for k, v in updates.items() if v})
        return session.target().describe()

    @mcp.tool(annotations=READ)
    @handle_errors
    def adf_context() -> dict:
        """Show who this session is signed in as, the active target factory, and its Git / authoring mode."""
        session = get_session()
        profile = session.profile
        result: dict = {"profile": profile.name, "auth_method": profile.auth_method}
        flow = session.device_flows.get(credential_key(profile))
        if flow and not flow.done:
            result["device_code_sign_in"] = {"pending": flow.error is None, "instructions": flow.message, "error": flow.error}
        try:
            result["identity"] = token_identity(session.credential(profile))
        except Exception as exc:
            result["identity"] = None
            result["auth_error"] = str(exc).splitlines()[0][:300]
            result["hint"] = "Call adf_login."
            return result
        try:
            target = session.target()
        except AdfMcpError as exc:
            result["target"] = None
            result["hint"] = exc.hint
            return result
        result["target"] = target.describe()
        try:
            factory = session.factory(target, refresh=True)
            result["git"] = _git_summary(git_repo_configuration(factory))
            result["authoring_mode"] = authoring_mode(session, target)
        except AdfMcpError as exc:
            result["authoring_mode"] = f"blocked: {exc.message}"
        result["writes_blocked"] = profile.writes_blocked_reason
        return result

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
    @handle_errors
    def adf_login(profile: str | None = None) -> dict:
        """Sign in for a profile. Browser auth opens a browser on this machine; device code returns a code for the user to enter.

        Service principal, managed identity, workload identity and Azure CLI profiles just verify their credential.
        """
        session = get_session()
        target_profile = session.use(profile) if profile else session.profile
        return {"profile": target_profile.name, **session.login(target_profile)}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
    @handle_errors
    def adf_logout(profile: str | None = None) -> dict:
        """Forget the saved sign-in for a profile and drop its cached credential in this session."""
        session = get_session()
        target_profile = session.profiles.profiles.get(profile) if profile else session.profile
        if target_profile is None:
            raise AdfMcpError("unknown_profile", f"Profile '{profile}' not found.")
        return {"profile": target_profile.name, **session.logout(target_profile)}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    @handle_errors
    def adf_job_status(job_id: str, wait_seconds: int = 45) -> dict:
        """Check a background job (debug run, publish, connection test, IR start/stop, ...). Waits up to
        wait_seconds (capped) for it to finish, then returns its status, recent progress and result."""
        job = get_session().jobs.get(job_id)
        job.done.wait(bounded(wait_seconds))
        out = job.view()
        if job.status == "running":
            out["next"] = f"Still running; call adf_job_status job_id={job.id} again."
        return out

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    @handle_errors
    def adf_jobs_list() -> dict:
        """List this session's background jobs and their status."""
        return {"jobs": [{k: v for k, v in j.view(progress_lines=1).items() if k != "result"} for j in get_session().jobs.all()]}

    @mcp.tool(annotations=READ)
    @handle_errors
    def adf_factories_list(
        subscription_id: str | None = None, resource_group: str | None = None, name_contains: str | None = None
    ) -> dict:
        """Discover data factories in a subscription (defaults to the active profile's), optionally within one resource group."""
        session = get_session()
        sub = subscription_id or session.overrides.get("subscription_id") or session.profile.subscription_id
        if not sub:
            raise AdfMcpError("target_incomplete", "No subscription_id given and the profile has none.")
        client = session.client(sub)
        pager = client.factories.list_by_resource_group(resource_group) if resource_group else client.factories.list()
        factories = []
        for factory in pager:
            data = factory.as_dict()
            if name_contains and name_contains.lower() not in data.get("name", "").lower():
                continue
            git = _git_summary(git_repo_configuration(data))
            factories.append(
                {
                    "name": data.get("name"),
                    "resource_group": _resource_group_of(data.get("id", "")),
                    "location": data.get("location"),
                    "git": f"{git['repository']}@{git['collaboration_branch']}" if git else None,
                }
            )
        return {"subscription_id": sub, "count": len(factories), "factories": factories}
