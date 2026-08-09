"""Workspace discovery and configuration.

A fwcopilot workspace is any directory containing a `.fwcopilot/` folder. All
derived state (index database, sessions, extracted datasheet text) lives there
so it can be gitignored, while the two files a human edits — `config.yaml` and
`board.yaml` — are meant to be committed alongside the firmware.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from . import DEFAULT_MODEL

STATE_DIR = ".fwcopilot"

DEFAULT_SOURCE_GLOBS = [
    "**/*.c", "**/*.h", "**/*.cpp", "**/*.hpp", "**/*.cc", "**/*.ino",
    "**/*.s", "**/*.S", "**/*.ld", "**/*.dts", "**/*.dtsi", "**/*.overlay",
    "**/Kconfig*", "**/CMakeLists.txt", "**/*.cmake", "**/Makefile", "**/*.mk",
    "**/platformio.ini", "**/prj.conf", "**/sdkconfig*", "**/*.rs",
    "**/*.md",
]

DEFAULT_EXCLUDES = [
    ".git", ".venv", "venv", "node_modules", "build", "cmake-build-debug",
    ".pio", ".pio/build", "__pycache__", ".fwcopilot", "dist", "out",
    "Debug", "Release", ".vscode", ".idea", "twister-out", "zephyr/build",
]


@dataclass
class Config:
    """Resolved configuration for one workspace."""

    root: Path
    name: str = "firmware-project"
    model: str = DEFAULT_MODEL
    effort: str = "high"
    max_tokens: int = 16000
    datasheet_dir: str = "datasheets"
    board_file: str = "board.yaml"
    source_globs: List[str] = field(default_factory=lambda: list(DEFAULT_SOURCE_GLOBS))
    excludes: List[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    build_command: Optional[str] = None
    flash_command: Optional[str] = None
    permissions: Dict[str, str] = field(
        default_factory=lambda: {"write": "ask", "build": "ask"}
    )
    raw: Dict[str, Any] = field(default_factory=dict)

    # ---- derived paths -------------------------------------------------
    @property
    def state_dir(self) -> Path:
        return self.root / STATE_DIR

    @property
    def db_path(self) -> Path:
        return self.state_dir / "index.db"

    @property
    def sessions_dir(self) -> Path:
        return self.state_dir / "sessions"

    @property
    def cache_dir(self) -> Path:
        return self.state_dir / "cache"

    @property
    def notes_path(self) -> Path:
        """Long-lived project memory the assistant may append to."""
        return self.state_dir / "notes.md"

    @property
    def config_path(self) -> Path:
        return self.state_dir / "config.yaml"

    @property
    def datasheets_path(self) -> Path:
        return self.root / self.datasheet_dir

    @property
    def board_path(self) -> Path:
        return self.root / self.board_file

    def ensure_dirs(self) -> None:
        for p in (self.state_dir, self.sessions_dir, self.cache_dir, self.datasheets_path):
            p.mkdir(parents=True, exist_ok=True)

    def rel(self, path: Path) -> str:
        try:
            return str(Path(path).resolve().relative_to(self.root.resolve()))
        except ValueError:
            return str(path)

    def resolve_in_root(self, path: str) -> Path:
        """Resolve a user/model supplied path, refusing anything outside the workspace.

        Model-supplied paths are untrusted: this is the single choke point that
        keeps file tools inside the project directory.
        """
        candidate = (self.root / path).resolve() if not os.path.isabs(path) else Path(path).resolve()
        root = self.root.resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError(f"path escapes the project root: {path}")
        return candidate

    # ---- serialization -------------------------------------------------
    def to_yaml(self) -> str:
        data = {
            "project": {"name": self.name},
            "model": {"id": self.model, "effort": self.effort, "max_tokens": self.max_tokens},
            "paths": {"datasheets": self.datasheet_dir, "board": self.board_file},
            "index": {"source_globs": self.source_globs, "exclude": self.excludes},
            "build": {"command": self.build_command, "flash": self.flash_command},
            "permissions": self.permissions,
        }
        return yaml.safe_dump(data, sort_keys=False, default_flow_style=False)


def find_root(start: Optional[Path] = None) -> Optional[Path]:
    """Walk upward from `start` looking for a `.fwcopilot/` directory."""
    cur = (start or Path.cwd()).resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / STATE_DIR).is_dir():
            return candidate
    return None


def load_config(start: Optional[Path] = None) -> Config:
    root = find_root(start)
    if root is None:
        raise FileNotFoundError(
            "no fwcopilot workspace found here (or in any parent directory).\n"
            "Run `fwcopilot init` in your firmware project to create one."
        )
    return load_config_from_root(root)


def load_config_from_root(root: Path) -> Config:
    root = Path(root).resolve()
    cfg_path = root / STATE_DIR / "config.yaml"
    data: Dict[str, Any] = {}
    if cfg_path.is_file():
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    project = data.get("project") or {}
    model = data.get("model") or {}
    paths = data.get("paths") or {}
    index = data.get("index") or {}
    build = data.get("build") or {}

    cfg = Config(
        root=root,
        name=project.get("name") or root.name,
        model=model.get("id") or DEFAULT_MODEL,
        effort=model.get("effort") or "high",
        max_tokens=int(model.get("max_tokens") or 16000),
        datasheet_dir=paths.get("datasheets") or "datasheets",
        board_file=paths.get("board") or "board.yaml",
        source_globs=index.get("source_globs") or list(DEFAULT_SOURCE_GLOBS),
        excludes=index.get("exclude") or list(DEFAULT_EXCLUDES),
        build_command=build.get("command"),
        flash_command=build.get("flash"),
        permissions={**{"write": "ask", "build": "ask"}, **(data.get("permissions") or {})},
        raw=data,
    )
    return cfg


def init_workspace(root: Path, name: Optional[str] = None) -> Config:
    """Create `.fwcopilot/` plus starter config and board files."""
    root = Path(root).resolve()
    cfg = Config(root=root, name=name or root.name)
    cfg.ensure_dirs()
    if not cfg.config_path.exists():
        cfg.config_path.write_text(cfg.to_yaml(), encoding="utf-8")
    if not cfg.notes_path.exists():
        cfg.notes_path.write_text(
            "# Project notes\n\n"
            "Durable context for this firmware project. The assistant reads this on every\n"
            "session and appends to it when you tell it to remember something.\n",
            encoding="utf-8",
        )
    return cfg
