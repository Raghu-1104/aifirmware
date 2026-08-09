"""Custom board description: parsing, validation and rendering.

`board.yaml` is the hand-authored source of truth for a custom board — the MCU,
every IC on it, which bus each part sits on, and the pin map. It is what lets the
assistant answer "wire up the pressure sensor" with *your* I2C address and *your*
pin numbers instead of a generic example, and it is what drives code generation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

TEMPLATE = """\
# Board profile for fwcopilot.
# Everything here is optional, but the more you fill in, the more specific the
# assistant's answers and the generated code become.

board:
  name: my-custom-board
  revision: A
  description: Custom sensor node

mcu:
  part: STM32F411CEU6
  vendor: STMicroelectronics
  core: Cortex-M4F
  clock_hz: 100000000
  flash_kb: 512
  ram_kb: 128
  package: UFQFPN48
  datasheet: datasheets/stm32f411.pdf      # optional, relative to project root

power:
  rails:
    - name: 3V3
      voltage: 3.3
      source: LDO (AMS1117)
    - name: VBAT
      voltage: 3.7
      source: Li-Po

# Buses defined once, referenced by components below.
buses:
  - name: I2C1
    type: i2c
    speed_hz: 400000
    pins: { scl: PB6, sda: PB7 }
  - name: SPI1
    type: spi
    speed_hz: 8000000
    pins: { sck: PA5, miso: PA6, mosi: PA7 }
  - name: USART1
    type: uart
    baud: 115200
    pins: { tx: PA9, rx: PA10 }

# Every IC on the board. `datasheet` links a PDF you have added with
# `fwcopilot add-datasheet`, which is how answers get page citations.
components:
  - ref: U2
    part: BME280
    role: Temperature/humidity/pressure sensor
    bus: { name: I2C1, address: 0x76 }
    datasheet: datasheets/bme280.pdf
    notes: SDO tied to GND -> address 0x76

  - ref: U3
    part: W25Q128JV
    role: 16 MB QSPI NOR flash
    bus: { name: SPI1, cs: PA4 }
    datasheet: datasheets/w25q128jv.pdf

# Pin map. `net` is your schematic net name; `to` records what it connects to.
pins:
  - { pin: PA9,  net: UART1_TX, function: USART1_TX, af: 7, to: J1.2 }
  - { pin: PA10, net: UART1_RX, function: USART1_RX, af: 7, to: J1.3 }
  - { pin: PB6,  net: I2C1_SCL, function: I2C1_SCL, af: 4, to: U2.SCL }
  - { pin: PB7,  net: I2C1_SDA, function: I2C1_SDA, af: 4, to: U2.SDA }
  - { pin: PA5,  net: SPI1_SCK, function: SPI1_SCK, af: 5, to: U3.CLK }
  - { pin: PA6,  net: SPI1_MISO, function: SPI1_MISO, af: 5, to: U3.DO }
  - { pin: PA7,  net: SPI1_MOSI, function: SPI1_MOSI, af: 5, to: U3.DI }
  - { pin: PA4,  net: FLASH_CS, function: GPIO_Output, to: U3.CS }
  - { pin: PC13, net: LED_STATUS, function: GPIO_Output, active: low }
"""


@dataclass
class Component:
    ref: str
    part: str = ""
    role: str = ""
    bus: Dict[str, Any] = field(default_factory=dict)
    datasheet: Optional[str] = None
    notes: str = ""

    @property
    def bus_name(self) -> Optional[str]:
        return self.bus.get("name") or self.bus.get("bus")

    @property
    def address(self) -> Optional[Any]:
        return self.bus.get("address") or self.bus.get("addr")

    def describe(self) -> str:
        bits = [f"{self.ref}: {self.part}"]
        if self.role:
            bits.append(f"({self.role})")
        if self.bus_name:
            conn = self.bus_name
            if self.address is not None:
                conn += f" @ {_fmt_addr(self.address)}"
            if self.bus.get("cs"):
                conn += f", CS={self.bus['cs']}"
            bits.append(f"on {conn}")
        if self.datasheet:
            bits.append(f"[datasheet: {self.datasheet}]")
        if self.notes:
            bits.append(f"— {self.notes}")
        return " ".join(bits)


@dataclass
class BoardProfile:
    path: Optional[Path] = None
    exists: bool = False
    data: Dict[str, Any] = field(default_factory=dict)

    # ---- accessors -----------------------------------------------------
    @property
    def board(self) -> Dict[str, Any]:
        return self.data.get("board") or {}

    @property
    def mcu(self) -> Dict[str, Any]:
        return self.data.get("mcu") or {}

    @property
    def name(self) -> str:
        return str(self.board.get("name") or "unnamed board")

    @property
    def components(self) -> List[Component]:
        out: List[Component] = []
        for raw in self.data.get("components") or []:
            if not isinstance(raw, dict):
                continue
            out.append(
                Component(
                    ref=str(raw.get("ref") or raw.get("designator") or raw.get("part") or "?"),
                    part=str(raw.get("part") or ""),
                    role=str(raw.get("role") or raw.get("description") or ""),
                    bus=raw.get("bus") if isinstance(raw.get("bus"), dict) else {},
                    datasheet=raw.get("datasheet"),
                    notes=str(raw.get("notes") or ""),
                )
            )
        return out

    @property
    def buses(self) -> List[Dict[str, Any]]:
        return [b for b in (self.data.get("buses") or []) if isinstance(b, dict)]

    @property
    def pins(self) -> List[Dict[str, Any]]:
        return [p for p in (self.data.get("pins") or []) if isinstance(p, dict)]

    def datasheet_map(self) -> Dict[str, Dict[str, str]]:
        """Map datasheet path -> component metadata, for attribution at ingest."""
        mapping: Dict[str, Dict[str, str]] = {}
        mcu_ds = self.mcu.get("datasheet")
        if mcu_ds:
            mapping[str(mcu_ds)] = {"component": "MCU", "part": str(self.mcu.get("part") or "")}
        for comp in self.components:
            if comp.datasheet:
                mapping[str(comp.datasheet)] = {"component": comp.ref, "part": comp.part}
        return mapping

    # ---- rendering -----------------------------------------------------
    def to_markdown(self, full: bool = False) -> str:
        if not self.exists:
            return (
                "## Board\nNo board.yaml in this project yet. Run `fwcopilot board init` "
                "to create one — MCU, ICs, buses and pin map make answers board-specific."
            )
        lines = [f"## Board: {self.name}"]
        if self.board.get("revision"):
            lines.append(f"- Revision: {self.board['revision']}")
        if self.board.get("description"):
            lines.append(f"- {self.board['description']}")

        mcu = self.mcu
        if mcu:
            mcu_bits = [str(mcu.get("part", "unknown MCU"))]
            for key, label in (("core", ""), ("clock_hz", "Hz"), ("flash_kb", "KB flash"),
                               ("ram_kb", "KB RAM"), ("package", "")):
                if mcu.get(key) is not None:
                    value = mcu[key]
                    if key == "clock_hz":
                        value = f"{int(value) / 1_000_000:g} MHz"
                        label = ""
                    mcu_bits.append(f"{value} {label}".strip())
            lines.append(f"- MCU: {', '.join(mcu_bits)}")

        for bus in self.buses:
            pins = bus.get("pins") or {}
            pin_str = ", ".join(f"{k}={v}" for k, v in pins.items())
            speed = bus.get("speed_hz") or bus.get("baud")
            speed_str = f" @ {speed}" if speed else ""
            lines.append(f"- Bus {bus.get('name')} ({bus.get('type')}){speed_str}: {pin_str}")

        comps = self.components
        if comps:
            lines.append("- Components:")
            for comp in comps:
                lines.append(f"  - {comp.describe()}")

        pins = self.pins
        if pins:
            if full:
                lines.append("- Pin map:")
                for pin in pins:
                    lines.append(f"  - {_fmt_pin(pin)}")
            else:
                lines.append(f"- Pin map: {len(pins)} pins defined (use get_board_profile for the full map)")
        return "\n".join(lines)

    # ---- validation ----------------------------------------------------
    def validate(self, root: Optional[Path] = None) -> List[str]:
        """Return a list of human-readable problems with the board profile."""
        problems: List[str] = []
        if not self.exists:
            return ["board.yaml not found — run `fwcopilot board init`"]

        if not self.mcu.get("part"):
            problems.append("mcu.part is not set")

        bus_names = {str(b.get("name")) for b in self.buses if b.get("name")}
        seen_refs: Dict[str, int] = {}
        i2c_addresses: Dict[str, str] = {}

        for comp in self.components:
            seen_refs[comp.ref] = seen_refs.get(comp.ref, 0) + 1
            if comp.bus_name and bus_names and comp.bus_name not in bus_names:
                problems.append(
                    f"{comp.ref} references bus '{comp.bus_name}' which is not declared under `buses`"
                )
            if comp.datasheet and root is not None:
                if not (root / comp.datasheet).is_file():
                    problems.append(
                        f"{comp.ref} ({comp.part}): datasheet '{comp.datasheet}' not found on disk"
                    )
            if comp.address is not None and comp.bus_name:
                key = f"{comp.bus_name}:{_fmt_addr(comp.address)}"
                if key in i2c_addresses:
                    problems.append(
                        f"address collision on {comp.bus_name}: {comp.ref} and "
                        f"{i2c_addresses[key]} both at {_fmt_addr(comp.address)}"
                    )
                else:
                    i2c_addresses[key] = comp.ref

        for ref, count in seen_refs.items():
            if count > 1:
                problems.append(f"duplicate component ref '{ref}' ({count} entries)")

        pin_users: Dict[str, List[str]] = {}
        for pin in self.pins:
            name = str(pin.get("pin") or "")
            if not name:
                problems.append(f"pin entry without a `pin` field: {pin}")
                continue
            pin_users.setdefault(name, []).append(str(pin.get("net") or pin.get("function") or "?"))
        for name, users in pin_users.items():
            if len(users) > 1:
                problems.append(f"pin {name} assigned {len(users)} times: {', '.join(users)}")

        for bus in self.buses:
            for role, pin_name in (bus.get("pins") or {}).items():
                if pin_users and str(pin_name) not in pin_users:
                    problems.append(
                        f"bus {bus.get('name')} uses pin {pin_name} ({role}) "
                        "which is missing from the pin map"
                    )
        return problems


def _fmt_addr(value: Any) -> str:
    if isinstance(value, int):
        return f"0x{value:02X}"
    return str(value)


def _fmt_pin(pin: Dict[str, Any]) -> str:
    bits = [str(pin.get("pin"))]
    if pin.get("net"):
        bits.append(f"net={pin['net']}")
    if pin.get("function"):
        bits.append(f"fn={pin['function']}")
    if pin.get("af") is not None:
        bits.append(f"AF{pin['af']}")
    if pin.get("to"):
        bits.append(f"-> {pin['to']}")
    if pin.get("active"):
        bits.append(f"active-{pin['active']}")
    return "  ".join(bits)


def load_board(path: Path) -> BoardProfile:
    path = Path(path)
    if not path.is_file():
        return BoardProfile(path=path, exists=False, data={})
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping at the top level")
    return BoardProfile(path=path, exists=True, data=data)


def write_template(path: Path, overwrite: bool = False) -> bool:
    path = Path(path)
    if path.exists() and not overwrite:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE, encoding="utf-8")
    return True
