"""Build output parsing.

Turns a wall of compiler and linker output into structured diagnostics, so the
first real error is surfaced instead of the last line of the log, and so the
assistant receives `file:line` facts rather than raw text to re-read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# gcc/clang:  src/main.c:42:9: error: 'x' undeclared
_GCC_RE = re.compile(
    r"^(?P<file>[^\s:][^:]*):(?P<line>\d+):(?:(?P<col>\d+):)?\s*"
    r"(?P<severity>error|warning|note|fatal error):\s*(?P<message>.*)$"
)
# Arm Compiler / IAR:  "src/main.c", line 42: Error[Pe020]: identifier is undefined
_ARMCC_RE = re.compile(
    r'^"(?P<file>[^"]+)",\s*line\s*(?P<line>\d+):\s*(?P<severity>Error|Warning|Remark)'
    r"(?:\[[^\]]*\])?:\s*(?P<message>.*)$"
)
_UNDEFINED_REF_RE = re.compile(r"undefined reference to [`'\"](?P<symbol>[^`'\"]+)")
_MULTIPLE_DEF_RE = re.compile(r"multiple definition of [`'\"](?P<symbol>[^`'\"]+)")
_REGION_OVERFLOW_RE = re.compile(
    r"region [`'\"](?P<region>\w+)['\"`] overflowed by (?P<bytes>\d+) bytes"
)
_NO_SPACE_RE = re.compile(r"will not fit in region [`'\"](?P<region>\w+)")
_MISSING_TOOL_RE = re.compile(
    r"(?:^|\s)(?P<tool>[\w\-]*(?:gcc|g\+\+|make|cmake|ninja|objcopy|size|ld))"
    r"[:\s].*(?:command not found|No such file or directory)"
)


@dataclass
class Diagnostic:
    severity: str  # error | warning | note
    message: str
    file: Optional[str] = None
    line: Optional[int] = None
    column: Optional[int] = None
    kind: str = "compile"  # compile | link | memory | toolchain
    symbol: Optional[str] = None
    hint: str = ""

    def location(self) -> str:
        if self.file and self.line:
            return f"{self.file}:{self.line}" + (f":{self.column}" if self.column else "")
        return self.file or "(build)"

    def format(self) -> str:
        out = f"{self.location()}: {self.severity}: {self.message}"
        if self.hint:
            out += f"\n    = {self.hint}"
        return out

    def to_dict(self) -> Dict[str, object]:
        return {
            "severity": self.severity,
            "message": self.message,
            "file": self.file,
            "line": self.line,
            "column": self.column,
            "kind": self.kind,
            "symbol": self.symbol,
            "hint": self.hint,
        }


@dataclass
class BuildResult:
    command: str
    returncode: int
    output: str
    diagnostics: List[Diagnostic] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def errors(self) -> List[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == "error"]

    @property
    def warnings(self) -> List[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == "warning"]

    def summary(self) -> str:
        status = "succeeded" if self.ok else f"failed (exit {self.returncode})"
        return (
            f"`{self.command}` {status} — "
            f"{len(self.errors)} error(s), {len(self.warnings)} warning(s)"
        )

    def format(self, max_items: int = 20) -> str:
        lines = [self.summary()]
        shown = self.errors or self.diagnostics
        for diag in shown[:max_items]:
            lines.append("  " + diag.format().replace("\n", "\n  "))
        if len(shown) > max_items:
            lines.append(f"  … and {len(shown) - max_items} more")
        if not self.diagnostics and not self.ok:
            tail = "\n".join(self.output.strip().splitlines()[-15:])
            lines.append("  (no diagnostics parsed; last lines of output)")
            lines.append(tail)
        return "\n".join(lines)


def parse_build_output(text: str) -> List[Diagnostic]:
    """Extract diagnostics from compiler/linker output, deduplicated in order."""
    diagnostics: List[Diagnostic] = []
    seen = set()

    def add(diag: Diagnostic) -> None:
        key = (diag.file, diag.line, diag.message, diag.kind)
        if key in seen:
            return
        seen.add(key)
        diagnostics.append(diag)

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line:
            continue

        overflow = _REGION_OVERFLOW_RE.search(line)
        if overflow:
            n = int(overflow.group("bytes"))
            add(
                Diagnostic(
                    severity="error",
                    kind="memory",
                    message=f"region {overflow.group('region')} overflowed by {n} bytes",
                    hint="The image no longer fits. Run `fwcopilot size` to see the largest "
                    "contributors, then drop features, enable -Os/-flto, or check that "
                    "the linker script matches the part in board.yaml.",
                )
            )
            continue

        nospace = _NO_SPACE_RE.search(line)
        if nospace:
            add(
                Diagnostic(
                    severity="error",
                    kind="memory",
                    message=f"section will not fit in region {nospace.group('region')}",
                    hint="Run `fwcopilot size` for the budget breakdown.",
                )
            )
            continue

        undef = _UNDEFINED_REF_RE.search(line)
        if undef:
            symbol = undef.group("symbol")
            add(
                Diagnostic(
                    severity="error",
                    kind="link",
                    symbol=symbol,
                    message=f"undefined reference to {symbol}",
                    hint="The symbol is declared but never defined or linked — check that its "
                    ".c file is in the build and that the name is not C++-mangled "
                    '(wrap C headers in extern "C").',
                )
            )
            continue

        multi = _MULTIPLE_DEF_RE.search(line)
        if multi:
            add(
                Diagnostic(
                    severity="error",
                    kind="link",
                    symbol=multi.group("symbol"),
                    message=f"multiple definition of {multi.group('symbol')}",
                    hint="A definition sits in a header included from several translation "
                    "units — mark it `static`, or declare it `extern` and define it once.",
                )
            )
            continue

        tool = _MISSING_TOOL_RE.search(line)
        if tool:
            add(
                Diagnostic(
                    severity="error",
                    kind="toolchain",
                    message=f"{tool.group('tool')} not found on PATH",
                    hint="Install the cross toolchain or add it to PATH; `fwcopilot doctor` "
                    "shows what is missing.",
                )
            )
            continue

        m = _GCC_RE.match(line) or _ARMCC_RE.match(line)
        if m:
            severity = m.group("severity").lower().replace("fatal error", "error")
            severity = {"remark": "note"}.get(severity, severity)
            add(
                Diagnostic(
                    severity=severity,
                    message=m.group("message").strip(),
                    file=m.group("file"),
                    line=int(m.group("line")),
                    column=int(m.group("col")) if m.groupdict().get("col") else None,
                    kind="compile",
                )
            )
    return diagnostics
