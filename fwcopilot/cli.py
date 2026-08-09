"""Command line interface for fwcopilot."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__
from .board import load_board, write_template
from .config import Config, init_workspace, load_config
from .context import load_or_scan_profile, save_profile
from .datasheets import ingest_datasheet, ingest_directory
from .errors import ExitCode, FwcopilotError, ResourceNotFoundError
from .logging_setup import configure_logging, get_logger
from .project import index_sources, scan_project
from .store import Store

# ---- small output helpers --------------------------------------------------


def _c(code: str, text: str) -> str:
    return text if not sys.stdout.isatty() else f"\033[{code}m{text}\033[0m"


def bold(t: str) -> str:
    return _c("1", t)


def dim(t: str) -> str:
    return _c("2", t)


def green(t: str) -> str:
    return _c("32", t)


def yellow(t: str) -> str:
    return _c("33", t)


def red(t: str) -> str:
    return _c("31", t)


def cyan(t: str) -> str:
    return _c("36", t)


def die(message: str, code: int = ExitCode.ERROR) -> None:
    print(red(f"error: {message}"), file=sys.stderr)
    raise SystemExit(code)


def get_config(args: argparse.Namespace) -> Config:
    """Load the workspace config; raises FwcopilotError, handled in main()."""
    return load_config(Path(args.path) if getattr(args, "path", None) else None)


# ---- commands --------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.path or ".").resolve()
    root.mkdir(parents=True, exist_ok=True)
    cfg = init_workspace(root, args.name)
    print(green(f"Initialized fwcopilot workspace in {root}"))
    print(f"  config     {cfg.rel(cfg.config_path)}")
    print(f"  datasheets {cfg.rel(cfg.datasheets_path)}/")

    created = write_template(cfg.board_path)
    print(
        f"  board      {cfg.rel(cfg.board_path)}" + ("" if created else dim(" (already existed)"))
    )

    profile = scan_project(cfg.root, cfg.source_globs, cfg.excludes)
    save_profile(cfg, profile)
    print()
    print(profile.to_markdown())
    print()
    print(bold("Next steps:"))
    print(f"  1. Describe your board:   $EDITOR {cfg.rel(cfg.board_path)}")
    print("  2. Add datasheets:        fwcopilot add-datasheet path/to/ic.pdf --component U2")
    print("  3. Build the index:       fwcopilot index")
    print("  4. Ask something:         fwcopilot chat")
    return 0


def cmd_index(args: argparse.Namespace) -> int:
    cfg = get_config(args)
    cfg.ensure_dirs()
    board = load_board(cfg.board_path)
    with Store(cfg.db_path) as store:
        print(bold("Indexing source files…"))
        stats = index_sources(store, cfg.root, cfg.source_globs, cfg.excludes, force=args.force)
        print(
            f"  {stats['indexed']} indexed, {stats['unchanged']} unchanged, "
            f"{stats['removed']} removed, {stats['chunks']} chunks"
        )

        print(bold("Indexing datasheets…"))
        results = ingest_directory(
            store,
            cfg.datasheets_path,
            cfg.root,
            force=args.force,
            component_map=board.datasheet_map(),
        )
        if not results:
            print(dim(f"  no datasheets in {cfg.rel(cfg.datasheets_path)}/ yet"))
        for res in results:
            if res.skipped:
                mark = dim("skip") if res.reason == "unchanged" else yellow("skip")
                print(f"  {mark} {res.path} ({res.reason})")
            else:
                print(
                    f"  {green('ok')}   {res.path}: {res.pages} pages, "
                    f"{res.chunks} chunks, {res.registers} registers"
                )

        profile = scan_project(cfg.root, cfg.source_globs, cfg.excludes)
        save_profile(cfg, profile)
        totals = store.stats()
    print(
        green(
            f"\nIndex ready: {totals['datasheets']} datasheets, "
            f"{totals['source_files']} source files, {totals['chunks']} chunks, "
            f"{totals['registers']} registers"
        )
    )
    return 0


def cmd_add_datasheet(args: argparse.Namespace) -> int:
    cfg = get_config(args)
    cfg.ensure_dirs()
    src = Path(args.file).expanduser()
    if not src.is_file():
        die(f"no such file: {src}")

    dest = cfg.datasheets_path / src.name
    try:
        already_inside = src.resolve().is_relative_to(cfg.datasheets_path.resolve())  # py3.9+
    except AttributeError:  # pragma: no cover - Python < 3.9 guard
        already_inside = str(src.resolve()).startswith(str(cfg.datasheets_path.resolve()))
    if not already_inside:
        if dest.exists() and not args.force:
            die(f"{cfg.rel(dest)} already exists (use --force to replace)")
        shutil.copy2(src, dest)
        print(f"Copied to {cfg.rel(dest)}")
    else:
        dest = src

    rel = cfg.rel(dest)
    with Store(cfg.db_path) as store:
        result = ingest_datasheet(
            store,
            dest,
            rel_path=rel,
            part=args.part,
            component=args.component,
            force=True,
        )
    if result.skipped:
        die(f"could not index {rel}: {result.reason}")
    print(
        green(
            f"Indexed {result.part or rel}: {result.pages} pages, "
            f"{result.chunks} searchable chunks, {result.registers} registers extracted"
        )
    )
    if args.component:
        print(
            dim(
                f"Attributed to board component {args.component}. "
                f"Add `datasheet: {rel}` under that component in board.yaml to link it permanently."
            )
        )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = get_config(args)
    board = load_board(cfg.board_path)
    with Store(cfg.db_path) as store:
        stats = store.stats()
        datasheets = store.list_docs("datasheet")
    profile = load_or_scan_profile(cfg, refresh=getattr(args, "refresh", False))

    print(bold(f"fwcopilot {__version__} — {cfg.name}"))
    print(f"root:  {cfg.root}")
    print(f"model: {cfg.model} (effort={cfg.effort})")
    print()
    print(profile.to_markdown())
    print()
    print(board.to_markdown())
    print()
    print(
        f"## Index\n- {stats['datasheets']} datasheets, {stats['source_files']} source files, "
        f"{stats['chunks']} chunks, {stats['registers']} registers"
    )
    for row in datasheets:
        print(f"  - {row['part'] or row['title']} ({row['pages']}p) {dim(row['path'])}")
    if not stats["chunks"]:
        print(yellow("\nIndex is empty — run `fwcopilot index`."))
    problems = board.validate(cfg.root)
    if problems:
        print(yellow(f"\nBoard profile warnings ({len(problems)}): run `fwcopilot board check`"))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    cfg = get_config(args)
    kind = None if args.kind == "all" else args.kind
    with Store(cfg.db_path) as store:
        hits = store.search(args.query, kind=kind, part=args.component, limit=args.limit)
    if not hits:
        print("No matches.")
        return 1
    for hit in hits:
        print(bold(cyan(hit.locator())) + (dim(f"  {hit.heading}") if hit.heading else ""))
        body = (
            hit.text
            if args.full
            else textwrap.shorten(" ".join(hit.text.split()), width=400, placeholder=" …")
        )
        print(textwrap.indent(body, "    "))
        print()
    return 0


def cmd_board(args: argparse.Namespace) -> int:
    cfg = get_config(args)
    if args.board_command == "init":
        created = write_template(cfg.board_path, overwrite=args.force)
        if created:
            print(green(f"Wrote board template to {cfg.rel(cfg.board_path)}"))
            print("Fill in your MCU, components, buses and pin map, then run `fwcopilot index`.")
        else:
            print(yellow(f"{cfg.rel(cfg.board_path)} already exists (use --force to overwrite)"))
        return 0

    board = load_board(cfg.board_path)
    if args.board_command == "show":
        print(board.to_markdown(full=True))
        return 0

    problems = board.validate(cfg.root)
    if not problems:
        print(green("Board profile looks consistent."))
        return 0
    print(yellow(f"{len(problems)} issue(s) in {cfg.rel(cfg.board_path)}:"))
    for p in problems:
        print(f"  - {p}")
    return 1


def cmd_scaffold(args: argparse.Namespace) -> int:
    from .scaffold import generate, write_files

    cfg = get_config(args)
    board = load_board(cfg.board_path)
    try:
        files = generate(board, args.target)
    except ValueError as exc:
        die(str(exc))
        return 1

    out_dir = Path(args.out).resolve() if args.out else cfg.root
    if args.dry_run:
        print(bold(f"Would generate {len(files)} file(s) into {out_dir}:"))
        for rel, content in files:
            marker = yellow(" (exists — needs --force)") if (out_dir / rel).exists() else ""
            print(f"  {rel} ({len(content.splitlines())} lines){marker}")
        return 0

    written = write_files(out_dir, files, force=args.force)
    created = [f for f in written if not f.existed]
    skipped = [f for f in written if f.existed and not args.force]
    for f in created:
        print(f"  {green('write')} {f.path}")
    for f in skipped:
        print(f"  {yellow('skip ')} {f.path} (exists — use --force to overwrite)")
    print(green(f"\nGenerated {len(created)} file(s) for target '{args.target}' in {out_dir}"))
    if skipped:
        print(dim(f"{len(skipped)} existing file(s) left untouched."))
    print(
        dim(
            "Generated code is a starting point: TODOs mark what must be confirmed "
            "against the datasheets."
        )
    )
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from .doctor import FAIL, WARN, run_checks, worst_status

    cfg = get_config(args)
    checks = run_checks(cfg, deep=args.deep)
    print(bold(f"fwcopilot doctor — {cfg.name}"))
    for check in checks:
        print(check.format())
    status = worst_status(checks)
    print()
    if status == FAIL:
        print(red("Some checks failed — see the suggestions above."))
        return ExitCode.FINDINGS
    if status == WARN:
        print(yellow("Everything essential works; the warnings above are worth fixing."))
        return 0
    print(green("All checks passed."))
    return 0


def cmd_lint(args: argparse.Namespace) -> int:
    import json as _json

    from .lint import RULE_DOCS, SEVERITIES, lint_paths, summarize
    from .project import iter_source_files

    cfg = get_config(args)
    if args.explain:
        rule = args.explain.upper()
        if rule not in RULE_DOCS:
            die(f"unknown rule '{rule}'. Known rules: {', '.join(sorted(RULE_DOCS))}")
        print(f"{rule}: {RULE_DOCS[rule]}")
        return 0

    paths = iter_source_files(cfg.root, cfg.source_globs, cfg.excludes)
    if args.path:
        paths = [p for p in paths if str(p.relative_to(cfg.root)).startswith(args.path)]

    rank = {sev: i for i, sev in enumerate(SEVERITIES)}
    threshold = rank[args.severity]
    findings = [f for f in lint_paths(paths, cfg.root) if rank.get(f.severity, 2) <= threshold]
    counts = summarize(findings)

    if args.format == "json":
        print(
            _json.dumps({"findings": [f.to_dict() for f in findings], "counts": counts}, indent=2)
        )
    else:
        for finding in findings:
            print(finding.format(color=sys.stdout.isatty()))
        c_files = sum(1 for p in paths if p.suffix.lower() in {".c", ".cpp", ".cc", ".ino"})
        print()
        if findings:
            print(
                f"{len(findings)} finding(s) in {c_files} file(s): "
                f"{red(str(counts['error']) + ' error')}, "
                f"{yellow(str(counts['warning']) + ' warning')}, "
                f"{counts['info']} info"
            )
            print(dim("Explain a rule with: fwcopilot lint --explain FW001"))
        else:
            print(
                green(
                    f"No findings at severity '{args.severity}' or above across {c_files} file(s)."
                )
            )

    if counts["error"]:
        return ExitCode.FINDINGS
    if args.strict and findings:
        return ExitCode.FINDINGS
    return 0


def cmd_size(args: argparse.Namespace) -> int:
    import json as _json

    from .memory import analyze, discover_artifacts, run_size_tool

    cfg = get_config(args)
    board = load_board(cfg.board_path)

    elf, map_file = discover_artifacts(cfg.root, args.elf or cfg.elf_path, args.map or cfg.map_path)
    if not elf and not map_file:
        raise ResourceNotFoundError(
            "no build artifacts found",
            "Build the firmware first, pass --elf/--map, or set build.elf / build.map "
            "in .fwcopilot/config.yaml.",
        )

    size_output = None
    if elf:
        try:
            size_output, tool = run_size_tool(elf, args.size_tool)
            print(dim(f"{tool} {cfg.rel(elf)}"))
        except (FileNotFoundError, OSError, subprocess.SubprocessError) as exc:
            print(yellow(f"could not run a size tool ({exc}); falling back to the map file"))
    if map_file:
        print(dim(f"map: {cfg.rel(map_file)}"))
    map_text = map_file.read_text(encoding="utf-8", errors="replace") if map_file else None

    report = analyze(board, size_output=size_output, map_text=map_text)
    if args.format == "json":
        print(
            _json.dumps(
                {
                    "flash_used": report.sizes.flash,
                    "flash_total": report.flash_total,
                    "ram_used": report.sizes.ram,
                    "ram_total": report.ram_total,
                    "text": report.sizes.text,
                    "data": report.sizes.data,
                    "bss": report.sizes.bss,
                    "objects": [
                        {"name": o.name, "flash": o.flash, "ram": o.ram}
                        for o in report.objects[: args.top]
                    ],
                    "warnings": report.warnings,
                },
                indent=2,
            )
        )
    else:
        print()
        print(report.to_markdown(top=args.top))
    return ExitCode.FINDINGS if report.over_budget else 0


def cmd_build(args: argparse.Namespace) -> int:
    from .tools import run_build

    cfg = get_config(args)
    if not cfg.build_command:
        raise FwcopilotError(
            "no build command configured",
            "Set build.command in .fwcopilot/config.yaml, e.g. `cmake --build build`.",
        )
    print(dim(f"$ {cfg.build_command}"))
    result = run_build(cfg)
    assert result is not None
    if args.verbose_output:
        print(result.output)
    print()
    print(result.format())
    return 0 if result.ok else ExitCode.ERROR


def cmd_regs(args: argparse.Namespace) -> int:
    from .regsgen import available_parts, collect_registers, generate_header

    cfg = get_config(args)
    with Store(cfg.db_path) as store:
        parts = available_parts(store)
        if args.list:
            if not parts:
                print("No datasheets indexed.")
                return 0
            for part in parts:
                count = len(collect_registers(store, part))
                print(f"  {part}: {count} register(s) extracted")
            return 0

        if not args.part:
            die("a part is required (or use --list)", ExitCode.USAGE)
        match = next((p for p in parts if p.lower() == args.part.lower()), None)
        if match is None:
            match = next((p for p in parts if args.part.lower() in p.lower()), None)
        if match is None:
            raise ResourceNotFoundError(
                f"no indexed datasheet matches '{args.part}'",
                f"Indexed parts: {', '.join(parts) if parts else 'none'}.",
            )
        registers = collect_registers(store, match)
        doc = next(
            (d for d in store.list_docs("datasheet") if (d["part"] or d["title"]) == match), None
        )
        header = generate_header(match, registers, source=doc["path"] if doc else "")

    if args.out:
        out_path = cfg.resolve_in_root(args.out)
        if out_path.exists() and not args.force:
            die(f"{cfg.rel(out_path)} exists (use --force to overwrite)")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(header, encoding="utf-8")
        print(green(f"Wrote {len(registers)} register definition(s) to {cfg.rel(out_path)}"))
        print(dim("Each #define carries the datasheet page it came from — spot-check before use."))
    else:
        print(header, end="")
    return 0


def cmd_sessions(args: argparse.Namespace) -> int:
    cfg = get_config(args)
    files = sorted(cfg.sessions_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        print("No saved sessions yet.")
        return 0
    import json

    for path in files[: args.limit]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        msgs = data.get("messages", [])
        first = next(
            (
                m["content"]
                for m in msgs
                if m.get("role") == "user" and isinstance(m.get("content"), str)
            ),
            "(empty)",
        )
        print(f"{bold(path.stem)}  {len(msgs)} messages")
        print(dim("    " + textwrap.shorten(first, width=100, placeholder=" …")))
    return 0


# ---- chat ------------------------------------------------------------------


def _make_agent(cfg: Config, store: Store, args: argparse.Namespace):
    from .agent import Agent, Session
    from .tools import ToolRunner

    allow_write = bool(getattr(args, "allow_write", False))
    allow_build = bool(getattr(args, "allow_build", False))

    def approve(name: str, tool_args: Dict[str, Any]) -> bool:
        if getattr(args, "yes", False):
            return True
        if not sys.stdin.isatty():
            return False
        print()
        print(yellow(f"  ⚠ {name} requests approval:"))
        if name == "write_file":
            content = str(tool_args.get("content", ""))
            preview = "\n".join(content.splitlines()[:12])
            print(f"    path: {tool_args.get('path')} ({len(content.splitlines())} lines)")
            print(textwrap.indent(preview, "    | "))
            if len(content.splitlines()) > 12:
                print(dim("    | …"))
        elif name == "edit_file":
            print(f"    path: {tool_args.get('path')}")
            print(red(textwrap.indent(str(tool_args.get("old_text", ""))[:400], "    - ")))
            print(green(textwrap.indent(str(tool_args.get("new_text", ""))[:400], "    + ")))
        elif name == "run_build":
            print(f"    command: {cfg.build_command}")
        try:
            answer = input("    allow? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return answer in ("y", "yes")

    session = None
    if getattr(args, "continue_session", False):
        session = Session.latest(cfg)
    elif getattr(args, "session", None):
        session = Session.load(cfg, args.session)

    runner = ToolRunner(
        cfg, store, approve=approve, allow_write=allow_write, allow_build=allow_build
    )
    return Agent(
        cfg,
        store,
        runner,
        session=session,
        allow_write=allow_write,
        allow_build=allow_build,
    )


def _render_turn(agent, message: str) -> bool:
    """Stream one turn to the terminal. Returns False if the turn errored."""
    printed_any = False
    ok = True
    for event in agent.stream_turn(message):
        if event.type == "text_delta":
            sys.stdout.write(event.text)
            sys.stdout.flush()
            printed_any = True
        elif event.type == "tool_use":
            detail = (
                event.data.get("query")
                or event.data.get("path")
                or event.data.get("name")
                or event.data.get("note")
                or ""
            )
            if printed_any:
                print()
                printed_any = False
            print(dim(f"  · {event.name}({textwrap.shorten(str(detail), 70, placeholder='…')})"))
        elif event.type == "tool_result":
            if event.data.get("is_error"):
                print(dim(yellow(f"    ↳ {event.text or 'error'}")))
            elif event.text:
                print(dim(f"    ↳ {event.text}"))
        elif event.type == "error":
            print(red(f"\n{event.text}"))
            ok = False
        elif event.type == "done":
            print()
    return ok


def cmd_ask(args: argparse.Namespace) -> int:
    cfg = get_config(args)
    with Store(cfg.db_path) as store:
        if not store.stats()["chunks"]:
            print(yellow("Index is empty — running `fwcopilot index` first would help."))
        agent = _make_agent(cfg, store, args)
        ok = _render_turn(agent, args.question)
    return 0 if ok else 1


def cmd_chat(args: argparse.Namespace) -> int:
    cfg = get_config(args)
    with Store(cfg.db_path) as store:
        stats = store.stats()
        agent = _make_agent(cfg, store, args)
        board = load_board(cfg.board_path)

        print(bold(f"fwcopilot — {cfg.name}") + dim(f"  ({cfg.model})"))
        print(
            dim(
                f"board: {board.name if board.exists else 'no board.yaml'} · "
                f"{stats['datasheets']} datasheets · {stats['source_files']} files indexed · "
                f"session {agent.session.id}"
            )
        )
        if not args.allow_write:
            print(dim("read-only session — pass --allow-write to let it edit files"))
        print(dim("Ctrl-D or /exit to quit, /reset to start a fresh session.\n"))

        while True:
            try:
                message = input(cyan("you › ")).strip()  # noqa: RUF001 - deliberate prompt glyph
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not message:
                continue
            if message in ("/exit", "/quit"):
                break
            if message == "/reset":
                from .agent import Session

                agent.session = Session(cfg)
                print(dim(f"new session {agent.session.id}\n"))
                continue
            if message == "/status":
                cmd_status(args)
                continue
            print()
            try:
                _render_turn(agent, message)
            except KeyboardInterrupt:
                print(red("\n[interrupted]"))
            print()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    cfg = get_config(args)
    try:
        from .server import run_server
    except ImportError as exc:
        raise FwcopilotError(
            f"the web UI needs FastAPI and uvicorn ({exc})",
            "Install them with: pip install 'fwcopilot[server]'",
        ) from exc
    run_server(
        cfg,
        host=args.host,
        port=args.port,
        allow_write=args.allow_write,
        allow_build=args.allow_build,
        auth_token=args.auth_token,
        cors_origins=[o.strip() for o in (args.cors_origin or []) if o.strip()],
    )
    return 0


# ---- parser ----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fwcopilot",
        description="A firmware assistant that knows your project, your board and your datasheets.",
    )
    parser.add_argument("--version", action="version", version=f"fwcopilot {__version__}")
    parser.add_argument(
        "-v", "--verbose", action="count", default=0, help="more logging (-vv for debug)"
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="errors only")
    parser.add_argument("--log-file", help="also write JSON logs to this file")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create a fwcopilot workspace in this project")
    p.add_argument("path", nargs="?", default=".", help="project directory (default: .)")
    p.add_argument("--name", help="project name")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("index", help="(re)index source files and datasheets")
    p.add_argument("--force", action="store_true", help="reindex even if unchanged")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("add-datasheet", help="add and index an IC datasheet (PDF/TXT/MD)")
    p.add_argument("file", help="path to the datasheet")
    p.add_argument("--component", help="board ref this IC is on, e.g. U2")
    p.add_argument("--part", help="part number override, e.g. BME280")
    p.add_argument("--force", action="store_true", help="overwrite an existing copy")
    p.set_defaults(func=cmd_add_datasheet)

    p = sub.add_parser("status", help="show project, board and index state")
    p.add_argument("--refresh", action="store_true", help="rescan the project tree")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("search", help="search datasheets and code directly (no model call)")
    p.add_argument("query")
    p.add_argument("--kind", choices=["all", "datasheet", "code"], default="all")
    p.add_argument("--component", help="restrict to a part or board ref")
    p.add_argument("--limit", type=int, default=6)
    p.add_argument("--full", action="store_true", help="print whole chunks")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("board", help="create, show or validate the board profile")
    p.add_argument("board_command", choices=["init", "show", "check"], nargs="?", default="show")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_board)

    from .scaffold import TARGETS

    p = sub.add_parser("scaffold", help="generate firmware skeleton + drivers from board.yaml")
    p.add_argument("--target", choices=list(TARGETS), default="cmsis-bare")
    p.add_argument("--out", help="output directory (default: project root)")
    p.add_argument("--force", action="store_true", help="overwrite existing files")
    p.add_argument("--dry-run", action="store_true", help="list what would be written")
    p.set_defaults(func=cmd_scaffold)

    p = sub.add_parser("ask", help="ask one question and print the answer")
    p.add_argument("question")
    _add_agent_flags(p)
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("chat", help="interactive firmware chat session")
    _add_agent_flags(p)
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("doctor", help="check toolchain, index, board and credentials")
    p.add_argument("--deep", action="store_true", help="also report tool versions")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("lint", help="firmware-specific static checks (ISR safety, HAL status, …)")
    p.add_argument(
        "--severity",
        choices=["error", "warning", "info"],
        default="warning",
        help="minimum severity to report (default: warning)",
    )
    p.add_argument("--path", help="limit to a subtree, e.g. src/drivers")
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.add_argument("--strict", action="store_true", help="exit non-zero on any finding")
    p.add_argument("--explain", help="describe a rule, e.g. FW001")
    p.set_defaults(func=cmd_lint)

    p = sub.add_parser("size", help="flash/RAM budget from the latest build artifacts")
    p.add_argument("--elf", help="path to the ELF (default: newest found)")
    p.add_argument("--map", help="path to the linker map (default: newest found)")
    p.add_argument("--size-tool", help="override the `size` binary")
    p.add_argument("--top", type=int, default=10, help="how many contributors to list")
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.set_defaults(func=cmd_size)

    p = sub.add_parser("build", help="run the configured build and parse its diagnostics")
    p.add_argument("--verbose-output", action="store_true", help="also print raw build output")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("regs", help="generate a C register header from an indexed datasheet")
    p.add_argument("part", nargs="?", help="part name, e.g. BME280")
    p.add_argument("--out", help="write to this file instead of stdout")
    p.add_argument("--list", action="store_true", help="list parts with extracted registers")
    p.add_argument("--force", action="store_true", help="overwrite an existing file")
    p.set_defaults(func=cmd_regs)

    p = sub.add_parser("sessions", help="list saved chat sessions")
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(func=cmd_sessions)

    p = sub.add_parser("serve", help="run the browser chat UI")
    p.add_argument("--host", default="127.0.0.1", help="bind address (default: localhost only)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--allow-write", action="store_true")
    p.add_argument("--allow-build", action="store_true")
    p.add_argument("--auth-token", help="require this bearer token (or set FWCOPILOT_AUTH_TOKEN)")
    p.add_argument(
        "--cors-origin",
        action="append",
        help="allow this browser origin (repeatable; default: same-origin only)",
    )
    p.set_defaults(func=cmd_serve)

    return parser


def _add_agent_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--allow-write", action="store_true", help="let the assistant edit files (asks first)"
    )
    p.add_argument(
        "--allow-build", action="store_true", help="let the assistant run the build command"
    )
    p.add_argument("--yes", action="store_true", help="auto-approve write/build requests")
    p.add_argument(
        "--continue",
        dest="continue_session",
        action="store_true",
        help="resume the most recent session",
    )
    p.add_argument("--session", help="resume a specific session id")


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(
        verbosity=getattr(args, "verbose", 0),
        quiet=getattr(args, "quiet", False),
        log_file=Path(args.log_file) if getattr(args, "log_file", None) else None,
    )
    log = get_logger("cli")
    log.debug("running command %s", args.command)
    try:
        return int(args.func(args) or 0)
    except FwcopilotError as exc:
        print(red(f"error: {exc.message}"), file=sys.stderr)
        if exc.hint:
            print(dim(exc.hint), file=sys.stderr)
        log.debug("command failed", exc_info=True)
        return exc.exit_code
    except KeyboardInterrupt:
        print()
        return ExitCode.INTERRUPTED
    except BrokenPipeError:  # pragma: no cover
        return ExitCode.OK


if __name__ == "__main__":
    raise SystemExit(main())
