"""Datasheet ingestion: PDF/text -> page chunks + register table extraction.

Datasheets are the reason this tool exists: once a PDF is ingested, every answer
about that IC can cite a page number instead of guessing from model memory.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .store import Store, sha256_file

CHUNK_CHARS = 1400
CHUNK_OVERLAP = 200

# "7.4.2 Control register 1" / "8 ELECTRICAL CHARACTERISTICS" / "Table 22. CTRL_REG1"
_HEADING_RE = re.compile(
    r"^\s*(?:(?:\d+(?:\.\d+)*)\s+[A-Z][^\n]{2,70}|(?:Table|Figure)\s+\d+[.:][^\n]{2,70})\s*$"
)

# Register table rows, e.g.:
#   "CTRL_REG1  0x20  R/W  Control register 1"
#   "0x20  CTRL_REG1  Control register 1"
#   "Address: 0x20 (CTRL_REG1)"
_REG_NAME = r"[A-Z][A-Z0-9]{1,}(?:_[A-Z0-9]+){0,4}"
_REG_PATTERNS = [
    re.compile(rf"^\s*(?P<name>{_REG_NAME})\s+(?P<addr>0x[0-9A-Fa-f]{{1,4}})\b(?P<desc>.{{0,80}})"),
    re.compile(rf"^\s*(?P<addr>0x[0-9A-Fa-f]{{1,4}})\s+(?P<name>{_REG_NAME})\b(?P<desc>.{{0,80}})"),
    re.compile(rf"(?P<name>{_REG_NAME})\s*\(\s*(?P<addr>0x[0-9A-Fa-f]{{1,4}})\s*\)(?P<desc>.{{0,60}})"),
]

# Words that look like register names but never are.
_REG_STOPWORDS = {
    "AND", "THE", "FOR", "NOT", "ALL", "MAX", "MIN", "TYP", "NOTE", "TABLE",
    "FIGURE", "PAGE", "DOC", "ID", "REV", "VDD", "VSS", "GND", "NC", "TBD",
    "I2C", "SPI", "UART", "USB", "PDF", "LSB", "MSB", "MHZ", "KHZ",
}

SUPPORTED_SUFFIXES = {".pdf", ".txt", ".md"}


@dataclass
class IngestResult:
    path: str
    part: Optional[str]
    pages: int
    chunks: int
    registers: int
    skipped: bool = False
    reason: str = ""


def _clean_page_text(text: str) -> str:
    text = text.replace("\x00", " ")
    # Collapse the ragged intra-word spacing PDF extraction often produces on
    # headings ("C O N T R O L") while leaving normal prose alone.
    lines = []
    for line in text.splitlines():
        line = line.rstrip()
        if line and len(line) > 6:
            letters = [c for c in line if not c.isspace()]
            if letters and (len(line) - len(letters)) / len(line) > 0.45:
                line = re.sub(r"(?<=\S) (?=\S)", "", line)
        lines.append(line)
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{3,}", "  ", text)
    return text.strip()


def extract_pages(path: Path) -> List[str]:
    """Return per-page text. Non-PDF inputs are treated as a single page."""
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md"}:
        return [_clean_page_text(path.read_text(encoding="utf-8", errors="replace"))]
    if suffix != ".pdf":
        raise ValueError(f"unsupported datasheet format: {suffix} (use PDF, TXT or MD)")

    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError("pypdf is required to ingest PDFs: pip install pypdf") from exc

    reader = PdfReader(str(path))
    pages: List[str] = []
    for page in reader.pages:
        try:
            pages.append(_clean_page_text(page.extract_text() or ""))
        except Exception:
            pages.append("")
    return pages


def find_heading(page_text: str) -> Optional[str]:
    for line in page_text.splitlines():
        if _HEADING_RE.match(line):
            return line.strip()[:120]
    return None


def chunk_page(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> List[str]:
    """Split a page into overlapping chunks, preferring paragraph boundaries."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    chunks: List[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            window = text[start:end]
            for sep in ("\n\n", "\n", ". "):
                cut = window.rfind(sep)
                if cut > size * 0.5:
                    end = start + cut + len(sep)
                    break
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def extract_registers(pages: Iterable[str]) -> List[Dict[str, Any]]:
    """Heuristically pull `NAME <-> 0xADDR` pairs out of register-map tables."""
    found: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for page_no, text in enumerate(pages, start=1):
        for line in text.splitlines():
            line = line.strip()
            if not line or len(line) > 200:
                continue
            for pattern in _REG_PATTERNS:
                m = pattern.search(line)
                if not m:
                    continue
                name = m.group("name").strip()
                if name in _REG_STOPWORDS or len(name) < 3 or name.isdigit():
                    continue
                addr = m.group("addr").strip().lower()
                desc = (m.groupdict().get("desc") or "").strip(" \t|-:")
                key = (name, addr)
                if key not in found:
                    found[key] = {
                        "name": name,
                        "address": addr,
                        "page": page_no,
                        "description": desc[:160],
                    }
                break
    return list(found.values())


def guess_part_number(filename: str, pages: List[str]) -> Optional[str]:
    """Best-effort part number from the file name, falling back to the cover page."""
    stem = Path(filename).stem
    # Only strip boilerplate that stands alone as its own token — otherwise the
    # "ds" in a part number like LSM6DSOX gets eaten.
    cleaned = re.sub(
        r"(?i)(?:^|[-_ ])(?:datasheet|data[-_ ]?sheet|ds|rev[-_ ]?[a-z0-9.]+|v\d+(?:\.\d+)*)(?=$|[-_ .])",
        "",
        stem,
    )
    cleaned = cleaned.strip(" -_.")
    if cleaned and re.search(r"\d", cleaned) and len(cleaned) <= 32:
        return cleaned.upper()

    if pages:
        for line in pages[0].splitlines()[:25]:
            line = line.strip()
            m = re.match(r"^([A-Z]{2,6}[0-9]{2,6}[A-Z0-9\-]{0,8})\b", line)
            if m:
                return m.group(1)
    return cleaned.upper() or None


def ingest_datasheet(
    store: Store,
    path: Path,
    *,
    rel_path: str,
    part: Optional[str] = None,
    component: Optional[str] = None,
    force: bool = False,
) -> IngestResult:
    """Index a single datasheet file into the store."""
    path = Path(path)
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        return IngestResult(rel_path, part, 0, 0, 0, skipped=True,
                            reason=f"unsupported format {path.suffix}")

    sha = sha256_file(path)
    if not force and store.doc_is_current(rel_path, sha):
        row = store.get_doc(rel_path)
        return IngestResult(rel_path, row["part"] if row else part,
                            row["pages"] if row else 0, 0, 0,
                            skipped=True, reason="unchanged")

    pages = extract_pages(path)
    text_pages = [p for p in pages if p.strip()]
    if not text_pages:
        return IngestResult(rel_path, part, len(pages), 0, 0, skipped=True,
                            reason="no extractable text (scanned PDF? run OCR first)")

    part = part or guess_part_number(path.name, pages)
    doc_id = store.upsert_doc(
        kind="datasheet",
        path=rel_path,
        title=path.stem,
        component=component,
        part=part,
        pages=len(pages),
        sha=sha,
        mtime=path.stat().st_mtime,
    )

    chunk_rows: List[Dict[str, Any]] = []
    for page_no, page_text in enumerate(pages, start=1):
        heading = find_heading(page_text)
        for piece in chunk_page(page_text):
            chunk_rows.append({"page": page_no, "heading": heading, "text": piece})
    n_chunks = store.add_chunks(doc_id, chunk_rows)

    regs = extract_registers(pages)
    n_regs = store.add_registers(doc_id, part, regs)

    return IngestResult(rel_path, part, len(pages), n_chunks, n_regs)


def ingest_directory(
    store: Store,
    directory: Path,
    root: Path,
    *,
    force: bool = False,
    component_map: Optional[Dict[str, Dict[str, str]]] = None,
) -> List[IngestResult]:
    """Ingest every supported file under `directory`.

    `component_map` maps a workspace-relative datasheet path to
    `{"component": ref, "part": part}` taken from board.yaml, so hits can be
    attributed to the board component that uses the chip.
    """
    results: List[IngestResult] = []
    if not directory.is_dir():
        return results
    component_map = component_map or {}
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        rel = str(path.resolve().relative_to(root.resolve()))
        meta = component_map.get(rel, {})
        results.append(
            ingest_datasheet(
                store, path, rel_path=rel, part=meta.get("part"),
                component=meta.get("component"), force=force,
            )
        )
    return results
