"""Assembles the persistent context the assistant carries into every session.

This is what makes the assistant "already know" the project: a firmware-engineer
system prompt, plus a live snapshot of the project structure, the board profile,
the datasheet catalogue and the project notes file. The snapshot is rebuilt from
cached index state so it costs nothing per turn, and it sits behind a prompt
cache breakpoint so repeated turns re-read it at cache rates.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .board import BoardProfile, load_board
from .config import Config
from .project import ProjectProfile, MemoryRegion, scan_project
from .store import Store

PROFILE_CACHE = "project_profile.json"
NOTES_BUDGET = 6000

SYSTEM_PERSONA = """\
You are fwcopilot, an embedded firmware engineering assistant working inside a \
specific firmware project for a specific custom board. You behave like a senior \
firmware engineer sitting next to the user: concrete, board-aware, and grounded \
in the actual datasheets rather than recollection.

How you work:
- The project snapshot, board profile and datasheet catalogue below describe THIS \
project. Treat them as authoritative over any general knowledge you have about \
similar chips or reference designs.
- Before answering anything that depends on a chip's real behaviour — register \
addresses and bit fields, timing, power-up sequences, electrical limits, command \
sets, protocol framing — call `search_datasheets` or `lookup_register` and answer \
from what you find. Cite the part and page like `BME280 p.28`. If a datasheet for \
the part is not indexed, say so plainly rather than inventing values.
- Before editing or reasoning about existing code, use `search_code` and \
`read_file` to see what is actually there. Do not assume APIs or file layout.
- Use the board profile's real pin names, bus instances and device addresses. \
Never substitute a generic example pinout for the user's board.
- Firmware defaults you should apply unless told otherwise: check every return \
code, never block in an ISR, keep ISRs short and hand work to the main loop or a \
task, mark memory shared with an ISR `volatile`, respect the datasheet's power-up \
and reset timing, prefer explicit-width types (`uint32_t`), and be explicit about \
units and endianness on the wire.
- When you write or change code, match the project's existing style, build system \
and HAL. Say which files you touched and why.
- When something depends on information you do not have (a schematic detail, a \
strap resistor, a scope trace), ask one specific question instead of guessing.
- Use `remember` to record durable project facts the user tells you (board quirks, \
decisions, errata workarounds) so future sessions start with them.

Be direct and technical. Skip preamble. Prefer a short correct answer with a \
citation over a long hedged one.
"""


def load_or_scan_profile(cfg: Config, refresh: bool = False) -> ProjectProfile:
    """Return the cached project profile, rescanning when missing or stale."""
    cache_path = cfg.cache_dir / PROFILE_CACHE
    if not refresh and cache_path.is_file():
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            regions = [MemoryRegion(**m) for m in data.pop("memory_regions", [])]
            profile = ProjectProfile(**data)
            profile.memory_regions = regions
            return profile
        except (json.JSONDecodeError, TypeError, OSError):
            pass
    profile = scan_project(cfg.root, cfg.source_globs, cfg.excludes)
    save_profile(cfg, profile)
    return profile


def save_profile(cfg: Config, profile: ProjectProfile) -> None:
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    (cfg.cache_dir / PROFILE_CACHE).write_text(
        json.dumps(profile.to_dict(), indent=2), encoding="utf-8"
    )


def datasheet_catalogue(store: Store) -> str:
    rows = store.list_docs("datasheet")
    if not rows:
        return (
            "## Datasheets\nNone indexed yet. The user can add one with "
            "`fwcopilot add-datasheet <file.pdf> --component U2`. Until then, say "
            "explicitly when an answer would need the datasheet you do not have."
        )
    lines = ["## Datasheets indexed (searchable, with page numbers)"]
    for row in rows:
        label = row["part"] or row["title"]
        attribution = f" [{row['component']}]" if row["component"] else ""
        lines.append(f"- {label}{attribution} — {row['pages']} pages — `{row['path']}`")
    return "\n".join(lines)


def read_notes(cfg: Config) -> str:
    if not cfg.notes_path.is_file():
        return ""
    text = cfg.notes_path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return ""
    if len(text) > NOTES_BUDGET:
        text = text[-NOTES_BUDGET:]
        text = "…(older notes truncated)…\n" + text
    return f"## Project notes (persistent memory)\n{text}"


def build_context_block(cfg: Config, store: Store, board: Optional[BoardProfile] = None) -> str:
    """The project/board/datasheet snapshot injected as system context."""
    board = board or load_board(cfg.board_path)
    profile = load_or_scan_profile(cfg)
    stats = store.stats()

    parts = [
        f"# Workspace: {cfg.name}",
        f"Root: `{cfg.root}`. Index: {stats['datasheets']} datasheets, "
        f"{stats['source_files']} source files, {stats['chunks']} searchable chunks, "
        f"{stats['registers']} extracted registers.",
        board.to_markdown(),
        profile.to_markdown(),
        datasheet_catalogue(store),
    ]
    if cfg.build_command:
        parts.append(f"## Build\nBuild command: `{cfg.build_command}`")
    notes = read_notes(cfg)
    if notes:
        parts.append(notes)
    return "\n\n".join(p for p in parts if p)


def build_system_blocks(
    cfg: Config, store: Store, board: Optional[BoardProfile] = None
) -> List[Dict[str, Any]]:
    """System prompt as content blocks, with a cache breakpoint on the snapshot.

    Persona first (never changes), snapshot second (changes only when the project
    does) — both inside the cached prefix, so a long chat re-reads them cheaply.
    """
    return [
        {"type": "text", "text": SYSTEM_PERSONA},
        {
            "type": "text",
            "text": build_context_block(cfg, store, board),
            "cache_control": {"type": "ephemeral"},
        },
    ]
