# fwcopilot

[![CI](https://github.com/Raghu-1104/aifirmware/actions/workflows/ci.yml/badge.svg)](https://github.com/Raghu-1104/aifirmware/actions/workflows/ci.yml)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**A firmware engineering companion that knows your board, your ICs and your code.**

Generic AI assistants answer firmware questions from memory — a plausible register
address, a pinout from some reference design, an I²C address for the wrong variant
of the part. That is worse than no answer: it costs a day on a bench with a logic
analyser. fwcopilot answers from artifacts you control, and cites them.

It is also useful when you never open a chat window at all. Half of what it does —
static analysis, memory budgets, board validation, code generation — is
deterministic, runs offline, needs no API key, and belongs in CI.

```
$ fwcopilot chat
fwcopilot — sensor-node  (claude-opus-5)
board: sensor-node · 2 datasheets · 41 files indexed

you › the sensor returns 0xFF on every register read after a cold boot

  · get_board_profile()
  · search_datasheets(power-up sequence reset timing)   ↳ 4 hits
  · search_code(sensor_init)                            ↳ 2 hits
  · run_lint(src/drivers)                               ↳ 1E/2W

U2 is the BME280 on I2C1 at 0x76 (SDO tied to GND on your board).

Two things line up with a cold-boot-only failure:

1. `sensor_init()` in src/drivers/bme280.c:42 issues the first read 1 ms after
   power-up. The datasheet requires t_startup = 2 ms after VDD ≥ 1.71 V before
   the device will ACK (BME280 p.3, section 6). A warm reset keeps VDD up, which
   is why it only fails cold.
2. Unrelated but worth fixing: src/drivers/bme280.c:58 discards the
   HAL_I2C_Mem_Read status, so a NACK reads as success and you get 0xFF.
```

---

## Table of contents

- [Install](#install) · [Quickstart](#quickstart)
- [What it knows](#what-it-knows)
- [Deterministic tools](#deterministic-tools-no-api-key-needed) — lint, size, board check, regs, scaffold
- [Describing your board](#describing-your-board)
- [CI integration](#ci-integration)
- [Deployment](#deployment) — Docker, server, auth
- [Command reference](#command-reference) · [Configuration](#configuration)
- [Security](#security-model) · [How it works](#how-it-works) · [Limits](#limits)

---

## Install

```bash
pip install "fwcopilot[server]"          # once published
# or from source:
git clone https://github.com/Raghu-1104/aifirmware && cd aifirmware
pip install -e ".[server]"

export ANTHROPIC_API_KEY=sk-ant-...      # only needed for chat
```

Docker:

```bash
docker run --rm -it \
  -v /path/to/firmware:/workspace \
  -e ANTHROPIC_API_KEY \
  -p 127.0.0.1:8765:8765 \
  ghcr.io/raghu-1104/fwcopilot serve --host 0.0.0.0
```

## Quickstart

```bash
cd ~/projects/my-firmware

fwcopilot init                                     # workspace + board template
$EDITOR board.yaml                                 # describe your board
fwcopilot add-datasheet ~/Downloads/bme280.pdf --component U2
fwcopilot index                                    # index sources + datasheets
fwcopilot doctor                                   # verify the setup

fwcopilot lint                                     # find firmware bugs now
fwcopilot chat                                     # or ask questions
```

## What it knows

Every session opens with a snapshot the assistant never has to ask you for:

| Source | Contributes |
|---|---|
| `datasheets/*.pdf` | Page-addressable text + an extracted register map per IC |
| `board.yaml` | MCU, buses, every IC with its bus/address/CS, the full pin map |
| Your source tree | Build system, toolchain, RTOS, memory regions from the linker script, ISRs, peripherals actually driven |
| `.fwcopilot/notes.md` | Durable facts recorded across sessions |

That snapshot sits behind a prompt-cache breakpoint, so long conversations re-read
it at cache rates. From there the assistant uses tools rather than recall:

`search_datasheets` · `read_datasheet_page` · `lookup_register` · `list_datasheets` ·
`get_board_profile` · `search_code` · `read_file` · `list_files` · `run_lint` ·
`analyze_memory` · `remember` · `write_file`\* · `edit_file`\* · `run_build`\*

\* off unless you pass `--allow-write` / `--allow-build`, and each call still asks.

---

## Deterministic tools (no API key needed)

These never call a model. Same input, same output, every time — which is what makes
them safe to put in CI.

### `fwcopilot lint` — firmware static analysis

General C linters do not know that `HAL_Delay()` inside an ISR is a hang, or that
`xQueueSend()` has a `FromISR` variant that must be used from interrupt context.

```
$ fwcopilot lint
src/drivers/imu.c:31: error: [FW003] interrupt handler EXTI0_IRQHandler writes
    global 'g_samples', which is not volatile
    | g_samples++;
    = Declare it `volatile` so the compiler reloads it in the main context.

src/drivers/imu.c:34: error: [FW009] xQueueSend() called from interrupt handler
    EXTI0_IRQHandler instead of xQueueSendFromISR()
    = Use xQueueSendFromISR() and honour pxHigherPriorityTaskWoken with
      portYIELD_FROM_ISR().

src/bus/spi.c:88: warning: [FW004] busy-wait loop with no timeout — a stuck
    peripheral hangs the firmware here
    = Bound the wait with a tick deadline and return an error on expiry.
```

| Rule | Catches |
|---|---|
| FW001 | Blocking or non-reentrant call in an ISR (`HAL_Delay`, `printf`, `malloc`, …) |
| FW002 | Floating-point arithmetic in an ISR |
| FW003 | ISR writes a global that is not `volatile` |
| FW004 | Busy-wait loop with no timeout |
| FW005 | Discarded `HAL_*` status code |
| FW006 | Unbounded string function (`strcpy`, `sprintf`, `gets`) |
| FW007 | Dynamic allocation in firmware |
| FW008 | Blocking work, or a long span, with interrupts disabled |
| FW009 | FreeRTOS API used from an ISR without its `FromISR` variant |
| FW011 | Hardware register accessed through a non-`volatile` pointer |

Comment- and string-aware: a `// TODO: remove HAL_Delay` never trips it.
`--format json` for tooling, `--explain FW003` for the reasoning, exit code 7 when
errors are found.

### `fwcopilot size` — flash/RAM budget

```
$ fwcopilot size
  Flash     412.3 KB / 512.0 KB   80.5%  [################....]  [board.yaml]
  RAM        84.1 KB / 128.0 KB   65.7%  [#############.......]  [board.yaml]

  .text 401.2 KB   .data 11.1 KB   .bss 73.0 KB

Largest contributors (top 10):
    57.0 KB  libc.a(vfprintf.o)
    31.2 KB  build/drivers/display.o   (flash 28.1 KB, ram 3.1 KB)

  ! static RAM is 65.7% full — remember stack and heap are on top of this
```

Reads `size` output and the GNU ld map, measures against the part's real capacity
from `board.yaml` (not just what the linker script allows), and exits non-zero when
over budget.

### `fwcopilot board check` — catch hardware description bugs

```
$ fwcopilot board check
3 issue(s) in board.yaml:
  - address collision on I2C1: U5 and U2 both at 0x76
  - pin PB6 assigned 2 times: I2C1_SCL, SPI2_SCK
  - U3 (W25Q128JV): datasheet 'datasheets/w25q128.pdf' not found on disk
```

### `fwcopilot regs` — register header from a datasheet

```c
/* ACME1234 register map — generated by fwcopilot.
 * Source: datasheets/acme1234.pdf
 */
#define ACME1234_REG_CHIP_ID    0xD0u  /* R  Chip identification, reads 0x60 — p.2 */
#define ACME1234_REG_CTRL_MEAS  0xF4u  /* R/W  Oversampling and power mode — p.2 */
```

Every `#define` cites the page it came from, so a reviewer can verify it in seconds
instead of retyping a table and transposing a digit.

### `fwcopilot scaffold` — firmware from the board profile

| Target | Produces |
|---|---|
| `cmsis-bare` | `board_pins.h`, per-IC drivers, `main.c`, Cortex-M `startup.c`, linker script sized from the MCU, `Makefile` with the right `-mcpu`/`-mfpu` |
| `platformio` | pin header, drivers, `src/main.c`, `platformio.ini` |
| `zephyr` | pin header, drivers, `src/main.c`, `prj.conf`, devicetree overlay with your I²C/SPI nodes |
| `drivers-only` | just `board_pins.h` + driver skeletons |

Generated code carries *your* values — `ACME1234_I2C_ADDR 0x76u`, `PIN_I2C1_SCL "PB6"` —
and the drivers are transport-agnostic (you supply read/write callbacks from your
HAL), so they compile standalone and are unit-testable. CI compiles the generated
firmware on every push. Existing files are never overwritten without `--force`.

### `fwcopilot build` — build with parsed diagnostics

Turns a wall of log output into structured errors, with firmware-specific
explanations: a `region FLASH overflowed` points you at `fwcopilot size`; an
`undefined reference` reminds you about `extern "C"`.

---

## Describing your board

`board.yaml` is the highest-leverage file in the project.

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

## CI integration

Everything deterministic runs without credentials:

```yaml
- run: pip install fwcopilot
- run: fwcopilot index
- run: fwcopilot board check        # pin conflicts, address collisions
- run: fwcopilot lint               # exit 7 on errors
- run: fwcopilot size               # exit 7 when over budget
```

Exit codes: `0` ok · `1` error · `2` usage · `3` no workspace · `4` bad config ·
`5` not found · `6` no credentials · `7` findings (the command worked, the project
has problems).

## Deployment

```bash
fwcopilot serve                                     # localhost only
FWCOPILOT_AUTH_TOKEN=$(openssl rand -hex 24) \
  fwcopilot serve --host 0.0.0.0                    # token required
```

```bash
export FWCOPILOT_PROJECT=/path/to/firmware
export ANTHROPIC_API_KEY=sk-ant-...
export FWCOPILOT_AUTH_TOKEN=$(openssl rand -hex 24)
docker compose up
```

The image is multi-stage, runs as a non-root user, and ships a healthcheck.
`/healthz` and `/readyz` are unauthenticated probes for orchestrators; everything
under `/api` requires the bearer token when one is set. There is no TLS —
terminate it at a reverse proxy.

## Command reference

| Command | |
|---|---|
| `init` | create the workspace, board template and datasheet folder |
| `add-datasheet FILE [--component U2] [--part BME280]` | copy in and index a datasheet |
| `index [--force]` | (re)index sources and datasheets |
| `status` / `doctor [--deep]` | project summary / health check |
| `search QUERY [--kind datasheet\|code]` | search directly, no model call |
| `board init\|show\|check` | manage and validate the board profile |
| `lint [--severity] [--format json] [--explain FW001]` | firmware static analysis |
| `size [--elf] [--map] [--format json]` | flash/RAM budget |
| `build` | run the build, parse diagnostics |
| `regs PART [--out FILE]` / `regs --list` | register header from a datasheet |
| `scaffold --target T [--out DIR]` | generate firmware from the board profile |
| `ask "question"` / `chat [--continue] [--allow-write]` | one-shot / interactive |
| `sessions` | list saved sessions |
| `serve [--auth-token] [--cors-origin]` | browser UI |

Global: `-v`/`-vv` verbose, `-q` quiet, `--log-file FILE`.

## Configuration

`.fwcopilot/config.yaml`, with `FWCOPILOT_*` environment overrides for containers
and CI:

| Variable | Overrides |
|---|---|
| `FWCOPILOT_MODEL` | `model.id` (default `claude-opus-5`) |
| `FWCOPILOT_EFFORT` | `model.effort` — `low`/`medium`/`high`/`xhigh`/`max` |
| `FWCOPILOT_MAX_TOKENS` | `model.max_tokens` |
| `FWCOPILOT_BUILD_COMMAND` | `build.command` |
| `FWCOPILOT_AUTH_TOKEN` | server bearer token |
| `FWCOPILOT_LOG_FORMAT` | `human` or `json` |
| `FWCOPILOT_LOG_LEVEL` | `DEBUG`…`ERROR` |

Invalid values fail fast with a specific message rather than silently defaulting.

## Security model

- **Read-only by default.** Writes and builds need an explicit flag, then prompt
  per call.
- **Sandboxed paths.** Every model-supplied path is resolved and rejected if it
  escapes the project root.
- **Local data.** The index is a SQLite file in `.fwcopilot/`. Only the text the
  assistant actually retrieves is sent to the API; whole datasheets are not
  uploaded. The offline commands make no network calls at all.
- **Server.** Localhost by default, constant-time bearer token comparison,
  same-origin CORS unless you name origins, non-root container.

Full details, including the prompt-injection posture, in [SECURITY.md](SECURITY.md).

## How it works

- **Retrieval** is SQLite FTS5 with BM25 — no embedding provider, no external
  service, works offline. The tokenizer keeps `_` inside tokens so `CTRL_REG1` and
  `HAL_I2C_Mem_Read` stay single terms, and user queries are quoted into terms so
  punctuation can never be parsed as an FTS5 operator.
- **Datasheets** are chunked per page with overlap, keeping page numbers for
  citation; a heuristic pass pulls `NAME ↔ 0xADDR` pairs from register tables into
  a separate index.
- **Indexing** is content-hashed: unchanged files skipped, deleted files pruned.
- **The linter** blanks comments and string literals before matching, tracks
  brace depth to find ISR bodies, and knows which globals are `volatile`.
- **The agent** is a manual streaming tool-use loop, so history, tool results and
  thinking-block signatures persist verbatim and sessions resume days later.

## Limits

- Scanned PDFs with no text layer can't be indexed — OCR them first
  (`ocrmypdf in.pdf out.pdf`). fwcopilot says so rather than indexing nothing.
- Register extraction is a heuristic over table text. It finds most `NAME/0xADDR`
  maps but is not a substitute for reading the page — which is why
  `lookup_register` returns page numbers.
- Lint rules are regex- and structure-based, not a full C parser: heavy macro
  indirection can hide a violation. Findings are precise; absence of findings is
  not proof.
- Generated scaffolding is a starting point. Verify the memory map, the IRQ vector
  table and every `TODO` against the reference manual before flashing.

## Development

```bash
make install     # venv + dev extras
make check       # lint + types + tests — the same gate CI runs
make docker      # build the image
```

See [CONTRIBUTING.md](CONTRIBUTING.md). Contributions of new lint rules are
especially welcome — each needs a test that fires *and* one that must not.

## License

MIT — see [LICENSE](LICENSE).
