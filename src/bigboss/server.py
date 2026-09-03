from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
import json
import os
from pathlib import Path
import socket
import time
from urllib.parse import parse_qs, urlparse

from .ops import pin_rate_limit_ok, record_pin_failure, reset_pin_failures
from .pairing import (
    DEFAULT_DEVICE_NAME,
    DEFAULT_PAIR_TTL_SECONDS,
    issue_pair_code,
    pairing_host,
    render_desk_page,
    render_pair_page,
)
from .intel import refresh_intel
from .intel.endpoints import list_endpoints
from .registry import refresh as registry_refresh
from .security import token_matches
from .store import Store


STATIC_DIR = Path(__file__).parent / "static"


def squire_status_annotated(store: Store, days: int = 7) -> dict:
    """squire_status with each endpoint annotated from the registry (exposure
    channel, opt-in flag) so the phone can flag guarded/audited boxes. M6.5-Full also
    attaches `activity`: the latest live-job/queue snapshots remote hosts (the remote host) have
    reported, each carrying `age_seconds` provenance (never presented as live truth)."""
    status = store.squire_status(days=days)
    registry = {ep.name: ep for ep in list_endpoints()}
    for entry in status["endpoints"]:
        ep = registry.get(entry["endpoint"])
        entry["exposure"] = ep.exposure if ep else ""
        entry["auto_ok"] = ep.auto_ok if ep else True
    status["activity"] = store.get_remote_snapshots(kind="squire")
    return status


class ApprovalHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(
        self,
        server_address: tuple[str, int],
        store: Store,
        admin_token_hash: str,
        adapter_token_hash: str,
        lan_pin_hash: str,
        public_host: str | None = None,
    ):
        super().__init__(server_address, ApprovalHandler)
        self.store = store
        self.admin_token_hash = admin_token_hash
        self.adapter_token_hash = adapter_token_hash
        self.lan_pin_hash = lan_pin_hash
        self.public_host = public_host
        # E3.5: SSE streams close at this cap (the browser then reconnects with
        # Last-Event-ID and the server replays missed events). Env-overridable so a
        # test can shorten it. Kept finite to bound per-connection resource use.
        try:
            self.sse_cap_seconds = float(os.environ.get("BIGBOSS_SSE_CAP_SECONDS") or 3600)
        except ValueError:
            self.sse_cap_seconds = 3600.0


class ApprovalHandler(BaseHTTPRequestHandler):
    server: ApprovalHTTPServer

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.log_date_time_string()} {self.address_string()} {fmt % args}")

    def do_GET(self) -> None:
        if not self._validate_host_header():
            return
        parsed = urlparse(self.path)
        path = self._normalize_path(parsed.path)
        if path in {"/", "/index.html"}:
            return self._send_static("index.html", "text/html; charset=utf-8")
        if path == "/static/app.js":
            return self._send_static("app.js", "application/javascript; charset=utf-8")
        if path == "/static/styles.css":
            return self._send_static("styles.css", "text/css; charset=utf-8")
        if path == "/static/pair.css":
            return self._send_static("pair.css", "text/css; charset=utf-8")
        if path == "/static/desk.css":
            return self._send_static("desk.css", "text/css; charset=utf-8")
        if path == "/static/qrcode.js":
            return self._send_static("qrcode.js", "application/javascript; charset=utf-8")
        if path == "/desk":
            return self._serve_desk_page(parse_qs(parsed.query))
        if path == "/pair":
            # Renders a claimable pair code into the page, so minting stays
            # workstation-only; phones re-pair via LAN PIN or a claim URL.
            if not self._require_loopback():
                return
            return self._serve_pair_page()
        if path == "/api/health":
            port = self.server.server_address[1]
            return self._send_json(
                {
                    "ok": True,
                    "service": "bigboss-approval-plane",
                    "port": port,
                    "desk_url": f"http://127.0.0.1:{port}/desk?autoshow=1",
                    "phone_url": f"http://{pairing_host(port)}/",
                }
            )
        if path == "/api/me":
            device = self._require_device()
            if not device:
                return
            return self._send_json({"device_id": device["id"], "device_name": device["name"]})
        if path == "/api/approvals":
            if not self._require_device():
                return
            query = parse_qs(parsed.query)
            status = query.get("status", ["pending"])[0]
            return self._send_json({"approvals": self.server.store.list_approvals(status=status)})
        if path.startswith("/api/approvals/"):
            if not self._require_device():
                return
            approval_id = path.removeprefix("/api/approvals/").split("/")[0]
            approval = self.server.store.get_approval(approval_id)
            if approval is None:
                return self._send_error(HTTPStatus.NOT_FOUND, "Approval not found.")
            return self._send_json({"approval": approval})
        if path == "/api/registry/projects":
            if not self._require_device():
                return
            query = parse_qs(parsed.query)
            include_ambiguous = query.get("all", ["false"])[0].lower() == "true"
            return self._send_json(
                {"projects": self.server.store.list_projects(include_ambiguous=include_ambiguous)}
            )
        if path == "/api/intel/portfolio":
            if not self._require_device():
                return
            return self._send_json(
                {
                    "projects": self.server.store.list_projects(),
                    "squire": squire_status_annotated(self.server.store),
                }
            )
        if path == "/api/squire/status":
            if not self._require_device():
                return
            query = parse_qs(parsed.query)
            days = int(query.get("days", ["7"])[0])
            return self._send_json({"squire": squire_status_annotated(self.server.store, days=days)})
        if path == "/api/daemons":
            if not self._require_device():
                return
            from .daemon_registry import status as daemon_status
            return self._send_json(daemon_status())
        if path == "/api/squire/egress":
            if not self._require_device():
                return
            query = parse_qs(parsed.query)
            days = int(query.get("days", ["7"])[0])
            endpoint = query.get("endpoint", [None])[0]
            return self._send_json(
                {"egress": self.server.store.list_egress(days=days, endpoint=endpoint)}
            )
        if path == "/api/sessions":
            if not self._require_device():
                return
            query = parse_qs(parsed.query)
            days = int(query.get("days", ["7"])[0])
            project = query.get("project", [None])[0]
            harness = query.get("harness", [None])[0]
            host = query.get("host", [None])[0]
            limit = int(query.get("limit", ["100"])[0])
            return self._send_json(
                {
                    "sessions": self.server.store.list_harness_sessions(
                        days=days, project=project, harness=harness, host=host, limit=limit
                    )
                }
            )
        if path == "/api/commands":
            # Cross-machine control: the REMOTE host polls for approved commands. Adapter-LAN
            # gated (token, no loopback requirement) — NOT device-authed. Returns + marks claimed.
            if not self._require_adapter_lan():
                return
            query = parse_qs(parsed.query)
            host = (query.get("host", ["lanbox"])[0] or "lanbox").strip().lower()
            limit = int(query.get("limit", ["50"])[0])
            return self._send_json(
                {"commands": self.server.store.claim_pending_commands(host, limit=limit)}
            )
        if path == "/api/events":
            return self._stream_events(parsed.query)
        return self._send_error(HTTPStatus.NOT_FOUND, "Route not found.")

    def do_POST(self) -> None:
        if not self._validate_host_header():
            return
        parsed = urlparse(self.path)
        path = self._normalize_path(parsed.path)
        if not self._origin_is_allowed():
            return self._send_error(HTTPStatus.FORBIDDEN, "Origin is not allowed.")
        if path == "/api/devices/claim":
            body = self._read_json()
            try:
                claimed = self.server.store.claim_pair_code(
                    str(body.get("code", "")),
                    fallback_name=str(body.get("name") or "Phone"),
                )
            except ValueError as exc:
                return self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
            return self._send_json(claimed, status=HTTPStatus.CREATED)
        if path == "/api/devices/claim-pin":
            return self._handle_claim_pin()
        if path == "/api/events/token":
            device = self._require_device(require_csrf=True)
            if not device:
                return
            token = self.server.store.create_stream_token(device["id"])
            return self._send_json({"stream_token": token})
        if path == "/api/pair/codes":
            # A minted code is an enrollment credential: loopback only, else
            # any LAN host could pair itself.
            if not self._require_loopback():
                return
            body = self._read_json()
            pair = self._issue_pair_payload(body)
            return self._send_json(pair, status=HTTPStatus.CREATED)
        if path == "/api/admin/pair-codes":
            if not self._require_admin():
                return
            body = self._read_json()
            pair = self._issue_pair_payload(body)
            return self._send_json(pair, status=HTTPStatus.CREATED)
        if path == "/api/admin/devices/revoke":
            if not self._require_admin():
                return
            body = self._read_json()
            device_id = str(body.get("device_id", ""))
            try:
                device = self.server.store.revoke_device(device_id)
            except KeyError as exc:
                return self._send_error(HTTPStatus.NOT_FOUND, str(exc))
            return self._send_json({"device": device})
        if path == "/api/harness/approval-requests":
            return self._handle_harness_approval(parsed.query)
        if path == "/api/harness/updates":
            return self._handle_harness_update()
        if path == "/api/demo/approval":
            device = self._require_device(require_csrf=True)
            if not device:
                return
            approval = self.server.store.create_approval_request(demo_payload())
            return self._send_json({"approval": approval}, status=HTTPStatus.CREATED)
        if path == "/api/registry/ingest":
            return self._handle_registry_ingest(parsed.query)
        if path == "/api/harness/session":
            return self._handle_session_ingest()
        if path == "/api/squire/activity":
            return self._handle_squire_activity_ingest()
        if path.startswith("/api/commands/") and path.endswith("/ack"):
            return self._handle_command_ack(path)
        if path == "/api/registry/pin":
            device = self._require_device(require_csrf=True)
            if not device:
                return
            body = self._read_json()
            pin_rank = body.get("pin_rank")
            try:
                project = self.server.store.set_pin(
                    project_id=str(body.get("project_id", "")),
                    pinned=bool(body.get("pinned", True)),
                    pin_rank=int(pin_rank) if pin_rank is not None else None,
                )
            except KeyError as exc:
                return self._send_error(HTTPStatus.NOT_FOUND, str(exc))
            except ValueError as exc:
                return self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
            return self._send_json({"project": project})
        if path == "/api/registry/refresh":
            device = self._require_device(require_csrf=True)
            if not device:
                return
            summary = registry_refresh(self.server.store)
            return self._send_json({"summary": summary})
        if path == "/api/intel/refresh":
            device = self._require_device(require_csrf=True)
            if not device:
                return
            body = self._read_json()
            slugs = body.get("slugs")
            summary = refresh_intel(
                self.server.store,
                slugs=[str(s) for s in slugs] if slugs else None,
                force=bool(body.get("force", False)),
            )
            return self._send_json({"summary": summary})
        if path.startswith("/api/approvals/") and path.endswith("/decide"):
            device = self._require_device(require_csrf=True)
            if not device:
                return
            approval_id = path.removeprefix("/api/approvals/").removesuffix("/decide").strip("/")
            body = self._read_json()
            try:
                approval = self.server.store.resolve_approval(
                    approval_id=approval_id,
                    decision=str(body.get("decision", "")),
                    note=str(body.get("note") or ""),
                    device=device,
                )
            except KeyError as exc:
                return self._send_error(HTTPStatus.NOT_FOUND, str(exc))
            except ValueError as exc:
                return self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
            return self._send_json({"approval": approval})
        return self._send_error(HTTPStatus.NOT_FOUND, "Route not found.")

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.end_headers()

    def _handle_harness_approval(self, query_string: str) -> None:
        if not self._require_adapter():
            return
        body = self._read_json()
        approval = self.server.store.create_approval_request(body)
        query = parse_qs(query_string)
        wait = query.get("wait", ["false"])[0].lower() == "true"
        timeout = min(int(query.get("timeout", ["600"])[0]), 3600)
        if wait and approval["status"] == "pending":
            approval = self.server.store.wait_for_resolution(approval["id"], timeout)
        self._send_json({"approval": approval}, status=HTTPStatus.CREATED)

    def _handle_harness_update(self) -> None:
        if not self._require_adapter():
            return
        update = self.server.store.submit_update(self._read_json())
        self._send_json({"update": update}, status=HTTPStatus.CREATED)

    # Max remote crawl bundle: generous for a few dozen projects, bounded so a
    # bad/hostile client can't make us buffer an unbounded body.
    INGEST_MAX_BYTES = 2 * 1024 * 1024

    def _handle_registry_ingest(self, query_string: str = "") -> None:
        """Ingest a remote crawl bundle (e.g. from the remote host) into the registry. LAN-accepted,
        adapter-token gated (see _require_adapter_lan); only reachable under `serve --lan`.
        `?prune=1` gives full-sync (retire this host's rows absent from the bundle)."""
        from .registry.ingest import BundleError, ingest_bundle

        if not self._require_adapter_lan():
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > self.INGEST_MAX_BYTES:
            return self._send_error(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                f"Bundle exceeds {self.INGEST_MAX_BYTES} bytes.",
            )
        raw = self.rfile.read(length) if length else b""
        try:
            bundle = json.loads(raw.decode("utf-8")) if raw else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._send_error(HTTPStatus.BAD_REQUEST, "Body is not valid JSON.")
        prune = parse_qs(query_string).get("prune", ["0"])[0].lower() in {"1", "true", "yes"}
        try:
            summary = ingest_bundle(self.server.store, bundle, prune=prune)
        except BundleError as exc:
            return self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        return self._send_json(summary, status=HTTPStatus.CREATED)

    def _read_ingest_body(self) -> dict | None:
        """Adapter-LAN-gated JSON body read shared by the M6.5-Full remote ingests.
        Returns the parsed dict, or None after having already sent an error response."""
        if not self._require_adapter_lan():
            return None
        length = int(self.headers.get("Content-Length") or 0)
        if length > self.INGEST_MAX_BYTES:
            self._send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                             f"Body exceeds {self.INGEST_MAX_BYTES} bytes.")
            return None
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_error(HTTPStatus.BAD_REQUEST, "Body is not valid JSON.")
            return None
        if not isinstance(body, dict):
            self._send_error(HTTPStatus.BAD_REQUEST, "Body must be a JSON object.")
            return None
        return body

    def _handle_session_ingest(self) -> None:
        """M6.5-Full: a remote host (e.g. the remote host) pushes a harness session-report into the
        fleet log. LAN-accepted, adapter-token gated. `host` is validated to a short label."""
        from .registry.ingest import _HOST_RE

        body = self._read_ingest_body()
        if body is None:
            return
        if not str(body.get("harness") or "").strip():
            return self._send_error(HTTPStatus.BAD_REQUEST, "A harness is required.")
        host = str(body.get("host") or "").strip().lower()
        if host and not _HOST_RE.match(host):
            return self._send_error(HTTPStatus.BAD_REQUEST, "host must be a short kebab/lowercase label.")
        body["host"] = host
        session = self.server.store.record_harness_session(body)
        return self._send_json({"session": session}, status=HTTPStatus.CREATED)

    def _handle_command_ack(self, path: str) -> None:
        """Cross-machine control: the remote reports a command outcome. Adapter-LAN gated.
        `POST /api/commands/{id}/ack` with {status, result}. Idempotent (double-ack no-op)."""
        body = self._read_ingest_body()
        if body is None:
            return
        command_id = path[len("/api/commands/"):-len("/ack")]
        status = str(body.get("status") or "acked")
        result = body.get("result") if isinstance(body.get("result"), dict) else {}
        outcome = self.server.store.ack_command(command_id, status, result)
        if not outcome.get("ok"):
            return self._send_error(HTTPStatus.NOT_FOUND, outcome.get("error") or "Unknown command.")
        return self._send_json(outcome)

    def _handle_squire_activity_ingest(self) -> None:
        """M6.5-Full: a remote host pushes its live Squire activity + queue snapshot
        (current-state, schema-tolerant). LAN-accepted, adapter-token gated."""
        from .registry.ingest import _HOST_RE

        body = self._read_ingest_body()
        if body is None:
            return
        host = str(body.get("host") or "").strip().lower()
        if not host or not _HOST_RE.match(host):
            return self._send_error(HTTPStatus.BAD_REQUEST, "A short kebab/lowercase host label is required.")
        reported_at = str(body.get("reported_at") or "")
        snapshot = self.server.store.record_remote_snapshot(host, "squire", body, reported_at=reported_at)
        return self._send_json({"snapshot": snapshot}, status=HTTPStatus.CREATED)

    def _stream_events(self, query_string: str) -> None:
        query = parse_qs(query_string)
        stream_token = query.get("stream_token", [None])[0]
        device = self.server.store.claim_stream_token(stream_token)
        if device is None:
            return self._send_error(HTTPStatus.UNAUTHORIZED, "Invalid or expired stream token.")

        last_id_header = self.headers.get("Last-Event-ID") or query.get("last_id", ["0"])[0]
        try:
            last_id = int(last_id_header)
        except ValueError:
            last_id = 0

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        # Close at the cap so the client sees EOF and reconnects (with Last-Event-ID);
        # a lingering keep-alive socket would just hang until the client times out.
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()

        started = time.monotonic()
        last_heartbeat = 0.0
        while time.monotonic() - started < self.server.sse_cap_seconds:
            try:
                events = self.server.store.events_after(last_id)
            except Exception:
                # DB briefly unavailable (e.g. teardown/lock) — end the stream; the
                # client reconnects. Never crash the handler thread on a read blip.
                return
            if events:
                for event in events:
                    last_id = int(event["id"])
                    self._write_sse(event)
                continue
            if time.monotonic() - last_heartbeat > 15:
                # A NAMED event (not a ':comment'), so the client's EventSource can see
                # it and reset its staleness watchdog (E3.5).
                try:
                    self.wfile.write(b"event: heartbeat\ndata: {}\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                last_heartbeat = time.monotonic()
            time.sleep(0.75)

    def _write_sse(self, event: dict) -> None:
        data = json.dumps(event, sort_keys=True)
        try:
            self.wfile.write(f"id: {event['id']}\n".encode("utf-8"))
            self.wfile.write(f"event: {event['event_type']}\n".encode("utf-8"))
            self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            raise

    def _send_static(self, filename: str, content_type: str) -> None:
        path = STATIC_DIR / filename
        if not path.exists():
            return self._send_error(HTTPStatus.NOT_FOUND, "Static asset not found.")
        content = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        self._send_json({"error": message}, status=status)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def _bearer_token(self) -> str | None:
        value = self.headers.get("Authorization") or ""
        if value.lower().startswith("bearer "):
            return value[7:].strip()
        return None

    def _require_device(self, require_csrf: bool = False) -> dict | None:
        device = self.server.store.authenticate_device(self._bearer_token())
        if device is None:
            self._send_error(HTTPStatus.UNAUTHORIZED, "Device is not paired or token is invalid.")
            return None
        if require_csrf and not self.server.store.csrf_is_valid(device, self.headers.get("X-BigBoss-CSRF")):
            self._send_error(HTTPStatus.FORBIDDEN, "CSRF token is invalid.")
            return None
        return device

    def _client_key(self) -> str:
        return self.client_address[0]

    def _handle_claim_pin(self) -> None:
        client_key = self._client_key()
        if not pin_rate_limit_ok(client_key):
            return self._send_error(
                HTTPStatus.TOO_MANY_REQUESTS,
                "Too many failed PIN attempts. Wait a minute and try again.",
            )
        body = self._read_json()
        pin = str(body.get("pin", "")).strip()
        name = str(body.get("name") or body.get("device_name") or DEFAULT_DEVICE_NAME).strip() or DEFAULT_DEVICE_NAME
        if not pin or not token_matches(pin, self.server.lan_pin_hash):
            record_pin_failure(client_key)
            return self._send_error(HTTPStatus.UNAUTHORIZED, "PIN is invalid.")
        reset_pin_failures(client_key)
        claimed = self.server.store.enroll_device(name, method="lan_pin")
        return self._send_json(claimed, status=HTTPStatus.CREATED)

    def _pair_public_host(self) -> str:
        port = self.server.server_address[1]
        return pairing_host(port)

    def _issue_pair_payload(self, body: dict) -> dict:
        device_name = str(body.get("device_name") or body.get("name") or DEFAULT_DEVICE_NAME)
        ttl_seconds = int(body.get("ttl_seconds") or DEFAULT_PAIR_TTL_SECONDS)
        return issue_pair_code(
            self.server.store,
            device_name=device_name,
            ttl_seconds=ttl_seconds,
            public_host=self._pair_public_host(),
        )

    def _serve_desk_page(self, query: dict[list[str]]) -> None:
        autoshow = query.get("autoshow", ["0"])[0].lower() in {"1", "true", "yes"}
        port = self.server.server_address[1]
        phone_url = f"http://{pairing_host(port)}/"
        html_page = render_desk_page(phone_url=phone_url, port=port, autoshow=autoshow)
        encoded = html_page.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _serve_pair_page(self) -> None:
        payload = self._issue_pair_payload({})
        html_page = render_pair_page(payload)
        encoded = html_page.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    @staticmethod
    def _normalize_path(path: str) -> str:
        if path != "/" and path.endswith("/"):
            return path.rstrip("/")
        return path

    def _require_loopback(self) -> bool:
        if self._client_is_loopback():
            return True
        self._send_error(HTTPStatus.FORBIDDEN, "This endpoint is only available from localhost.")
        return False

    def _require_admin(self) -> bool:
        if not self._client_is_loopback():
            self._send_error(HTTPStatus.FORBIDDEN, "Admin endpoint is only available from localhost.")
            return False
        token = self.headers.get("X-Admin-Token")
        if token and token_matches(token, self.server.admin_token_hash):
            return True
        self._send_error(HTTPStatus.UNAUTHORIZED, "Admin token is invalid.")
        return False

    def _require_adapter(self) -> bool:
        if not self._client_is_loopback():
            self._send_error(HTTPStatus.FORBIDDEN, "Adapter endpoint is only available from localhost.")
            return False
        token = self.headers.get("X-Adapter-Token")
        if token and token_matches(token, self.server.adapter_token_hash):
            return True
        self._send_error(HTTPStatus.UNAUTHORIZED, "Adapter token is invalid.")
        return False

    def _require_adapter_lan(self) -> bool:
        """Adapter-token auth WITHOUT the loopback requirement — for machine-to-machine
        ingestion from a known LAN host (e.g. the remote host). Only reachable when serving with
        --lan; token-gated, with an optional source-IP allowlist (BIGBOSS_INGEST_HOSTS)."""
        allow = os.environ.get("BIGBOSS_INGEST_HOSTS", "").strip()
        if allow:
            allowed_ips = {h.strip() for h in allow.split(",") if h.strip()}
            if self.client_address[0] not in allowed_ips:
                self._send_error(HTTPStatus.FORBIDDEN, "Client address is not in BIGBOSS_INGEST_HOSTS.")
                return False
        token = self.headers.get("X-Adapter-Token")
        if token and token_matches(token, self.server.adapter_token_hash):
            return True
        self._send_error(HTTPStatus.UNAUTHORIZED, "Adapter token is invalid.")
        return False

    def _client_is_loopback(self) -> bool:
        try:
            if not ip_address(self.client_address[0]).is_loopback:
                return False
        except ValueError:
            return False
        
        host_header = self.headers.get("Host")
        if host_header:
            host_part = host_header.split(":")[0].lower()
            if host_part not in {"localhost", "127.0.0.1", "::1"}:
                return False
        return True

    def _validate_host_header(self) -> bool:
        import os
        host_header = self.headers.get("Host")
        if not host_header:
            self._send_error(HTTPStatus.BAD_REQUEST, "Missing Host header.")
            return False

        host_part = host_header.split(":")[0].lower()

        # Build list of allowed hosts
        allowed = {"localhost", "127.0.0.1", "::1"}

        server_ip = self.server.server_address[0]
        if server_ip:
            allowed.add(server_ip.lower())

        from .hosts import local_ip_hint, get_mdns_hostname
        allowed.add(local_ip_hint().lower())
        allowed.add(get_mdns_hostname().lower())

        if self.server.public_host:
            allowed.add(self.server.public_host.split(":")[0].lower())

        env_public = os.environ.get("BIGBOSS_PUBLIC_HOST")
        if env_public:
            allowed.add(env_public.split(":")[0].lower())

        if host_part not in allowed:
            self._send_error(HTTPStatus.FORBIDDEN, f"Host header '{host_part}' is not allowed.")
            return False

        # If it's an external host, enforce HTTPS (relays set X-Forwarded-Proto)
        is_local = host_part in {"localhost", "127.0.0.1", "::1"}
        if not is_local:
            forwarded_proto = self.headers.get("X-Forwarded-Proto")
            if forwarded_proto and forwarded_proto.lower() != "https":
                self._send_error(HTTPStatus.FORBIDDEN, "HTTPS is required for remote connections.")
                return False

        return True

    def _origin_is_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host")
        if not host:
            return False
        parsed = urlparse(origin)
        return parsed.netloc.lower() == host.lower()


def demo_payload() -> dict:
    return {
        "run_id": "run_demo_codex",
        "run_title": "Demo Codex session",
        "harness": "codex",
        "workspace": str(Path.cwd()),
        "title": "Approve package install",
        "summary": "The demo harness wants to install project dependencies before running tests.",
        "proposed_action": {
            "kind": "shell_command",
            "command": "uv sync",
            "cwd": str(Path.cwd()),
        },
        "diff_summary": {"files_changed": 0, "insertions": 0, "deletions": 0},
    }

