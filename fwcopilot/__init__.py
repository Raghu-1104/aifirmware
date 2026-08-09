"""fwcopilot — a firmware engineering assistant with persistent project, board and datasheet context."""

#: Single source of truth for the version — `pyproject.toml` reads it from here
#: via setuptools' dynamic metadata, so the two can never drift apart.
__version__ = "0.2.0"

DEFAULT_MODEL = "claude-opus-5"
