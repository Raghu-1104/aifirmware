"""Firmware project scanning: structure detection and source indexing.

The detector answers the questions a firmware engineer would ask when opening an
unfamiliar repo — what MCU, what toolchain, what build system, what RTOS, how is
memory laid out, which peripherals are actually driven — and the result is fed
into the assistant's system prompt so it starts every session already oriented.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .store import Store, sha256_file

CODE_CHUNK_LINES = 70
CODE_CHUNK_OVERLAP = 12
MAX_FILE_BYTES = 1_500_000

# --- signature tables -------------------------------------------------------

BUILD_SIGNATURES = [
    ("PlatformIO", ["platformio.ini"]),
    ("ESP-IDF", ["sdkconfig", "sdkconfig.defaults"]),
    ("Zephyr (west)", ["west.yml", "prj.conf"]),
    ("CMake", ["CMakeLists.txt"]),
    ("Make", ["Makefile", "makefile", "GNUmakefile"]),
    ("Arduino", ["*.ino"]),
    ("Cargo (embedded Rust)", ["Cargo.toml"]),
    ("SEGGER Embedded Studio", ["*.emProject"]),
    ("Keil MDK", ["*.uvprojx"]),
    ("IAR EWARM", ["*.ewp"]),
    ("STM32CubeIDE", [".cproject"]),
]

RTOS_SIGNATURES = {
    "FreeRTOS": [r"FreeRTOS\.h", r"xTaskCreate", r"vTaskStartScheduler"],
    "Zephyr": [r"zephyr/kernel\.h", r"k_thread_create", r"ZEPHYR_BASE"],
    "ThreadX": [r"tx_api\.h", r"tx_thread_create"],
    "RT-Thread": [r"rtthread\.h", r"rt_thread_create"],
    "Mbed OS": [r"mbed\.h", r"Thread\s+\w+;"],
    "ChibiOS": [r"ch\.h", r"chThdCreateStatic"],
}

MCU_HEADER_PATTERNS = [
    (r"stm32([a-z]\d)[a-z0-9]*xx\.h", "STM32{0}"),
    (r"nrf52(\d+)?\.h", "nRF52"),
    (r"nrf53(\d+)?\.h", "nRF53"),
    (r"sam(\w+)\.h", "Microchip SAM{0}"),
    (r"esp_system\.h|esp_idf_version\.h", "Espressif ESP32"),
    (r"rp2040|pico/stdlib\.h", "RP2040"),
    (r"MK\w+\.h", "NXP Kinetis"),
    (r"msp430\.h", "TI MSP430"),
    (r"avr/io\.h", "AVR"),
    (r"ti_msp_dl_config\.h", "TI MSPM0"),
]

TOOLCHAIN_PATTERNS = [
    (r"arm-none-eabi", "arm-none-eabi-gcc (ARM Cortex-M/R)"),
    (r"riscv\d*-unknown-elf|riscv64-unknown-elf", "riscv-gcc"),
    (r"xtensa-esp32\w*-elf", "xtensa-esp32-elf-gcc"),
    (r"avr-gcc", "avr-gcc"),
    (r"msp430-elf", "msp430-elf-gcc"),
    (r"armclang|armcc", "Arm Compiler"),
]

PERIPHERAL_PATTERNS = {
    "UART/USART": [
        r"\bUSART\d?\b",
        r"\bUART\d?\b",
        r"HAL_UART_",
        r"uart_(write|read|init)",
        r"Serial\d?\.",
    ],
    "I2C": [r"\bI2C\d?\b", r"HAL_I2C_", r"i2c_(write|read|master)", r"Wire\."],
    "SPI": [r"\bSPI\d?\b", r"HAL_SPI_", r"spi_(write|read|transfer)"],
    "ADC": [r"\bADC\d?\b", r"HAL_ADC_", r"adc_(read|init|oneshot)", r"analogRead"],
    "DAC": [r"\bDAC\d?\b", r"HAL_DAC_"],
    "Timers/PWM": [r"\bTIM\d+\b", r"HAL_TIM_", r"pwm_(set|init)", r"analogWrite", r"ledc_"],
    "DMA": [r"\bDMA\d?\b", r"HAL_DMA_", r"dma_(start|init)"],
    "GPIO/EXTI": [r"HAL_GPIO_", r"gpio_(set|get|init|pin)", r"EXTI\d*", r"digitalWrite"],
    "CAN": [r"\bCAN\d?\b", r"HAL_CAN_", r"can_(send|receive)", r"twai_"],
    "USB": [r"\bUSB\b", r"tud_", r"CDC_", r"HAL_PCD_"],
    "RTC": [r"\bRTC\b", r"HAL_RTC_"],
    "Watchdog": [r"\bIWDG\b", r"\bWWDG\b", r"wdt_", r"esp_task_wdt"],
    "Flash/NVM": [r"HAL_FLASH_", r"nvs_", r"flash_(erase|write)", r"EEPROM"],
    "BLE/Wi-Fi": [r"\bBLE\b", r"esp_wifi_", r"bt_(enable|le)", r"nimble"],
    "Sleep/Low power": [r"__WFI\(\)", r"HAL_PWR_", r"esp_light_sleep|esp_deep_sleep", r"pm_"],
}

INTERRUPT_RE = re.compile(
    r"^\s*(?:void|__attribute__\([^)]*\)\s*void)\s+([A-Za-z_][A-Za-z0-9_]*(?:_IRQHandler|_Handler|_isr|_ISR))\s*\(",
    re.M,
)


@dataclass
class MemoryRegion:
    name: str
    origin: str
    length: str


@dataclass
class ProjectProfile:
    root: str
    build_systems: List[str] = field(default_factory=list)
    toolchains: List[str] = field(default_factory=list)
    rtos: List[str] = field(default_factory=list)
    mcu_hints: List[str] = field(default_factory=list)
    memory_regions: List[MemoryRegion] = field(default_factory=list)
    linker_scripts: List[str] = field(default_factory=list)
    peripherals: List[str] = field(default_factory=list)
    interrupt_handlers: List[str] = field(default_factory=list)
    entry_points: List[str] = field(default_factory=list)
    top_dirs: List[str] = field(default_factory=list)
    source_count: int = 0
    total_lines: int = 0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["memory_regions"] = [asdict(m) for m in self.memory_regions]
        return d

    def to_markdown(self) -> str:
        lines = ["## Project structure"]
        lines.append(f"- Files indexed: {self.source_count} ({self.total_lines} lines)")
        if self.top_dirs:
            lines.append(f"- Top-level dirs: {', '.join(self.top_dirs)}")
        if self.build_systems:
            lines.append(f"- Build system: {', '.join(self.build_systems)}")
        if self.toolchains:
            lines.append(f"- Toolchain: {', '.join(self.toolchains)}")
        if self.mcu_hints:
            lines.append(f"- MCU (detected from sources): {', '.join(self.mcu_hints)}")
        lines.append(
            f"- RTOS: {', '.join(self.rtos) if self.rtos else 'bare-metal / none detected'}"
        )
        if self.memory_regions:
            regions = ", ".join(f"{m.name} @ {m.origin} ({m.length})" for m in self.memory_regions)
            lines.append(f"- Memory map ({', '.join(self.linker_scripts)}): {regions}")
        if self.peripherals:
            lines.append(f"- Peripherals in use: {', '.join(self.peripherals)}")
        if self.entry_points:
            lines.append(f"- Entry points: {', '.join(self.entry_points)}")
        if self.interrupt_handlers:
            shown = self.interrupt_handlers[:12]
            more = (
                ""
                if len(self.interrupt_handlers) <= 12
                else f" (+{len(self.interrupt_handlers) - 12} more)"
            )
            lines.append(f"- ISRs: {', '.join(shown)}{more}")
        return "\n".join(lines)


def _is_excluded(rel_parts: List[str], excludes: List[str]) -> bool:
    return any(part in excludes for part in rel_parts)


def iter_source_files(root: Path, globs: List[str], excludes: List[str]) -> List[Path]:
    seen: Dict[Path, None] = {}
    for pattern in globs:
        for path in root.glob(pattern):
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            if _is_excluded(list(rel.parts), excludes):
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            seen.setdefault(path, None)
    return sorted(seen.keys())


def parse_linker_script(text: str) -> List[MemoryRegion]:
    """Pull MEMORY{} regions out of a GNU ld script."""
    regions: List[MemoryRegion] = []
    m = re.search(r"MEMORY\s*\{(.*?)\}", text, re.S)
    if not m:
        return regions
    body = m.group(1)
    row = re.compile(
        r"(?P<name>\w+)\s*(?:\([^)]*\))?\s*:\s*ORIGIN\s*=\s*(?P<origin>[^,\s]+)\s*,\s*"
        r"LENGTH\s*=\s*(?P<length>[^\n]+)",
        re.I,
    )
    for match in row.finditer(body):
        regions.append(
            MemoryRegion(
                name=match.group("name"),
                origin=match.group("origin").strip().rstrip(","),
                length=match.group("length").strip().rstrip(" ,"),
            )
        )
    return regions


def chunk_source(text: str, lines_per_chunk: int = CODE_CHUNK_LINES) -> List[Dict[str, Any]]:
    lines = text.splitlines()
    chunks: List[Dict[str, Any]] = []
    step = max(lines_per_chunk - CODE_CHUNK_OVERLAP, 1)
    for start in range(0, max(len(lines), 1), step):
        window = lines[start : start + lines_per_chunk]
        if not window:
            break
        body = "\n".join(window).strip()
        if body:
            chunks.append(
                {
                    "line": start + 1,
                    "end_line": start + len(window),
                    "heading": _nearest_symbol(lines, start),
                    "text": body,
                }
            )
        if start + lines_per_chunk >= len(lines):
            break
    return chunks


_SYMBOL_RE = re.compile(r"^[A-Za-z_][\w \*]*\b(\w+)\s*\([^;]*\)\s*\{?\s*$")


def _nearest_symbol(lines: List[str], index: int) -> Optional[str]:
    for i in range(index, max(index - 60, -1), -1):
        m = _SYMBOL_RE.match(lines[i].strip())
        if m:
            return m.group(1)
    return None


def scan_project(root: Path, globs: List[str], excludes: List[str]) -> ProjectProfile:
    """Detect the shape of the firmware project without indexing anything."""
    profile = ProjectProfile(root=str(root))
    files = iter_source_files(root, globs, excludes)
    profile.source_count = len(files)

    top_dirs = sorted(
        {p.relative_to(root).parts[0] for p in files if len(p.relative_to(root).parts) > 1}
    )
    profile.top_dirs = top_dirs[:15]

    # Build system detection from marker files anywhere in the tree.
    names = {p.name for p in files} | {p.name for p in root.glob("*") if p.is_file()}
    for label, markers in BUILD_SIGNATURES:
        for marker in markers:
            if marker.startswith("*"):
                if any(n.endswith(marker[1:]) for n in names):
                    profile.build_systems.append(label)
                    break
            elif marker in names:
                profile.build_systems.append(label)
                break

    peripherals: Dict[str, None] = {}
    rtos: Dict[str, None] = {}
    toolchains: Dict[str, None] = {}
    mcus: Dict[str, None] = {}
    isrs: Dict[str, None] = {}
    entries: Dict[str, None] = {}

    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        profile.total_lines += text.count("\n") + 1
        rel = str(path.relative_to(root))

        if path.suffix.lower() == ".ld":
            profile.linker_scripts.append(rel)
            for region in parse_linker_script(text):
                if all(r.name != region.name for r in profile.memory_regions):
                    profile.memory_regions.append(region)

        for name, patterns in RTOS_SIGNATURES.items():
            if any(re.search(p, text) for p in patterns):
                rtos.setdefault(name, None)

        for pattern, label in TOOLCHAIN_PATTERNS:
            if re.search(pattern, text):
                toolchains.setdefault(label, None)

        for pattern, template in MCU_HEADER_PATTERNS:
            m = re.search(pattern, text, re.I)
            if m:
                groups = [g for g in m.groups() if g]
                mcus.setdefault(template.format(*(groups or [""])).upper().strip(), None)

        if path.suffix.lower() in {".c", ".cpp", ".cc", ".h", ".hpp", ".ino", ".rs"}:
            for name, patterns in PERIPHERAL_PATTERNS.items():
                if any(re.search(p, text) for p in patterns):
                    peripherals.setdefault(name, None)
            for match in INTERRUPT_RE.finditer(text):
                isrs.setdefault(match.group(1), None)
            if re.search(r"\b(?:int|void)\s+main\s*\(", text) or re.search(
                r"\bvoid\s+app_main\s*\(", text
            ):
                entries.setdefault(rel, None)
            if re.search(r"\bvoid\s+setup\s*\(\s*\)", text) and re.search(
                r"\bvoid\s+loop\s*\(\s*\)", text
            ):
                entries.setdefault(rel, None)

    profile.peripherals = list(peripherals)
    profile.rtos = list(rtos)
    profile.toolchains = list(toolchains)
    profile.mcu_hints = list(mcus)
    profile.interrupt_handlers = list(isrs)
    profile.entry_points = list(entries)[:8]
    profile.build_systems = list(dict.fromkeys(profile.build_systems))
    return profile


def index_sources(
    store: Store,
    root: Path,
    globs: List[str],
    excludes: List[str],
    *,
    force: bool = False,
) -> Dict[str, int]:
    """Index (or refresh) every source file into the store."""
    files = iter_source_files(root, globs, excludes)
    indexed = skipped = chunks = 0
    rel_paths: List[str] = []

    for path in files:
        rel = str(path.relative_to(root))
        rel_paths.append(rel)
        try:
            sha = sha256_file(path)
        except OSError:
            continue
        if not force and store.doc_is_current(rel, sha):
            skipped += 1
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        doc_id = store.upsert_doc(
            kind="code", path=rel, title=path.name, sha=sha, mtime=path.stat().st_mtime
        )
        chunks += store.add_chunks(doc_id, chunk_source(text))
        indexed += 1

    removed = store.prune_missing(rel_paths, kind="code")
    return {"indexed": indexed, "unchanged": skipped, "chunks": chunks, "removed": removed}
