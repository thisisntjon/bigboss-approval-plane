"""`bigboss chat` — a stdlib terminal chat REPL. Cheap by default (each turn goes to one fast
cheap seat), escalate on command: `/council` runs the full 4-seat deliberation, `/fable` brings in
the throne. Same cheap-by-default → escalate cascade as the rest of BigBoss. Stdlib only.

`call_chat` adds the multi-turn conversation call the council providers lack (they're single-shot);
`ChatSession` is the testable core (no stdin); `run_chat` is the thin input() loop over it.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

from .council import providers as P
from . import agent_tools

CHAT_FABLE_SYSTEM = (
    "You are Fable, the most capable model, brought into a chat to give a careful, decisive answer to the "
    "user's latest message. Be direct and honest about genuine uncertainty."
)

# M6.1 — static self-knowledge: a compact persona + portfolio summary so the chat model knows what
# BigBoss is and what the operator is working on (before this, a fresh seat guessed "BigBoss" was a TV show).
BIGBOSS_PERSONA = (
    "You are Big Boss — the AI management brain of BigBoss, a local-first, stdlib-only governance and "
    "portfolio-orchestration system for solo developer. You know their projects, priorities, and lineages, "
    "and you can convene a multi-model Council (4 cheap seats: Claude Haiku, GPT-mini, Gemini Flash, Grok) or "
    "bring in Fable (the expensive throne) to tie-break when the Council is unsure. Be concise, direct, and "
    "grounded in the portfolio below — do not invent projects. For a genuinely hard or high-stakes question, "
    "suggest /council (full deliberation) or /fable (the throne). This chat's quick turns are handled by one "
    "cheap seat, keeping Fable rare. You cannot execute shell or `bigboss` commands from this chat — suggest "
    "them for the operator to run; never narrate running one yourself. You cannot raise an approval card or know an "
    "approval id except from a propose_* tool result in the CURRENT turn — NEVER write 'approval card "
    "raised' or an apr_ id unless it came from a tool call you actually made this turn; if you want to "
    "propose a change, CALL the tool."
)


def _portfolio_summary(store, cap: int = 18) -> str:
    from .council.prioritize import build_portfolio_context

    lines = []
    for c in build_portfolio_context(store)[:cap]:  # priority-ordered; excludes work/third-party/done
        tag = "/".join(t for t in (c.get("lifecycle"), c.get("ownership")) if t)
        pin = f" [pinned #{c['pin_rank']}]" if c.get("pinned") else ""
        status = (c.get("status") or "").strip()
        purpose = (c.get("purpose") or "").strip()[:100]
        lines.append(f"- {c['slug']}{f' ({tag})' if tag else ''}{pin}: {purpose}"
                     + (f" — {status[:90]}" if status else ""))
    return "\n".join(lines) or "(portfolio empty — run registry-refresh)"


def build_bigboss_system(store) -> str:
    """The default chat system prompt: Big Boss persona + live portfolio summary + a derived
    capability manifest (M6.1b), so Big Boss can accurately state what it can do."""
    from .capabilities import build_capabilities_block

    return (f"{BIGBOSS_PERSONA}\n\nThe operator's active project portfolio (priority-ordered, most-important first):\n"
            f"{_portfolio_summary(store)}\n\n"
            f"{build_capabilities_block()}")


# CHAT_COMMANDS is the single source of truth for the chat's slash-commands: HELP_TEXT (below) and
# the capability manifest (capabilities.chat_commands) both derive from it, so they cannot drift.
CHAT_COMMANDS: list[tuple[str, str]] = [
    ("/council <q>", "run the full 4-seat Council (deliberate + verify + tie-break signal)"),
    ("/fable <q>", "ask Fable (the expensive throne) directly"),
    ("/recall <q>", "search Brainz for past conversations (shown locally only, never sent to a model)"),
    ("/seat <id>", "switch chat model seat (claude|gpt|gemini|grok)"),
    ("/system <t>", "set the system prompt"),
    ("/context", "show the current system context (Big Boss self-knowledge)"),
    ("/reset", "start a new conversation"),
    ("/save <path>", "append the transcript to a JSONL file"),
    ("/help", "this help"),
    ("/exit", "quit"),
]

HELP_TEXT = "Commands:\n" + "\n".join(f"  {cmd:<13} {desc}" for cmd, desc in CHAT_COMMANDS)


def call_chat(messages: list[dict[str, str]], *, seat: str = "claude", system: str = "",
              max_tokens: int = 1024, timeout: int = 90) -> dict[str, Any]:
    """Multi-turn chat completion for one seat. `messages` = [{"role":"user"|"assistant","content":str}].
    Routes by the seat's vendor; reuses the council providers' shared plumbing. Never raises."""
    P._ensure_env()
    cfg = P.COUNCIL_SEATS.get(seat)
    if cfg is None:
        return {"answer": None, "status": "error", "error": f"unknown seat {seat!r}",
                "model": seat, "usage": {"input": 0, "output": 0}}
    model, key, provider = P.seat_model(seat), P.seat_key(seat), cfg["provider"]
    if not key:
        return {"answer": None, "status": "error", "error": f"no API key ({cfg['key_env']})",
                "model": model, "usage": {"input": 0, "output": 0}}
    try:
        if provider == "anthropic":
            r = P._with_retry(lambda: _chat_anthropic(model, key, system, messages, max_tokens, timeout))
        elif provider in ("openai", "xai"):
            url = P.OPENAI_URL if provider == "openai" else P.XAI_URL
            r = P._with_retry(lambda: _chat_openai(url, model, key, system, messages, max_tokens, timeout))
        elif provider == "google":
            r = P._with_retry(lambda: _chat_gemini(model, key, system, messages, max_tokens, timeout))
        else:
            raise P.ProviderError(f"unknown provider {provider!r}")
    except P.ProviderError as exc:
        return {"answer": None, "status": "error", "error": str(exc), "model": model,
                "usage": {"input": 0, "output": 0}}
    return {"answer": r["answer"], "status": "success", "model": r.get("model") or model, "usage": r["usage"]}


def _chat_anthropic(model, key, system, messages, max_tokens, timeout):
    body = {"model": model, "max_tokens": max_tokens,
            "messages": [{"role": m["role"], "content": m["content"]} for m in messages]}
    if system:
        body["system"] = system
    data = P._http_post(P.ANTHROPIC_URL, {"x-api-key": key, "anthropic-version": "2023-06-01"}, body, timeout)
    text = "".join(b.get("text", "") for b in (data.get("content") or []) if b.get("type") == "text")
    u = data.get("usage") or {}
    return {"answer": text, "model": data.get("model") or model,
            "usage": {"input": int(u.get("input_tokens") or 0), "output": int(u.get("output_tokens") or 0)}}


def _chat_openai(url, model, key, system, messages, max_tokens, timeout):
    msgs = ([{"role": "system", "content": system}] if system else []) + \
           [{"role": m["role"], "content": m["content"]} for m in messages]
    body = {"model": model, "messages": msgs, "max_completion_tokens": max_tokens}
    data = P._http_post(url, {"Authorization": f"Bearer {key}"}, body, timeout)
    try:
        text = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        text = ""
    u = data.get("usage") or {}
    return {"answer": text, "model": data.get("model") or model,
            "usage": {"input": int(u.get("prompt_tokens") or 0), "output": int(u.get("completion_tokens") or 0)}}


def _chat_gemini(model, key, system, messages, max_tokens, timeout):
    url = P.GEMINI_URL.format(model=model) + f"?key={key}"
    contents = [{"role": ("model" if m["role"] == "assistant" else "user"), "parts": [{"text": m["content"]}]}
                for m in messages]
    body = {"contents": contents,
            "generationConfig": {"maxOutputTokens": max_tokens, "thinkingConfig": {"thinkingBudget": 0}}}
    if system:
        body["system_instruction"] = {"parts": [{"text": system}]}
    data = P._http_post(url, {}, body, timeout)
    try:
        text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
    except (KeyError, IndexError, TypeError):
        text = ""
    um = data.get("usageMetadata") or {}
    return {"answer": text, "model": model,
            "usage": {"input": int(um.get("promptTokenCount") or 0),
                      "output": int(um.get("candidatesTokenCount") or 0)}}


# --- M6.3: read-only agentic tool loop ---------------------------------------
# Cheap seats are reliable single-shot but degrade multi-hop, so tool ROUNDS are hard-capped and a
# genuine multi-step need becomes a Fable-escalation signal. Tools are read-only local state, so
# tool-result content can authorize nothing (it stays untrusted regardless — matters for M6.4 writes).

MAX_TOOL_HOPS = 4
_TOOL_RESULT_CHARS = 8000  # bound the JSON fed back per tool call


def _anthropic_turn(model, key, system, messages, tools, max_tokens, timeout):
    body = {"model": model, "max_tokens": max_tokens, "messages": messages, "tools": tools}
    if system:
        body["system"] = system
    data = P._http_post(P.ANTHROPIC_URL, {"x-api-key": key, "anthropic-version": "2023-06-01"}, body, timeout)
    content = data.get("content") or []
    text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
    tool_calls = [{"id": b.get("id"), "name": b.get("name"), "args": b.get("input") or {}}
                  for b in content if b.get("type") == "tool_use"]
    u = data.get("usage") or {}
    return {"text": text, "tool_calls": tool_calls,
            "assistant_native": {"role": "assistant", "content": content},
            "usage": {"input": int(u.get("input_tokens") or 0), "output": int(u.get("output_tokens") or 0)}}


def _anthropic_toolresult(results):
    return [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": r["id"], "content": r["result_str"]} for r in results]}]


def _openai_turn(url, model, key, system, messages, tools, max_tokens, timeout):
    msgs = ([{"role": "system", "content": system}] if system else []) + messages
    body = {"model": model, "messages": msgs, "max_completion_tokens": max_tokens, "tools": tools}
    data = P._http_post(url, {"Authorization": f"Bearer {key}"}, body, timeout)
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    text = msg.get("content") or ""
    tool_calls = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except (ValueError, TypeError):
            args = {}
        tool_calls.append({"id": tc.get("id"), "name": fn.get("name"), "args": args})
    u = data.get("usage") or {}
    return {"text": text, "tool_calls": tool_calls, "assistant_native": msg,
            "usage": {"input": int(u.get("prompt_tokens") or 0), "output": int(u.get("completion_tokens") or 0)}}


def _openai_toolresult(results):
    return [{"role": "tool", "tool_call_id": r["id"], "content": r["result_str"]} for r in results]


def _gemini_turn(model, key, system, contents, tools, max_tokens, timeout):
    url = P.GEMINI_URL.format(model=model) + f"?key={key}"
    body = {"contents": contents, "tools": tools,
            "generationConfig": {"maxOutputTokens": max_tokens, "thinkingConfig": {"thinkingBudget": 0}}}
    if system:
        body["system_instruction"] = {"parts": [{"text": system}]}
    data = P._http_post(url, {}, body, timeout)
    cand = (data.get("candidates") or [{}])[0]
    parts = ((cand.get("content") or {}).get("parts")) or []
    text = "".join(p.get("text", "") for p in parts if "text" in p)
    tool_calls = [{"id": p["functionCall"].get("name"), "name": p["functionCall"].get("name"),
                   "args": p["functionCall"].get("args") or {}}
                  for p in parts if p.get("functionCall")]
    um = data.get("usageMetadata") or {}
    return {"text": text, "tool_calls": tool_calls,
            "assistant_native": cand.get("content") or {"role": "model", "parts": parts},
            "usage": {"input": int(um.get("promptTokenCount") or 0),
                      "output": int(um.get("candidatesTokenCount") or 0)}}


def _gemini_toolresult(results):
    return [{"role": "user", "parts": [
        {"functionResponse": {"name": r["name"], "response": {"result": r["result_obj"]}}} for r in results]}]


def run_tool_loop(store, history, *, seat="claude", system="", max_hops=MAX_TOOL_HOPS,
                  max_tokens=1024, timeout=90):
    """Read-only agentic loop for one seat. Converts the flat history to vendor-native messages, calls
    with tools, executes any tool calls locally, feeds results back, and repeats up to `max_hops`
    rounds. Returns {answer, status, usage, rounds, tools_used, multistep, capped?}. Never raises."""
    P._ensure_env()
    cfg = P.COUNCIL_SEATS.get(seat)
    if cfg is None:
        return {"answer": None, "status": "error", "error": f"unknown seat {seat!r}",
                "usage": {"input": 0, "output": 0}, "rounds": 0, "tools_used": [], "multistep": False}
    model, key, provider = P.seat_model(seat), P.seat_key(seat), cfg["provider"]
    if not key:
        return {"answer": None, "status": "error", "error": f"no API key ({cfg['key_env']})",
                "usage": {"input": 0, "output": 0}, "rounds": 0, "tools_used": [], "multistep": False}

    if provider == "google":
        messages = [{"role": ("model" if m["role"] == "assistant" else "user"),
                     "parts": [{"text": m["content"]}]} for m in history]
        tools = agent_tools.gemini_tools()
    else:
        messages = [{"role": m["role"], "content": m["content"]} for m in history]
        tools = agent_tools.openai_tools() if provider in ("openai", "xai") else agent_tools.anthropic_tools()
    url = P.OPENAI_URL if provider == "openai" else P.XAI_URL if provider == "xai" else None

    usage_total = {"input": 0, "output": 0}
    tools_used: list[str] = []
    cards_raised: list[str] = []
    rounds = 0
    while True:
        try:
            if provider == "anthropic":
                turn = P._with_retry(lambda: _anthropic_turn(model, key, system, messages, tools, max_tokens, timeout))
            elif provider in ("openai", "xai"):
                turn = P._with_retry(lambda: _openai_turn(url, model, key, system, messages, tools, max_tokens, timeout))
            elif provider == "google":
                turn = P._with_retry(lambda: _gemini_turn(model, key, system, messages, tools, max_tokens, timeout))
            else:
                return {"answer": None, "status": "error", "error": f"unknown provider {provider!r}",
                        "usage": usage_total, "rounds": rounds, "tools_used": tools_used, "multistep": False}
        except P.ProviderError as exc:
            return {"answer": None, "status": "error", "error": str(exc), "usage": usage_total,
                    "rounds": rounds, "tools_used": tools_used, "multistep": rounds >= 2}
        usage_total["input"] += turn["usage"]["input"]
        usage_total["output"] += turn["usage"]["output"]

        if not turn["tool_calls"]:
            return {"answer": turn["text"], "status": "success", "model": model, "usage": usage_total,
                    "rounds": rounds, "tools_used": tools_used, "cards_raised": cards_raised,
                    "multistep": rounds >= 2}
        if rounds >= max_hops:  # would need another round — stop and let Fable take it
            return {"answer": turn["text"] or "", "status": "success", "model": model, "usage": usage_total,
                    "rounds": rounds, "tools_used": tools_used, "cards_raised": cards_raised,
                    "multistep": True, "capped": True}

        messages.append(turn["assistant_native"])
        results = []
        for tc in turn["tool_calls"]:
            tools_used.append(tc["name"])
            out = agent_tools.execute(tc["name"], tc["args"], store)
            if isinstance(out, dict) and out.get("status") == "approval_requested" and out.get("approval_id"):
                cards_raised.append(out["approval_id"])  # a write tool proposed a change (human-gated)
            results.append({"id": tc["id"], "name": tc["name"], "result_obj": out,
                            "result_str": json.dumps(out)[:_TOOL_RESULT_CHARS]})
        rounds += 1
        if provider == "anthropic":
            messages.extend(_anthropic_toolresult(results))
        elif provider in ("openai", "xai"):
            messages.extend(_openai_toolresult(results))
        else:
            messages.extend(_gemini_toolresult(results))


class ChatSession:
    """Testable chat core (no stdin). Holds the running conversation + routes turns."""

    def __init__(self, store, seat: str = "claude", system: str = "", save_path: str | None = None):
        self.store = store
        self.seat = seat
        self.system = system
        self.save_path = save_path
        self.messages: list[dict[str, str]] = []
        self.usage = {"input": 0, "output": 0}
        self.raised_cards: set[str] = set()  # real approval ids actually created this session

    def _acc(self, u: dict) -> None:
        self.usage["input"] += int(u.get("input") or 0)
        self.usage["output"] += int(u.get("output") or 0)

    def _save(self, role: str, content: str) -> None:
        if not self.save_path:
            return
        try:
            with open(self.save_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"role": role, "content": content}) + "\n")
        except OSError:
            pass

    def send(self, text: str) -> dict[str, Any]:
        self.messages.append({"role": "user", "content": text})
        # M6.3 — read-only agentic loop: the seat may call portfolio_intel / squire_status / etc. to
        # answer from live state instead of the static prompt snapshot.
        r = run_tool_loop(self.store, self.messages, seat=self.seat, system=self.system)
        if r["status"] != "success":
            self.messages.pop()
            return {"ok": False, "text": f"[error: {r.get('error')}]"}
        ans = r["answer"] or ""
        cards = r.get("cards_raised") or []
        self.raised_cards.update(cards)
        # Fabrication guard: the model can only learn an apr_ id from a tool result. Any apr_ id it emits
        # that was never actually raised (this session) is a hallucinated card — a cheap seat mimicking the
        # "card raised" format without calling the tool. Redact it and correct, so it can never claim a
        # card that doesn't exist (the acceptance-test failure).
        fabricated = set(re.findall(r"\bapr_[A-Za-z0-9_\-]{6,}", ans)) - self.raised_cards
        if fabricated:
            for fid in fabricated:
                ans = ans.replace(fid, "(no such card)")
            ans += ("\n\n[correction: no approval card was actually created this turn — I can only raise "
                    "one by calling the tool. Please restate the request and I'll raise it properly.]")
        self.messages.append({"role": "assistant", "content": ans})
        self._acc(r["usage"])
        self._save("user", text)
        self._save("assistant", ans)
        note = ""
        used = list(dict.fromkeys(r.get("tools_used") or []))
        if used:
            note += f"\n\n[queried: {', '.join(used)}]"
        if cards:
            note += (f"\n[approval card raised: {', '.join(cards)} — nothing changes until you approve "
                     "it on the dashboard]")
        if r.get("multistep"):
            note += "\n[multi-step reasoning — /fable would give a more thorough answer]"
        return {"ok": True, "text": ans + note, "usage": r["usage"]}

    def council(self, text: str) -> dict[str, Any]:
        from .council.engine import ProviderNotConfigured, run_live_council
        from .council.cost import estimate_cost

        try:
            report = run_live_council(text, context=self.system)
        except ProviderNotConfigured as exc:
            return {"ok": False, "text": f"[council unavailable: {exc}]"}
        cost = estimate_cost(report)
        rec = self.store.record_council_session(report, cost_usd=cost)
        ans = (report.get("final") or {}).get("final_answer") or ""
        note = f"\n\n[council winner: {report.get('winner')} | seats: {', '.join(report.get('seats') or [])} | ${cost}]"
        esc = report.get("escalation") or {}
        if esc.get("escalate"):
            from .council.escalation import estimate_tiebreak_cost_usd, MIN_MEAN_CONFIDENCE
            contested = [c.get("text", "") for c in (report.get("verified_claims") or [])
                         if c.get("verdict") == "refuted" or float(c.get("confidence") or 1) < MIN_MEAN_CONFIDENCE]
            card = self.store.create_approval_request({
                "harness": "bigboss", "run_title": "Council escalation (chat)",
                "title": f"Council split ({esc['trigger']}) — bring in Fable?",
                "summary": f"{esc['reason']}\n\nTentative answer:\n{ans[:600]}",
                "proposed_action": {"kind": "fable_escalation", "session_id": rec["session_id"],
                                    "question": text, "contested": contested,
                                    "trigger": esc.get("trigger"), "reason": esc.get("reason")},
            })
            note += (f"\n[unsure ({esc['trigger']}): {esc['reason']} — escalation card {card['id']}; "
                     f"approve to bring in Fable (~${estimate_tiebreak_cost_usd(report)})]")
        self.messages.append({"role": "user", "content": text})
        self.messages.append({"role": "assistant", "content": ans})
        self._save("council", text)
        self._save("assistant", ans)
        return {"ok": True, "text": ans + note}

    def fable(self, text: str) -> dict[str, Any]:
        from .council.providers import call_anthropic_model
        from .council.research import research_model

        fable_system = (
            f"{CHAT_FABLE_SYSTEM}\n\nContext you are advising within (the operator's BigBoss ecosystem):\n{self.system}"
            if self.system else CHAT_FABLE_SYSTEM
        )
        # Fable is deliberately rare; give it room to finish (the 1024 default truncated answers).
        r = call_anthropic_model(research_model(), fable_system, text, max_tokens=3000)
        if r["status"] != "success":
            return {"ok": False, "text": f"[fable error: {r.get('error')}]"}
        ans = r.get("answer") or ""
        self._acc(r.get("usage", {}))
        self.messages.append({"role": "user", "content": text})
        self.messages.append({"role": "assistant", "content": ans})
        self._save("fable", text)
        self._save("assistant", ans)
        return {"ok": True, "text": ans + f"\n\n[via {r.get('model')}]"}

    def handle(self, line: str) -> tuple[str, bool]:
        """Process one input line. Returns (display_text, done)."""
        line = line.strip()
        if not line:
            return "", False
        if not line.startswith("/"):
            return self.send(line)["text"], False
        cmd, _, rest = line[1:].partition(" ")
        cmd, rest = cmd.lower(), rest.strip()
        if cmd in ("exit", "quit", "q"):
            return "bye.", True
        if cmd == "help":
            return HELP_TEXT, False
        if cmd == "context":
            return (self.system or "[no system context]"), False
        if cmd == "reset":
            self.messages = []
            return "[conversation reset]", False
        if cmd == "system":
            self.system = rest
            return "[system prompt set]", False
        if cmd == "seat":
            if rest in P.COUNCIL_SEATS:
                self.seat = rest
                return f"[chat seat -> {rest} ({P.seat_model(rest)})]", False
            return f"[unknown seat; options: {', '.join(P.COUNCIL_SEATS)}]", False
        if cmd == "save":
            self.save_path = rest or self.save_path
            return f"[transcript -> {self.save_path}]" if self.save_path else "[usage: /save <path>]", False
        if cmd == "council":
            return (self.council(rest)["text"] if rest else "[usage: /council <question>]"), False
        if cmd == "fable":
            return (self.fable(rest)["text"] if rest else "[usage: /fable <question>]"), False
        if cmd == "recall":
            return (self.recall(rest) if rest else "[usage: /recall <query>]"), False
        return f"[unknown command /{cmd} — try /help]", False

    def recall(self, query: str) -> str:
        """DISPLAY-ONLY Brainz recall. Prints relevant past conversations to the operator's terminal and
        deliberately does NOT touch self.messages — so nothing reaches call_chat / the cloud vendor.
        (The privacy invariant is enforced structurally here + by test, not by a runtime scrub, because
        the operator may legitimately choose to paste a snippet themselves — they are the human gate.)"""
        from .brainz import BrainzClient, BrainzError, format_recall

        try:
            client = BrainzClient()
        except BrainzError as exc:
            return f"[recall unavailable: {exc}]"
        health = client.ping()
        if not health["ok"]:
            return (f"[Brainz not reachable at {client.base_url}. "
                    f"({health['detail']})]")
        try:
            rec = client.recall(query)
        except BrainzError as exc:
            return f"[recall error: {exc}]"
        try:  # reconciliation audit — records THAT a recall happened, no content, display-only
            import hashlib
            self.store.append_event("brainz.recall", "chat", {
                "query_hash": hashlib.sha256(query.encode("utf-8")).hexdigest()[:16],
                "results": len(rec["results"]), "mode": rec["mode"], "destination": "display_local"})
        except Exception:
            pass
        return format_recall(rec)


def _ansi(enabled: bool):
    if not enabled:
        return {"dim": "", "bold": "", "user": "", "bot": "", "reset": ""}
    return {"dim": "\033[2m", "bold": "\033[1m", "user": "\033[36m", "bot": "\033[32m", "reset": "\033[0m"}


def run_chat(store, args) -> int:
    try:
        import readline  # noqa: F401 — arrow-key history on Linux/mac; absent on Windows stdlib (fine)
    except ImportError:
        pass
    seat = getattr(args, "seat", None) or os.environ.get("BIGBOSS_CHAT_SEAT") or "claude"
    if seat not in P.COUNCIL_SEATS:
        print(f"Unknown seat {seat!r}; using 'claude'. Options: {', '.join(P.COUNCIL_SEATS)}")
        seat = "claude"
    if getattr(args, "model", None):  # override this seat's model for the session
        os.environ[P.COUNCIL_SEATS[seat]["model_env"]] = args.model
    # Default system prompt = Big Boss self-knowledge (M6.1); --system overrides, --no-context skips.
    system = getattr(args, "system", None)
    if not system and not getattr(args, "no_context", False):
        try:
            system = build_bigboss_system(store)
        except Exception:
            system = ""
    session = ChatSession(store, seat=seat, system=system or "", save_path=getattr(args, "save", None))
    c = _ansi(sys.stdout.isatty() and not os.environ.get("NO_COLOR"))
    ctx = "portfolio-aware" if session.system and "Big Boss" in session.system else "no context"
    print(f"{c['bold']}BigBoss chat{c['reset']} {c['dim']}— seat: {seat} ({P.seat_model(seat)}), {ctx}. "
          f"/help for commands, /exit to quit.{c['reset']}")
    while True:
        try:
            line = input(f"{c['user']}you ›{c['reset']} ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        display, done = session.handle(line)
        if display:
            label = "" if display.startswith("[") or display == "bye." else f"{c['bot']}bigboss ›{c['reset']} "
            print(f"{label}{display}")
        if done:
            break
    if session.usage["input"] or session.usage["output"]:
        print(f"{c['dim']}session tokens: {session.usage['input']} in / {session.usage['output']} out{c['reset']}")
    return 0
