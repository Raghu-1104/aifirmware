"""The chat agent: streaming Claude turns with a firmware-aware tool loop.

Sessions are persisted to `.fwcopilot/sessions/` so a conversation about a driver
can be resumed tomorrow with its context intact.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import anthropic

from .config import Config
from .context import build_system_blocks
from .store import Store
from .tools import ToolRunner, tool_definitions

MAX_HISTORY_MESSAGES = 60
MAX_TOOL_ITERATIONS = 12
FALLBACK_BETA = "server-side-fallback-2026-07-01"

NO_CREDENTIALS_HELP = (
    "No Anthropic credentials found.\n"
    "  export ANTHROPIC_API_KEY=sk-ant-...   (or run `ant auth login`)\n"
    "Everything except chat — index, search, board check, scaffold — works without one."
)


@dataclass
class Event:
    """One streamed happening in a turn, rendered by the CLI or the web UI."""

    type: str  # text | text_delta | tool_use | tool_result | notice | error | done
    text: str = ""
    name: str = ""
    data: Dict[str, Any] = None  # type: ignore[assignment]

    def to_dict(self) -> Dict[str, Any]:
        return {"type": self.type, "text": self.text, "name": self.name, "data": self.data or {}}


class Session:
    """Persisted conversation history for one workspace."""

    def __init__(self, cfg: Config, session_id: Optional[str] = None):
        self.cfg = cfg
        self.id = session_id or time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        self.messages: List[Dict[str, Any]] = []
        self.created = time.time()
        cfg.sessions_dir.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self.cfg.sessions_dir / f"{self.id}.json"

    @classmethod
    def load(cls, cfg: Config, session_id: str) -> "Session":
        session = cls(cfg, session_id)
        if session.path.is_file():
            data = json.loads(session.path.read_text(encoding="utf-8"))
            session.messages = data.get("messages", [])
            session.created = data.get("created", time.time())
        return session

    @classmethod
    def latest(cls, cfg: Config) -> Optional["Session"]:
        files = sorted(cfg.sessions_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        return cls.load(cfg, files[-1].stem) if files else None

    def save(self) -> None:
        self.path.write_text(
            json.dumps(
                {"id": self.id, "created": self.created, "updated": time.time(),
                 "messages": self.messages},
                indent=2,
            ),
            encoding="utf-8",
        )

    def trim(self, limit: int = MAX_HISTORY_MESSAGES) -> None:
        """Drop the oldest turns, cutting only at a real user message.

        Cutting elsewhere could orphan a `tool_result` from its `tool_use`, which
        the API rejects.
        """
        if len(self.messages) <= limit:
            return
        for idx in range(len(self.messages) - limit, len(self.messages)):
            msg = self.messages[idx]
            if msg.get("role") == "user" and isinstance(msg.get("content"), str):
                self.messages = self.messages[idx:]
                return
        self.messages = self.messages[-limit:]


def _blocks_to_dicts(content: Any) -> List[Dict[str, Any]]:
    """Normalize SDK content blocks to plain dicts for storage and replay.

    Thinking blocks (and their signatures) are preserved verbatim — the API
    rejects modified ones.
    """
    out: List[Dict[str, Any]] = []
    for block in content:
        if isinstance(block, dict):
            out.append(block)
        elif hasattr(block, "model_dump"):
            out.append(block.model_dump(exclude_none=True, mode="json"))
        else:  # pragma: no cover - defensive
            out.append(json.loads(json.dumps(block, default=str)))
    return out


class Agent:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        runner: ToolRunner,
        *,
        client: Optional[Any] = None,
        session: Optional[Session] = None,
        allow_write: bool = False,
        allow_build: bool = False,
        use_fallbacks: bool = True,
    ):
        self.cfg = cfg
        self.store = store
        self.runner = runner
        self.client = client or anthropic.Anthropic()
        self.session = session or Session(cfg)
        self.tools = tool_definitions(allow_write, allow_build)
        self.use_fallbacks = use_fallbacks
        self._system: Optional[List[Dict[str, Any]]] = None

    def system_blocks(self, refresh: bool = False) -> List[Dict[str, Any]]:
        if self._system is None or refresh:
            self._system = build_system_blocks(self.cfg, self.store)
        return self._system

    # ---- the turn ------------------------------------------------------
    def stream_turn(self, user_message: str) -> Iterator[Event]:
        """Run one user turn to completion, yielding events as they happen."""
        self.session.messages.append({"role": "user", "content": user_message})
        self.session.trim()

        for _ in range(MAX_TOOL_ITERATIONS):
            try:
                response = yield from self._stream_once()
            except anthropic.AuthenticationError:
                yield Event("error", text=NO_CREDENTIALS_HELP)
                return
            except TypeError as exc:
                # The SDK raises this when no credential source resolves at all.
                if "authentication" not in str(exc).lower():
                    raise
                yield Event("error", text=NO_CREDENTIALS_HELP)
                return
            except anthropic.RateLimitError as exc:
                retry = exc.response.headers.get("retry-after", "60")
                yield Event("error", text=f"Rate limited. Retry in about {retry}s.")
                return
            except anthropic.APIStatusError as exc:
                yield Event("error", text=f"API error {exc.status_code}: {exc.message}")
                return
            except anthropic.APIConnectionError as exc:
                yield Event("error", text=f"Connection error: {exc}")
                return

            blocks = _blocks_to_dicts(response.content)
            self.session.messages.append({"role": "assistant", "content": blocks})

            if response.stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                category = getattr(details, "category", None) if details else None
                yield Event(
                    "error",
                    text=f"The request was declined by safety classifiers"
                         f"{f' ({category})' if category else ''}. Try rephrasing.",
                )
                self.session.save()
                return

            if response.stop_reason == "pause_turn":
                # A server-side tool hit its iteration limit; resend to resume.
                continue

            tool_uses = [b for b in blocks if b.get("type") == "tool_use"]
            if not tool_uses:
                self.session.save()
                yield Event("done", data={"stop_reason": response.stop_reason})
                return

            results: List[Dict[str, Any]] = []
            for call in tool_uses:
                name = call.get("name", "")
                args = call.get("input") or {}
                yield Event("tool_use", name=name, data=args)
                result = self.runner.run(name, args)
                yield Event(
                    "tool_result",
                    name=name,
                    text=result.summary or (result.text[:160] if result.is_error else ""),
                    data={"is_error": result.is_error},
                )
                results.append({
                    "type": "tool_result",
                    "tool_use_id": call.get("id"),
                    "content": result.text or "(no output)",
                    "is_error": result.is_error,
                })
            self.session.messages.append({"role": "user", "content": results})
            self.session.save()

        yield Event("error", text=f"Stopped after {MAX_TOOL_ITERATIONS} tool rounds without finishing.")

    def _request_kwargs(self) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {
            "model": self.cfg.model,
            "max_tokens": self.cfg.max_tokens,
            "system": self.system_blocks(),
            # Snapshot: the loop appends to session.messages while the request is
            # in flight, and the request body must not change underneath it.
            "messages": list(self.session.messages),
            "tools": self.tools,
        }
        if self.cfg.effort:
            kwargs["output_config"] = {"effort": self.cfg.effort}
        return kwargs

    def _stream_once(self) -> Iterator[Event]:
        """Stream one assistant message; returns the final Message object."""
        kwargs = self._request_kwargs()

        if self.use_fallbacks:
            # On a policy decline, the API re-serves the request on Anthropic's
            # recommended fallback model inside the same call. Degrade quietly if
            # the account or endpoint doesn't have the beta.
            try:
                with self.client.beta.messages.stream(
                    betas=[FALLBACK_BETA], fallbacks="default", **kwargs
                ) as stream:
                    yield from self._pump(stream)
                    return stream.get_final_message()
            except anthropic.BadRequestError as exc:
                if "fallback" not in str(exc).lower() and "beta" not in str(exc).lower():
                    raise
                self.use_fallbacks = False

        with self.client.messages.stream(**kwargs) as stream:
            yield from self._pump(stream)
            return stream.get_final_message()

    @staticmethod
    def _pump(stream: Any) -> Iterator[Event]:
        for event in stream:
            etype = getattr(event, "type", "")
            if etype == "content_block_delta":
                delta = getattr(event, "delta", None)
                dtype = getattr(delta, "type", "")
                if dtype == "text_delta":
                    yield Event("text_delta", text=delta.text)
                elif dtype == "thinking_delta":
                    yield Event("thinking_delta", text=getattr(delta, "thinking", ""))
            elif etype == "content_block_start":
                block = getattr(event, "content_block", None)
                if getattr(block, "type", "") == "thinking":
                    yield Event("notice", text="thinking")
