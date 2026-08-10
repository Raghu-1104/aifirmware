"""Firmware-specific static checks.

General C linters do not know that `HAL_Delay()` inside an interrupt handler is a
hang, that a global shared with an ISR must be `volatile`, or that
`xQueueSend()` has a `FromISR` variant that must be used from interrupt context.
These are the bugs that cost days on a bench with a logic analyser, and they are
all detectable from source without running anything.

Every rule is deterministic and cites a file:line, so this runs in CI as well as
inside a chat session.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

SEVERITIES = ("error", "warning", "info")
C_SUFFIXES = {".c", ".cpp", ".cc", ".cxx", ".ino"}


@dataclass
class Finding:
    rule: str
    severity: str
    file: str
    line: int
    message: str
    snippet: str = ""
    suggestion: str = ""

    def format(self, color: bool = False) -> str:
        head = f"{self.file}:{self.line}: {self.severity}: [{self.rule}] {self.message}"
        if color:
            code = {"error": "31", "warning": "33", "info": "36"}.get(self.severity, "0")
            head = f"{self.file}:{self.line}: \033[{code}m{self.severity}\033[0m: [{self.rule}] {self.message}"
        out = [head]
        if self.snippet:
            out.append(f"    | {self.snippet.strip()}")
        if self.suggestion:
            out.append(f"    = {self.suggestion}")
        return "\n".join(out)

    def to_dict(self) -> Dict[str, object]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "file": self.file,
            "line": self.line,
            "message": self.message,
            "snippet": self.snippet.strip(),
            "suggestion": self.suggestion,
        }


@dataclass
class Function:
    name: str
    start: int  # 1-based
    end: int
    is_isr: bool


# --- source preprocessing ---------------------------------------------------


def strip_noncode(text: str) -> str:
    """Blank out comments and string/char literals, preserving line structure.

    Every rule below matches on identifiers, so a `// TODO: remove HAL_Delay`
    comment or a `"malloc"` string must not produce a finding.
    """
    out: List[str] = []
    i, n = 0, len(text)
    state = "code"  # code | line_comment | block_comment | string | char
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if state == "code":
            if ch == "/" and nxt == "/":
                state, i = "line_comment", i + 2
                out.append("  ")
                continue
            if ch == "/" and nxt == "*":
                state, i = "block_comment", i + 2
                out.append("  ")
                continue
            if ch == '"':
                state = "string"
                out.append(" ")
                i += 1
                continue
            if ch == "'":
                state = "char"
                out.append(" ")
                i += 1
                continue
            out.append(ch)
            i += 1
        elif state == "line_comment":
            if ch == "\n":
                state = "code"
                out.append("\n")
            else:
                out.append(" ")
            i += 1
        elif state == "block_comment":
            if ch == "*" and nxt == "/":
                state, i = "code", i + 2
                out.append("  ")
                continue
            out.append("\n" if ch == "\n" else " ")
            i += 1
        else:  # string / char
            if ch == "\\":
                out.append("  ")
                i += 2
                continue
            if (state == "string" and ch == '"') or (state == "char" and ch == "'"):
                state = "code"
            out.append("\n" if ch == "\n" else " ")
            i += 1
    return "".join(out)


_ISR_NAME_RE = re.compile(r"(_IRQHandler|_Handler|_isr|_ISR|_irq_handler)$")
_FUNC_DEF_RE = re.compile(r"^[A-Za-z_][\w\s\*\(\),]*?\b(?P<name>[A-Za-z_]\w*)\s*\([^;]*\)\s*$")
_AVR_ISR_RE = re.compile(r"^\s*ISR\s*\(\s*(?P<name>\w+)\s*\)")
_ATTR_ISR_RE = re.compile(r"__attribute__\s*\(\s*\(\s*(interrupt|signal)")


def find_functions(code: str) -> List[Function]:
    """Locate top-level function bodies by brace matching, flagging ISRs."""
    lines = code.splitlines()
    functions: List[Function] = []
    depth = 0
    pending: Optional[Tuple[str, int, bool]] = None
    current: Optional[Tuple[str, int, bool]] = None

    for idx, raw in enumerate(lines):
        line = raw.rstrip()
        stripped = line.strip()

        if depth == 0 and current is None:
            avr = _AVR_ISR_RE.match(line)
            if avr:
                pending = (f"ISR({avr.group('name')})", idx + 1, True)
            else:
                candidate = stripped[:-1].strip() if stripped.endswith("{") else stripped
                if candidate and not candidate.endswith(";"):
                    m = _FUNC_DEF_RE.match(candidate)
                    if m and m.group("name") not in ("if", "for", "while", "switch", "return"):
                        name = m.group("name")
                        is_isr = bool(_ISR_NAME_RE.search(name)) or bool(_ATTR_ISR_RE.search(line))
                        # An attribute on the preceding line also marks an ISR.
                        if idx > 0 and _ATTR_ISR_RE.search(lines[idx - 1]):
                            is_isr = True
                        pending = (name, idx + 1, is_isr)

        opens = line.count("{")
        closes = line.count("}")
        if opens and pending and current is None:
            current = pending
            pending = None
        depth += opens - closes
        if current and depth <= 0:
            functions.append(Function(current[0], current[1], idx + 1, current[2]))
            current = None
            depth = 0
        if not opens and stripped.endswith(";"):
            pending = None
    return functions


_GLOBAL_VAR_RE = re.compile(
    r"^(?P<qual>(?:static\s+|extern\s+|const\s+|volatile\s+|unsigned\s+|signed\s+)*)"
    r"(?P<type>[A-Za-z_]\w*(?:\s*\*+)?)\s+(?P<name>[A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*(?:=[^;]*)?;"
)


def find_globals(code: str, functions: Sequence[Function]) -> Dict[str, bool]:
    """Map global variable name -> declared volatile."""
    inside: Set[int] = set()
    for fn in functions:
        inside.update(range(fn.start, fn.end + 1))
    result: Dict[str, bool] = {}
    for idx, line in enumerate(code.splitlines(), start=1):
        if idx in inside:
            continue
        m = _GLOBAL_VAR_RE.match(line.strip())
        if not m:
            continue
        name = m.group("name")
        if name in ("return", "typedef", "struct", "union", "enum"):
            continue
        qual = m.group("qual") or ""
        result[name] = "volatile" in qual or "volatile" in (m.group("type") or "")
    return result


# --- rules ------------------------------------------------------------------

BLOCKING_IN_ISR = {
    "HAL_Delay": "HAL_Delay() spins on a tick that usually cannot advance inside an ISR",
    "delay": "delay() blocks the interrupt for its whole duration",
    "delay_ms": "delay_ms() blocks the interrupt for its whole duration",
    "k_sleep": "k_sleep() cannot be called from interrupt context",
    "k_msleep": "k_msleep() cannot be called from interrupt context",
    "vTaskDelay": "vTaskDelay() must never be called from an ISR",
    "sleep": "sleep() blocks the interrupt",
    "usleep": "usleep() blocks the interrupt",
    "printf": "printf() is slow, non-reentrant and may block on the UART",
    "puts": "puts() may block on the output device",
    "fprintf": "fprintf() is slow, non-reentrant and may block",
    "sprintf": "sprintf() is heavy and non-reentrant",
    "malloc": "malloc() is not reentrant and may block on a heap lock",
    "calloc": "calloc() is not reentrant and may block on a heap lock",
    "free": "free() is not reentrant and may block on a heap lock",
    "scanf": "scanf() blocks",
    "strtok": "strtok() keeps hidden static state and is not reentrant",
    "rand": "rand() is not reentrant",
}

FREERTOS_NEEDS_FROMISR = [
    "xQueueSend",
    "xQueueSendToBack",
    "xQueueSendToFront",
    "xQueueReceive",
    "xSemaphoreGive",
    "xSemaphoreTake",
    "xTaskNotify",
    "xTaskNotifyGive",
    "xEventGroupSetBits",
    "xTimerStart",
    "xTimerStop",
    "xStreamBufferSend",
]

UNSAFE_STRING_FUNCS = {
    "strcpy": "strncpy/strlcpy with an explicit bound",
    "strcat": "strncat/strlcat with an explicit bound",
    "sprintf": "snprintf with sizeof(buffer)",
    "gets": "fgets with an explicit bound",
    "vsprintf": "vsnprintf with an explicit bound",
}

_CALL_RE = r"\b{}\s*\("
_HAL_CALL_RE = re.compile(r"^\s*(?P<call>HAL_\w+)\s*\(")
_CHECKED_HAL_PREFIXES = (
    "HAL_GPIO_",
    "HAL_Delay",
    "HAL_NVIC_",
    "HAL_IncTick",
    "HAL_GetTick",
    "HAL_SYSTICK_",
    "HAL_MspInit",
    "HAL_RCC_",
)
_FLOAT_DECL_RE = re.compile(r"\b(float|double)\b")
_ASSIGN_RE = r"\b{}\s*(?:\[[^\]]*\])?\s*(?:=[^=]|\+\+|--|\+=|-=|\|=|&=|\^=)"
_EMPTY_WHILE_RE = re.compile(r"^\s*while\s*\(.+\)\s*;\s*$")
_EMPTY_WHILE_BRACE_RE = re.compile(r"^\s*while\s*\(.+\)\s*\{\s*\}\s*$")
_TIMEOUT_HINT_RE = re.compile(
    r"timeout|deadline|tick|retry|attempt|elapsed|HAL_GetTick|k_uptime", re.I
)
_HW_CAST_RE = re.compile(
    r"\(\s*(?:u?int(?:8|16|32|64)_t|unsigned\s+\w+)\s*\*\s*\)\s*(0x[0-9A-Fa-f]{6,})"
)
_DISABLE_IRQ_RE = re.compile(
    r"__disable_irq\s*\(|taskENTER_CRITICAL|portDISABLE_INTERRUPTS|__set_PRIMASK\s*\(\s*1"
)
_ENABLE_IRQ_RE = re.compile(
    r"__enable_irq\s*\(|taskEXIT_CRITICAL|portENABLE_INTERRUPTS|__set_PRIMASK\s*\(\s*0"
)


def lint_source(path: str, text: str) -> List[Finding]:
    """Run every rule over one translation unit."""
    findings: List[Finding] = []
    code = strip_noncode(text)
    raw_lines = text.splitlines()
    code_lines = code.splitlines()
    functions = find_functions(code)
    globals_map = find_globals(code, functions)

    def snippet(line_no: int) -> str:
        return raw_lines[line_no - 1] if 0 < line_no <= len(raw_lines) else ""

    def add(rule: str, severity: str, line_no: int, message: str, suggestion: str = "") -> None:
        findings.append(
            Finding(rule, severity, path, line_no, message, snippet(line_no), suggestion)
        )

    isr_ranges = [(f.start, f.end, f.name) for f in functions if f.is_isr]

    def enclosing_isr(line_no: int) -> Optional[str]:
        for start, end, name in isr_ranges:
            if start <= line_no <= end:
                return name
        return None

    for idx, line in enumerate(code_lines, start=1):
        isr = enclosing_isr(idx)

        # FW001 — blocking / non-reentrant calls inside an ISR
        if isr:
            for func, why in BLOCKING_IN_ISR.items():
                if re.search(_CALL_RE.format(re.escape(func)), line):
                    add(
                        "FW001",
                        "error",
                        idx,
                        f"{func}() called inside interrupt handler {isr}: {why}",
                        "Set a flag or post to a queue and do the work in the main loop or a task.",
                    )
                    break

            # FW002 — floating point in an ISR
            if _FLOAT_DECL_RE.search(line):
                add(
                    "FW002",
                    "warning",
                    idx,
                    f"floating-point used inside interrupt handler {isr}",
                    "On parts without lazy FPU stacking this corrupts FPU state or costs "
                    "significant latency; use integer or fixed-point math in ISRs.",
                )

            # FW009 — FreeRTOS API that has a FromISR variant
            for api in FREERTOS_NEEDS_FROMISR:
                if re.search(_CALL_RE.format(re.escape(api)), line) and "FromISR" not in line:
                    add(
                        "FW009",
                        "error",
                        idx,
                        f"{api}() called from interrupt handler {isr} instead of {api}FromISR()",
                        f"Use {api}FromISR() and honour the resulting "
                        "pxHigherPriorityTaskWoken with portYIELD_FROM_ISR().",
                    )
                    break

            # FW003 — writing a non-volatile global from an ISR
            for name, is_volatile in globals_map.items():
                if is_volatile:
                    continue
                if re.search(_ASSIGN_RE.format(re.escape(name)), line):
                    add(
                        "FW003",
                        "error",
                        idx,
                        f"interrupt handler {isr} writes global '{name}', which is not volatile",
                        "Declare it `volatile` so the compiler reloads it in the main context "
                        "(and use an atomic access or a critical section if it is wider than a word).",
                    )
                    break

        # FW004 — busy-wait with no timeout
        is_empty_wait = _EMPTY_WHILE_RE.match(line) or _EMPTY_WHILE_BRACE_RE.match(line)
        if is_empty_wait and not _TIMEOUT_HINT_RE.search(line):
            add(
                "FW004",
                "warning",
                idx,
                "busy-wait loop with no timeout — a stuck peripheral hangs the firmware here",
                "Bound the wait with a tick deadline and return an error on expiry.",
            )

        # FW005 — discarded HAL status
        m = _HAL_CALL_RE.match(line)
        if m and not line.strip().startswith(("return", "if", "while", "}")):
            call = m.group("call")
            if not call.startswith(_CHECKED_HAL_PREFIXES) and "=" not in line.split("(")[0]:
                add(
                    "FW005",
                    "warning",
                    idx,
                    f"return value of {call}() is discarded",
                    "HAL calls return HAL_StatusTypeDef; check it or explicitly cast to (void) "
                    "to show the omission is deliberate.",
                )

        # FW006 — unbounded string functions
        for func, replacement in UNSAFE_STRING_FUNCS.items():
            if re.search(_CALL_RE.format(re.escape(func)), line):
                if func == "sprintf" and isr:
                    break  # already reported as FW001
                add(
                    "FW006",
                    "warning",
                    idx,
                    f"{func}() has no bound and can overflow the destination buffer",
                    f"Use {replacement}.",
                )
                break

        # FW007 — dynamic allocation
        if re.search(r"\b(malloc|calloc|realloc)\s*\(", line) and not isr:
            add(
                "FW007",
                "info",
                idx,
                "dynamic allocation in firmware",
                "Heap fragmentation and allocation failure are hard to handle on a "
                "long-running device; prefer static or pool allocation.",
            )

        # FW011 — hardware register access without volatile
        hw = _HW_CAST_RE.search(line)
        if hw and "volatile" not in line:
            add(
                "FW011",
                "error",
                idx,
                f"hardware address {hw.group(1)} is cast to a non-volatile pointer",
                "The compiler may cache or reorder the access; cast to "
                "`volatile uint32_t *` instead.",
            )

    findings.extend(_check_critical_sections(path, code_lines, raw_lines))
    findings.sort(key=lambda f: (f.line, f.rule))
    return findings


def _check_critical_sections(
    path: str, code_lines: List[str], raw_lines: List[str]
) -> List[Finding]:
    """FW008 — long or blocking work between disable/enable interrupt calls."""
    findings: List[Finding] = []
    open_line: Optional[int] = None
    for idx, line in enumerate(code_lines, start=1):
        if _DISABLE_IRQ_RE.search(line):
            open_line = idx
            continue
        if open_line is None:
            continue
        if _ENABLE_IRQ_RE.search(line):
            if idx - open_line > 40:
                findings.append(
                    Finding(
                        "FW008",
                        "warning",
                        path,
                        open_line,
                        f"critical section spans {idx - open_line} lines — interrupts are "
                        "disabled for all of it",
                        raw_lines[open_line - 1] if open_line <= len(raw_lines) else "",
                        "Keep critical sections to the few instructions that truly need atomicity.",
                    )
                )
            open_line = None
            continue
        for func in ("HAL_Delay", "delay", "printf", "vTaskDelay", "k_sleep", "malloc"):
            if re.search(_CALL_RE.format(re.escape(func)), line):
                findings.append(
                    Finding(
                        "FW008",
                        "error",
                        path,
                        idx,
                        f"{func}() called with interrupts disabled",
                        raw_lines[idx - 1] if idx <= len(raw_lines) else "",
                        "Move it outside the critical section; blocking with interrupts off "
                        "stalls every other interrupt on the device.",
                    )
                )
                break
    return findings


def lint_paths(paths: Iterable[Path], root: Optional[Path] = None) -> List[Finding]:
    findings: List[Finding] = []
    for path in paths:
        if path.suffix.lower() not in C_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(path.relative_to(root)) if root else str(path)
        findings.extend(lint_source(rel, text))
    return findings


def summarize(findings: Sequence[Finding]) -> Dict[str, int]:
    counts = dict.fromkeys(SEVERITIES, 0)
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    return counts


RULE_DOCS = {
    "FW001": "Blocking or non-reentrant call inside an interrupt handler",
    "FW002": "Floating-point arithmetic inside an interrupt handler",
    "FW003": "Interrupt handler writes a global that is not declared volatile",
    "FW004": "Busy-wait loop with no timeout",
    "FW005": "HAL return status discarded",
    "FW006": "Unbounded string function that can overflow its destination",
    "FW007": "Dynamic memory allocation in firmware",
    "FW008": "Blocking work or a long span with interrupts disabled",
    "FW009": "FreeRTOS API used from an ISR without its FromISR variant",
    "FW011": "Hardware register accessed through a non-volatile pointer",
}
