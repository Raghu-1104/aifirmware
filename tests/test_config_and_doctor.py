"""Configuration validation, environment overrides, and workspace health checks."""

import pytest

from fwcopilot.config import load_config_from_root
from fwcopilot.doctor import FAIL, OK, WARN, run_checks, worst_status
from fwcopilot.errors import ConfigError, ExitCode, FwcopilotError, WorkspaceNotFoundError


def write_config(cfg, text):
    cfg.config_path.write_text(text, encoding="utf-8")


class TestConfigValidation:
    def test_defaults_are_valid(self, workspace):
        load_config_from_root(workspace.root).validate()

    def test_rejects_unknown_effort(self, workspace):
        write_config(workspace, "model:\n  effort: turbo\n")
        with pytest.raises(ConfigError) as exc:
            load_config_from_root(workspace.root)
        assert "turbo" in exc.value.message
        assert "low" in exc.value.hint  # lists the valid values

    def test_rejects_out_of_range_max_tokens(self, workspace):
        write_config(workspace, "model:\n  max_tokens: 10\n")
        with pytest.raises(ConfigError, match="max_tokens"):
            load_config_from_root(workspace.root)

    def test_rejects_empty_model_id(self, workspace):
        write_config(workspace, 'model:\n  id: ""\n')
        with pytest.raises(ConfigError, match=r"model\.id"):
            load_config_from_root(workspace.root)

    def test_rejects_unknown_permission_value(self, workspace):
        write_config(workspace, "permissions:\n  write: maybe\n")
        with pytest.raises(ConfigError, match=r"permissions\.write"):
            load_config_from_root(workspace.root)

    def test_rejects_malformed_yaml(self, workspace):
        write_config(workspace, "model: [unclosed\n")
        with pytest.raises(ConfigError, match="valid YAML"):
            load_config_from_root(workspace.root)

    def test_rejects_non_mapping_config(self, workspace):
        write_config(workspace, "- just\n- a\n- list\n")
        with pytest.raises(ConfigError, match="mapping"):
            load_config_from_root(workspace.root)

    def test_config_errors_carry_the_config_exit_code(self, workspace):
        write_config(workspace, "model:\n  effort: nope\n")
        with pytest.raises(ConfigError) as exc:
            load_config_from_root(workspace.root)
        assert exc.value.exit_code == ExitCode.CONFIG


class TestEnvOverrides:
    def test_model_and_effort_from_environment(self, workspace, monkeypatch):
        monkeypatch.setenv("FWCOPILOT_MODEL", "claude-sonnet-5")
        monkeypatch.setenv("FWCOPILOT_EFFORT", "low")
        cfg = load_config_from_root(workspace.root)
        assert cfg.model == "claude-sonnet-5"
        assert cfg.effort == "low"

    def test_build_command_from_environment(self, workspace, monkeypatch):
        monkeypatch.setenv("FWCOPILOT_BUILD_COMMAND", "ninja -C build")
        assert load_config_from_root(workspace.root).build_command == "ninja -C build"

    def test_max_tokens_must_be_an_integer(self, workspace, monkeypatch):
        monkeypatch.setenv("FWCOPILOT_MAX_TOKENS", "lots")
        with pytest.raises(ConfigError, match="integer"):
            load_config_from_root(workspace.root)

    def test_empty_env_var_does_not_override(self, workspace, monkeypatch):
        monkeypatch.setenv("FWCOPILOT_MODEL", "")
        assert load_config_from_root(workspace.root).model == "claude-opus-5"

    def test_env_override_is_still_validated(self, workspace, monkeypatch):
        monkeypatch.setenv("FWCOPILOT_EFFORT", "ludicrous")
        with pytest.raises(ConfigError):
            load_config_from_root(workspace.root)


class TestPathSandbox:
    def test_resolves_inside_the_root(self, workspace):
        assert workspace.resolve_in_root("src/main.c").is_relative_to(workspace.root)

    @pytest.mark.parametrize("path", ["../evil", "/etc/passwd", "src/../../evil"])
    def test_rejects_escapes(self, workspace, path):
        with pytest.raises(ValueError, match="escapes the project root"):
            workspace.resolve_in_root(path)

    def test_root_itself_is_allowed(self, workspace):
        assert workspace.resolve_in_root(".") == workspace.root.resolve()


class TestErrors:
    def test_workspace_error_has_actionable_hint(self):
        exc = WorkspaceNotFoundError("nothing here")
        assert exc.exit_code == ExitCode.NO_WORKSPACE
        assert "fwcopilot init" in exc.hint

    def test_hint_can_be_overridden(self):
        exc = FwcopilotError("boom", "try this instead")
        assert exc.hint == "try this instead"

    def test_exit_codes_are_distinct(self):
        codes = [
            ExitCode.OK,
            ExitCode.ERROR,
            ExitCode.USAGE,
            ExitCode.NO_WORKSPACE,
            ExitCode.CONFIG,
            ExitCode.NOT_FOUND,
            ExitCode.CREDENTIALS,
            ExitCode.FINDINGS,
        ]
        assert len(set(codes)) == len(codes)


class TestDoctor:
    def test_reports_a_healthy_indexed_workspace(self, indexed):
        cfg, store = indexed
        store.close()
        checks = {c.name: c for c in run_checks(cfg)}
        assert checks["workspace"].status == OK
        assert checks["index"].status == OK
        assert checks["board profile"].status == OK

    def test_flags_a_missing_index(self, workspace):
        checks = {c.name: c for c in run_checks(workspace)}
        assert checks["index"].status == FAIL
        assert "fwcopilot index" in checks["index"].hint
        assert worst_status(list(checks.values())) == FAIL

    def test_flags_an_invalid_board_profile(self, workspace):
        workspace.board_path.write_text(
            "board: {name: b}\n"
            "components:\n"
            "  - {ref: U1, bus: {name: I2C1, address: 0x40}}\n"
            "  - {ref: U2, bus: {name: I2C1, address: 0x40}}\n",
            encoding="utf-8",
        )
        checks = {c.name: c for c in run_checks(workspace)}
        assert checks["board profile"].status == WARN
        assert "board check" in checks["board profile"].hint

    def test_detects_credentials_from_the_environment(self, indexed, monkeypatch):
        cfg, store = indexed
        store.close()
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        checks = {c.name: c for c in run_checks(cfg)}
        assert checks["credentials"].status == OK

    def test_missing_credentials_are_a_warning_not_a_failure(self, indexed, monkeypatch, tmp_path):
        cfg, store = indexed
        store.close()
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        # Point HOME at an empty dir so no `ant auth login` profile is found.
        monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "empty-home"))
        checks = {c.name: c for c in run_checks(cfg)}
        assert checks["credentials"].status == WARN
        assert "ANTHROPIC_API_KEY" in checks["credentials"].hint

    def test_stale_index_is_detected(self, indexed):
        import time

        cfg, store = indexed
        store.close()
        time.sleep(0.01)
        (cfg.root / "src" / "main.c").write_text("// touched\n", encoding="utf-8")
        import os

        future = time.time() + 60
        os.utime(cfg.root / "src" / "main.c", (future, future))
        checks = {c.name: c for c in run_checks(cfg)}
        assert checks["index freshness"].status == WARN

    def test_check_formatting_includes_hints_only_when_relevant(self, workspace):
        checks = run_checks(workspace)
        for check in checks:
            text = check.format()
            assert check.name in text
            if check.status == OK:
                assert "→" not in text
