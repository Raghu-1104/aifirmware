"""Environment and workspace health checks.

`fwcopilot doctor` answers "why isn't this working" before you have to ask:
missing cross toolchain, stale index, invalid board profile, absent credentials.
It exits non-zero only on failures that actually block work.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List

from .board import load_board
from .config import Config
from .store import Store

OK, WARN, FAIL = "ok", "warn", "fail"

TOOLCHAINS = [
    ("arm-none-eabi-gcc", "ARM Cortex-M/R cross compiler"),
    ("riscv64-unknown-elf-gcc", "RISC-V cross compiler"),
    ("xtensa-esp32-elf-gcc", "ESP32 (Xtensa) cross compiler"),
    ("avr-gcc", "AVR cross compiler"),
]
BUILD_TOOLS = [
    ("cmake", "CMake"),
    ("make", "GNU Make"),
    ("ninja", "Ninja"),
    ("west", "Zephyr meta-tool"),
    ("pio", "PlatformIO"),
    ("idf.py", "ESP-IDF"),
]
FLASH_TOOLS = [
    ("openocd", "OpenOCD"),
    ("pyocd", "pyOCD"),
    ("JLinkExe", "SEGGER J-Link"),
    ("st-flash", "stlink"),
    ("probe-rs", "probe-rs"),
    ("esptool.py", "esptool"),
]


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    hint: str = ""

    def format(self) -> str:
        mark = {OK: "ok  ", WARN: "warn", FAIL: "FAIL"}[self.status]
        out = f"  [{mark}] {self.name}"
        if self.detail:
            out += f": {self.detail}"
        if self.hint and self.status != OK:
            out += f"\n         → {self.hint}"
        return out


def _version_of(tool: str) -> str:
    for flag in ("--version", "-v", "version"):
        try:
            proc = subprocess.run([tool, flag], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        text = (proc.stdout or proc.stderr).strip().splitlines()
        if text:
            return text[0][:80]
    return "found"


def run_checks(cfg: Config, *, deep: bool = False) -> List[Check]:
    checks: List[Check] = []

    # --- workspace ------------------------------------------------------
    checks.append(Check("workspace", OK, str(cfg.root)))
    checks.append(
        Check("config", OK, cfg.rel(cfg.config_path))
        if cfg.config_path.is_file()
        else Check(
            "config",
            WARN,
            "missing, using defaults",
            "Run `fwcopilot init` to write .fwcopilot/config.yaml.",
        )
    )

    board = load_board(cfg.board_path) if cfg.board_path.is_file() else None
    if board is None or not board.exists:
        checks.append(
            Check(
                "board profile",
                WARN,
                "no board.yaml",
                "Run `fwcopilot board init` — without it answers cannot be board-specific.",
            )
        )
    else:
        problems = board.validate(cfg.root)
        if problems:
            checks.append(
                Check(
                    "board profile",
                    WARN,
                    f"{len(problems)} issue(s) in {cfg.rel(cfg.board_path)}",
                    "Run `fwcopilot board check` for the list.",
                )
            )
        else:
            checks.append(
                Check("board profile", OK, f"{board.name}, {len(board.components)} component(s)")
            )

    # --- index ----------------------------------------------------------
    if not cfg.db_path.is_file():
        checks.append(Check("index", FAIL, "not built", "Run `fwcopilot index`."))
    else:
        with Store(cfg.db_path) as store:
            stats = store.stats()
        if stats["chunks"] == 0:
            checks.append(Check("index", FAIL, "empty", "Run `fwcopilot index`."))
        else:
            detail = (
                f"{stats['datasheets']} datasheets, {stats['source_files']} files, "
                f"{stats['chunks']} chunks, {stats['registers']} registers"
            )
            if stats["datasheets"] == 0:
                checks.append(
                    Check(
                        "index",
                        WARN,
                        detail,
                        "No datasheets indexed — add one with `fwcopilot add-datasheet` so "
                        "hardware answers can cite pages.",
                    )
                )
            else:
                checks.append(Check("index", OK, detail))

        stale = _stale_sources(cfg)
        if stale:
            checks.append(
                Check(
                    "index freshness",
                    WARN,
                    f"{len(stale)} file(s) changed since the last index",
                    "Run `fwcopilot index` to refresh.",
                )
            )
        else:
            checks.append(Check("index freshness", OK, "up to date"))

    # --- credentials ----------------------------------------------------
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        checks.append(Check("credentials", OK, "ANTHROPIC_API_KEY is set"))
    elif (Path.home() / ".config" / "anthropic").is_dir():
        checks.append(Check("credentials", OK, "an `ant auth login` profile is present"))
    else:
        checks.append(
            Check(
                "credentials",
                WARN,
                "no API key or auth profile found",
                "export ANTHROPIC_API_KEY=... to enable chat. Index, search, lint, size and "
                "scaffold work without it.",
            )
        )

    # --- toolchain ------------------------------------------------------
    found_cross = [(t, d) for t, d in TOOLCHAINS if shutil.which(t)]
    if found_cross:
        for tool, desc in found_cross:
            checks.append(Check(f"toolchain: {tool}", OK, _version_of(tool) if deep else desc))
    else:
        checks.append(
            Check(
                "toolchain",
                WARN,
                "no cross compiler on PATH",
                "Install e.g. arm-none-eabi-gcc to build and to use `fwcopilot size`.",
            )
        )

    found_build = [(t, d) for t, d in BUILD_TOOLS if shutil.which(t)]
    checks.append(
        Check("build tools", OK, ", ".join(t for t, _ in found_build))
        if found_build
        else Check("build tools", WARN, "none found (cmake/make/ninja/west/pio)")
    )

    found_flash = [t for t, _ in FLASH_TOOLS if shutil.which(t)]
    checks.append(
        Check("flash/debug tools", OK, ", ".join(found_flash))
        if found_flash
        else Check("flash/debug tools", WARN, "none found (openocd/pyocd/J-Link/st-flash)")
    )

    if cfg.build_command:
        checks.append(Check("build command", OK, cfg.build_command))
    else:
        checks.append(
            Check(
                "build command",
                WARN,
                "not configured",
                "Set build.command in .fwcopilot/config.yaml to enable `fwcopilot build`.",
            )
        )

    return checks


def _stale_sources(cfg: Config, limit: int = 2000) -> List[str]:
    """Source files modified more recently than the index database."""
    if not cfg.db_path.is_file():
        return []
    index_mtime = cfg.db_path.stat().st_mtime
    stale: List[str] = []
    from .project import iter_source_files

    for path in iter_source_files(cfg.root, cfg.source_globs, cfg.excludes)[:limit]:
        try:
            if path.stat().st_mtime > index_mtime + 1:
                stale.append(str(path.relative_to(cfg.root)))
        except OSError:
            continue
    return stale


def worst_status(checks: List[Check]) -> str:
    if any(c.status == FAIL for c in checks):
        return FAIL
    if any(c.status == WARN for c in checks):
        return WARN
    return OK
