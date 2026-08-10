"""Board profile validation and deterministic code generation."""

import pytest
import yaml

from fwcopilot.board import load_board, write_template
from fwcopilot.scaffold import TARGETS, generate, write_files


class TestBoardProfile:
    def test_parses_components_and_buses(self, workspace):
        board = load_board(workspace.board_path)
        assert board.name == "sensor-node"
        assert board.mcu["part"] == "STM32F411CEU6"
        refs = {c.ref: c for c in board.components}
        assert refs["U2"].address == 0x76
        assert refs["U2"].bus_name == "I2C1"
        assert refs["U3"].bus["cs"] == "PA4"

    def test_describe_is_citable(self, workspace):
        board = load_board(workspace.board_path)
        text = next(c.describe() for c in board.components if c.ref == "U2")
        assert "ACME1234" in text and "I2C1" in text and "0x76" in text

    def test_clean_profile_has_no_warnings(self, workspace):
        board = load_board(workspace.board_path)
        assert board.validate(workspace.root) == []

    def test_detects_duplicate_pin_assignment(self, workspace):
        data = yaml.safe_load(workspace.board_path.read_text())
        data["pins"].append({"pin": "PB6", "net": "SOMETHING_ELSE"})
        workspace.board_path.write_text(yaml.safe_dump(data))
        problems = load_board(workspace.board_path).validate(workspace.root)
        assert any("PB6 assigned 2 times" in p for p in problems)

    def test_detects_i2c_address_collision(self, workspace):
        data = yaml.safe_load(workspace.board_path.read_text())
        data["components"].append(
            {"ref": "U5", "part": "OTHER", "bus": {"name": "I2C1", "address": 0x76}}
        )
        workspace.board_path.write_text(yaml.safe_dump(data))
        problems = load_board(workspace.board_path).validate(workspace.root)
        assert any("address collision" in p and "0x76" in p for p in problems)

    def test_detects_missing_datasheet_file(self, workspace):
        (workspace.root / "datasheets" / "acme1234.txt").unlink()
        problems = load_board(workspace.board_path).validate(workspace.root)
        assert any("not found on disk" in p for p in problems)

    def test_detects_undeclared_bus_reference(self, workspace):
        data = yaml.safe_load(workspace.board_path.read_text())
        data["components"][0]["bus"]["name"] = "I2C9"
        workspace.board_path.write_text(yaml.safe_dump(data))
        problems = load_board(workspace.board_path).validate(workspace.root)
        assert any("I2C9" in p for p in problems)

    def test_detects_duplicate_refs(self, workspace):
        data = yaml.safe_load(workspace.board_path.read_text())
        data["components"].append({"ref": "U2", "part": "CLONE"})
        workspace.board_path.write_text(yaml.safe_dump(data))
        problems = load_board(workspace.board_path).validate(workspace.root)
        assert any("duplicate component ref 'U2'" in p for p in problems)

    def test_missing_board_file_is_not_fatal(self, tmp_path):
        board = load_board(tmp_path / "nope.yaml")
        assert board.exists is False
        assert "No board.yaml" in board.to_markdown()

    def test_template_is_valid_and_self_consistent(self, tmp_path):
        path = tmp_path / "board.yaml"
        assert write_template(path) is True
        assert write_template(path) is False  # no clobber without overwrite
        board = load_board(path)
        # The shipped template must not itself contain pin or address conflicts.
        problems = [p for p in board.validate(None) if "not found on disk" not in p]
        assert problems == []


class TestScaffold:
    def test_board_pins_header_uses_real_hardware_values(self, workspace):
        board = load_board(workspace.board_path)
        files = dict(generate(board, "drivers-only"))
        header = files["board_pins.h"]
        assert "#define PIN_I2C1_SCL" in header and '"PB6"' in header
        assert "#define ACME1234_I2C_ADDR         0x76u" in header
        assert "#define BOARD_SYSCLK_HZ       100000000u" in header
        assert "#define BOARD_FLASH_BYTES     (512u * 1024u)" in header

    def test_i2c_driver_embeds_the_device_address(self, workspace):
        board = load_board(workspace.board_path)
        files = dict(generate(board, "drivers-only"))
        header = files["drivers/acme1234.h"]
        assert "#define ACME1234_I2C_ADDRESS 0x76u" in header
        assert "acme1234_read_reg" in header
        assert "acme1234_init" in files["drivers/acme1234.c"]

    def test_spi_device_gets_an_spi_driver(self, workspace):
        board = load_board(workspace.board_path)
        files = dict(generate(board, "drivers-only"))
        assert "w25q128_spi_xfer_fn" in files["drivers/w25q128.h"]
        assert "CS=PA4" in files["drivers/w25q128.h"]

    def test_driver_source_points_at_the_linked_datasheet(self, workspace):
        board = load_board(workspace.board_path)
        files = dict(generate(board, "drivers-only"))
        assert "datasheets/acme1234.txt" in files["drivers/acme1234.c"]

    def test_linker_script_matches_declared_memory(self, workspace):
        board = load_board(workspace.board_path)
        files = dict(generate(board, "cmsis-bare"))
        ld = files["sensor_node.ld"]
        assert "LENGTH = 512K" in ld and "LENGTH = 128K" in ld
        assert "ORIGIN = 0x08000000" in ld

    def test_makefile_picks_cpu_flags_from_the_core(self, workspace):
        board = load_board(workspace.board_path)
        makefile = dict(generate(board, "cmsis-bare"))["Makefile"]
        assert "-mcpu=cortex-m4" in makefile
        assert "-mfpu=fpv4-sp-d16" in makefile  # Cortex-M4F -> hard float

    def test_zephyr_overlay_declares_the_i2c_node(self, workspace):
        board = load_board(workspace.board_path)
        overlay = dict(generate(board, "zephyr"))["boards/sensor_node.overlay"]
        assert "&i2c1 {" in overlay
        assert "reg = <0x76>;" in overlay
        assert "I2C_BITRATE_FAST" in overlay

    def test_zephyr_prj_enables_the_buses_in_use(self, workspace):
        board = load_board(workspace.board_path)
        prj = dict(generate(board, "zephyr"))["prj.conf"]
        assert "CONFIG_I2C=y" in prj and "CONFIG_SPI=y" in prj

    @pytest.mark.parametrize("target", TARGETS)
    def test_every_target_generates_files(self, workspace, target):
        board = load_board(workspace.board_path)
        files = generate(board, target)
        assert files
        assert all(content.strip() for _, content in files)

    def test_unknown_target_is_rejected(self, workspace):
        board = load_board(workspace.board_path)
        with pytest.raises(ValueError):
            generate(board, "arduino-mega")

    def test_generate_requires_a_board_profile(self, tmp_path):
        with pytest.raises(ValueError, match=r"board\.yaml"):
            generate(load_board(tmp_path / "missing.yaml"), "cmsis-bare")

    def test_write_files_never_clobbers_without_force(self, workspace, tmp_path):
        board = load_board(workspace.board_path)
        files = generate(board, "drivers-only")
        out = tmp_path / "gen"
        write_files(out, files)
        (out / "board_pins.h").write_text("/* hand edited */", encoding="utf-8")

        again = write_files(out, files)
        assert all(f.existed for f in again)
        assert (out / "board_pins.h").read_text() == "/* hand edited */"

        write_files(out, files, force=True)
        assert "PIN_I2C1_SCL" in (out / "board_pins.h").read_text()
