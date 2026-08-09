import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fwcopilot.config import init_workspace
from fwcopilot.store import Store

SAMPLE_DATASHEET = """\
ACME1234 Digital Pressure Sensor
Rev 1.2

5 REGISTER MAP
The device exposes the following registers over I2C.

CHIP_ID     0xD0    R      Chip identification, reads 0x60
RESET       0xE0    W      Write 0xB6 to trigger a soft reset
CTRL_MEAS   0xF4    R/W    Oversampling and power mode
CONFIG      0xF5    R/W    Standby time and IIR filter
PRESS_MSB   0xF7    R      Raw pressure bits 19:12

6 POWER-UP SEQUENCE
After VDD reaches 1.71 V the device requires t_startup = 2 ms before the first
I2C transaction. The CHIP_ID register must read 0x60 before configuration.

7 ELECTRICAL CHARACTERISTICS
Supply voltage VDD 1.71 V to 3.6 V. The maximum SCK frequency is 400 kHz in
fast mode. Absolute maximum VDD is 4.2 V.
"""

SAMPLE_BOARD = """\
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
  - name: I2C1
    type: i2c
    speed_hz: 400000
    pins: { scl: PB6, sda: PB7 }
  - name: SPI1
    type: spi
    speed_hz: 8000000
    pins: { sck: PA5, miso: PA6, mosi: PA7 }
components:
  - ref: U2
    part: ACME1234
    role: Pressure sensor
    bus: { name: I2C1, address: 0x76 }
    datasheet: datasheets/acme1234.txt
  - ref: U3
    part: W25Q128
    role: NOR flash
    bus: { name: SPI1, cs: PA4 }
pins:
  - { pin: PB6, net: I2C1_SCL, function: I2C1_SCL, af: 4 }
  - { pin: PB7, net: I2C1_SDA, function: I2C1_SDA, af: 4 }
  - { pin: PA5, net: SPI1_SCK, function: SPI1_SCK, af: 5 }
  - { pin: PA6, net: SPI1_MISO, function: SPI1_MISO, af: 5 }
  - { pin: PA7, net: SPI1_MOSI, function: SPI1_MOSI, af: 5 }
  - { pin: PA4, net: FLASH_CS, function: GPIO_Output }
  - { pin: PC13, net: LED_STATUS, function: GPIO_Output, active: low }
"""

SAMPLE_MAIN_C = """\
#include "stm32f4xx.h"
#include <stdint.h>

static volatile uint32_t g_ticks;

void SysTick_Handler(void)
{
    g_ticks++;
}

int sensor_read(uint8_t reg, uint8_t *value)
{
    return HAL_I2C_Mem_Read(&hi2c1, 0x76 << 1, reg, 1, value, 1, 100);
}

int main(void)
{
    HAL_Init();
    HAL_UART_Init(&huart1);
    for (;;) {
        __WFI();
    }
}
"""

SAMPLE_LD = """\
ENTRY(Reset_Handler)

MEMORY
{
  FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 512K
  RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 128K
}

SECTIONS { .text : { *(.text) } > FLASH }
"""


@pytest.fixture()
def workspace(tmp_path):
    """A realistic little firmware project with a board profile and a datasheet."""
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "datasheets").mkdir(parents=True)

    (root / "src" / "main.c").write_text(SAMPLE_MAIN_C, encoding="utf-8")
    (root / "app.ld").write_text(SAMPLE_LD, encoding="utf-8")
    (root / "Makefile").write_text(
        "CC := arm-none-eabi-gcc\nall:\n\t$(CC) -c src/main.c\n", encoding="utf-8"
    )
    (root / "datasheets" / "acme1234.txt").write_text(SAMPLE_DATASHEET, encoding="utf-8")

    cfg = init_workspace(root, "sensor-node-fw")
    cfg.board_path.write_text(SAMPLE_BOARD, encoding="utf-8")
    return cfg


@pytest.fixture()
def indexed(workspace):
    """Workspace with code and datasheets fully indexed."""
    from fwcopilot.board import load_board
    from fwcopilot.datasheets import ingest_directory
    from fwcopilot.project import index_sources

    cfg = workspace
    board = load_board(cfg.board_path)
    store = Store(cfg.db_path)
    index_sources(store, cfg.root, cfg.source_globs, cfg.excludes)
    ingest_directory(store, cfg.datasheets_path, cfg.root, component_map=board.datasheet_map())
    yield cfg, store
    store.close()
