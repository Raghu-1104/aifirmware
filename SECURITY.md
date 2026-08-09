# Security Policy

## Reporting a vulnerability

Please report security issues privately through
[GitHub Security Advisories](https://github.com/Raghu-1104/aifirmware/security/advisories/new)
rather than opening a public issue. Include reproduction steps and the affected
version; you can expect an acknowledgement within a few days.

## Security model

Understanding what fwcopilot does with your data and your machine:

### What leaves your machine

- **Only what the assistant retrieves.** The index is a local SQLite file. When
  you chat, the system prompt (project snapshot, board profile, datasheet
  catalogue) and the excerpts a tool returns are sent to the Anthropic API.
  Whole datasheets and whole repositories are not uploaded.
- **Nothing at all for the offline commands.** `index`, `search`, `lint`,
  `size`, `board check`, `regs`, `scaffold` and `doctor` make no network calls.

### Filesystem access

- Every model-supplied path is resolved and rejected if it escapes the project
  root (`Config.resolve_in_root`).
- Writes and build execution are **off by default**. They require
  `--allow-write` / `--allow-build`, and each call then prompts for approval
  unless `--yes` is passed.
- `run_build` executes the command **you** configured in
  `.fwcopilot/config.yaml`. Treat that file as executable configuration: do not
  accept one from an untrusted source, and review it in code review like any
  other script.

### Server deployment

- `fwcopilot serve` binds to `127.0.0.1` by default.
- Set `FWCOPILOT_AUTH_TOKEN` (or `--auth-token`) before exposing it to anything
  beyond localhost. The server warns when bound to a non-local address without
  one, and compares tokens in constant time.
- There is no TLS: terminate it at a reverse proxy.
- CORS is same-origin unless you name origins with `--cors-origin`.
- `/healthz` and `/readyz` are intentionally unauthenticated so orchestrators
  can probe them; they expose no project data.
- The container runs as a non-root user (uid 10001).

### Secrets

- `ANTHROPIC_API_KEY` is read from the environment and never written to the
  workspace.
- Sessions in `.fwcopilot/sessions/` contain conversation history, including
  any file contents the assistant read. `.fwcopilot/index.db` contains your
  indexed source and datasheet text. Both are gitignored by default — keep it
  that way if your project is sensitive.
- Do not paste credentials into chat: they are persisted in the session file.

### Prompt injection

Datasheets and source files are untrusted input in the sense that matters here:
text inside them is retrieved and shown to the model. A crafted document could
attempt to influence the assistant. The mitigations are structural — the model
cannot write files or run commands without an explicit flag *and* per-call
approval, and it cannot reach outside the project root. Keep writes disabled
when working with documents from an untrusted source.

## Supported versions

The latest released minor version receives security fixes.
