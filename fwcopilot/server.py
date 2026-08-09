"""Browser chat UI: FastAPI + server-sent events streaming.

The HTTP layer is deliberately thin — it reuses the same Agent, ToolRunner and
Store as the CLI, so the browser session sees exactly the same project context.
"""

from __future__ import annotations

import json
import queue
import threading
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from .agent import Agent, Session
from .board import load_board
from .config import Config
from .context import load_or_scan_profile
from .store import Store
from .tools import ToolRunner

WEB_DIR = Path(__file__).parent / "web"


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None


def create_app(cfg: Config, *, allow_write: bool = False, allow_build: bool = False) -> FastAPI:
    app = FastAPI(title=f"fwcopilot — {cfg.name}")
    lock = threading.Lock()
    sessions: Dict[str, Session] = {}

    def approve(name: str, args: Dict[str, Any]) -> bool:
        # In server mode the flags are the policy: there is no interactive prompt,
        # so a tool is either enabled for the whole run or refused.
        if name in ToolRunner.WRITE_TOOLS:
            return allow_write
        if name in ToolRunner.EXEC_TOOLS:
            return allow_build
        return False

    def make_agent(session_id: Optional[str]) -> Agent:
        store = Store(cfg.db_path)
        runner = ToolRunner(
            cfg, store, approve=approve, allow_write=allow_write, allow_build=allow_build
        )
        session = sessions.get(session_id) if session_id else None
        if session is None:
            session = Session(cfg, session_id) if session_id else Session(cfg)
            sessions[session.id] = session
        return Agent(cfg, store, runner, session=session,
                     allow_write=allow_write, allow_build=allow_build)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (WEB_DIR / "index.html").read_text(encoding="utf-8")

    @app.get("/api/status")
    def status() -> Dict[str, Any]:
        board = load_board(cfg.board_path)
        with Store(cfg.db_path) as store:
            stats = store.stats()
            datasheets = [
                {"part": r["part"] or r["title"], "component": r["component"],
                 "pages": r["pages"], "path": r["path"]}
                for r in store.list_docs("datasheet")
            ]
        profile = load_or_scan_profile(cfg)
        return {
            "project": cfg.name,
            "root": str(cfg.root),
            "model": cfg.model,
            "board": {
                "name": board.name if board.exists else None,
                "mcu": board.mcu.get("part") if board.exists else None,
                "components": [c.describe() for c in board.components] if board.exists else [],
                "warnings": board.validate(cfg.root) if board.exists else [],
            },
            "project_profile": {
                "build_systems": profile.build_systems,
                "rtos": profile.rtos,
                "toolchains": profile.toolchains,
                "peripherals": profile.peripherals,
                "mcu_hints": profile.mcu_hints,
            },
            "index": stats,
            "datasheets": datasheets,
            "permissions": {"write": allow_write, "build": allow_build},
        }

    @app.get("/api/search")
    def search(q: str, kind: str = "all", limit: int = 6) -> Dict[str, Any]:
        with Store(cfg.db_path) as store:
            hits = store.search(q, kind=None if kind == "all" else kind, limit=limit)
        return {"hits": [h.to_dict() for h in hits]}

    @app.post("/api/chat")
    def chat(req: ChatRequest) -> StreamingResponse:
        if not req.message.strip():
            raise HTTPException(status_code=400, detail="empty message")

        def event_stream() -> Iterator[str]:
            # One turn at a time per process: the tool runner touches the
            # workspace, and concurrent turns would interleave file writes.
            with lock:
                agent = make_agent(req.session_id)
                yield _sse({"type": "session", "data": {"id": agent.session.id}})
                try:
                    for event in agent.stream_turn(req.message):
                        yield _sse(event.to_dict())
                except Exception as exc:  # keep the socket well-formed on failure
                    yield _sse({"type": "error", "text": f"{type(exc).__name__}: {exc}"})
                finally:
                    agent.store.close()

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


def _sse(payload: Dict[str, Any]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def run_server(
    cfg: Config, host: str = "127.0.0.1", port: int = 8765,
    *, allow_write: bool = False, allow_build: bool = False,
) -> None:
    import uvicorn

    app = create_app(cfg, allow_write=allow_write, allow_build=allow_build)
    mode = []
    if allow_write:
        mode.append("writes enabled")
    if allow_build:
        mode.append("build enabled")
    print(f"fwcopilot serving {cfg.name} at http://{host}:{port}"
          + (f"  [{', '.join(mode)}]" if mode else "  [read-only]"))
    uvicorn.run(app, host=host, port=port, log_level="warning")
