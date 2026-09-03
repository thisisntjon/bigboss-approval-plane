from __future__ import annotations

import html
import json
from typing import Any

from .hosts import local_ip_hint
from .store import Store


DEFAULT_DEVICE_NAME = "Phone"
DEFAULT_PAIR_TTL_SECONDS = 3600


def pairing_host(port: int) -> str:
    """Host:port phones on the LAN should use (never 127.0.0.1)."""
    return f"{local_ip_hint()}:{port}"


def build_claim_url(public_host: str, code: str) -> str:
    return f"http://{public_host}/?pair={code}"


def issue_pair_code(
    store: Store,
    *,
    device_name: str = DEFAULT_DEVICE_NAME,
    ttl_seconds: int = DEFAULT_PAIR_TTL_SECONDS,
    public_host: str,
) -> dict[str, Any]:
    pair = store.create_pair_code(device_name, ttl_seconds=ttl_seconds)
    claim_url = build_claim_url(public_host, pair["code"])
    return {
        **pair,
        "claim_url": claim_url,
        "ttl_seconds": ttl_seconds,
        "public_host": public_host,
    }


def render_pair_page(payload: dict[str, Any]) -> str:
    claim_url = html.escape(str(payload["claim_url"]), quote=True)
    code = html.escape(str(payload["code"]))
    device_name = html.escape(str(payload["device_name"]))
    expires_at = html.escape(str(payload["expires_at"]))
    device_json = json.dumps(payload["device_name"])

    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="theme-color" content="#111827">
    <title>Pair phone - BigBoss</title>
    <link rel="stylesheet" href="/static/pair.css">
  </head>
  <body>
    <main class="pair-shell">
      <header class="pair-header">
        <h1>Pair your phone</h1>
        <p class="muted">Scan the QR code with your iPhone camera, or enter the code on the phone.</p>
      </header>

      <section class="pair-card">
        <div id="qr" class="qr-wrap" aria-label="Pairing QR code"></div>
        <p class="claim-url"><a id="claim-link" href="{claim_url}">{claim_url}</a></p>
        <p class="pair-code">Code: <strong id="pair-code">{code}</strong></p>
        <button id="copy-link-button" type="button" class="secondary-button">Copy claim link</button>
        <p id="countdown" class="muted expires"></p>
        <p class="muted expires">Expires <span id="expires-at">{expires_at}</span></p>
        <label class="device-name-field">
          Device name
          <input id="device-name" value="{device_name}" autocomplete="off">
        </label>
        <button id="refresh-button" type="button" class="primary-button">New code</button>
        <p id="pair-error" class="error-text"></p>
      </section>

      <p class="muted pair-foot">Keep the BigBoss server running while pairing. Scan the QR from your phone, or tap the claim link below the code.</p>
    </main>
    <script src="/static/qrcode.js"></script>
    <script>
      const defaultDeviceName = {device_json};

      function renderQr(url) {{
        const qr = qrcode(0, "M");
        qr.addData(url);
        qr.make();
        document.getElementById("qr").innerHTML = qr.createSvgTag(5, 2);
      }}

      function applyPayload(payload) {{
        document.getElementById("claim-link").href = payload.claim_url;
        document.getElementById("claim-link").textContent = payload.claim_url;
        document.getElementById("pair-code").textContent = payload.code;
        document.getElementById("expires-at").textContent = payload.expires_at;
        renderQr(payload.claim_url);
      }}

      applyPayload({{
        claim_url: {json.dumps(payload["claim_url"])},
        code: {json.dumps(payload["code"])},
        expires_at: {json.dumps(payload["expires_at"])},
      }});

      function startCountdown(isoExpires) {{
        const countdownEl = document.getElementById("countdown");
        function tick() {{
          const remainingMs = Date.parse(isoExpires) - Date.now();
          if (remainingMs <= 0) {{
            countdownEl.textContent = "Code expired — click New code";
            return;
          }}
          const mins = Math.floor(remainingMs / 60000);
          const secs = Math.floor((remainingMs % 60000) / 1000);
          countdownEl.textContent = `Fresh for ${{mins}}m ${{String(secs).padStart(2, "0")}}s`;
          window.setTimeout(tick, 1000);
        }}
        tick();
      }}
      startCountdown({json.dumps(payload["expires_at"])});

      document.getElementById("copy-link-button").addEventListener("click", async () => {{
        const url = document.getElementById("claim-link").href;
        try {{
          await navigator.clipboard.writeText(url);
          document.getElementById("pair-error").textContent = "Claim link copied.";
        }} catch (_error) {{
          document.getElementById("pair-error").textContent = "Copy failed — select the link manually.";
        }}
      }});

      document.getElementById("refresh-button").addEventListener("click", async () => {{
        const errorEl = document.getElementById("pair-error");
        errorEl.textContent = "";
        const button = document.getElementById("refresh-button");
        button.disabled = true;
        try {{
          const response = await fetch("/api/pair/codes", {{
            method: "POST",
            headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify({{
              device_name: document.getElementById("device-name").value.trim() || defaultDeviceName,
            }}),
          }});
          const data = await response.json();
          if (!response.ok) {{
            throw new Error(data.error || `Request failed: ${{response.status}}`);
          }}
          applyPayload(data);
          startCountdown(data.expires_at);
        }} catch (error) {{
          errorEl.textContent = error.message;
        }} finally {{
          button.disabled = false;
        }}
      }});
    </script>
  </body>
</html>
"""


def render_desk_page(*, phone_url: str, port: int, autoshow: bool = False) -> str:
    autoshow_js = "true" if autoshow else "false"
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="theme-color" content="#111827">
    <title>BigBoss Desk</title>
    <link rel="stylesheet" href="/static/desk.css">
  </head>
  <body>
    <main class="desk-shell">
      <header class="desk-header">
        <div class="desk-logo" aria-hidden="true">BB</div>
        <div>
          <h1>BigBoss</h1>
          <p class="muted">Phone pairing desk · port {port}</p>
        </div>
      </header>
      <p id="desk-status" class="desk-status ok">Server running</p>
      <button id="show-qr-button" type="button" class="desk-qr-button" aria-label="Show pairing QR code">
        <span class="desk-qr-icon" aria-hidden="true">▦</span>
        <span class="desk-qr-label">Pair phone</span>
        <span class="desk-qr-hint">Tap to show QR code</span>
      </button>
      <p class="muted desk-foot">Phone dashboard: <a href="{html.escape(phone_url)}">{html.escape(phone_url)}</a></p>
    </main>

    <div id="qr-modal" class="qr-modal hidden" role="dialog" aria-modal="true" aria-labelledby="qr-modal-title">
      <div class="qr-modal-card">
        <button id="close-qr-button" type="button" class="qr-close" aria-label="Close">×</button>
        <h2 id="qr-modal-title">Scan with your iPhone</h2>
        <p class="muted">Point the camera at the code — pairing happens automatically.</p>
        <div id="qr" class="qr-wrap" aria-label="Pairing QR code"></div>
        <p class="claim-url"><a id="claim-link" href="#"></a></p>
        <p class="pair-code">Code: <strong id="pair-code"></strong></p>
        <p id="countdown" class="muted"></p>
        <button id="refresh-button" type="button" class="secondary-button">New code</button>
        <button id="copy-link-button" type="button" class="secondary-button">Copy link</button>
        <p id="pair-error" class="error-text"></p>
      </div>
    </div>

    <script src="/static/qrcode.js"></script>
    <script>
      const defaultDeviceName = {json.dumps(DEFAULT_DEVICE_NAME)};
      const autoshow = {autoshow_js};
      const modal = document.getElementById("qr-modal");

      function renderQr(url) {{
        const qr = qrcode(0, "M");
        qr.addData(url);
        qr.make();
        document.getElementById("qr").innerHTML = qr.createSvgTag(6, 2);
      }}

      function applyPayload(payload) {{
        document.getElementById("claim-link").href = payload.claim_url;
        document.getElementById("claim-link").textContent = payload.claim_url;
        document.getElementById("pair-code").textContent = payload.code;
        renderQr(payload.claim_url);
        startCountdown(payload.expires_at);
      }}

      function startCountdown(isoExpires) {{
        const countdownEl = document.getElementById("countdown");
        function tick() {{
          const remainingMs = Date.parse(isoExpires) - Date.now();
          if (remainingMs <= 0) {{
            countdownEl.textContent = "Code expired — tap New code";
            return;
          }}
          const mins = Math.floor(remainingMs / 60000);
          const secs = Math.floor((remainingMs % 60000) / 1000);
          countdownEl.textContent = `Fresh for ${{mins}}m ${{String(secs).padStart(2, "0")}}s`;
          window.setTimeout(tick, 1000);
        }}
        tick();
      }}

      async function refreshPairCode() {{
        const errorEl = document.getElementById("pair-error");
        errorEl.textContent = "";
        const response = await fetch("/api/pair/codes", {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ device_name: defaultDeviceName }}),
        }});
        const data = await response.json();
        if (!response.ok) {{
          throw new Error(data.error || `Request failed: ${{response.status}}`);
        }}
        applyPayload(data);
      }}

      function openModal() {{
        modal.classList.remove("hidden");
        refreshPairCode().catch((error) => {{
          document.getElementById("pair-error").textContent = error.message;
        }});
      }}

      function closeModal() {{
        modal.classList.add("hidden");
      }}

      document.getElementById("show-qr-button").addEventListener("click", openModal);
      document.getElementById("close-qr-button").addEventListener("click", closeModal);
      modal.addEventListener("click", (event) => {{
        if (event.target === modal) closeModal();
      }});
      document.getElementById("refresh-button").addEventListener("click", () => {{
        refreshPairCode().catch((error) => {{
          document.getElementById("pair-error").textContent = error.message;
        }});
      }});
      document.getElementById("copy-link-button").addEventListener("click", async () => {{
        try {{
          await navigator.clipboard.writeText(document.getElementById("claim-link").href);
          document.getElementById("pair-error").textContent = "Link copied.";
        }} catch (_error) {{
          document.getElementById("pair-error").textContent = "Copy failed.";
        }}
      }});
      if (autoshow) openModal();
    </script>
  </body>
</html>
"""

