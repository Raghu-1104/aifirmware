# fwcopilot

An AI chat tool for firmware work that actually knows *your* project: your board,
the ICs on it, and what their datasheets say.

Generic chat assistants answer firmware questions from memory — a plausible
register address, a pinout from some reference design, an I2C address for the
wrong variant of the part. fwcopilot answers from artifacts you control:

- **Your datasheets.** Drop in the PDFs for the ICs on your board. They're parsed,
  indexed and searchable, and answers cite them by page (`ACME1234 p.3`).
- **Your board.** `board.yaml` describes the MCU, every IC, each bus and the pin
  map — so answers use *your* pin names and *your* device addresses.
- **Your code.** The repo is scanned and indexed: toolchain, RTOS, memory map from
  the linker script, ISRs, which peripherals are actually driven.
- **Your history.** Sessions persist, and the assistant keeps a project notes file
  so board quirks and decisions survive into the next session.

It runs as a terminal chat, a one-shot `ask`, or a local web UI. It also generates
firmware scaffolding directly from the board profile — no model call involved,
because once the pin map and addresses are written down the code is deterministic.

---

## Install

```bash
git clone <this repo> && cd aifirmware
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[server]"
export ANTHROPIC_API_KEY=sk-ant-...      # or: ant auth login
```

## Quickstart

```bash
cd ~/projects/my-firmware

fwcopilot init                            # create the workspace + board template
$EDITOR board.yaml                        # describe your board (see below)
fwcopilot add-datasheet ~/Downloads/bme280.pdf --component U2
fwcopilot index                           # index sources + datasheets
fwcopilot chat                            # ask away
```

```
$ fwcopilot chat
fwcopilot — my-firmware  (claude-opus-5)
board: sensor-node · 2 datasheets · 41 files indexed · session 20260809-142211-3f2a

you › how do I bring up U2 after power-on?
  · get_board_profile()
  · search_datasheets(power-up sequence reset timing)
    ↳ 4 hits
  · lookup_register(CHIP_ID)
    ↳ 2 registers

U2 is the BME280 on I2C1 (PB6/PB7, 400 kHz) at address 0x76 — SDO is tied to GND
on your board, so it is 0x76 and not 0x77.

Bring-up order:
1. Wait t_startup = 2 ms after VDD ≥ 1.71 V before the first transaction
   (BME280 p.3). ...
```

## How the context works

Every session opens with a snapshot the assistant does not have to ask for:

| Source | What it contributes |
|---|---|
| `board.yaml` | MCU, buses, ICs with bus/address/CS, full pin map |
| `datasheets/` | Page-addressable text + an extracted register map per IC |
| Your source tree | Build system, toolchain, RTOS, memory regions, ISRs, peripherals in use |
| `.fwcopilot/notes.md` | Durable facts recorded across sessions |

The snapshot sits behind a prompt-cache breakpoint, so a long conversation
re-reads it at cache rates instead of paying for it every turn.

From there the assistant uses tools rather than recall:

`search_datasheets` · `read_datasheet_page` · `lookup_register` ·
`list_datasheets` · `get_board_profile` · `search_code` · `read_file` ·
`list_files` · `remember` · `write_file`\* · `edit_file`\* · `run_build`\*

\* off unless you pass `--allow-write` / `--allow-build`, and then each call still
asks for confirmation.

## Describing your board

`board.yaml` is the highest-leverage file. Everything is optional; more detail
means more specific answers and better generated code.

```yaml
board:
  name: sensor-node
  revision: B

mcu:
  part: STM32F411CEU6
  core: Cortex-M4F
  clock_hz: 100000000
  flash_kb: 512
  ram_kb: 128

buses:
  - { name: I2C1, type: i2c, speed_hz: 400000, pins: { scl: PB6, sda: PB7 } }
  - { name: SPI1, type: spi, speed_hz: 8000000, pins: { sck: PA5, miso: PA6, mosi: PA7 } }

components:
  - ref: U2
    part: BME280
    role: Pressure/humidity sensor
    bus: { name: I2C1, address: 0x76 }
    datasheet: datasheets/bme280.pdf
    notes: SDO tied to GND -> address 0x76

pins:
  - { pin: PB6, net: I2C1_SCL, function: I2C1_SCL, af: 4, to: U2.SCL }
  - { pin: PC13, net: LED_STATUS, function: GPIO_Output, active: low }
```

`fwcopilot board check` validates it — duplicate pin assignments, I2C address
collisions on the same bus, components on undeclared buses, bus pins missing from
the pin map, and datasheet paths that don't exist:

```
$ fwcopilot board check
3 issue(s) in board.yaml:
  - address collision on I2C1: U5 and U2 both at 0x76
  - pin PB6 assigned 2 times: I2C1_SCL, SPI2_SCK
  - U3 (W25Q128JV): datasheet 'datasheets/w25q128.pdf' not found on disk
```

## Generating firmware from the board profile

```bash
fwcopilot scaffold --target cmsis-bare --out .
```

| Target | Produces |
|---|---|
| `cmsis-bare` | `board_pins.h`, per-IC drivers, `main.c`, Cortex-M `startup.c`, linker script sized from the MCU, `Makefile` with the right `-mcpu`/`-mfpu` |
| `platformio` | `board_pins.h`, drivers, `src/main.c`, `platformio.ini` |
| `zephyr` | `board_pins.h`, drivers, `src/main.c`, `prj.conf`, devicetree overlay with your I2C/SPI nodes |
| `drivers-only` | Just `board_pins.h` + the per-IC driver skeletons |

The generated code carries your real values — the pin header defines
`ACME1234_I2C_ADDR 0x76u` and `PIN_I2C1_SCL "PB6"` because that is what the board
profile says — and the drivers are transport-agnostic (you supply read/write
callbacks from your HAL), so they compile standalone and are unit-testable.
`TODO` comments mark exactly what must be confirmed against the datasheet.

Existing files are never overwritten without `--force`; `--dry-run` shows the plan.

## Web UI

```bash
fwcopilot serve            # http://127.0.0.1:8765
```

Streaming chat with a sidebar showing the board, detected project shape, indexed
datasheets and live tool calls. Single self-contained HTML file, no CDN.

## Commands

| Command | |
|---|---|
| `fwcopilot init` | create the workspace, board template and datasheet folder |
| `fwcopilot add-datasheet FILE [--component U2] [--part BME280]` | copy in and index a datasheet |
| `fwcopilot index [--force]` | (re)index sources and datasheets |
| `fwcopilot status` | project, board and index summary |
| `fwcopilot search QUERY [--kind datasheet\|code]` | search directly, no model call |
| `fwcopilot board init\|show\|check` | manage and validate the board profile |
| `fwcopilot scaffold --target T` | generate firmware from the board profile |
| `fwcopilot ask "question"` | one-shot question |
| `fwcopilot chat [--continue] [--allow-write]` | interactive session |
| `fwcopilot sessions` | list saved sessions |
| `fwcopilot serve` | browser UI |

## Safety model

- **Read-only by default.** File writes and builds require an explicit flag, and
  then each call prompts (`--yes` to auto-approve in scripted runs).
- **Sandboxed paths.** Every model-supplied path is resolved and rejected if it
  escapes the project root.
- **Nothing is silently overwritten.** Scaffolding skips existing files;
  `edit_file` refuses a snippet that matches zero or several times.
- **Your data stays local.** The index is a SQLite file in `.fwcopilot/`. Only the
  text the assistant actually retrieves is sent to the API.

## Layout

```
.fwcopilot/
  config.yaml        model, paths, build command, permissions  (commit this)
  notes.md           durable project memory                    (commit this)
  index.db           SQLite + FTS5 index                       (gitignored)
  sessions/          saved conversations                       (gitignored)
board.yaml           your board                                (commit this)
datasheets/          the PDFs                                  (commit if licensing allows)
```

## How it works

- **Retrieval** is SQLite FTS5 with BM25 ranking — no embedding provider, no
  external service, works offline. The tokenizer keeps `_` inside tokens so
  `CTRL_REG1` and `HAL_I2C_Mem_Read` stay searchable as single terms, and user
  queries are always quoted into terms so punctuation can never be parsed as an
  FTS5 operator.
- **Datasheets** are chunked per page with overlap, keeping the page number for
  citation. A heuristic pass pulls `NAME ↔ 0xADDR` pairs out of register-map
  tables into a separate index for `lookup_register`.
- **Incremental indexing** is content-hashed: unchanged files are skipped,
  deleted files are pruned.
- **The agent** is a manual streaming tool-use loop against the Messages API, so
  history, tool results and thinking blocks are persisted verbatim and a session
  can resume days later.

## Limits

- Scanned PDFs with no text layer can't be indexed — OCR them first
  (`ocrmypdf in.pdf out.pdf`). fwcopilot tells you rather than indexing nothing.
- Register extraction is a heuristic over table text; it finds most
  `NAME/0xADDR` maps but is not a substitute for reading the page — which is why
  `lookup_register` returns page numbers to read.
- Generated scaffolding is a starting point sized from `board.yaml`. Verify the
  memory map, the IRQ vector table and every `TODO` against the reference manual
  before you flash a board.

## Development

```bash
pip install -e ".[server,dev]"
pytest -q
```
