# Squire — local inference on a LAN box

**Squire** is LM Studio running on a LAN box (referred to below as "the remote host"), serving an OpenAI-compatible API. It is the
ecosystem's zero-cost local model for triage, classification, summarization, and cheap pre-filtering before
any paid model.

## Endpoint

| | |
|---|---|
| Base URL | `http://<lan-box-ip>:1234/v1` |
| Chat model | `google/gemma-4-e4b` |
| Embeddings | `text-embedding-nomic-embed-text-v1.5` |
| Auth | none (blank / `lm-studio` / anything) |
| Bind | LM Studio on `0.0.0.0:1234` (LAN-reachable) |

Canonical env names used across the ecosystem: `SQUIRE_BASE_URL`, `SQUIRE_MODEL`.

## Two access paths

1. **`squire` MCP server (ecosystem-wide)** — registered at Claude Code **user scope**, so `ask_squire` is
   available in every project. Tools:
   - `ask_squire(prompt, [system], [temperature], [max_tokens])` → model text
   - `squire_models()` → served model ids

   Server: `<squire-mcp-dir>\squire-mcp-server.mjs` (dependency-free Node stdio JSON-RPC, uses built-in `fetch`).
   Registered with:
   ```
   claude mcp add squire --scope user \
     -e SQUIRE_BASE_URL=http://<lan-box-ip>:1234/v1 \
     -e SQUIRE_MODEL=google/gemma-4-e4b \
     -- node <squire-mcp-dir>\squire-mcp-server.mjs
   ```
   Verify: `claude mcp get squire` → `Connected`.

2. **Raw HTTP `/v1`** — point any OpenAI-compatible tool at the base URL. This is what BigBoss's stdlib
   router `triage.py` will call directly when Ecosystem Phase E4 (router v2 triage) is built — no MCP hop
   needed for in-process routing decisions.

## Quick tests (from the BigBoss host)

```powershell
Invoke-RestMethod http://<lan-box-ip>:1234/v1/models

$body = @{ model = "google/gemma-4-e4b"; messages = @(@{ role="user"; content="Say: the BigBoss host sees Squire." }); temperature = 0; max_tokens = 50 } | ConvertTo-Json -Depth 5
Invoke-RestMethod -Uri http://<lan-box-ip>:1234/v1/chat/completions -Method Post -ContentType "application/json" -Body $body
```

If the BigBoss host cannot connect, add the firewall rule on **the remote host** (Admin PowerShell, from `<remote-checkout>`):
```
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-lmstudio-firewall.ps1 -Action Add
```

## Notes / nuances

- **The MCP stdio server runs on the BigBoss host.** `ask_squire` correctly reaches the remote host's model over HTTP, but any
  project-scanning behavior would scan **the BigBoss host** paths. BigBoss already has a native portfolio scanner
  (Ecosystem Phase E3), so we use Squire only for model delegation (`ask_squire`), not scanning.
- The Node server here is a minimal bridge authored on the BigBoss host because the remote host's original `mcp` folder was not
  reachable over the network. If the remote host's fuller `squire-mcp-server.mjs` is later copied to `<squire-mcp-dir>\`,
  it drops in over this one (the registration path is unchanged).
- The embedding model (`text-embedding-nomic-embed-text-v1.5`) is available for E3's deferred
  note-similarity synergy detector.
