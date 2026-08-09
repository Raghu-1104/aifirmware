"""Tool surface exposed to the model, plus their local implementations.

Read-only tools run automatically. Anything that changes the workspace or runs a
command goes through `approve`, so the caller (CLI prompt, server policy) decides
— the model never gets unilateral write or execute access.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable, ClassVar, Dict, List, Optional, Set

from .board import load_board
from .config import Config
from .diagnostics import BuildResult, parse_build_output
from .store import Store

MAX_TOOL_CHARS = 20000
BUILD_TIMEOUT_SECONDS = 900


def run_build(cfg: Config, timeout: int = BUILD_TIMEOUT_SECONDS) -> Optional[BuildResult]:
    """Run the configured build command and parse its diagnostics.

    Shared by the CLI's `build` command and the agent's `run_build` tool so both
    report identically. Returns None when no build command is configured.
    """
    command = cfg.build_command
    if not command:
        return None
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(cfg.root),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        returncode = proc.returncode
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        output += f"\n[build timed out after {timeout}s]"
        returncode = 124
    return BuildResult(
        command=command,
        returncode=returncode,
        output=output,
        diagnostics=parse_build_output(output),
    )


def tool_definitions(allow_write: bool, allow_build: bool) -> List[Dict[str, Any]]:
    tools: List[Dict[str, Any]] = [
        {
            "name": "search_datasheets",
            "description": (
                "Full-text search across every indexed datasheet for this board. Use this "
                "before stating any register address, bit field, timing parameter, "
                "electrical limit, command opcode or power-up sequence. Returns text "
                "excerpts with the part name and page number so you can cite them."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to look for, e.g. 'CTRL_MEAS oversampling settings' or 'I2C write timing tSU'.",
                    },
                    "component": {
                        "type": "string",
                        "description": "Optional: restrict to one part or board ref, e.g. 'BME280' or 'U2'.",
                    },
                    "limit": {"type": "integer", "description": "Max results (default 6, max 15)."},
                },
                "required": ["query"],
            },
        },
        {
            "name": "read_datasheet_page",
            "description": (
                "Read the full text of a specific datasheet page (and optionally the pages "
                "around it). Use after search_datasheets when an excerpt is cut off or you "
                "need the whole register table."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "datasheet": {
                        "type": "string",
                        "description": "Part name, board ref, or datasheet path.",
                    },
                    "page": {"type": "integer", "description": "1-based page number."},
                    "radius": {
                        "type": "integer",
                        "description": "Also include N pages either side (default 0, max 3).",
                    },
                },
                "required": ["datasheet", "page"],
            },
        },
        {
            "name": "lookup_register",
            "description": (
                "Look up a register by name or address in the register maps extracted from "
                "the indexed datasheets. Returns the address, page and description. Follow "
                "up with read_datasheet_page for the bit-level detail."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Register name or address, e.g. 'CTRL_MEAS' or '0xF4'.",
                    },
                    "component": {
                        "type": "string",
                        "description": "Optional part or board ref to narrow the search.",
                    },
                },
                "required": ["name"],
            },
        },
        {
            "name": "list_datasheets",
            "description": "List every indexed datasheet with its part name, board ref and page count.",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "get_board_profile",
            "description": (
                "Read the board profile: MCU, buses, every IC with its bus/address, and the "
                "full pin map. Use this whenever an answer involves a specific pin, bus "
                "instance, device address or chip select on this board."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "section": {
                        "type": "string",
                        "enum": ["all", "mcu", "components", "buses", "pins", "power"],
                        "description": "Which part of the profile to return (default 'all').",
                    }
                },
            },
        },
        {
            "name": "search_code",
            "description": (
                "Full-text search over the project's indexed source files (C/C++/asm, linker "
                "scripts, device trees, Kconfig, build files). Use before describing or "
                "changing existing code."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Symbol, function, macro or phrase to find.",
                    },
                    "path_prefix": {
                        "type": "string",
                        "description": "Optional path prefix filter, e.g. 'src/drivers'.",
                    },
                    "limit": {"type": "integer", "description": "Max results (default 6, max 15)."},
                },
                "required": ["query"],
            },
        },
        {
            "name": "read_file",
            "description": "Read a file from the project, optionally a line range. Paths are relative to the project root.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Project-relative path."},
                    "start_line": {
                        "type": "integer",
                        "description": "1-based first line (default 1).",
                    },
                    "line_count": {
                        "type": "integer",
                        "description": "How many lines to read (default 200, max 800).",
                    },
                },
                "required": ["path"],
            },
        },
        {
            "name": "list_files",
            "description": "List project files matching a glob pattern, e.g. 'src/**/*.c' or 'drivers/*'.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob relative to the project root (default '**/*').",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max paths to return (default 100).",
                    },
                },
            },
        },
        {
            "name": "run_lint",
            "description": (
                "Run firmware-specific static checks over the project's C sources: blocking "
                "or non-reentrant calls in ISRs, non-volatile globals shared with an ISR, "
                "FreeRTOS APIs used without their FromISR variant, busy-waits with no "
                "timeout, discarded HAL status codes, unbounded string functions, and "
                "hardware registers accessed without volatile. Read-only and fast — use it "
                "when reviewing code or when the user reports a hang or a race."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path_prefix": {
                        "type": "string",
                        "description": "Limit to a subtree, e.g. 'src/drivers'.",
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["error", "warning", "info"],
                        "description": "Minimum severity to report (default 'warning').",
                    },
                },
            },
        },
        {
            "name": "analyze_memory",
            "description": (
                "Report the firmware's flash and RAM budget from the latest build artifacts "
                "(ELF and/or linker map), measured against the part's capacity declared in "
                "board.yaml, including the largest contributing object files. Use when asked "
                "about size, whether something will fit, or after a region-overflow link error."
            ),
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "remember",
            "description": (
                "Append a durable fact about this project to the persistent notes file, which "
                "is loaded into context at the start of every future session. Use for board "
                "quirks, errata workarounds, design decisions and conventions the user states "
                "— not for transient chat details."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "note": {
                        "type": "string",
                        "description": "One or two sentences, self-contained.",
                    },
                },
                "required": ["note"],
            },
        },
    ]

    if allow_write:
        tools.append(
            {
                "name": "write_file",
                "description": (
                    "Create or overwrite a project file with full content. Requires user approval. "
                    "Read the file first if it already exists — this replaces it entirely."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Project-relative path."},
                        "content": {"type": "string", "description": "Complete new file content."},
                    },
                    "required": ["path", "content"],
                },
            }
        )
        tools.append(
            {
                "name": "edit_file",
                "description": (
                    "Replace an exact snippet in an existing file. Requires user approval. "
                    "`old_text` must appear exactly once in the file."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Project-relative path."},
                        "old_text": {
                            "type": "string",
                            "description": "Exact text to replace, including indentation.",
                        },
                        "new_text": {"type": "string", "description": "Replacement text."},
                    },
                    "required": ["path", "old_text", "new_text"],
                },
            }
        )

    if allow_build:
        tools.append(
            {
                "name": "run_build",
                "description": (
                    "Run the project's configured build command and return its output. "
                    "Requires user approval. Use after making code changes to check they compile."
                ),
                "input_schema": {"type": "object", "properties": {}},
            }
        )

    return tools


@dataclass
class ToolResult:
    text: str
    is_error: bool = False
    summary: str = ""


ApprovalFn = Callable[[str, Dict[str, Any]], bool]


class ToolRunner:
    """Executes tool calls against a workspace."""

    WRITE_TOOLS: ClassVar[Set[str]] = {"write_file", "edit_file"}
    EXEC_TOOLS: ClassVar[Set[str]] = {"run_build"}

    def __init__(
        self,
        cfg: Config,
        store: Store,
        *,
        approve: Optional[ApprovalFn] = None,
        allow_write: bool = False,
        allow_build: bool = False,
    ):
        self.cfg = cfg
        self.store = store
        self.approve = approve or (lambda name, args: False)
        self.allow_write = allow_write
        self.allow_build = allow_build

    # ---- dispatch ------------------------------------------------------
    def run(self, name: str, args: Dict[str, Any]) -> ToolResult:
        handler = getattr(self, f"_t_{name}", None)
        if handler is None:
            return ToolResult(f"Unknown tool '{name}'.", is_error=True)

        if name in self.WRITE_TOOLS and not self.allow_write:
            return ToolResult(
                "Writing is disabled in this session. Show the user the proposed change "
                "instead; they can re-run with --allow-write to let you apply it.",
                is_error=True,
            )
        if name in self.EXEC_TOOLS and not self.allow_build:
            return ToolResult("Running the build is disabled in this session.", is_error=True)
        if name in self.WRITE_TOOLS | self.EXEC_TOOLS and not self.approve(name, args):
            return ToolResult("The user declined this action.", is_error=True)

        try:
            result = handler(args)
        except Exception as exc:  # surfaced to the model so it can adapt
            return ToolResult(f"{type(exc).__name__}: {exc}", is_error=True)
        if len(result.text) > MAX_TOOL_CHARS:
            result.text = result.text[:MAX_TOOL_CHARS] + "\n…[truncated]"
        return result

    # ---- datasheet tools -----------------------------------------------
    def _t_search_datasheets(self, args: Dict[str, Any]) -> ToolResult:
        query = str(args.get("query", "")).strip()
        limit = max(1, min(int(args.get("limit") or 6), 15))
        component = args.get("component")
        hits = self.store.search(query, kind="datasheet", part=component, limit=limit)
        if not hits:
            available = [r["part"] or r["title"] for r in self.store.list_docs("datasheet")]
            note = (
                f" Indexed datasheets: {', '.join(available)}."
                if available
                else " No datasheets are indexed for this project yet."
            )
            return ToolResult(f"No datasheet matches for '{query}'.{note}")
        out = [f"{len(hits)} result(s) for '{query}':\n"]
        for i, hit in enumerate(hits, 1):
            heading = f" — {hit.heading}" if hit.heading else ""
            out.append(f"[{i}] {hit.locator()}{heading}\n{hit.text}\n")
        return ToolResult("\n".join(out), summary=f"{len(hits)} hits")

    def _t_read_datasheet_page(self, args: Dict[str, Any]) -> ToolResult:
        target = str(args.get("datasheet", "")).strip()
        page = int(args.get("page") or 1)
        radius = max(0, min(int(args.get("radius") or 0), 3))
        doc = self._resolve_datasheet(target)
        if doc is None:
            names = [r["part"] or r["title"] for r in self.store.list_docs("datasheet")]
            return ToolResult(
                f"No indexed datasheet matches '{target}'. Available: {', '.join(names) or 'none'}.",
                is_error=True,
            )
        label = doc["part"] or doc["title"]
        blocks: List[str] = []
        for p in range(max(1, page - radius), page + radius + 1):
            rows = self.store.page_text(doc["id"], p)
            if not rows:
                continue
            text = "\n".join(r["text"] for r in rows)
            blocks.append(f"--- {label} p.{p} ---\n{text}")
        if not blocks:
            return ToolResult(
                f"{label} has no indexed text on page {page} (document has {doc['pages']} pages).",
                is_error=True,
            )
        return ToolResult("\n\n".join(blocks), summary=f"{label} p.{page}")

    def _t_lookup_register(self, args: Dict[str, Any]) -> ToolResult:
        name = str(args.get("name", "")).strip()
        rows = self.store.find_registers(name, args.get("component"))
        if not rows:
            fallback = self.store.search(
                name, kind="datasheet", part=args.get("component"), limit=4
            )
            if fallback:
                body = "\n\n".join(f"[{h.locator()}] {h.text[:800]}" for h in fallback)
                return ToolResult(
                    f"No extracted register entry named '{name}'. Closest datasheet text:\n\n{body}"
                )
            return ToolResult(f"No register '{name}' found in the indexed datasheets.")
        lines = [f"{len(rows)} register match(es) for '{name}':"]
        for r in rows:
            part = r["part"] or r["title"]
            desc = f" — {r['description']}" if r["description"] else ""
            lines.append(f"- {part}: {r['name']} @ {r['address']} (p.{r['page']}){desc}")
        lines.append("\nUse read_datasheet_page for the bit-field detail on those pages.")
        return ToolResult("\n".join(lines), summary=f"{len(rows)} registers")

    def _t_list_datasheets(self, args: Dict[str, Any]) -> ToolResult:
        rows = self.store.list_docs("datasheet")
        if not rows:
            return ToolResult(
                "No datasheets indexed. Add one with `fwcopilot add-datasheet <pdf>`."
            )
        lines = [f"{len(rows)} datasheet(s) indexed:"]
        for r in rows:
            ref = f" [{r['component']}]" if r["component"] else ""
            lines.append(f"- {r['part'] or r['title']}{ref}: {r['pages']} pages, `{r['path']}`")
        return ToolResult("\n".join(lines))

    def _resolve_datasheet(self, target: str):
        target_l = target.lower().strip()
        rows = self.store.list_docs("datasheet")
        for row in rows:
            for field in ("part", "component", "path", "title"):
                value = row[field]
                if value and str(value).lower() == target_l:
                    return row
        for row in rows:
            for field in ("part", "component", "path", "title"):
                value = row[field]
                if value and target_l in str(value).lower():
                    return row
        return None

    # ---- board ---------------------------------------------------------
    def _t_get_board_profile(self, args: Dict[str, Any]) -> ToolResult:
        section = str(args.get("section") or "all").lower()
        board = load_board(self.cfg.board_path)
        if not board.exists:
            return ToolResult(
                "No board.yaml in this project. Ask the user to run `fwcopilot board init` and "
                "fill in the MCU, components and pin map so answers can be board-specific."
            )
        if section in ("all", ""):
            problems = board.validate(self.cfg.root)
            text = board.to_markdown(full=True)
            if problems:
                text += "\n\n### Board profile warnings\n" + "\n".join(f"- {p}" for p in problems)
            return ToolResult(text)
        data = board.data.get(section if section != "components" else "components")
        if data is None:
            return ToolResult(f"board.yaml has no '{section}' section.")
        return ToolResult(json.dumps(data, indent=2, default=str))

    # ---- code ----------------------------------------------------------
    def _t_search_code(self, args: Dict[str, Any]) -> ToolResult:
        query = str(args.get("query", "")).strip()
        limit = max(1, min(int(args.get("limit") or 6), 15))
        hits = self.store.search(
            query, kind="code", limit=limit, path_prefix=args.get("path_prefix")
        )
        if not hits:
            return ToolResult(f"No source matches for '{query}'.")
        out = [f"{len(hits)} result(s) for '{query}':\n"]
        for hit in hits:
            sym = f" (in {hit.heading})" if hit.heading else ""
            out.append(f"--- {hit.path}:{hit.line}-{hit.end_line}{sym} ---\n{hit.text}\n")
        return ToolResult("\n".join(out), summary=f"{len(hits)} hits")

    def _t_read_file(self, args: Dict[str, Any]) -> ToolResult:
        path = self.cfg.resolve_in_root(str(args["path"]))
        if not path.is_file():
            return ToolResult(f"No such file: {args['path']}", is_error=True)
        start = max(1, int(args.get("start_line") or 1))
        count = max(1, min(int(args.get("line_count") or 200), 800))
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        window = lines[start - 1 : start - 1 + count]
        if not window:
            return ToolResult(f"{args['path']} has {len(lines)} lines; nothing at line {start}.")
        numbered = "\n".join(f"{start + i:>5} | {line}" for i, line in enumerate(window))
        header = f"{self.cfg.rel(path)} lines {start}-{start + len(window) - 1} of {len(lines)}"
        return ToolResult(f"{header}\n{numbered}", summary=header)

    def _t_list_files(self, args: Dict[str, Any]) -> ToolResult:
        pattern = str(args.get("pattern") or "**/*")
        limit = max(1, min(int(args.get("limit") or 100), 500))
        matches: List[str] = []
        for path in sorted(self.cfg.root.glob(pattern)):
            if not path.is_file():
                continue
            rel = path.relative_to(self.cfg.root)
            if any(part in self.cfg.excludes for part in rel.parts):
                continue
            matches.append(str(rel))
            if len(matches) >= limit:
                break
        if not matches:
            return ToolResult(f"No files match '{pattern}'.")
        return ToolResult(f"{len(matches)} file(s):\n" + "\n".join(matches))

    # ---- analysis ------------------------------------------------------
    def _t_run_lint(self, args: Dict[str, Any]) -> ToolResult:
        from .lint import SEVERITIES, lint_paths, summarize
        from .project import iter_source_files

        minimum = str(args.get("severity") or "warning")
        rank = {sev: i for i, sev in enumerate(SEVERITIES)}
        threshold = rank.get(minimum, 1)

        paths = iter_source_files(self.cfg.root, self.cfg.source_globs, self.cfg.excludes)
        prefix = args.get("path_prefix")
        if prefix:
            paths = [p for p in paths if str(p.relative_to(self.cfg.root)).startswith(prefix)]

        findings = [
            f for f in lint_paths(paths, self.cfg.root) if rank.get(f.severity, 2) <= threshold
        ]
        if not findings:
            return ToolResult(
                f"No findings at severity '{minimum}' or above across {len(paths)} file(s).",
                summary="clean",
            )
        counts = summarize(findings)
        head = (
            f"{len(findings)} finding(s): {counts['error']} error, "
            f"{counts['warning']} warning, {counts['info']} info\n"
        )
        body = "\n".join(f.format() for f in findings[:60])
        if len(findings) > 60:
            body += f"\n… and {len(findings) - 60} more"
        return ToolResult(head + body, summary=f"{counts['error']}E/{counts['warning']}W")

    def _t_analyze_memory(self, args: Dict[str, Any]) -> ToolResult:
        from .board import load_board
        from .memory import analyze, discover_artifacts, run_size_tool

        elf, map_file = discover_artifacts(self.cfg.root, self.cfg.elf_path, self.cfg.map_path)
        if not elf and not map_file:
            return ToolResult(
                "No build artifacts found. Build the firmware first, or set build.elf / "
                "build.map in .fwcopilot/config.yaml.",
                is_error=True,
            )
        size_output = None
        if elf:
            try:
                size_output, _ = run_size_tool(elf)
            except (FileNotFoundError, OSError, Exception):
                size_output = None
        map_text = map_file.read_text(encoding="utf-8", errors="replace") if map_file else None

        report = analyze(
            load_board(self.cfg.board_path), size_output=size_output, map_text=map_text
        )
        header = "Artifacts: " + ", ".join(
            self.cfg.rel(p) for p in (elf, map_file) if p is not None
        )
        return ToolResult(
            f"{header}\n\n{report.to_markdown()}",
            summary=f"flash {report.flash_pct:.0f}%" if report.flash_pct else "size report",
        )

    # ---- memory --------------------------------------------------------
    def _t_remember(self, args: Dict[str, Any]) -> ToolResult:
        note = str(args.get("note", "")).strip()
        if not note:
            return ToolResult("Empty note; nothing recorded.", is_error=True)
        stamp = time.strftime("%Y-%m-%d")
        self.cfg.notes_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.cfg.notes_path, "a", encoding="utf-8") as fh:
            fh.write(f"\n- ({stamp}) {note}\n")
        return ToolResult(f"Recorded in {self.cfg.rel(self.cfg.notes_path)}.", summary=note[:60])

    # ---- mutation ------------------------------------------------------
    def _t_write_file(self, args: Dict[str, Any]) -> ToolResult:
        path = self.cfg.resolve_in_root(str(args["path"]))
        content = str(args.get("content", ""))
        existed = path.is_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        verb = "Updated" if existed else "Created"
        rel = self.cfg.rel(path)
        return ToolResult(
            f"{verb} {rel} ({len(content.splitlines())} lines). "
            "Re-run `fwcopilot index` to refresh search.",
            summary=f"{verb} {rel}",
        )

    def _t_edit_file(self, args: Dict[str, Any]) -> ToolResult:
        path = self.cfg.resolve_in_root(str(args["path"]))
        if not path.is_file():
            return ToolResult(f"No such file: {args['path']}", is_error=True)
        old = str(args["old_text"])
        new = str(args["new_text"])
        text = path.read_text(encoding="utf-8")
        occurrences = text.count(old)
        if occurrences == 0:
            return ToolResult(
                "old_text was not found in the file. Read the file again and copy the exact "
                "text, including indentation.",
                is_error=True,
            )
        if occurrences > 1:
            return ToolResult(
                f"old_text appears {occurrences} times; include more surrounding context "
                "so it identifies exactly one location.",
                is_error=True,
            )
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        rel = self.cfg.rel(path)
        return ToolResult(f"Edited {rel}.", summary=f"Edited {rel}")

    def _t_run_build(self, args: Dict[str, Any]) -> ToolResult:
        result = run_build(self.cfg)
        if result is None:
            return ToolResult(
                "No build command configured. Set `build.command` in .fwcopilot/config.yaml.",
                is_error=True,
            )
        # Structured diagnostics beat a wall of log text: the model gets
        # file:line facts instead of having to re-read the build output.
        body = result.format()
        if not result.ok and not result.diagnostics:
            body += "\n\nLast output lines:\n" + "\n".join(result.output.strip().splitlines()[-25:])
        return ToolResult(body, is_error=not result.ok, summary=result.summary())
