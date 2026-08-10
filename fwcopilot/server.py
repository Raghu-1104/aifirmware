"""Browser chat UI and HTTP API.

The HTTP layer is deliberately thin — it reuses the same Agent, ToolRunner and
Store as the CLI, so a browser session sees exactly the same project context.

Deployment posture: bind to localhost by default, require a bearer token when
one is configured, deny cross-origin requests unless origins are named, and
expose liveness/readiness probes for a container orchestrator.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import __version__
from .agent import Agent, Session
from .board import load_board
from .config import Config
from .context import load_or_scan_profile
from .logging_setup import get_logger
from .store import Store
from .tools import ToolRunner

WEB_DIR = Path(__file__).parent / "web"
MAX_MESSAGE_CHARS = 32000
MAX_SESSIONS_IN_MEMORY = 100

log = get_logger("server")


class ChatRequest(BaseModel):
    message: str = Field(..., max_length=MAX_MESSAGE_CHARS)
    session_id: Optional[str] = None


class ServerSettings:
    """Runtime policy for one server process."""

    def __init__(
        self,
        *,
        allow_write: bool = False,
        allow_build: bool = False,
        auth_token: Optional[str] = None,
        cors_origins: Optional[List[str]] = None,
    ):
        self.allow_write = allow_write
        self.allow_build = allow_build
        self.auth_token = auth_token or os.environ.get("FWCOPILOT_AUTH_TOKEN") or None
        self.cors_origins = cors_origins or []
        self.started_at = time.time()

    @property
    def auth_required(self) -> bool:
        return bool(self.auth_token)


def create_app(
    cfg: Config,
    *,
    allow_write: bool = False,
    allow_build: bool = False,
    auth_token: Optional[str] = None,
    cors_origins: Optional[List[str]] = None,
) -> FastAPI:
    settings = ServerSettings(
        allow_write=allow_write,
        allow_build=allow_build,
        auth_token=auth_token,
        cors_origins=cors_origins,
    )
    app = FastAPI(title=f"fwcopilot — {cfg.name}", version=__version__)
    app.state.settings = settings

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
        )

    turn_lock = threading.Lock()
    sessions: Dict[str, Session] = {}

    def require_auth(authorization: Optional[str] = Header(default=None)) -> None:
        if not settings.auth_required:
            return
        expected = f"Bearer {settings.auth_token}"
        # Constant-time compare so the token can't be recovered by timing.
        if not authorization or not secrets.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="invalid or missing bearer token")

    def approve(name: str, args: Dict[str, Any]) -> bool:
        # In server mode the flags are the policy: there is no interactive
        # prompt, so a tool is either enabled for the whole run or refused.
        if name in ToolRunner.WRITE_TOOLS:
            return settings.allow_write
        if name in ToolRunner.EXEC_TOOLS:
            return settings.allow_build
        return False

    def make_agent(session_id: Optional[str]) -> Agent:
        store = Store(cfg.db_path)
        runner = ToolRunner(
            cfg,
            store,
            approve=approve,
            allow_write=settings.allow_write,
            allow_build=settings.allow_build,
        )
        session = sessions.get(session_id) if session_id else None
        if session is None:
            session = Session(cfg, session_id) if session_id else Session(cfg)
            if len(sessions) >= MAX_SESSIONS_IN_MEMORY:
                # Sessions are on disk; the map is only a warm cache.
                sessions.pop(next(iter(sessions)), None)
            sessions[session.id] = session
        return Agent(
            cfg,
            store,
            runner,
            session=session,
            allow_write=settings.allow_write,
            allow_build=settings.allow_build,
        )

    # ---- probes (never authenticated: orchestrators can't hold a token) ----
    @app.get("/healthz")
    def healthz() -> Dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "uptime_seconds": round(time.time() - settings.started_at, 1),
        }

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        problems: List[str] = []
        if not cfg.db_path.is_file():
            problems.append("index not built — run `fwcopilot index`")
        else:
            try:
                with Store(cfg.db_path) as store:
                    if store.stats()["chunks"] == 0:
                        problems.append("index is empty — run `fwcopilot index`")
            except Exception as exc:  # pragma: no cover - corrupt db
                problems.append(f"index unreadable: {exc}")
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            problems.append("no Anthropic credentials configured — chat will fail")
        ready = not problems
        return JSONResponse(
            {"ready": ready, "problems": problems},
            status_code=200 if ready else 503,
        )

    # ---- UI --------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (WEB_DIR / "index.html").read_text(encoding="utf-8")

    @app.get("/api/meta")
    def meta() -> Dict[str, Any]:
        """Unauthenticated: lets the UI know whether to prompt for a token."""
        return {"auth_required": settings.auth_required, "version": __version__}

    # ---- API -------------------------------------------------------------
    @app.get("/api/status", dependencies=[Depends(require_auth)])
    def status() -> Dict[str, Any]:
        board = load_board(cfg.board_path)
        with Store(cfg.db_path) as store:
            stats = store.stats()
            datasheets = [
                {
                    "part": r["part"] or r["title"],
                    "component": r["component"],
                    "pages": r["pages"],
                    "path": r["path"],
                }
                for r in store.list_docs("datasheet")
            ]
        profile = load_or_scan_profile(cfg)
        return {
            "project": cfg.name,
            "root": str(cfg.root),
            "model": cfg.model,
            "version": __version__,
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
            "permissions": {"write": settings.allow_write, "build": settings.allow_build},
        }

    @app.get("/api/search", dependencies=[Depends(require_auth)])
    def search(q: str, kind: str = "all", limit: int = 6) -> Dict[str, Any]:
        limit = max(1, min(limit, 25))
        with Store(cfg.db_path) as store:
            hits = store.search(q, kind=None if kind == "all" else kind, limit=limit)
        return {"hits": [h.to_dict() for h in hits]}

    @app.get("/api/lint", dependencies=[Depends(require_auth)])
    def lint(severity: str = "warning", path_prefix: Optional[str] = None) -> Dict[str, Any]:
        from .lint import SEVERITIES, lint_paths, summarize
        from .project import iter_source_files

        rank = {sev: i for i, sev in enumerate(SEVERITIES)}
        threshold = rank.get(severity, 1)
        paths = iter_source_files(cfg.root, cfg.source_globs, cfg.excludes)
        if path_prefix:
            paths = [p for p in paths if str(p.relative_to(cfg.root)).startswith(path_prefix)]
        findings = [f for f in lint_paths(paths, cfg.root) if rank.get(f.severity, 2) <= threshold]
        return {
            "findings": [f.to_dict() for f in findings],
            "counts": summarize(findings),
        }

    @app.post("/api/chat", dependencies=[Depends(require_auth)])
    def chat(req: ChatRequest) -> StreamingResponse:
        if not req.message.strip():
            raise HTTPException(status_code=400, detail="empty message")

        def event_stream() -> Iterator[str]:
            # One turn at a time per process: the tool runner touches the
            # workspace, and concurrent turns would interleave file writes.
            with turn_lock:
                agent = make_agent(req.session_id)
                yield _sse({"type": "session", "data": {"id": agent.session.id}})
                try:
                    for event in agent.stream_turn(req.message):
                        yield _sse(event.to_dict())
                except Exception as exc:  # keep the socket well-formed on failure
                    log.exception("chat turn failed")
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
    cfg: Config,
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    allow_write: bool = False,
    allow_build: bool = False,
    auth_token: Optional[str] = None,
    cors_origins: Optional[List[str]] = None,
) -> None:
    import uvicorn

    app = create_app(
        cfg,
        allow_write=allow_write,
        allow_build=allow_build,
        auth_token=auth_token,
        cors_origins=cors_origins,
    )
    settings: ServerSettings = app.state.settings

    mode = []
    if settings.allow_write:
        mode.append("writes enabled")
    if settings.allow_build:
        mode.append("build enabled")
    print(
        f"fwcopilot {__version__} serving {cfg.name} at http://{host}:{port}"
        + (f"  [{', '.join(mode)}]" if mode else "  [read-only]")
    )
    if settings.auth_required:
        print("  bearer token required (FWCOPILOT_AUTH_TOKEN)")
    elif host not in ("127.0.0.1", "localhost", "::1"):
        print("  WARNING: bound to a non-local address with no auth token set.")
        print("  Set FWCOPILOT_AUTH_TOKEN or --auth-token before exposing this.")
    uvicorn.run(app, host=host, port=port, log_level="warning")
