# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] — 2026-08-09

Production hardening plus the analysis features that make fwcopilot useful even
when you never open a chat session.

### Added

- **`fwcopilot lint`** — firmware-specific static analysis with ten rules
  (FW001–FW011): blocking or non-reentrant calls in ISRs, floating point in
  ISRs, non-volatile globals written from an ISR, busy-waits with no timeout,
  discarded HAL status codes, unbounded string functions, dynamic allocation,
  blocking inside a critical section, FreeRTOS APIs missing their `FromISR`
  variant, and hardware registers accessed through non-volatile pointers.
  Comment- and string-aware, so a `// TODO: remove HAL_Delay` never trips it.
  JSON output and CI-friendly exit codes.
- **`fwcopilot size`** — flash/RAM budget from `size` output and GNU ld map
  files, measured against the part's capacity from `board.yaml`, with the
  largest contributing object files and over-budget warnings.
- **`fwcopilot build`** — runs the configured build and parses compiler and
  linker output into structured diagnostics, with firmware-specific
  explanations for region overflow, undefined references and multiple
  definitions.
- **`fwcopilot regs`** — generates a C register header from a datasheet's
  extracted register map, each `#define` citing the page it came from.
- **`fwcopilot doctor`** — checks toolchain, build tools, flash/debug probes,
  index freshness, board profile and credentials.
- Agent tools `run_lint` and `analyze_memory`; `run_build` now returns parsed
  diagnostics rather than raw log text.
- Server: bearer-token auth (`--auth-token` / `FWCOPILOT_AUTH_TOKEN`),
  `/healthz` and `/readyz` probes, configurable CORS origins, request size
  limits, and a `/api/lint` endpoint. The web UI prompts for and stores a token.
- Deployment: multi-stage `Dockerfile` running as a non-root user with a
  healthcheck, `docker-compose.yml`, and a `Makefile` of developer entry points.
- CI: lint, format, mypy, a five-version × three-OS test matrix, an end-to-end
  smoke job that compiles the generated firmware, wheel build and install
  verification, and a Docker image build.
- Typed error hierarchy with stable exit codes, structured logging
  (human or JSON via `FWCOPILOT_LOG_FORMAT`), config validation and
  `FWCOPILOT_*` environment overrides.
- `py.typed` marker — the package ships type information.

### Changed

- `main()` returns an exit code instead of raising `SystemExit`, so the CLI is
  callable as a library and testable without catching exceptions.
- An explicitly empty `model.id` in the config is now an error rather than
  being silently replaced by the default.
- Register headers align their columns; part numbers like `LSM6DSOX` are no
  longer mangled by the datasheet-filename cleanup.

### Fixed

- `guess_part_number` stripped `ds` from inside part numbers.
- The shipped `board.yaml` template referenced SPI pins that were absent from
  its own pin map.
- Requests no longer hand the SDK a live reference to the session history,
  which could mutate while a request was in flight.

## [0.1.0] — 2026-08-09

### Added

- Datasheet ingestion (PDF/TXT/MD) into page-addressable chunks with heuristic
  register-map extraction.
- Board profile (`board.yaml`) with validation for pin conflicts, I2C address
  collisions, undeclared buses and missing datasheet files.
- Project scanning: build system, toolchain, RTOS, linker memory map, ISRs and
  peripherals in use.
- SQLite FTS5 + BM25 retrieval over datasheets and source, offline and
  dependency-light.
- Streaming chat agent with persistent sessions, project notes memory, path
  sandboxing and per-call approval for writes and builds.
- Deterministic scaffolding from the board profile: pin header, per-IC driver
  skeletons, `main.c`, Cortex-M startup, linker script, and Makefile /
  PlatformIO / Zephyr build files.
- CLI and a self-contained streaming web UI.

[Unreleased]: https://github.com/Raghu-1104/aifirmware/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Raghu-1104/aifirmware/releases/tag/v0.2.0
[0.1.0]: https://github.com/Raghu-1104/aifirmware/releases/tag/v0.1.0
