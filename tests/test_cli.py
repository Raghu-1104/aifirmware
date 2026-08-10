"""End-to-end CLI behaviour (everything except the model call)."""

import pytest

from fwcopilot.cli import main
from fwcopilot.errors import ExitCode


@pytest.fixture()
def project(tmp_path, monkeypatch):
    root = tmp_path / "fw"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.c").write_text(
        '#include "stm32f4xx.h"\nint main(void){ HAL_I2C_Init(&hi2c1); for(;;){} }\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(root)
    return root


def run(*argv) -> int:
    return main(list(argv))


class TestInitAndIndex:
    def test_init_creates_workspace(self, project, capsys):
        assert run("init", ".") == 0
        assert (project / ".fwcopilot" / "config.yaml").is_file()
        assert (project / "board.yaml").is_file()
        assert (project / "datasheets").is_dir()
        out = capsys.readouterr().out
        assert "Initialized fwcopilot workspace" in out
        assert "Next steps" in out

    def test_commands_fail_clearly_outside_a_workspace(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        assert run("status") == ExitCode.NO_WORKSPACE
        err = capsys.readouterr().err
        assert "no fwcopilot workspace found" in err
        assert "fwcopilot init" in err  # the hint, not just the failure

    def test_workspace_is_found_from_a_subdirectory(self, project, monkeypatch):
        run("init", ".")
        monkeypatch.chdir(project / "src")
        assert run("status") == 0

    def test_index_then_search(self, project, capsys):
        run("init", ".")
        (project / "datasheets" / "acme.txt").write_text(
            "ACME1234 sensor\nCTRL_MEAS 0xF4 R/W Oversampling control\n"
            "Maximum SCK frequency is 400 kHz.\n",
            encoding="utf-8",
        )
        assert run("index") == 0
        out = capsys.readouterr().out
        assert "Index ready" in out and "1 datasheets" in out

        assert run("search", "oversampling") == 0
        assert "CTRL_MEAS" in capsys.readouterr().out

        assert run("search", "HAL_I2C_Init", "--kind", "code") == 0
        assert "main.c" in capsys.readouterr().out

    def test_search_with_no_match_exits_nonzero(self, project, capsys):
        run("init", ".")
        run("index")
        assert run("search", "zzzzznotpresent") == 1

    def test_add_datasheet_copies_and_indexes(self, project, tmp_path, capsys):
        run("init", ".")
        external = tmp_path / "bme280-datasheet.txt"
        external.write_text("BME280\nCTRL_HUM 0xF2 R/W Humidity oversampling\n", encoding="utf-8")

        assert run("add-datasheet", str(external), "--component", "U7") == 0
        out = capsys.readouterr().out
        assert (project / "datasheets" / "bme280-datasheet.txt").is_file()
        assert "Indexed BME280" in out
        assert "registers extracted" in out

        assert run("search", "humidity oversampling") == 0
        assert "CTRL_HUM" in capsys.readouterr().out


class TestBoardCommands:
    def test_board_check_reports_problems(self, project, capsys):
        run("init", ".")
        (project / "board.yaml").write_text(
            "board: {name: b}\nmcu: {part: STM32F401}\n"
            "components:\n"
            "  - {ref: U1, part: A, bus: {name: I2C1, address: 0x40}}\n"
            "  - {ref: U2, part: B, bus: {name: I2C1, address: 0x40}}\n",
            encoding="utf-8",
        )
        assert run("board", "check") == 1
        assert "address collision" in capsys.readouterr().out

    def test_board_check_passes_on_a_clean_profile(self, project, capsys):
        run("init", ".")
        (project / "board.yaml").write_text(
            "board: {name: b}\nmcu: {part: STM32F401}\n", encoding="utf-8"
        )
        assert run("board", "check") == 0
        assert "consistent" in capsys.readouterr().out

    def test_board_show(self, project, capsys):
        run("init", ".")
        assert run("board", "show") == 0
        assert "my-custom-board" in capsys.readouterr().out


class TestScaffoldCommand:
    def _board(self, project):
        (project / "board.yaml").write_text(
            "board: {name: node}\n"
            "mcu: {part: STM32F411, core: Cortex-M4F, flash_kb: 512, ram_kb: 128}\n"
            "buses:\n  - {name: I2C1, type: i2c, speed_hz: 400000, pins: {scl: PB6, sda: PB7}}\n"
            "components:\n  - {ref: U2, part: ACME1234, bus: {name: I2C1, address: 0x76}}\n"
            "pins:\n  - {pin: PB6, net: I2C1_SCL}\n  - {pin: PB7, net: I2C1_SDA}\n",
            encoding="utf-8",
        )

    def test_dry_run_writes_nothing(self, project, capsys):
        run("init", ".")
        self._board(project)
        assert run("scaffold", "--target", "cmsis-bare", "--dry-run") == 0
        assert "Would generate" in capsys.readouterr().out
        assert not (project / "board_pins.h").exists()

    def test_generates_pins_drivers_and_build(self, project, tmp_path):
        run("init", ".")
        self._board(project)
        out_dir = tmp_path / "gen"
        assert run("scaffold", "--target", "cmsis-bare", "--out", str(out_dir)) == 0
        assert "0x76u" in (out_dir / "board_pins.h").read_text()
        assert (out_dir / "drivers" / "acme1234.c").is_file()
        assert (out_dir / "node.ld").is_file()
        assert "cortex-m4" in (out_dir / "Makefile").read_text()

    def test_existing_files_are_preserved(self, project, tmp_path, capsys):
        run("init", ".")
        self._board(project)
        out_dir = tmp_path / "gen"
        run("scaffold", "--target", "drivers-only", "--out", str(out_dir))
        (out_dir / "board_pins.h").write_text("/* mine */", encoding="utf-8")
        run("scaffold", "--target", "drivers-only", "--out", str(out_dir))
        assert (out_dir / "board_pins.h").read_text() == "/* mine */"
        assert "skip" in capsys.readouterr().out


class TestLintCommand:
    def _buggy(self, project):
        (project / "src" / "isr.c").write_text(
            "uint32_t g_count;\n"
            "void TIM2_IRQHandler(void)\n{\n    g_count++;\n    HAL_Delay(1);\n}\n",
            encoding="utf-8",
        )

    def test_reports_findings_and_exits_nonzero(self, project, capsys):
        run("init", ".")
        self._buggy(project)
        assert run("lint") == ExitCode.FINDINGS
        out = capsys.readouterr().out
        assert "FW001" in out and "src/isr.c" in out

    def test_clean_project_exits_zero(self, project, capsys):
        run("init", ".")
        assert run("lint") == 0
        assert "No findings" in capsys.readouterr().out

    def test_json_output_is_machine_readable(self, project, capsys):
        import json

        run("init", ".")
        self._buggy(project)
        capsys.readouterr()  # discard init output; the JSON must stand alone
        run("lint", "--format", "json")
        payload = json.loads(capsys.readouterr().out)
        assert payload["counts"]["error"] >= 1
        assert payload["findings"][0]["file"].endswith("isr.c")

    def test_explain_documents_a_rule(self, project, capsys):
        run("init", ".")
        assert run("lint", "--explain", "FW001") == 0
        assert "interrupt handler" in capsys.readouterr().out.lower()

    def test_path_filter(self, project, capsys):
        run("init", ".")
        self._buggy(project)
        assert run("lint", "--path", "docs") == 0  # nothing under docs/


class TestSizeCommand:
    def test_reports_missing_artifacts(self, project, capsys):
        run("init", ".")
        assert run("size") == ExitCode.NOT_FOUND
        assert "no build artifacts" in capsys.readouterr().err

    def test_reads_a_linker_map(self, project, capsys):
        run("init", ".")
        (project / "board.yaml").write_text(
            "board: {name: b}\nmcu: {part: X, flash_kb: 512, ram_kb: 128}\n", encoding="utf-8"
        )
        (project / "build").mkdir(exist_ok=True)
        (project / "build" / "fw.map").write_text(
            "Memory Configuration\n\n"
            "Name             Origin             Length             Attributes\n"
            "FLASH            0x08000000         0x00080000         xr\n"
            "RAM              0x20000000         0x00020000         xrw\n\n"
            "Linker script and memory map\n\n"
            " .text.main     0x08000000      0x400 build/main.o\n"
            " .bss.buf       0x20000000      0x800 build/main.o\n",
            encoding="utf-8",
        )
        assert run("size") == 0
        out = capsys.readouterr().out
        assert "Memory budget" in out and "build/main.o" in out


class TestRegsCommand:
    def test_lists_parts(self, project, capsys):
        run("init", ".")
        (project / "datasheets" / "acme.txt").write_text(
            "ACME1234\nCTRL_MEAS 0xF4 R/W Oversampling\nCHIP_ID 0xD0 R Identity\n",
            encoding="utf-8",
        )
        run("index")
        capsys.readouterr()
        assert run("regs", "--list") == 0
        assert "ACME1234" in capsys.readouterr().out

    def test_generates_a_header_file(self, project, capsys):
        run("init", ".")
        (project / "datasheets" / "acme.txt").write_text(
            "ACME1234\nCTRL_MEAS 0xF4 R/W Oversampling\n", encoding="utf-8"
        )
        run("index")
        assert run("regs", "ACME1234", "--out", "drivers/acme_regs.h") == 0
        header = (project / "drivers" / "acme_regs.h").read_text()
        assert "#define ACME1234_REG_CTRL_MEAS" in header and "0xF4u" in header

    def test_unknown_part_is_reported(self, project, capsys):
        run("init", ".")
        run("index")
        assert run("regs", "NOSUCHPART") == ExitCode.NOT_FOUND


class TestDoctorCommand:
    def test_runs_and_reports(self, project, capsys):
        run("init", ".")
        run("index")
        capsys.readouterr()
        code = run("doctor")
        out = capsys.readouterr().out
        assert "fwcopilot doctor" in out
        assert "workspace" in out
        assert code in (0, ExitCode.FINDINGS)


class TestBuildCommand:
    def test_requires_configuration(self, project, capsys):
        run("init", ".")
        assert run("build") == ExitCode.ERROR
        assert "no build command configured" in capsys.readouterr().err

    def test_parses_diagnostics_from_a_failing_build(self, project, capsys):
        run("init", ".")
        cfg_path = project / ".fwcopilot" / "config.yaml"
        cfg_path.write_text(
            cfg_path.read_text().replace(
                "  command: null",
                "  command: \"echo 'src/main.c:42:9: error: x undeclared' >&2; exit 1\"",
            ),
            encoding="utf-8",
        )
        assert run("build") == ExitCode.ERROR
        out = capsys.readouterr().out
        assert "src/main.c:42" in out and "1 error" in out


class TestStatus:
    def test_status_summarises_everything(self, project, capsys):
        run("init", ".")
        run("index")
        assert run("status") == 0
        out = capsys.readouterr().out
        assert "Project structure" in out
        assert "Board" in out
        assert "## Index" in out

    def test_sessions_listing_is_empty_at_first(self, project, capsys):
        run("init", ".")
        assert run("sessions") == 0
        assert "No saved sessions" in capsys.readouterr().out
