# Changelog

Notable changes to BigBoss, newest first. Dates are UTC.

## v0.1.0 - 2026-09-15

The first tagged version. It marks the point where a stranger can run the test suite and watch it pass on two operating systems. It does not change what the software does.

### What exists at this tag

- A local approval plane: an HTTP server, a SQLite store, Server-Sent Events, and a static web UI. Standard library only, zero runtime dependencies.
- Policy routing that classifies a proposed action as `auto_allowed`, `pending`, or `blocked`.
- Decisions bound to the `action_hash` of the proposed action, the workspace, and the policy version. An adapter may execute only the matching action. No MCP tool or API lets an agent approve its own request.
- Requests, decisions, runs, pair codes, and audit events persisted in SQLite across restarts.
- Harness integrations: an MCP stdio facade, a hook adapter, and a Codex app-server bridge.
- Phone approval over the LAN, opt-in with `--lan`, using per-device tokens and a one-time pair code.
- A project registry derived from local scans, a Council that ranks and researches it, a cost-metering router, and a Squire metering proxy.

### Added

- A GitHub Actions workflow that runs the full suite on ubuntu-latest and windows-latest against Python 3.12 and 3.13, on every push and pull request. No vendor API keys are configured, so no test may depend on one.
- This changelog.

### Changed

- The README now says what the repo is: a clone-and-run tool with no package to install. It carries the clone step and the prerequisites it was missing, so the quickstart can be followed on a clean machine.
- The Verify section now leads with the CI badge and a link to the run. The 2026-09-02 author-run observation is kept below it as dated history.

### Fixed

- `tests/test_harvest.py` asserted a Windows path fact through `Path`, so it failed anywhere that is not Windows. It asserts the same fact through `PureWindowsPath` now. No product code changed.
- `tests/test_registry_api.py::RegistryHTTPTests::test_daemons_route_returns_service_health` failed on Windows, the failure the README had recorded as author-run since 2026-09-02. A clean windows-latest runner reproduced it on both Python versions, so it is the platform and not one machine. The route probes six loopback endpoints serially with a two second connect timeout each, and on Windows those probes do not fail fast when nothing is listening, so the handler outran the test client's five second timeout. The probe is stubbed in that test now; the rest of the route still runs for real. The route's behavior is unchanged and is recorded under Limitations in the README.
- `tests/test_process_primitives.py` parented the process it then asked `terminate_pid` to kill. On POSIX that leaves a zombie, a zombie still answers `os.kill(pid, 0)`, and so the test read a successful kill as a failure. It spawns a detached process now, which is what the product operates on: both callers of `terminate_pid` act on pids found by scanning for orphans, never on their own children. No product code changed.

### Evidence boundary

Green CI is the author's own automation running on rented hardware. It is not independent reproduction, and this release claims none. There is no claim of external adoption, deployment, enterprise certification, security-sandbox completeness, or a measured benefit from using BigBoss. The claim is narrower than any of those: a working local MVP whose test suite a stranger can watch pass.
