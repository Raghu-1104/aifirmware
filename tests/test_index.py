"""Retrieval: query sanitization, datasheet ingest, register extraction, code search."""

import pytest

from fwcopilot.datasheets import (
    chunk_page,
    extract_registers,
    guess_part_number,
    ingest_datasheet,
)
from fwcopilot.project import chunk_source, parse_linker_script, scan_project
from fwcopilot.store import build_match_query


class TestMatchQuery:
    def test_plain_words_become_or_terms(self):
        assert build_match_query("chip id") == '"chip" OR "id"'

    def test_quoted_phrase_is_preserved(self):
        assert build_match_query('"power up sequence"') == '"power up sequence"'

    def test_underscores_survive_for_register_names(self):
        assert '"CTRL_MEAS"' in build_match_query("what does CTRL_MEAS do?")

    @pytest.mark.parametrize(
        "hostile",
        ['NEAR("a" "b")', "foo AND (bar", 'unbalanced " quote', "a OR OR b", "*:-^", "col:val"],
    )
    def test_fts5_operators_cannot_leak_through(self, hostile, indexed):
        """A user question is data, never an FTS5 expression."""
        _, store = indexed
        store.search(hostile)  # must not raise
        built = build_match_query(hostile)
        assert all(term.startswith('"') for term in built.split(" OR ") if term)

    def test_empty_query_returns_nothing(self, indexed):
        _, store = indexed
        assert store.search("!!!") == []


class TestDatasheetIngest:
    def test_indexes_pages_and_registers(self, indexed):
        _, store = indexed
        stats = store.stats()
        assert stats["datasheets"] == 1
        assert stats["registers"] >= 4

    def test_search_finds_power_up_timing(self, indexed):
        _, store = indexed
        hits = store.search("startup time after VDD", kind="datasheet")
        assert hits
        assert "t_startup" in hits[0].text

    def test_hits_carry_a_citable_locator(self, indexed):
        _, store = indexed
        hit = store.search("oversampling power mode", kind="datasheet")[0]
        assert hit.part == "ACME1234"
        assert hit.locator().startswith("ACME1234 p.")

    def test_registers_get_name_and_address(self, indexed):
        _, store = indexed
        rows = store.find_registers("CTRL_MEAS")
        assert len(rows) == 1
        assert rows[0]["address"] == "0xf4"
        assert "Oversampling" in rows[0]["description"]

    def test_lookup_by_address(self, indexed):
        _, store = indexed
        rows = store.find_registers("0xd0")
        assert [r["name"] for r in rows] == ["CHIP_ID"]

    def test_component_attribution_from_board(self, indexed):
        _, store = indexed
        doc = store.list_docs("datasheet")[0]
        assert doc["component"] == "U2"
        assert doc["part"] == "ACME1234"

    def test_unchanged_file_is_skipped_on_reingest(self, workspace):
        from fwcopilot.store import Store

        cfg = workspace
        path = cfg.datasheets_path / "acme1234.txt"
        with Store(cfg.db_path) as store:
            first = ingest_datasheet(store, path, rel_path="datasheets/acme1234.txt")
            assert not first.skipped
            second = ingest_datasheet(store, path, rel_path="datasheets/acme1234.txt")
            assert second.skipped and second.reason == "unchanged"

    def test_reingest_replaces_rather_than_duplicates(self, workspace):
        from fwcopilot.store import Store

        cfg = workspace
        path = cfg.datasheets_path / "acme1234.txt"
        with Store(cfg.db_path) as store:
            ingest_datasheet(store, path, rel_path="datasheets/acme1234.txt")
            before = store.stats()["chunks"]
            ingest_datasheet(store, path, rel_path="datasheets/acme1234.txt", force=True)
            assert store.stats()["chunks"] == before


class TestChunking:
    def test_short_page_is_one_chunk(self):
        assert chunk_page("hello world") == ["hello world"]

    def test_long_page_splits_with_overlap(self):
        text = "\n\n".join(f"Paragraph {i} " + "x" * 200 for i in range(20))
        chunks = chunk_page(text, size=600, overlap=100)
        assert len(chunks) > 1
        assert all(len(c) <= 700 for c in chunks)
        assert "".join(chunks).count("Paragraph 0") >= 1

    def test_source_chunks_are_line_addressed(self):
        text = "\n".join(f"line {i}" for i in range(200))
        chunks = chunk_source(text, lines_per_chunk=50)
        assert chunks[0]["line"] == 1
        assert chunks[0]["end_line"] == 50
        assert chunks[1]["line"] < chunks[0]["end_line"]  # overlapping windows


class TestRegisterExtraction:
    def test_name_then_address(self):
        regs = extract_registers(["CTRL_REG1  0x20  R/W  Control register 1"])
        assert regs[0]["name"] == "CTRL_REG1"
        assert regs[0]["address"] == "0x20"

    def test_address_then_name(self):
        regs = extract_registers(["0x28  OUT_X_L  X axis low byte"])
        assert regs[0]["name"] == "OUT_X_L"

    def test_parenthesised_form(self):
        regs = extract_registers(["Address: WHO_AM_I (0x0F) returns 0x33"])
        assert regs[0]["name"] == "WHO_AM_I"
        assert regs[0]["address"] == "0x0f"

    def test_prose_is_not_mistaken_for_a_register(self):
        assert extract_registers(["TABLE 0x01 lists the ordering information"]) == []

    def test_page_numbers_are_recorded(self):
        regs = extract_registers(["intro", "STATUS  0x27  R  Status register"])
        assert regs[0]["page"] == 2


class TestPartNumberGuess:
    @pytest.mark.parametrize(
        "filename,expected",
        [
            ("bme280-datasheet.pdf", "BME280"),
            ("W25Q128JV_DS_Rev_H.pdf", "W25Q128JV"),
            ("lsm6dsox.pdf", "LSM6DSOX"),
        ],
    )
    def test_from_filename(self, filename, expected):
        assert guess_part_number(filename, []) == expected


class TestProjectScan:
    def test_detects_toolchain_rtos_and_peripherals(self, workspace):
        cfg = workspace
        profile = scan_project(cfg.root, cfg.source_globs, cfg.excludes)
        assert any("arm-none-eabi" in t for t in profile.toolchains)
        assert "STM32F4" in " ".join(profile.mcu_hints)
        assert "I2C" in profile.peripherals
        assert "UART/USART" in profile.peripherals
        assert profile.rtos == []  # bare metal

    def test_finds_isrs_and_entry_point(self, workspace):
        cfg = workspace
        profile = scan_project(cfg.root, cfg.source_globs, cfg.excludes)
        assert "SysTick_Handler" in profile.interrupt_handlers
        assert any(e.endswith("main.c") for e in profile.entry_points)

    def test_reads_memory_map_from_linker_script(self, workspace):
        cfg = workspace
        profile = scan_project(cfg.root, cfg.source_globs, cfg.excludes)
        regions = {r.name: (r.origin, r.length) for r in profile.memory_regions}
        assert regions["FLASH"] == ("0x08000000", "512K")
        assert regions["RAM"] == ("0x20000000", "128K")

    def test_linker_parser_handles_attribute_syntax(self):
        regions = parse_linker_script(
            "MEMORY {\n CCMRAM (xrw) : ORIGIN = 0x10000000, LENGTH = 64K\n}"
        )
        assert regions[0].name == "CCMRAM"
        assert regions[0].length == "64K"

    def test_code_search_finds_symbols(self, indexed):
        _, store = indexed
        hits = store.search("HAL_I2C_Mem_Read", kind="code")
        assert hits
        assert hits[0].path.endswith("main.c")
        assert hits[0].line == 1

    def test_deleted_files_are_pruned_from_the_index(self, workspace):
        from fwcopilot.project import index_sources
        from fwcopilot.store import Store

        cfg = workspace
        with Store(cfg.db_path) as store:
            index_sources(store, cfg.root, cfg.source_globs, cfg.excludes)
            (cfg.root / "src" / "main.c").unlink()
            stats = index_sources(store, cfg.root, cfg.source_globs, cfg.excludes)
            assert stats["removed"] == 1
            assert store.search("HAL_I2C_Mem_Read", kind="code") == []
