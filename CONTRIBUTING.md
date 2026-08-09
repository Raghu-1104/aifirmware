# Contributing to fwcopilot

Thanks for helping. This project is a tool firmware engineers rely on while
bringing up real hardware, so the bar is: **be correct, be honest about limits,
and never silently guess at a hardware fact.**

## Getting set up

```bash
git clone https://github.com/Raghu-1104/aifirmware
cd aifirmware
make install          # venv + editable install with dev + server extras
make check            # lint + types + tests — the same gate CI runs
```

No `make`? The equivalents are:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[server,dev]"
ruff check fwcopilot tests && ruff format --check fwcopilot tests
mypy fwcopilot
pytest
```

## Ground rules

**Never invent a hardware fact.** Register addresses, timings and electrical
limits come from an indexed datasheet with a page citation, or the tool says it
does not know. A plausible-looking wrong address costs someone a day on a bench.

**Analysis features must be deterministic.** `lint`, `size`, `board check`,
`scaffold` and `regs` never call the model. They work offline, without
credentials, and produce the same output for the same input — which is what
makes them safe to run in CI.

**Everything is testable without the network.** No test may make a real API
call. The agent loop is tested against a scripted fake client
(`tests/test_tools_and_agent.py`); follow that pattern.

**Model-supplied paths are untrusted.** Anything reaching the filesystem goes
through `Config.resolve_in_root()`. Writes and builds stay behind the approval
callback.

## Adding a lint rule

1. Add the pattern and the check in `fwcopilot/lint.py`, emitting a `Finding`
   with a `file:line`, a severity, and a **suggestion that says what to do
   instead** — a rule that only says "this is bad" wastes the reader's time.
2. Register it in `RULE_DOCS` (the test suite asserts every emitted rule is
   documented).
3. Add tests in `tests/test_analysis.py`: one that fires, and — just as
   important — **one that must not fire**. False positives are what make people
   disable a linter.
4. Confirm the rule survives `strip_noncode`: it must not trigger on the same
   text inside a comment or a string literal.

Severity guide: `error` = a real bug on real hardware; `warning` = very likely
wrong or fragile; `info` = a design smell worth knowing about.

## Adding an agent tool

Tools live in `fwcopilot/tools.py`. A tool needs:

- A `description` that says **when to reach for it**, not just what it does.
  This is the single biggest factor in whether the model uses it correctly.
- A handler `_t_<name>` returning a `ToolResult`. Raise nothing — return
  `is_error=True` with text the model can act on.
- Read-only by default. If it mutates the workspace or executes anything, add
  it to `ToolRunner.WRITE_TOOLS` / `EXEC_TOOLS` so it goes through approval.
- Tests covering the success path, the failure path, and the permission gate.

## Style

- `ruff format` decides formatting; don't hand-align.
- Type annotations on public functions; `mypy fwcopilot` must be clean.
- Comments explain *why*, not *what*. The interesting comments in this codebase
  are the ones recording a hardware or API constraint.
- We keep Python 3.9 support, so `typing.Optional`/`Dict`/`List` rather than
  PEP 585/604 syntax (see the note in `pyproject.toml`).

## Pull requests

- One concern per PR.
- `make check` passes.
- New behaviour has tests; changed behaviour has updated tests.
- Update `CHANGELOG.md` under `## [Unreleased]`.
- Say plainly in the description what you did **not** verify. "Untested against
  real hardware" is a perfectly good note; a silent assumption is not.

## Releasing

1. Update `CHANGELOG.md` and bump `version` in `pyproject.toml`.
2. Tag `vX.Y.Z` and push. The release workflow re-runs the full gate, verifies
   the tag matches the package version, builds, and publishes a GitHub release.
   PyPI publishing stays off until the repository sets `PUBLISH_TO_PYPI=true`
   and configures a Trusted Publisher.
