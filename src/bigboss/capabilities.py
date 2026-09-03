"""Big Boss self-knowledge: a capability manifest DERIVED from the real command
definitions, so it can never drift out of sync (the hand-written summary in
CLAUDE.md already does). The CLI subcommands come straight from argparse, the
MCP tools from `tool_definitions()`, and the chat slash-commands from the single
`CHAT_COMMANDS` table in `chat.py`. `build_capabilities_block()` renders a
compact, grouped block for injection into the chat/Council system prompt (M6.1b).

All imports of sibling modules are lazy (inside functions) to avoid the
chat <-> capabilities <-> cli import cycle.
"""
from __future__ import annotations

# --- authored identity facts (small, stable — the one hand-kept part) ---------

GLOSSARY = (
    "- Squire: the remote host LM Studio local-compute endpoint (google/gemma-4-e4b) that BigBoss meters and "
    "digests project intel through — infrastructure, NOT a portfolio project (see squire-status / squire-egress).\n"
    "- Beastmode: the guarded RTX-5090 box — opt-in, rate-limited compute; never used automatically.\n"
    "- The Council: 4 cheap seats (Claude Haiku, GPT-mini, Gemini Flash, Grok) that answer independently, "
    "critique each other, verify cross-vendor, and synthesize.\n"
    "- Fable = claude-fable-5: the expensive 'throne' — used only for council-research and the human-gated "
    "tie-break escalation, never as a council seat.\n"
    "- Brainz: the operator's local memory spine (thousands of past AI conversations); recall is display-only today "
    "and is never sent off-box.\n"
    "- Router: the loopback-only Anthropic metering proxy (accountability, not gating)."
)

COST_NOTE = (
    "Cost model: quick chat turns use one cheap seat; a full /council run is ~$0.05-0.10; Fable is pricier and "
    "kept rare. There is no fixed per-session token budget."
)

_AREA_ORDER = [
    "Approval plane & dashboard",
    "Cost router",
    "Portfolio registry",
    "Council & portfolio brain",
    "Intel & compute",
    "Process ops",
    "Fleet tracking",
    "Harness integration",
]


def _area_for(name: str) -> str:
    if name.startswith("registry-"):
        return "Portfolio registry"
    if name.startswith("council-") or name.startswith("brainz-") or name == "chat":
        return "Council & portfolio brain"
    if name.startswith("squire-") or name.startswith("intel-") or name.startswith("digest-") or name == "crawl":
        return "Intel & compute"
    if name in ("ps", "reap", "daemons", "daemon", "security"):
        return "Process ops"
    if name in ("session-report", "sessions", "commands"):
        return "Fleet tracking"
    if name in ("serve", "open", "pair", "pair-code", "lan-pin", "devices", "secrets"):
        return "Approval plane & dashboard"
    if name == "router":
        return "Cost router"
    if name in ("mcp-stdio", "hook-approval", "codex-run", "demo-request"):
        return "Harness integration"
    return "Other"


# --- derived sources of truth -------------------------------------------------

def cli_commands() -> list[tuple[str, str]]:
    """Every `bigboss <cmd>` and its help string, read from the argparse registry."""
    import argparse

    try:
        from .cli import build_parser

        parser = build_parser()
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                return [(ca.dest, (ca.help or "").strip()) for ca in action._choices_actions]
    except Exception:
        return []
    return []


def mcp_tools() -> list[tuple[str, str]]:
    """Every MCP tool (name, description) from the canonical tool_definitions() list."""
    try:
        from .mcp_stdio import tool_definitions

        return [(t["name"], (t.get("description") or "").strip()) for t in tool_definitions()]
    except Exception:
        return []


def chat_commands() -> list[tuple[str, str]]:
    """The chat REPL slash-commands (single source shared with HELP_TEXT)."""
    try:
        from .chat import CHAT_COMMANDS

        return list(CHAT_COMMANDS)
    except Exception:
        return []


# --- render -------------------------------------------------------------------

def build_capabilities_block(compact: bool = True) -> str:
    """Grouped capability manifest for the Big Boss system prompt. Each CLI command carries its
    REAL help string (not just the name) so the model recites accurate semantics instead of
    guessing — the M6.1c fix for confabulated descriptions."""
    groups: dict[str, list[tuple[str, str]]] = {}
    for name, help_ in cli_commands():
        groups.setdefault(_area_for(name), []).append((name, help_))

    lines = ["What Big Boss can do — CLI commands (run as `bigboss <cmd>`), grouped by area "
             "(use these exact descriptions; do not invent command behavior):"]
    ordered = _AREA_ORDER + [a for a in groups if a not in _AREA_ORDER]
    for area in ordered:
        cmds = groups.get(area)
        if cmds:
            lines.append(f"{area}:")
            lines.extend(f"  {name} — {help_}" if help_ else f"  {name}" for name, help_ in cmds)

    tools = [n for n, _d in mcp_tools()]
    if tools:
        lines.append("MCP tools exposed to harnesses: " + ", ".join(tools))

    cc = chat_commands()
    if cc:
        lines.append("In this chat, slash-commands:")
        lines.extend(f"  {cmd} — {desc}" for cmd, desc in cc)

    lines.append("Key subsystems / terms:")
    lines.append(GLOSSARY)
    lines.append(COST_NOTE)
    return "\n".join(lines)
