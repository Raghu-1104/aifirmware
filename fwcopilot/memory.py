"""Flash/RAM budget analysis from `size` output and GNU ld map files.

"Will this still fit?" is a question firmware engineers ask on every commit, and
the answer is fully determined by build artifacts — no model call needed. This
module turns those artifacts into a budget report and, because `board.yaml`
already declares the part's flash and RAM, can say how much headroom is left
even when the linker script is generous.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .board import BoardProfile

# Berkeley format:  text  data  bss  dec  hex  filename
_BERKELEY_RE = re.compile(
    r"^\s*(?P<text>\d+)\s+(?P<data>\d+)\s+(?P<bss>\d+)\s+(?P<dec>\d+)\s+(?P<hex>[0-9a-fA-F]+)\s+(?P<file>\S+)"
)
# SysV format:  .section  size  addr
_SYSV_RE = re.compile(r"^(?P<name>\.[\w.\-$]+)\s+(?P<size>\d+)\s+(?P<addr>\d+)\s*$")

# Map file "Memory Configuration" rows.
_MEMCFG_RE = re.compile(
    r"^(?P<name>\*?\w[\w*]*)\s+0x(?P<origin>[0-9a-fA-F]+)\s+0x(?P<length>[0-9a-fA-F]+)"
)
# Map file allocation rows:  .text.foo   0x08000100   0x2c   build/main.o
_ALLOC_RE = re.compile(
    r"^\s+(?P<section>\.[\w.\-$]+)?\s*0x(?P<addr>[0-9a-fA-F]+)\s+0x(?P<size>[0-9a-fA-F]+)\s+(?P<obj>\S+)\s*$"
)
_SECTION_ONLY_RE = re.compile(r"^\s+(?P<section>\.[\w.\-$]+)\s*$")

FLASH_NAMES = {"FLASH", "ROM", "APP", "PROGRAM", "CODE", "IROM", "FLASH_APP"}
RAM_NAMES = {"RAM", "SRAM", "DRAM", "RAM1", "SRAM1", "DATA", "IRAM"}


@dataclass
class Sizes:
    text: int = 0
    data: int = 0
    bss: int = 0

    @property
    def flash(self) -> int:
        """Flash holds code, read-only data, and the initialisers for .data."""
        return self.text + self.data

    @property
    def ram(self) -> int:
        return self.data + self.bss


@dataclass
class Region:
    name: str
    origin: int
    length: int


@dataclass
class ObjectUsage:
    name: str
    flash: int = 0
    ram: int = 0

    @property
    def total(self) -> int:
        return self.flash + self.ram


@dataclass
class MemoryReport:
    sizes: Sizes
    flash_total: Optional[int] = None
    ram_total: Optional[int] = None
    flash_source: str = ""
    ram_source: str = ""
    regions: List[Region] = field(default_factory=list)
    objects: List[ObjectUsage] = field(default_factory=list)
    sections: Dict[str, int] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    @property
    def flash_pct(self) -> Optional[float]:
        if not self.flash_total:
            return None
        return 100.0 * self.sizes.flash / self.flash_total

    @property
    def ram_pct(self) -> Optional[float]:
        if not self.ram_total:
            return None
        return 100.0 * self.sizes.ram / self.ram_total

    @property
    def over_budget(self) -> bool:
        return bool(
            (self.flash_total and self.sizes.flash > self.flash_total)
            or (self.ram_total and self.sizes.ram > self.ram_total)
        )

    def to_markdown(self, top: int = 10) -> str:
        lines = ["## Memory budget", ""]
        lines.append(_bar_line("Flash", self.sizes.flash, self.flash_total, self.flash_source))
        lines.append(_bar_line("RAM  ", self.sizes.ram, self.ram_total, self.ram_source))
        lines.append("")
        lines.append(
            f"  .text {_human(self.sizes.text)}   "
            f".data {_human(self.sizes.data)}   "
            f".bss {_human(self.sizes.bss)}"
        )
        if self.regions:
            lines.append("")
            lines.append("Linker regions:")
            for region in self.regions:
                lines.append(f"  {region.name:<10} 0x{region.origin:08X}  {_human(region.length)}")
        if self.objects:
            lines.append("")
            lines.append(f"Largest contributors (top {top}):")
            for obj in self.objects[:top]:
                lines.append(
                    f"  {_human(obj.total):>9}  {obj.name}"
                    + (f"   (flash {_human(obj.flash)}, ram {_human(obj.ram)})" if obj.ram else "")
                )
        if self.warnings:
            lines.append("")
            for warning in self.warnings:
                lines.append(f"  ! {warning}")
        return "\n".join(lines)


def _human(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.2f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


def _bar_line(label: str, used: int, total: Optional[int], source: str) -> str:
    if not total:
        return f"  {label}  {_human(used):>10}   (no budget known — set mcu.flash_kb/ram_kb)"
    pct = 100.0 * used / total
    filled = min(int(pct / 5), 20)
    bar = "#" * filled + "." * (20 - filled)
    flag = "  OVER BUDGET" if used > total else ""
    src = f"  [{source}]" if source else ""
    return f"  {label}  {_human(used):>10} / {_human(total):<9} {pct:5.1f}%  [{bar}]{flag}{src}"


def parse_size_output(text: str) -> Sizes:
    """Parse `size` output in either Berkeley (default) or SysV (`-A`) format."""
    sizes = Sizes()
    matched = False
    for line in text.splitlines():
        m = _BERKELEY_RE.match(line)
        if m:
            sizes.text += int(m.group("text"))
            sizes.data += int(m.group("data"))
            sizes.bss += int(m.group("bss"))
            matched = True
    if matched:
        return sizes

    # SysV: attribute each section to flash or RAM by conventional name.
    for line in text.splitlines():
        m = _SYSV_RE.match(line.strip())
        if not m:
            continue
        name, size = m.group("name"), int(m.group("size"))
        if name in (".bss", ".sbss") or name.startswith(".bss"):
            sizes.bss += size
        elif name in (".data", ".sdata") or name.startswith(".data"):
            sizes.data += size
        elif name.startswith((".text", ".rodata", ".isr_vector", ".init", ".fini", ".ARM")):
            sizes.text += size
    return sizes


def parse_map_file(text: str) -> Tuple[List[Region], List[ObjectUsage], Dict[str, int]]:
    """Extract memory regions, per-object usage and per-section totals from an ld map."""
    regions: List[Region] = []
    per_object: Dict[str, ObjectUsage] = {}
    sections: Dict[str, int] = {}

    in_memcfg = False
    pending_section: Optional[str] = None

    lines = text.splitlines()
    for raw in lines:
        if raw.startswith("Memory Configuration"):
            in_memcfg = True
            continue
        if in_memcfg:
            if raw.startswith("Linker script and memory map"):
                in_memcfg = False
                continue
            m = _MEMCFG_RE.match(raw.strip())
            if m and m.group("name") not in ("*default*", "Name"):
                regions.append(
                    Region(
                        name=m.group("name"),
                        origin=int(m.group("origin"), 16),
                        length=int(m.group("length"), 16),
                    )
                )
            continue

        section_only = _SECTION_ONLY_RE.match(raw)
        if section_only:
            pending_section = section_only.group("section")
            continue

        m = _ALLOC_RE.match(raw)
        if not m:
            continue
        section = m.group("section") or pending_section
        pending_section = None
        if not section:
            continue
        size = int(m.group("size"), 16)
        addr = int(m.group("addr"), 16)
        obj = m.group("obj")
        # Only object-file contribution rows count; the map also contains
        # symbol-definition rows that would otherwise be double-counted.
        if size == 0 or not _is_object_file(obj):
            continue

        base = section.split(".")[1] if section.count(".") >= 1 else section
        sections["." + base] = sections.get("." + base, 0) + size

        name = _short_object_name(obj)
        usage = per_object.setdefault(name, ObjectUsage(name))
        if _is_ram_address(addr, regions) or base in ("bss", "data", "sbss", "sdata", "COMMON"):
            usage.ram += size
            if base in ("data", "sdata"):
                usage.flash += size  # .data initialisers also occupy flash
        else:
            usage.flash += size

    objects = sorted(per_object.values(), key=lambda o: o.total, reverse=True)
    return regions, objects, sections


def _is_object_file(obj: str) -> bool:
    """True for `build/main.o`, `libc.a`, and archive members like `libc.a(memcpy.o)`."""
    return obj.endswith((".o", ".obj", ".a", ".lo")) or ".o)" in obj


def _short_object_name(obj: str) -> str:
    """`build/drivers/foo.o` -> `drivers/foo.o`; `libc.a(memcpy.o)` -> `libc.a(memcpy.o)`."""
    if "(" in obj:
        archive, _, member = obj.partition("(")
        return f"{Path(archive).name}({member}"
    parts = Path(obj).parts
    return "/".join(parts[-2:]) if len(parts) > 1 else obj


def _is_ram_address(addr: int, regions: List[Region]) -> bool:
    for region in regions:
        if (
            region.name.upper() in RAM_NAMES
            and region.origin <= addr < region.origin + region.length
        ):
            return True
    return False


def find_region(regions: List[Region], names: set) -> Optional[Region]:
    for region in regions:
        if region.name.upper() in names:
            return region
    return None


def run_size_tool(elf: Path, tool: Optional[str] = None) -> Tuple[str, str]:
    """Run a `size` binary on an ELF. Returns (output, tool used)."""
    candidates = (
        [tool]
        if tool
        else [
            "arm-none-eabi-size",
            "riscv64-unknown-elf-size",
            "riscv32-unknown-elf-size",
            "xtensa-esp32-elf-size",
            "avr-size",
            "llvm-size",
            "size",
        ]
    )
    for candidate in candidates:
        if not candidate or not shutil.which(candidate):
            continue
        proc = subprocess.run([candidate, str(elf)], capture_output=True, text=True, timeout=60)
        if proc.returncode == 0:
            return proc.stdout, candidate
    raise FileNotFoundError(
        "no `size` tool found on PATH (tried: " + ", ".join(c for c in candidates if c) + ")"
    )


def analyze(
    board: BoardProfile,
    *,
    size_output: Optional[str] = None,
    map_text: Optional[str] = None,
) -> MemoryReport:
    """Build a budget report from whichever artifacts are available."""
    regions: List[Region] = []
    objects: List[ObjectUsage] = []
    sections: Dict[str, int] = {}
    if map_text:
        regions, objects, sections = parse_map_file(map_text)

    if size_output:
        sizes = parse_size_output(size_output)
    elif sections:
        sizes = Sizes(
            text=sum(
                v
                for k, v in sections.items()
                if k not in (".bss", ".data", ".sbss", ".sdata", ".COMMON")
            ),
            data=sections.get(".data", 0) + sections.get(".sdata", 0),
            bss=sections.get(".bss", 0) + sections.get(".sbss", 0) + sections.get(".COMMON", 0),
        )
    else:
        sizes = Sizes()

    report = MemoryReport(sizes=sizes, regions=regions, objects=objects, sections=sections)

    # Budget preference: the part's real capacity from board.yaml, falling back
    # to the linker script's region sizes.
    mcu = board.mcu if board.exists else {}
    if mcu.get("flash_kb"):
        report.flash_total = int(mcu["flash_kb"]) * 1024
        report.flash_source = "board.yaml"
    else:
        region = find_region(regions, FLASH_NAMES)
        if region:
            report.flash_total = region.length
            report.flash_source = f"map:{region.name}"

    if mcu.get("ram_kb"):
        report.ram_total = int(mcu["ram_kb"]) * 1024
        report.ram_source = "board.yaml"
    else:
        region = find_region(regions, RAM_NAMES)
        if region:
            report.ram_total = region.length
            report.ram_source = f"map:{region.name}"

    _add_warnings(report)
    return report


SEARCH_DIRS = (
    "build",
    "Build",
    "Debug",
    "Release",
    "out",
    "bin",
    "cmake-build-debug",
    "cmake-build-release",
    ".pio/build",
    "zephyr/build",
    "build/zephyr",
)


def discover_artifacts(
    root: Path, elf_hint: Optional[str] = None, map_hint: Optional[str] = None
) -> Tuple[Optional[Path], Optional[Path]]:
    """Find the most recently built ELF and linker map under the project."""
    elf = Path(root / elf_hint) if elf_hint else None
    map_file = Path(root / map_hint) if map_hint else None
    if elf and not elf.is_file():
        elf = None
    if map_file and not map_file.is_file():
        map_file = None
    if elf and map_file:
        return elf, map_file

    def newest(patterns: Sequence[str]) -> Optional[Path]:
        candidates: List[Path] = []
        for base in (root, *(root / d for d in SEARCH_DIRS)):
            if not base.is_dir():
                continue
            for pattern in patterns:
                candidates.extend(p for p in base.rglob(pattern) if p.is_file())
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.stat().st_mtime)

    return elf or newest(("*.elf", "*.axf")), map_file or newest(("*.map",))


def _add_warnings(report: MemoryReport) -> None:
    flash_pct, ram_pct = report.flash_pct, report.ram_pct
    if flash_pct is not None:
        if flash_pct > 100:
            over = report.sizes.flash - (report.flash_total or 0)
            report.warnings.append(f"flash overflows the budget by {_human(over)}")
        elif flash_pct > 90:
            report.warnings.append(
                f"flash is {flash_pct:.1f}% full — little headroom for OTA or growth"
            )
    if ram_pct is not None:
        if ram_pct > 100:
            over = report.sizes.ram - (report.ram_total or 0)
            report.warnings.append(f"static RAM overflows the budget by {_human(over)}")
        elif ram_pct > 80:
            report.warnings.append(
                f"static RAM is {ram_pct:.1f}% full — remember stack and heap are on top of this"
            )
    if report.sizes.flash == 0 and report.sizes.ram == 0:
        report.warnings.append("no sizes found — pass an ELF with --elf or a linker map with --map")
