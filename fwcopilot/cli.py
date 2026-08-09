"""Command line interface for fwcopilot."""

from __future__ import annotations

import argparse
import shutil
import sys
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__
from .board import load_board, write_template
from .config import Config, init_workspace, load_config
from .context import load_or_scan_profile, save_profile
from .datasheets import ingest_datasheet, ingest_directory
from .project import index_sources, scan_project
from .store import Store

# ---- small output helpers --------------------------------------------------

def _c(code: str, text: str) -> str:
    return text if not sys.stdout.isatty() else f"\033[{code}m{text}\033[0m"


def bold(t: str) -> str: return _c("1", t)
def dim(t: str) -> str: return _c("2", t)
def green(t: str) -> str: return _c("32", t)
def yellow(t: str) -> str: return _c("33", t)
def red(t: str) -> str: return _c("31", t)
def cyan(t: str) -> str: return _c("36", t)


def die(message: str, code: int = 1) -> None:
    print(red(f"error: {message}"), file=sys.stderr)
    raise SystemExit(code)


def get_config(args: argparse.Namespace) -> Config:
    try:
        return load_config(Path(args.path) if getattr(args, "path", None) else None)
    except FileNotFoundError as exc:
        die(str(exc))
        raise  # unreachable, keeps type checkers happy


# ---- commands --------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.path or ".").resolve()
    root.mkdir(parents=True, exist_ok=True)
    cfg = init_workspace(root, args.name)
    print(green(f"Initialized fwcopilot workspace in {root}"))
    print(f"  config     {cfg.rel(cfg.config_path)}")
    print(f"  datasheets {cfg.rel(cfg.datasheets_path)}/")

    created = write_template(cfg.board_path)
    print(f"  board      {cfg.rel(cfg.board_path)}" + ("" if created else dim(" (already existed)")))

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
        stats = index_sources(
            store, cfg.root, cfg.source_globs, cfg.excludes, force=args.force
        )
        print(f"  {stats['indexed']} indexed, {stats['unchanged']} unchanged, "
              f"{stats['removed']} removed, {stats['chunks']} chunks")

        print(bold("Indexing datasheets…"))
        results = ingest_directory(
            store, cfg.datasheets_path, cfg.root,
            force=args.force, component_map=board.datasheet_map(),
        )
        if not results:
            print(dim(f"  no datasheets in {cfg.rel(cfg.datasheets_path)}/ yet"))
        for res in results:
            if res.skipped:
                mark = dim("skip") if res.reason == "unchanged" else yellow("skip")
                print(f"  {mark} {res.path} ({res.reason})")
            else:
                print(f"  {green('ok')}   {res.path}: {res.pages} pages, "
                      f"{res.chunks} chunks, {res.registers} registers")

        profile = scan_project(cfg.root, cfg.source_globs, cfg.excludes)
        save_profile(cfg, profile)
        totals = store.stats()
    print(green(f"\nIndex ready: {totals['datasheets']} datasheets, "
                f"{totals['source_files']} source files, {totals['chunks']} chunks, "
                f"{totals['registers']} registers"))
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
            store, dest, rel_path=rel, part=args.part,
            component=args.component, force=True,
        )
    if result.skipped:
        die(f"could not index {rel}: {result.reason}")
    print(green(
        f"Indexed {result.part or rel}: {result.pages} pages, "
        f"{result.chunks} searchable chunks, {result.registers} registers extracted"
    ))
    if args.component:
        print(dim(f"Attributed to board component {args.component}. "
                  f"Add `datasheet: {rel}` under that component in board.yaml to link it permanently."))
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
    print(f"## Index\n- {stats['datasheets']} datasheets, {stats['source_files']} source files, "
          f"{stats['chunks']} chunks, {stats['registers']} registers")
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
        body = hit.text if args.full else textwrap.shorten(
            " ".join(hit.text.split()), width=400, placeholder=" …"
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
    from .scaffold import TARGETS, generate, write_files

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
    print(dim("Generated code is a starting point: TODOs mark what must be confirmed "
              "against the datasheets."))
    return 0


def cmd_sessions(args: argparse.Namespace) -> int:
    cfg = get_config(args)
    files = sorted(cfg.sessions_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        print("No saved sessions yet.")
        return 0
    import json
    for path in files[:args.limit]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        msgs = data.get("messages", [])
        first = next(
            (m["content"] for m in msgs if m.get("role") == "user" and isinstance(m.get("content"), str)),
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
        cfg, store, runner, session=session,
        allow_write=allow_write, allow_build=allow_build,
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
            detail = event.data.get("query") or event.data.get("path") or \
                event.data.get("name") or event.data.get("note") or ""
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
        print(dim(f"board: {board.name if board.exists else 'no board.yaml'} · "
                  f"{stats['datasheets']} datasheets · {stats['source_files']} files indexed · "
                  f"session {agent.session.id}"))
        if not args.allow_write:
            print(dim("read-only session — pass --allow-write to let it edit files"))
        print(dim("Ctrl-D or /exit to quit, /reset to start a fresh session.\n"))

        while True:
            try:
                message = input(cyan("you › ")).strip()
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
    except ImportError:
        die("the web UI needs FastAPI: pip install 'fwcopilot[server]'")
        return 1
    run_server(cfg, host=args.host, port=args.port,
               allow_write=args.allow_write, allow_build=args.allow_build)
    return 0


# ---- parser ----------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fwcopilot",
        description="A firmware assistant that knows your project, your board and your datasheets.",
    )
    parser.add_argument("--version", action="version", version=f"fwcopilot {__version__}")
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

    p = sub.add_parser("sessions", help="list saved chat sessions")
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(func=cmd_sessions)

    p = sub.add_parser("serve", help="run the browser chat UI")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--allow-write", action="store_true")
    p.add_argument("--allow-build", action="store_true")
    p.set_defaults(func=cmd_serve)

    return parser


def _add_agent_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--allow-write", action="store_true", help="let the assistant edit files (asks first)")
    p.add_argument("--allow-build", action="store_true", help="let the assistant run the build command")
    p.add_argument("--yes", action="store_true", help="auto-approve write/build requests")
    p.add_argument("--continue", dest="continue_session", action="store_true",
                   help="resume the most recent session")
    p.add_argument("--session", help="resume a specific session id")


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print()
        return 130
    except BrokenPipeError:  # pragma: no cover
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
