"""Typed errors with stable process exit codes.

Every failure path the CLI can hit maps to one exception class and one exit
code, so scripts and CI jobs can branch on the result instead of scraping
stderr.
"""

from __future__ import annotations


class ExitCode:
    OK = 0
    ERROR = 1
    USAGE = 2
    NO_WORKSPACE = 3
    CONFIG = 4
    NOT_FOUND = 5
    CREDENTIALS = 6
    FINDINGS = 7  # lint/check found problems; the command itself worked
    INTERRUPTED = 130


class FwcopilotError(Exception):
    """Base class for every expected, user-facing failure."""

    exit_code = ExitCode.ERROR
    hint: str = ""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.message = message
        if hint:
            self.hint = hint


class WorkspaceNotFoundError(FwcopilotError):
    exit_code = ExitCode.NO_WORKSPACE
    hint = "Run `fwcopilot init` in your firmware project to create one."


class ConfigError(FwcopilotError):
    exit_code = ExitCode.CONFIG


class BoardError(FwcopilotError):
    exit_code = ExitCode.CONFIG


class ResourceNotFoundError(FwcopilotError):
    exit_code = ExitCode.NOT_FOUND


class IngestError(FwcopilotError):
    exit_code = ExitCode.ERROR


class CredentialsError(FwcopilotError):
    exit_code = ExitCode.CREDENTIALS
    hint = (
        "Set ANTHROPIC_API_KEY=sk-ant-... (or run `ant auth login`).\n"
        "Index, search, board check, lint, size and scaffold all work without credentials."
    )
