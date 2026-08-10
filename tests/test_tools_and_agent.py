"""Tool execution, permission gating, and the agent's tool-use loop."""

from types import SimpleNamespace

import pytest

from fwcopilot.agent import Agent, Session, _blocks_to_dicts
from fwcopilot.context import build_context_block, build_system_blocks
from fwcopilot.tools import ToolRunner, tool_definitions


@pytest.fixture()
def runner(indexed):
    cfg, store = indexed
    return ToolRunner(cfg, store, approve=lambda n, a: True, allow_write=True, allow_build=True)


@pytest.fixture()
def readonly_runner(indexed):
    cfg, store = indexed
    return ToolRunner(cfg, store)


class TestToolDefinitions:
    def test_write_tools_hidden_unless_enabled(self):
        names = {t["name"] for t in tool_definitions(False, False)}
        assert "write_file" not in names and "run_build" not in names
        assert {"search_datasheets", "lookup_register", "get_board_profile"} <= names

    def test_write_tools_appear_when_enabled(self):
        names = {t["name"] for t in tool_definitions(True, True)}
        assert {"write_file", "edit_file", "run_build"} <= names

    def test_every_tool_has_a_schema_and_description(self):
        for tool in tool_definitions(True, True):
            assert tool["description"].strip()
            assert tool["input_schema"]["type"] == "object"


class TestDatasheetTools:
    def test_search_returns_cited_excerpts(self, runner):
        out = runner.run("search_datasheets", {"query": "power up sequence t_startup"})
        assert not out.is_error
        assert "ACME1234 p." in out.text
        assert "2 ms" in out.text

    def test_search_miss_names_what_is_available(self, runner):
        out = runner.run("search_datasheets", {"query": "zzzz nonexistent"})
        assert "ACME1234" in out.text

    def test_read_page_resolves_by_part_name(self, runner):
        out = runner.run("read_datasheet_page", {"datasheet": "ACME1234", "page": 1})
        assert "REGISTER MAP" in out.text

    def test_read_page_rejects_unknown_datasheet(self, runner):
        out = runner.run("read_datasheet_page", {"datasheet": "STM32H7", "page": 1})
        assert out.is_error and "Available" in out.text

    def test_lookup_register_reports_address_and_page(self, runner):
        out = runner.run("lookup_register", {"name": "CTRL_MEAS"})
        assert "0xf4" in out.text and "p.1" in out.text

    def test_lookup_falls_back_to_full_text(self, runner):
        out = runner.run("lookup_register", {"name": "IIR filter"})
        assert "No extracted register entry" in out.text or "CONFIG" in out.text

    def test_list_datasheets(self, runner):
        assert "ACME1234" in runner.run("list_datasheets", {}).text


class TestBoardAndCodeTools:
    def test_board_profile_includes_pins_and_addresses(self, runner):
        out = runner.run("get_board_profile", {})
        assert "0x76" in out.text and "PB6" in out.text

    def test_board_section_filter(self, runner):
        out = runner.run("get_board_profile", {"section": "mcu"})
        assert "STM32F411CEU6" in out.text and "PB6" not in out.text

    def test_search_code(self, runner):
        out = runner.run("search_code", {"query": "SysTick_Handler"})
        assert "main.c" in out.text

    def test_read_file_is_line_numbered(self, runner):
        out = runner.run("read_file", {"path": "src/main.c", "start_line": 1, "line_count": 5})
        assert "    1 | #include" in out.text

    def test_list_files_honours_globs(self, runner):
        out = runner.run("list_files", {"pattern": "src/**/*.c"})
        assert "src/main.c" in out.text

    def test_missing_file_is_reported_not_raised(self, runner):
        out = runner.run("read_file", {"path": "src/nope.c"})
        assert out.is_error and "No such file" in out.text


class TestSandboxing:
    @pytest.mark.parametrize("path", ["../../etc/passwd", "/etc/passwd", "src/../../outside.txt"])
    def test_reads_cannot_escape_the_project_root(self, runner, path):
        out = runner.run("read_file", {"path": path})
        assert out.is_error
        assert "escapes the project root" in out.text or "No such file" in out.text

    def test_writes_cannot_escape_the_project_root(self, runner, tmp_path):
        out = runner.run("write_file", {"path": "../escaped.c", "content": "nope"})
        assert out.is_error
        assert not (runner.cfg.root.parent / "escaped.c").exists()


class TestPermissions:
    def test_write_blocked_when_disabled(self, readonly_runner):
        out = readonly_runner.run("write_file", {"path": "x.c", "content": "int x;"})
        assert out.is_error and "--allow-write" in out.text
        assert not (readonly_runner.cfg.root / "x.c").exists()

    def test_write_blocked_when_user_declines(self, indexed):
        cfg, store = indexed
        runner = ToolRunner(cfg, store, approve=lambda n, a: False, allow_write=True)
        out = runner.run("write_file", {"path": "x.c", "content": "int x;"})
        assert out.is_error and "declined" in out.text
        assert not (cfg.root / "x.c").exists()

    def test_build_blocked_when_disabled(self, readonly_runner):
        assert readonly_runner.run("run_build", {}).is_error

    def test_approved_write_lands_on_disk(self, runner):
        out = runner.run("write_file", {"path": "drivers/new.c", "content": "int y;\n"})
        assert not out.is_error
        assert (runner.cfg.root / "drivers" / "new.c").read_text() == "int y;\n"


class TestEditFile:
    def test_replaces_a_unique_snippet(self, runner):
        out = runner.run(
            "edit_file",
            {
                "path": "src/main.c",
                "old_text": "g_ticks++;",
                "new_text": "g_ticks += 2u;",
            },
        )
        assert not out.is_error
        assert "g_ticks += 2u;" in (runner.cfg.root / "src" / "main.c").read_text()

    def test_ambiguous_match_is_refused(self, runner):
        runner.run("write_file", {"path": "dup.c", "content": "int a;\nint a;\n"})
        out = runner.run("edit_file", {"path": "dup.c", "old_text": "int a;", "new_text": "int b;"})
        assert out.is_error and "appears 2 times" in out.text

    def test_missing_snippet_is_refused(self, runner):
        out = runner.run(
            "edit_file",
            {
                "path": "src/main.c",
                "old_text": "not in the file",
                "new_text": "x",
            },
        )
        assert out.is_error and "not found" in out.text


class TestMemoryTool:
    def test_remember_appends_and_is_reloaded_into_context(self, runner, indexed):
        cfg, store = indexed
        runner.run("remember", {"note": "VDD rail droops on U3 writes; add 10uF bulk cap."})
        assert "droops" in cfg.notes_path.read_text()
        assert "droops" in build_context_block(cfg, store)


class TestRunBuild:
    def test_reports_missing_configuration(self, runner):
        assert "No build command configured" in runner.run("run_build", {}).text

    def test_runs_the_configured_command(self, indexed):
        cfg, store = indexed
        cfg.build_command = "echo compiling && exit 0"
        runner = ToolRunner(cfg, store, approve=lambda n, a: True, allow_build=True)
        out = runner.run("run_build", {})
        assert not out.is_error and "compiling" in out.text

    def test_surfaces_build_failure(self, indexed):
        cfg, store = indexed
        cfg.build_command = "echo 'main.c:12: error: undefined reference' >&2 && exit 1"
        runner = ToolRunner(cfg, store, approve=lambda n, a: True, allow_build=True)
        out = runner.run("run_build", {})
        assert out.is_error and "undefined reference" in out.text


class TestErrorHandling:
    def test_unknown_tool_is_reported(self, runner):
        assert runner.run("frobnicate", {}).is_error

    def test_exceptions_become_tool_errors(self, runner):
        out = runner.run("read_file", {})  # missing required arg
        assert out.is_error and "KeyError" in out.text

    def test_output_is_truncated(self, runner):
        big = "x" * 60000
        runner.run("write_file", {"path": "big.txt", "content": big})
        out = runner.run("read_file", {"path": "big.txt", "line_count": 800})
        assert len(out.text) <= 20100 and out.text.endswith("[truncated]")


class TestSystemContext:
    def test_context_names_the_board_and_datasheets(self, indexed):
        cfg, store = indexed
        text = build_context_block(cfg, store)
        assert "sensor-node" in text
        assert "STM32F411CEU6" in text
        assert "ACME1234" in text
        assert "arm-none-eabi" in text
        assert "FLASH @ 0x08000000" in text

    def test_system_blocks_are_cache_marked(self, indexed):
        cfg, store = indexed
        blocks = build_system_blocks(cfg, store)
        assert blocks[0]["text"].startswith("You are fwcopilot")
        assert blocks[-1]["cache_control"] == {"type": "ephemeral"}

    def test_missing_datasheets_are_called_out(self, workspace):
        from fwcopilot.store import Store

        with Store(workspace.db_path) as store:
            assert "None indexed yet" in build_context_block(workspace, store)


# --- agent loop -------------------------------------------------------------


class FakeStream:
    """Stands in for the SDK's streaming context manager."""

    def __init__(self, message, events):
        self._message = message
        self._events = events

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self._events)

    def get_final_message(self):
        return self._message


class FakeMessages:
    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.calls = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        message, events = self.scripted.pop(0)
        return FakeStream(message, events)


class FakeClient:
    def __init__(self, scripted):
        self.messages = FakeMessages(scripted)


def text_delta(text):
    return SimpleNamespace(
        type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=text)
    )


def message(blocks, stop_reason="end_turn"):
    return SimpleNamespace(content=blocks, stop_reason=stop_reason, stop_details=None)


class TestAgentLoop:
    def _agent(self, indexed, scripted, **kw):
        cfg, store = indexed
        runner = ToolRunner(cfg, store, approve=lambda n, a: True, **kw)
        return Agent(cfg, store, runner, client=FakeClient(scripted), use_fallbacks=False, **kw)

    def test_plain_answer_streams_and_persists(self, indexed):
        agent = self._agent(
            indexed,
            [
                (message([{"type": "text", "text": "Use 400 kHz."}]), [text_delta("Use 400 kHz.")]),
            ],
        )
        events = list(agent.stream_turn("what speed for I2C1?"))
        assert [e.text for e in events if e.type == "text_delta"] == ["Use 400 kHz."]
        assert events[-1].type == "done"
        assert agent.session.path.is_file()
        assert agent.session.messages[0]["content"] == "what speed for I2C1?"

    def test_tool_call_is_executed_and_fed_back(self, indexed):
        tool_use = {
            "type": "tool_use",
            "id": "toolu_1",
            "name": "lookup_register",
            "input": {"name": "CTRL_MEAS"},
        }
        agent = self._agent(
            indexed,
            [
                (message([tool_use], stop_reason="tool_use"), []),
                (
                    message([{"type": "text", "text": "CTRL_MEAS is at 0xF4 (ACME1234 p.1)."}]),
                    [text_delta("CTRL_MEAS is at 0xF4 (ACME1234 p.1).")],
                ),
            ],
        )
        events = list(agent.stream_turn("where is CTRL_MEAS?"))

        assert [e.name for e in events if e.type == "tool_use"] == ["lookup_register"]
        # The real tool ran and its output was sent back as a tool_result.
        result_msg = agent.session.messages[2]
        assert result_msg["role"] == "user"
        assert result_msg["content"][0]["tool_use_id"] == "toolu_1"
        assert "0xf4" in result_msg["content"][0]["content"]
        # And the follow-up request carried the full history.
        assert len(agent.client.messages.calls) == 2
        assert len(agent.client.messages.calls[1]["messages"]) == 3

    def test_tool_error_is_reported_not_raised(self, indexed):
        tool_use = {
            "type": "tool_use",
            "id": "t1",
            "name": "read_file",
            "input": {"path": "../../etc/passwd"},
        }
        agent = self._agent(
            indexed,
            [
                (message([tool_use], stop_reason="tool_use"), []),
                (message([{"type": "text", "text": "That path is outside the project."}]), []),
            ],
        )
        events = list(agent.stream_turn("read /etc/passwd"))
        errors = [e for e in events if e.type == "tool_result" and e.data.get("is_error")]
        assert errors
        assert agent.session.messages[2]["content"][0]["is_error"] is True

    def test_refusal_is_surfaced_cleanly(self, indexed):
        msg = message([], stop_reason="refusal")
        msg.stop_details = SimpleNamespace(category="cyber", explanation="declined")
        agent = self._agent(indexed, [(msg, [])])
        events = list(agent.stream_turn("do something disallowed"))
        assert events[-1].type == "error" and "cyber" in events[-1].text

    def test_pause_turn_resumes(self, indexed):
        agent = self._agent(
            indexed,
            [
                (message([{"type": "text", "text": "part 1 "}], stop_reason="pause_turn"), []),
                (message([{"type": "text", "text": "part 2"}]), []),
            ],
        )
        events = list(agent.stream_turn("long running question"))
        assert events[-1].type == "done"
        assert len(agent.client.messages.calls) == 2

    def test_missing_credentials_is_explained_not_raised(self, indexed):
        class NoAuthMessages:
            def stream(self, **kwargs):
                raise TypeError(
                    "Could not resolve authentication method. Expected one of "
                    "api_key, auth_token, or credentials to be set."
                )

        cfg, store = indexed
        agent = Agent(
            cfg,
            store,
            ToolRunner(cfg, store),
            client=SimpleNamespace(messages=NoAuthMessages()),
            use_fallbacks=False,
        )
        events = list(agent.stream_turn("hello"))
        assert events[-1].type == "error"
        assert "ANTHROPIC_API_KEY" in events[-1].text

    def test_unrelated_type_errors_still_propagate(self, indexed):
        class BrokenMessages:
            def stream(self, **kwargs):
                raise TypeError("stream() got an unexpected keyword argument 'nope'")

        cfg, store = indexed
        agent = Agent(
            cfg,
            store,
            ToolRunner(cfg, store),
            client=SimpleNamespace(messages=BrokenMessages()),
            use_fallbacks=False,
        )
        with pytest.raises(TypeError):
            list(agent.stream_turn("hello"))

    def test_request_carries_system_tools_and_effort(self, indexed):
        agent = self._agent(indexed, [(message([{"type": "text", "text": "ok"}]), [])])
        list(agent.stream_turn("hi"))
        kwargs = agent.client.messages.calls[0]
        assert kwargs["model"] == "claude-opus-5"
        assert kwargs["output_config"] == {"effort": "high"}
        assert isinstance(kwargs["system"], list)
        assert any(t["name"] == "search_datasheets" for t in kwargs["tools"])


class TestSession:
    def test_round_trips_through_disk(self, workspace):
        session = Session(workspace)
        session.messages = [{"role": "user", "content": "hello"}]
        session.save()
        assert Session.load(workspace, session.id).messages == session.messages
        assert Session.latest(workspace).id == session.id

    def test_trim_cuts_only_at_a_real_user_turn(self, workspace):
        session = Session(workspace)
        # Interleave tool-result user messages, which must never start the history.
        for i in range(40):
            session.messages.append({"role": "user", "content": f"question {i}"})
            session.messages.append(
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": f"t{i}", "name": "search_code", "input": {}}
                    ],
                }
            )
            session.messages.append(
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "…"}],
                }
            )
        session.trim(limit=10)
        assert len(session.messages) <= 12
        first = session.messages[0]
        assert first["role"] == "user" and isinstance(first["content"], str)

    def test_blocks_are_serialized_for_replay(self):
        class Block:
            def model_dump(self, **kw):
                return {"type": "thinking", "thinking": "", "signature": "sig-abc"}

        blocks = _blocks_to_dicts([Block(), {"type": "text", "text": "hi"}])
        assert blocks[0]["signature"] == "sig-abc"  # signatures must survive verbatim
        assert blocks[1]["text"] == "hi"
