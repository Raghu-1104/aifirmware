"""SQLite/FTS5 backed index over datasheet pages, source files and registers.

Everything retrieval-related lives in one file-backed database so the assistant
can answer from the project and its datasheets with no external services and no
embedding provider. FTS5 with BM25 ranking does the heavy lifting; the tokenizer
is configured to keep `_` inside a token so identifiers like `CTRL_REG1` and
`HAL_I2C_Mem_Read` survive tokenization intact.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,              -- 'datasheet' | 'code' | 'note'
    path        TEXT NOT NULL UNIQUE,       -- workspace-relative
    title       TEXT,
    component   TEXT,                       -- board ref (U2) or part number
    part        TEXT,                       -- e.g. BME280
    pages       INTEGER,
    sha         TEXT,
    mtime       REAL,
    indexed_at  REAL
);

CREATE TABLE IF NOT EXISTS chunks (
    id       INTEGER PRIMARY KEY,
    doc_id   INTEGER NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
    page     INTEGER,                       -- datasheets: 1-based page
    line     INTEGER,                       -- code: 1-based start line
    end_line INTEGER,
    heading  TEXT,
    text     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);

CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
    text,
    heading,
    tokenize = "unicode61 tokenchars '_'"
);

CREATE TABLE IF NOT EXISTS registers (
    id          INTEGER PRIMARY KEY,
    doc_id      INTEGER NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
    part        TEXT,
    name        TEXT NOT NULL,
    address     TEXT,
    page        INTEGER,
    description TEXT
);
CREATE INDEX IF NOT EXISTS idx_reg_name ON registers(name);
CREATE INDEX IF NOT EXISTS idx_reg_part ON registers(part);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# FTS5 operators/punctuation that must never reach the query parser verbatim.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.]*")


@dataclass
class Hit:
    chunk_id: int
    doc_id: int
    kind: str
    path: str
    title: str
    part: Optional[str]
    component: Optional[str]
    page: Optional[int]
    line: Optional[int]
    end_line: Optional[int]
    heading: Optional[str]
    text: str
    score: float

    def locator(self) -> str:
        """Human- and model-readable citation for this hit."""
        if self.kind == "datasheet":
            label = self.part or self.title or self.path
            return f"{label} p.{self.page}" if self.page else str(label)
        if self.line:
            return f"{self.path}:{self.line}"
        return self.path

    def to_dict(self) -> Dict[str, Any]:
        return {
            "locator": self.locator(),
            "kind": self.kind,
            "path": self.path,
            "part": self.part,
            "component": self.component,
            "page": self.page,
            "line": self.line,
            "heading": self.heading,
            "text": self.text,
            "score": round(self.score, 3),
        }


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


def build_match_query(query: str) -> str:
    """Turn free text into a safe FTS5 MATCH expression.

    Quoted phrases are preserved as phrases; everything else becomes a set of
    OR-ed quoted terms. Returning quoted terms means user punctuation can never
    be interpreted as an FTS5 operator.
    """
    phrases = re.findall(r'"([^"]+)"', query)
    remainder = re.sub(r'"[^"]+"', " ", query)
    terms: List[str] = []
    for phrase in phrases:
        tokens = _TOKEN_RE.findall(phrase)
        if tokens:
            terms.append('"' + " ".join(tokens) + '"')
    for token in _TOKEN_RE.findall(remainder):
        if len(token) < 2 and not token.isdigit():
            continue
        terms.append('"' + token + '"')
    # Deduplicate, preserving order.
    seen = set()
    unique = [t for t in terms if not (t in seen or seen.add(t))]
    return " OR ".join(unique)


class Store:
    """Thin wrapper over the workspace index database."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---- documents -----------------------------------------------------
    def get_doc(self, path: str) -> Optional[sqlite3.Row]:
        cur = self.conn.execute("SELECT * FROM docs WHERE path = ?", (path,))
        return cur.fetchone()

    def doc_is_current(self, path: str, sha: str) -> bool:
        row = self.get_doc(path)
        return bool(row and row["sha"] == sha)

    def delete_doc(self, path: str) -> None:
        row = self.get_doc(path)
        if not row:
            return
        chunk_ids = [
            r["id"] for r in self.conn.execute("SELECT id FROM chunks WHERE doc_id = ?", (row["id"],))
        ]
        for cid in chunk_ids:
            self.conn.execute("DELETE FROM chunk_fts WHERE rowid = ?", (cid,))
        self.conn.execute("DELETE FROM docs WHERE id = ?", (row["id"],))
        self.conn.commit()

    def upsert_doc(
        self,
        *,
        kind: str,
        path: str,
        title: str = "",
        component: Optional[str] = None,
        part: Optional[str] = None,
        pages: Optional[int] = None,
        sha: str = "",
        mtime: float = 0.0,
    ) -> int:
        self.delete_doc(path)
        cur = self.conn.execute(
            "INSERT INTO docs (kind, path, title, component, part, pages, sha, mtime, indexed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, path, title, component, part, pages, sha, mtime, time.time()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_docs(self, kind: Optional[str] = None) -> List[sqlite3.Row]:
        if kind:
            cur = self.conn.execute("SELECT * FROM docs WHERE kind = ? ORDER BY path", (kind,))
        else:
            cur = self.conn.execute("SELECT * FROM docs ORDER BY kind, path")
        return list(cur.fetchall())

    def prune_missing(self, existing_paths: Iterable[str], kind: str) -> int:
        keep = set(existing_paths)
        removed = 0
        for row in self.list_docs(kind):
            if row["path"] not in keep:
                self.delete_doc(row["path"])
                removed += 1
        return removed

    # ---- chunks --------------------------------------------------------
    def add_chunks(self, doc_id: int, chunks: Sequence[Dict[str, Any]]) -> int:
        count = 0
        for ch in chunks:
            text = (ch.get("text") or "").strip()
            if not text:
                continue
            cur = self.conn.execute(
                "INSERT INTO chunks (doc_id, page, line, end_line, heading, text)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (doc_id, ch.get("page"), ch.get("line"), ch.get("end_line"),
                 ch.get("heading"), text),
            )
            self.conn.execute(
                "INSERT INTO chunk_fts (rowid, text, heading) VALUES (?, ?, ?)",
                (cur.lastrowid, text, ch.get("heading") or ""),
            )
            count += 1
        self.conn.commit()
        return count

    def add_registers(self, doc_id: int, part: Optional[str], regs: Sequence[Dict[str, Any]]) -> int:
        for reg in regs:
            self.conn.execute(
                "INSERT INTO registers (doc_id, part, name, address, page, description)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (doc_id, part, reg.get("name"), reg.get("address"),
                 reg.get("page"), reg.get("description")),
            )
        self.conn.commit()
        return len(regs)

    # ---- retrieval -----------------------------------------------------
    def search(
        self,
        query: str,
        *,
        kind: Optional[str] = None,
        part: Optional[str] = None,
        limit: int = 8,
        path_prefix: Optional[str] = None,
    ) -> List[Hit]:
        match = build_match_query(query)
        if not match:
            return []
        sql = [
            "SELECT c.id AS chunk_id, c.doc_id, c.page, c.line, c.end_line, c.heading, c.text,",
            "       d.kind, d.path, d.title, d.part, d.component,",
            "       bm25(chunk_fts, 1.0, 4.0) AS rank",
            "FROM chunk_fts",
            "JOIN chunks c ON c.id = chunk_fts.rowid",
            "JOIN docs d ON d.id = c.doc_id",
            "WHERE chunk_fts MATCH ?",
        ]
        params: List[Any] = [match]
        if kind:
            sql.append("AND d.kind = ?")
            params.append(kind)
        if part:
            sql.append("AND (d.part LIKE ? OR d.component LIKE ? OR d.title LIKE ?)")
            params.extend([f"%{part}%"] * 3)
        if path_prefix:
            sql.append("AND d.path LIKE ?")
            params.append(f"{path_prefix}%")
        sql.append("ORDER BY rank LIMIT ?")
        params.append(int(limit))

        try:
            rows = self.conn.execute("\n".join(sql), params).fetchall()
        except sqlite3.OperationalError:
            # A malformed MATCH expression should degrade to "no results",
            # never crash a chat turn.
            return []

        hits: List[Hit] = []
        for r in rows:
            hits.append(
                Hit(
                    chunk_id=r["chunk_id"], doc_id=r["doc_id"], kind=r["kind"],
                    path=r["path"], title=r["title"] or "", part=r["part"],
                    component=r["component"], page=r["page"], line=r["line"],
                    end_line=r["end_line"], heading=r["heading"], text=r["text"],
                    # bm25() returns lower-is-better; flip it so higher is better.
                    score=-float(r["rank"]),
                )
            )
        return hits

    def page_text(self, doc_id: int, page: int) -> List[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM chunks WHERE doc_id = ? AND page = ? ORDER BY id", (doc_id, page)
        )
        return list(cur.fetchall())

    def find_registers(
        self, name_or_addr: str, part: Optional[str] = None, limit: int = 20
    ) -> List[sqlite3.Row]:
        sql = ["SELECT r.*, d.title, d.path FROM registers r JOIN docs d ON d.id = r.doc_id WHERE 1=1"]
        params: List[Any] = []
        if name_or_addr:
            sql.append("AND (r.name LIKE ? OR r.address LIKE ?)")
            params.extend([f"%{name_or_addr}%", f"%{name_or_addr}%"])
        if part:
            sql.append("AND (r.part LIKE ? OR d.title LIKE ?)")
            params.extend([f"%{part}%", f"%{part}%"])
        sql.append("ORDER BY r.name LIMIT ?")
        params.append(limit)
        return list(self.conn.execute("\n".join(sql), params).fetchall())

    # ---- meta ----------------------------------------------------------
    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def get_meta(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def stats(self) -> Dict[str, int]:
        def scalar(sql: str) -> int:
            return int(self.conn.execute(sql).fetchone()[0])

        return {
            "datasheets": scalar("SELECT COUNT(*) FROM docs WHERE kind='datasheet'"),
            "source_files": scalar("SELECT COUNT(*) FROM docs WHERE kind='code'"),
            "chunks": scalar("SELECT COUNT(*) FROM chunks"),
            "registers": scalar("SELECT COUNT(*) FROM registers"),
        }
