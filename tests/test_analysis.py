"""Firmware analysis: static checks, memory budget, build diagnostics, register codegen."""

from fwcopilot.diagnostics import BuildResult, parse_build_output
from fwcopilot.lint import (
    RULE_DOCS,
    find_functions,
    lint_source,
    strip_noncode,
    summarize,
)
from fwcopilot.memory import (
    analyze,
    discover_artifacts,
    parse_map_file,
    parse_size_output,
)
from fwcopilot.regsgen import collect_registers, generate_header

# --- lint -------------------------------------------------------------------

ISR_SOURCE = """\
#include <stdint.h>

uint32_t g_count;              /* not volatile */
volatile uint32_t g_flag;

void TIM2_IRQHandler(void)
{
    g_count++;
    g_flag = 1;
    HAL_Delay(5);
    xQueueSend(q, &g_count, 0);
    float f = 1.0f;
    (void)f;
}

void safe_function(void)
{
    HAL_Delay(100);
    g_count = 0;
}
"""


def rules_in(findings):
    return {f.rule for f in findings}


class TestStripNoncode:
    def test_comments_are_blanked_but_lines_preserved(self):
        src = "int a;\n// HAL_Delay(1) in a comment\n/* malloc */\nint b;\n"
        out = strip_noncode(src)
        assert "HAL_Delay" not in out
        assert "malloc" not in out
        assert out.count("\n") == src.count("\n")  # line numbers stay aligned

    def test_string_literals_are_blanked(self):
        out = strip_noncode('const char *s = "call malloc and HAL_Delay";')
        assert "malloc" not in out and "HAL_Delay" not in out

    def test_escaped_quote_does_not_end_the_string(self):
        out = strip_noncode('char *s = "a\\"malloc"; int x;')
        assert "malloc" not in out
        assert "int x;" in out

    def test_code_survives(self):
        assert "HAL_I2C_Init" in strip_noncode("HAL_I2C_Init(&h); // set up\n")


class TestFunctionDetection:
    def test_isr_detected_by_name(self):
        fns = {f.name: f for f in find_functions(ISR_SOURCE)}
        assert fns["TIM2_IRQHandler"].is_isr
        assert not fns["safe_function"].is_isr

    def test_avr_isr_macro(self):
        fns = find_functions("ISR(TIMER0_OVF_vect)\n{\n    x++;\n}\n")
        assert fns and fns[0].is_isr

    def test_attribute_marked_isr(self):
        code = "__attribute__((interrupt))\nvoid my_handler(void)\n{\n    y++;\n}\n"
        fns = find_functions(code)
        assert fns and fns[0].is_isr

    def test_prototypes_are_not_functions(self):
        assert find_functions("void foo(void);\nint bar(int a);\n") == []


class TestIsrRules:
    def setup_method(self):
        self.findings = lint_source("isr.c", ISR_SOURCE)

    def test_blocking_call_in_isr(self):
        hits = [f for f in self.findings if f.rule == "FW001"]
        assert hits and hits[0].line == 10
        assert hits[0].severity == "error"

    def test_float_in_isr(self):
        assert any(f.rule == "FW002" for f in self.findings)

    def test_nonvolatile_global_written_in_isr(self):
        hits = [f for f in self.findings if f.rule == "FW003"]
        assert hits and "g_count" in hits[0].message

    def test_volatile_global_is_not_flagged(self):
        assert all("g_flag" not in f.message for f in self.findings)

    def test_freertos_api_needs_fromisr(self):
        hits = [f for f in self.findings if f.rule == "FW009"]
        assert hits and "xQueueSendFromISR" in hits[0].suggestion

    def test_blocking_outside_an_isr_is_fine(self):
        # HAL_Delay on line 19 is in a normal function — must not be FW001.
        assert all(not (f.rule == "FW001" and f.line >= 18) for f in self.findings)

    def test_findings_are_line_sorted(self):
        assert [f.line for f in self.findings] == sorted(f.line for f in self.findings)


class TestOtherRules:
    def test_busy_wait_without_timeout(self):
        findings = lint_source("a.c", "void f(void)\n{\n    while (!(SR & 1));\n}\n")
        assert "FW004" in rules_in(findings)

    def test_busy_wait_with_a_timeout_is_accepted(self):
        findings = lint_source(
            "a.c", "void f(void)\n{\n    while (!(SR & 1) && HAL_GetTick() < timeout);\n}\n"
        )
        assert "FW004" not in rules_in(findings)

    def test_discarded_hal_status(self):
        findings = lint_source("a.c", "void f(void)\n{\n    HAL_I2C_Init(&h);\n}\n")
        assert "FW005" in rules_in(findings)

    def test_gpio_and_delay_helpers_are_not_status_checked(self):
        findings = lint_source(
            "a.c", "void f(void)\n{\n    HAL_GPIO_WritePin(a, b, c);\n    HAL_Delay(1);\n}\n"
        )
        assert "FW005" not in rules_in(findings)

    def test_checked_hal_call_is_accepted(self):
        findings = lint_source(
            "a.c", "void f(void)\n{\n    if (HAL_I2C_Init(&h) != HAL_OK) { return; }\n}\n"
        )
        assert "FW005" not in rules_in(findings)

    def test_unbounded_string_function(self):
        findings = lint_source("a.c", "void f(void)\n{\n    strcpy(dst, src);\n}\n")
        hits = [f for f in findings if f.rule == "FW006"]
        assert hits and "strncpy" in hits[0].suggestion

    def test_dynamic_allocation_is_info(self):
        findings = lint_source("a.c", "void f(void)\n{\n    void *p = malloc(8);\n}\n")
        hits = [f for f in findings if f.rule == "FW007"]
        assert hits and hits[0].severity == "info"

    def test_blocking_inside_a_critical_section(self):
        code = "void f(void)\n{\n    __disable_irq();\n    HAL_Delay(2);\n    __enable_irq();\n}\n"
        hits = [f for f in lint_source("a.c", code) if f.rule == "FW008"]
        assert hits and hits[0].severity == "error"

    def test_long_critical_section(self):
        body = "\n".join(f"    x{i}++;" for i in range(60))
        code = f"void f(void)\n{{\n    __disable_irq();\n{body}\n    __enable_irq();\n}}\n"
        assert "FW008" in rules_in(lint_source("a.c", code))

    def test_hardware_pointer_without_volatile(self):
        findings = lint_source(
            "a.c", "void f(void)\n{\n    uint32_t *r = (uint32_t *)0x40020000;\n}\n"
        )
        assert "FW011" in rules_in(findings)

    def test_volatile_hardware_pointer_is_accepted(self):
        code = "void f(void)\n{\n    volatile uint32_t *r = (volatile uint32_t *)0x40020000;\n}\n"
        assert "FW011" not in rules_in(lint_source("a.c", code))

    def test_clean_source_produces_nothing(self):
        code = (
            "#include <stdint.h>\n"
            "static volatile uint32_t g_ticks;\n"
            "void SysTick_Handler(void)\n{\n    g_ticks++;\n}\n"
        )
        assert lint_source("clean.c", code) == []


class TestLintReporting:
    def test_summarize_counts_by_severity(self):
        counts = summarize(lint_source("isr.c", ISR_SOURCE))
        assert counts["error"] >= 2
        assert set(counts) == {"error", "warning", "info"}

    def test_every_emitted_rule_is_documented(self):
        emitted = rules_in(lint_source("isr.c", ISR_SOURCE))
        assert emitted <= set(RULE_DOCS)

    def test_finding_serializes_with_location(self):
        finding = lint_source("isr.c", ISR_SOURCE)[0]
        data = finding.to_dict()
        assert data["file"] == "isr.c" and data["line"] > 0 and data["rule"].startswith("FW")


# --- memory -----------------------------------------------------------------

BERKELEY = """\
   text	   data	    bss	    dec	    hex	filename
  51280	   1128	   9216	  61624	   f0b8	firmware.elf
"""

SYSV = """\
firmware.elf  :
section              size         addr
.isr_vector           392   134217728
.text               51280   134218120
.data                1128   536870912
.bss                 9216   536872040
"""

MAP = """\
Memory Configuration

Name             Origin             Length             Attributes
FLASH            0x08000000         0x00080000         xr
RAM              0x20000000         0x00020000         xrw
*default*        0x00000000         0xffffffff

Linker script and memory map

.text           0x08000000     0x2000
 .text.main     0x08000000      0x400 build/main.o
 .text.driver   0x08000400      0x800 build/drivers/acme.o
 .text.libc     0x08000c00     0x1400 libc.a(printf.o)

.bss            0x20000000     0x1000
 .bss.buffers   0x20000000     0x0c00 build/main.o
 .bss.state     0x20000c00     0x0400 build/drivers/acme.o
"""


class TestSizeParsing:
    def test_berkeley_format(self):
        sizes = parse_size_output(BERKELEY)
        assert (sizes.text, sizes.data, sizes.bss) == (51280, 1128, 9216)

    def test_flash_includes_data_initialisers(self):
        sizes = parse_size_output(BERKELEY)
        assert sizes.flash == 51280 + 1128
        assert sizes.ram == 1128 + 9216

    def test_sysv_format(self):
        sizes = parse_size_output(SYSV)
        assert sizes.text == 392 + 51280
        assert sizes.data == 1128
        assert sizes.bss == 9216

    def test_garbage_yields_zeroes(self):
        sizes = parse_size_output("no sizes here")
        assert (sizes.text, sizes.data, sizes.bss) == (0, 0, 0)


class TestMapParsing:
    def test_memory_regions(self):
        regions, _, _ = parse_map_file(MAP)
        by_name = {r.name: r for r in regions}
        assert by_name["FLASH"].origin == 0x08000000
        assert by_name["FLASH"].length == 0x80000
        assert "*default*" not in by_name

    def test_object_attribution(self):
        _, objects, _ = parse_map_file(MAP)
        by_name = {o.name: o for o in objects}
        assert by_name["build/main.o"].flash == 0x400
        assert by_name["build/main.o"].ram == 0xC00

    def test_archive_members_are_named_readably(self):
        _, objects, _ = parse_map_file(MAP)
        assert any(o.name == "libc.a(printf.o)" for o in objects)

    def test_objects_are_sorted_largest_first(self):
        _, objects, _ = parse_map_file(MAP)
        assert objects == sorted(objects, key=lambda o: o.total, reverse=True)

    def test_empty_map_is_harmless(self):
        assert parse_map_file("") == ([], [], {})


class TestBudget:
    def test_uses_board_capacity_over_linker_regions(self, workspace):
        from fwcopilot.board import load_board

        report = analyze(load_board(workspace.board_path), size_output=BERKELEY, map_text=MAP)
        assert report.flash_total == 512 * 1024  # board.yaml, not the 0x80000 region
        assert report.flash_source == "board.yaml"
        assert report.ram_total == 128 * 1024

    def test_falls_back_to_map_regions_without_a_board(self, tmp_path):
        from fwcopilot.board import load_board

        report = analyze(load_board(tmp_path / "none.yaml"), size_output=BERKELEY, map_text=MAP)
        assert report.flash_total == 0x80000
        assert report.flash_source == "map:FLASH"

    def test_over_budget_is_detected_and_explained(self, workspace):
        from fwcopilot.board import load_board

        huge = "   text	   data	    bss	    dec	    hex	filename\n 900000	   1000	   2000	 903000	  dc6d8	f.elf\n"
        report = analyze(load_board(workspace.board_path), size_output=huge)
        assert report.over_budget
        assert any("overflows" in w for w in report.warnings)

    def test_headroom_warning_below_overflow(self, workspace):
        from fwcopilot.board import load_board

        near = "   text	   data	    bss	    dec	    hex	filename\n 500000	   1000	   2000	 503000	  7add8	f.elf\n"
        report = analyze(load_board(workspace.board_path), size_output=near)
        assert not report.over_budget
        assert any("headroom" in w or "%" in w for w in report.warnings)

    def test_markdown_report_shows_both_budgets(self, workspace):
        from fwcopilot.board import load_board

        text = analyze(
            load_board(workspace.board_path), size_output=BERKELEY, map_text=MAP
        ).to_markdown()
        assert "Flash" in text and "RAM" in text and "%" in text
        assert "build/main.o" in text

    def test_no_artifacts_reports_clearly(self, workspace):
        from fwcopilot.board import load_board

        report = analyze(load_board(workspace.board_path))
        assert any("no sizes found" in w for w in report.warnings)

    def test_percentages_are_none_without_a_budget(self, tmp_path):
        from fwcopilot.board import load_board

        report = analyze(load_board(tmp_path / "none.yaml"), size_output=BERKELEY)
        assert report.flash_pct is None and not report.over_budget


class TestArtifactDiscovery:
    def test_finds_the_newest_elf_and_map(self, tmp_path):
        (tmp_path / "build").mkdir()
        (tmp_path / "build" / "fw.elf").write_bytes(b"\x7fELF")
        (tmp_path / "build" / "fw.map").write_text("Memory Configuration\n")
        elf, map_file = discover_artifacts(tmp_path)
        assert elf.name == "fw.elf" and map_file.name == "fw.map"

    def test_explicit_hints_win(self, tmp_path):
        (tmp_path / "a.elf").write_bytes(b"\x7fELF")
        (tmp_path / "b.elf").write_bytes(b"\x7fELF")
        elf, _ = discover_artifacts(tmp_path, elf_hint="b.elf")
        assert elf.name == "b.elf"

    def test_missing_artifacts_return_none(self, tmp_path):
        assert discover_artifacts(tmp_path) == (None, None)


# --- diagnostics ------------------------------------------------------------


class TestDiagnostics:
    def test_gcc_error_with_line_and_column(self):
        diags = parse_build_output("src/main.c:42:9: error: 'x' undeclared here\n")
        assert diags[0].file == "src/main.c"
        assert (diags[0].line, diags[0].column) == (42, 9)
        assert diags[0].severity == "error"

    def test_warning_is_classified_separately(self):
        diags = parse_build_output("src/a.c:3:1: warning: unused variable 'y'\n")
        assert diags[0].severity == "warning"

    def test_arm_compiler_format(self):
        diags = parse_build_output('"src/main.c", line 12: Error[Pe020]: identifier is undefined\n')
        assert diags[0].file == "src/main.c" and diags[0].line == 12

    def test_undefined_reference_carries_the_symbol(self):
        diags = parse_build_output("main.o: undefined reference to `sensor_init'\n")
        assert diags[0].kind == "link" and diags[0].symbol == "sensor_init"
        assert "extern" in diags[0].hint

    def test_multiple_definition(self):
        diags = parse_build_output("b.o: multiple definition of `g_state'\n")
        assert diags[0].kind == "link" and "static" in diags[0].hint

    def test_region_overflow_is_a_memory_diagnostic(self):
        diags = parse_build_output("ld: region `FLASH' overflowed by 2048 bytes\n")
        assert diags[0].kind == "memory"
        assert "2048" in diags[0].message
        assert "fwcopilot size" in diags[0].hint

    def test_missing_toolchain(self):
        diags = parse_build_output("make: arm-none-eabi-gcc: command not found\n")
        assert diags[0].kind == "toolchain"
        assert "doctor" in diags[0].hint

    def test_duplicates_are_collapsed(self):
        line = "src/main.c:42:9: error: 'x' undeclared here\n"
        assert len(parse_build_output(line * 3)) == 1

    def test_plain_output_yields_nothing(self):
        assert parse_build_output("Building...\n[100%] Linking\n") == []

    def test_build_result_summary_and_format(self):
        result = BuildResult(
            command="make",
            returncode=1,
            output="src/main.c:42:9: error: 'x' undeclared here\n",
            diagnostics=parse_build_output("src/main.c:42:9: error: 'x' undeclared here\n"),
        )
        assert not result.ok
        assert len(result.errors) == 1
        assert "failed (exit 1)" in result.summary()
        assert "src/main.c:42" in result.format()

    def test_failed_build_without_diagnostics_shows_output_tail(self):
        result = BuildResult(command="make", returncode=2, output="something broke\n")
        assert "no diagnostics parsed" in result.format()


# --- register codegen -------------------------------------------------------


class TestRegisterHeader:
    def test_collects_registers_from_the_index(self, indexed):
        _, store = indexed
        regs = collect_registers(store, "ACME1234")
        assert {r.name for r in regs} >= {"CHIP_ID", "CTRL_MEAS", "CONFIG"}

    def test_sorted_by_address(self, indexed):
        _, store = indexed
        regs = collect_registers(store, "ACME1234")
        addrs = [int(r.address, 16) for r in regs]
        assert addrs == sorted(addrs)

    def test_header_is_guarded_and_cites_pages(self, indexed):
        _, store = indexed
        header = generate_header(
            "ACME1234", collect_registers(store, "ACME1234"), source="datasheets/acme.txt"
        )
        assert "#ifndef ACME1234_REGS_H" in header
        assert "#define ACME1234_REG_CTRL_MEAS" in header
        assert "0xF4u" in header
        assert "p." in header  # page citation for review
        assert header.rstrip().endswith("#endif /* ACME1234_REGS_H */")

    def test_addresses_are_uppercase_hex(self, indexed):
        _, store = indexed
        header = generate_header("ACME1234", collect_registers(store, "ACME1234"))
        assert "0xd0u" not in header and "0xD0u" in header

    def test_empty_register_set_still_produces_valid_c(self):
        header = generate_header("MYSTERY", [])
        assert "#ifndef MYSTERY_REGS_H" in header
        assert "#endif" in header
        assert "No register entries" in header

    def test_part_name_is_sanitized_into_an_identifier(self):
        header = generate_header("W25Q128-JV", [])
        assert "W25Q128_JV_REGS_H" in header
