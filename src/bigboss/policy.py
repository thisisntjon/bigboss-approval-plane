from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


@dataclass(frozen=True)
class PolicyResult:
    status: str
    risk_level: str
    reasons: list[str]


READ_ONLY_KINDS = {
    "file_read",
    "list_directory",
    "git_status",
    "git_diff",
    "log_read",
    "summarize",
}

BLOCK_PATTERNS = [
    r"\brm\s+-rf\s+[/\\]?(?:$|\s)",
    r"\bremove-item\b.*\b-recurse\b.*(?:c:\\|/)",
    r"\bformat\b\s+[a-z]:",
    r"\bset-executionpolicy\b",
    r"\b(reg\s+add|reg\s+delete)\b",
    r"\b(get-childitem|dir|ls)\s+env:",
    r"\b(printenv|env)\b.*\b(token|secret|key|password)\b",
    r"\b(curl|wget|iwr|invoke-webrequest)\b.*\b(env:|\$env:|\.env|id_rsa|token|secret)\b",
]

APPROVAL_PATTERNS = [
    r"\bgit\s+push\b",
    r"\bgit\s+reset\b",
    r"\bgit\s+clean\b",
    r"\b(npm|pnpm|yarn)\s+(install|add|remove)\b",
    r"\b(pip|uv)\s+(install|add|remove|sync)\b",
    r"\b(curl|wget|iwr|invoke-webrequest)\b",
    r"\b(kubectl|terraform|pulumi|aws|gcloud|az)\b",
    r"\b(docker|podman)\s+(run|compose|push|rm|rmi|system)\b",
    r"\b(del|erase|rm|remove-item|rmdir|mv|move-item|copy-item)\b",
    r"[>]{1,2}",
    r"\bchmod\b|\bchown\b|\bsudo\b",
]

HIGH_RISK_PATTERNS = [
    r"\bgit\s+push\b",
    r"\b(kubectl|terraform|pulumi|aws|gcloud|az)\b",
    r"\b(secret|password|token|credential|\.env)\b",
    r"\b(deploy|release|production|prod)\b",
]

# BigBoss's own gated write-action kinds → (risk_level, reason). Every one stays `pending`
# (human-gated); the risk_level only drives card-UX escalation. reap kills real processes
# (irreversible) → high; a remote command acts on another machine → medium; reversible
# registry writes → low.
AGENT_WRITE_RISK = {
    "agent_reprioritize": ("low", "Reprioritize a project (reversible)."),
    "agent_exclude": ("low", "Exclude/include a project from prioritization (reversible)."),
    "agent_set_context": ("low", "Set an authoritative project context note (reversible)."),
    "council_prioritization": ("medium", "Apply a Council priority ordering."),
    "digest_batch": ("medium", "Approve off-box digest of newly-discovered projects."),
    "fable_escalation": ("medium", "Spend on a Fable tie-break."),
    "remote_command": ("medium", "Act on another machine's queue (reversible v1 op)."),
    "daemon_restart": ("medium", "Restart a background daemon/service."),
    "reap_process": ("high", "Terminate running process(es) — irreversible."),
    "agent_reap": ("high", "Terminate running process(es) — irreversible."),
}


def classify_action(action: dict, workspace: str | None = None) -> PolicyResult:
    kind = str(action.get("kind", "")).strip().lower()
    if kind in READ_ONLY_KINDS:
        return PolicyResult("auto_allowed", "low", ["Read-only action."])

    if kind in {"file_write", "file_delete", "file_rename"}:
        return _classify_file_mutation(action, workspace)

    if kind == "shell_command":
        return _classify_shell(str(action.get("command", "")))

    if kind in {"network_request", "package_install", "database_mutation", "secret_access"}:
        risk = "high" if kind in {"database_mutation", "secret_access"} else "medium"
        return PolicyResult("pending", risk, [f"{kind.replace('_', ' ').title()} requires approval."])

    if kind in AGENT_WRITE_RISK:
        # BigBoss's own gated write kinds. Always human-gated (never auto-allowed) — this
        # only sets an ACCURATE risk_level so the dashboard can escalate the card UX
        # (e.g. approve-once-only + typed target for a HIGH kind like reap).
        risk, reason = AGENT_WRITE_RISK[kind]
        return PolicyResult("pending", risk, [reason])

    return PolicyResult("pending", "medium", ["Unknown or adapter-defined action requires approval."])


def _classify_shell(command: str) -> PolicyResult:
    normalized = re.sub(r"\s+", " ", command.strip().lower())
    if not normalized:
        return PolicyResult("blocked", "critical", ["Empty shell command is not actionable."])

    for pattern in BLOCK_PATTERNS:
        if re.search(pattern, normalized):
            return PolicyResult("blocked", "critical", ["Command matches a blocked destructive or secret-exposure pattern."])

    reasons: list[str] = []
    for pattern in APPROVAL_PATTERNS:
        if re.search(pattern, normalized):
            reasons.append("Command can change code, machine state, network state, or external systems.")
            break

    if not reasons:
        return PolicyResult("pending", "medium", ["Shell commands require human review unless explicitly read-only/test-safe."])

    risk = "high" if any(re.search(pattern, normalized) for pattern in HIGH_RISK_PATTERNS) else "medium"
    return PolicyResult("pending", risk, reasons)


def _classify_file_mutation(action: dict, workspace: str | None) -> PolicyResult:
    path_value = action.get("path") or action.get("target") or ""
    if not path_value:
        return PolicyResult("pending", "medium", ["File mutation without a path requires approval."])

    if workspace:
        try:
            target = Path(path_value).expanduser()
            if not target.is_absolute():
                target = Path(workspace).expanduser() / target
            workspace_path = Path(workspace).expanduser().resolve()
            target_path = target.resolve()
            if workspace_path not in [target_path, *target_path.parents]:
                return PolicyResult("pending", "high", ["File mutation targets a path outside the workspace."])
        except OSError:
            return PolicyResult("pending", "high", ["File path could not be resolved safely."])

    if str(action.get("kind", "")).lower() == "file_delete":
        return PolicyResult("pending", "high", ["File deletion requires approval."])
    return PolicyResult("pending", "medium", ["File mutation requires approval."])
