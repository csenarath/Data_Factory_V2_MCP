"""Classify activity errors into actionable categories."""

from __future__ import annotations

import re

# (category, pattern over "<errorCode> <message>", hint). First match wins, so specific rules come first.
RULES: list[tuple[str, re.Pattern, str]] = [
    (
        "self_hosted_ir",
        re.compile(r"self-?hosted integration runtime|integration runtime .*(offline|unavailable|not (online|reachable))|no (available|active) node", re.I),
        "The self-hosted IR is offline or overloaded: check its nodes (adf_factory_health) and the host machine/service.",
    ),
    (
        "dns_or_network",
        re.compile(
            r"name or service not known|no such host|could not be resolved|getaddrinfo|remote name could not be resolved"
            r"|connection (was )?refused|actively refused|network-related|unable to connect|ConnectionTimeout"
            r"|private endpoint|Client with IP address|firewall|SocketException",
            re.I,
        ),
        "Network path problem: check the endpoint name, firewall rules / IP allow-lists, private endpoints and DNS, "
        "and that the linked service runs on an IR that can reach the target (connectVia).",
    ),
    (
        "authentication",
        re.compile(r"login failed|AuthenticationFailed|invalid_client|invalid_grant|AADSTS\d+|\b401\b|Unauthorized|password|credential", re.I),
        "Credentials rejected: verify the linked service's auth type, the Key Vault secret's current value, or the "
        "managed identity / service principal being used.",
    ),
    (
        "permission",
        re.compile(r"\b403\b|Forbidden|AuthorizationPermissionMismatch|AuthorizationFailed|not authorized|permission|access (is )?denied", re.I),
        "Authenticated but not allowed: grant the factory's managed identity (or the SP) the needed role on the target, "
        "e.g. Storage Blob Data Contributor, db_datareader/db_datawriter, Key Vault Secrets User.",
    ),
    (
        "not_found",
        re.compile(r"does not exist|not ?found|PathNotFound|ContainerNotFound|Invalid object name|\b404\b|No such file", re.I),
        "The referenced path/table/object is missing: check dataset parameters, folder paths and upstream steps that should create it.",
    ),
    (
        "throttling",
        re.compile(r"\b429\b|throttl|TooManyRequests|server (is )?busy|rate limit|quota", re.I),
        "The target throttled requests: lower parallelism / DIUs / batch count, add retries with longer intervals, or scale the target.",
    ),
    (
        "timeout",
        re.compile(r"time(d)? ?out|timeout|exceeded the (maximum|allowed) time", re.I),
        "The activity hit a timeout: check the activity policy timeout, source query performance and target load.",
    ),
    (
        "data_or_schema",
        re.compile(r"conver(t|sion)|column|schema|mapping|truncat|data type|parse|format|delimiter|malformed", re.I),
        "Data/schema mismatch: compare source and sink schemas and the copy mapping; enable fault tolerance or fix the bad rows.",
    ),
    (
        "expression",
        re.compile(r"expression|template language|Unable to evaluate|is not a valid|ExpressionEvaluation|parameter .* not found", re.I),
        "An expression failed to evaluate: check parameter/variable names and types and activity output paths (adf_validate catches many of these).",
    ),
    (
        "databricks",
        re.compile(r"databricks|notebook|cluster|Spark", re.I),
        "The Databricks job failed: open run_page_url for the notebook error; check cluster availability and the linked service's auth.",
    ),
]


def classify(activity_type: str | None, code: str | None, message: str | None) -> tuple[str, str]:
    if activity_type == "Fail":
        return "intentional_fail_activity", f"A Fail activity raised error code {code}; this is the pipeline's own failure logic."
    text = f"{code or ''} {message or ''}"
    for category, pattern, hint in RULES:
        if pattern.search(text):
            return category, hint
    return "unclassified", "Read the full error with adf_activity_io and check the activity's linked service and inputs."
