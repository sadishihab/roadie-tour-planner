"""FastAPI app: the demo gallery (pre-built plans and narrations) and an optional, off-by-default live mode.

The gallery never calls a model or Qloo on a request. Live mode needs ROADIE_LIVE=1 and the qloo CLI
set up on the host; otherwise every live endpoint answers 503 {"error": "live_disabled"}.

Nothing here reads, logs or returns an environment value, and error bodies carry fixed codes only.
Run: ``uvicorn roadie.api:app`` (the module-level ``app`` reads the environment) from backend/.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.concurrency import run_in_threadpool

from .live import JOB_ID_RE, MESSAGES, ClientFactory, LiveError, LiveService, RatePacer, client_ip
from .paths import SLUG_RE
from .settings import Settings

log = logging.getLogger("roadie.api")

MAX_GALLERY_FILE = 5_000_000
MAX_BODY = 2048
SECURITY_HEADERS = ((b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer"))
FRONTEND_FILE = Path(__file__).resolve().parents[2] / "frontend" / "index.html"
MAX_FRONTEND = 1_000_000
CSP = (
    "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'"
)


def error_response(status: int, code: str, message: str | None = None, retry_after: int | None = None) -> JSONResponse:
    headers = {"Retry-After": str(retry_after)} if retry_after else None
    return JSONResponse({"error": code, "message": message or MESSAGES.get(code, "Request failed.")}, status, headers)


class SecurityHeaders:
    """Pure ASGI middleware (safe with streaming responses)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Any) -> None:
            if message["type"] == "http.response.start":
                present = {k.lower() for k, _ in message.get("headers", [])}
                message["headers"] = list(message.get("headers", [])) + [h for h in SECURITY_HEADERS if h[0] not in present]
            await send(message)

        await self.app(scope, receive, send_with_headers)


class SearchBody(BaseModel):
    name: str


class PlanBody(BaseModel):
    entity_id: str


# ------------------------------------------------------------------- gallery files


def _gallery_files(directory: Path) -> dict[str, Path]:
    """slug -> path for the gallery files that exist. Paths come from this listing, never from input."""
    try:
        return {p.stem: p for p in sorted(directory.glob("*.json")) if SLUG_RE.fullmatch(p.stem) and p.is_file()}
    except OSError:
        return {}


def _read_gallery(path: Path) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > MAX_GALLERY_FILE:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("unreadable gallery file")
        return None
    return data if isinstance(data, dict) else None


def _summary(slug: str, data: dict[str, Any]) -> dict[str, Any]:
    plan = data.get("plan") if isinstance(data.get("plan"), dict) else {}
    cities = plan.get("cities") if isinstance(plan.get("cities"), list) else []
    comics = (plan.get("comics_to_bill") or {}).get("items") if isinstance(plan.get("comics_to_bill"), dict) else []
    return {
        "slug": slug,
        "name": data.get("name"),
        "description": data.get("description"),
        "built_with": data.get("built_with"),
        "strong_city_count": sum(isinstance(c, dict) and c.get("tier") == "strong" for c in cities),
        "identified_comics": sum(isinstance(c, dict) and c.get("identified_as_comedian") is True for c in comics or []),
    }


# ----------------------------------------------------------------------------- app


def create_app(
    settings: Settings | None = None,
    client_factory: ClientFactory | None = None,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] | None = None,
    pacer: RatePacer | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    service = LiveService(settings, client_factory, clock, now, pacer)
    qloo_found = shutil.which(settings.qloo_bin) is not None
    # With a client_factory injected (tests), the CLI check is the factory's business.
    live_configured = settings.live and (qloo_found or client_factory is not None)

    def live_enabled() -> bool:
        return live_configured and not service.ended()

    app = FastAPI(title="Roadie", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.live_service = service
    app.add_middleware(SecurityHeaders)
    if settings.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.allowed_origins),
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type"],
            allow_credentials=False,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, __: RequestValidationError) -> JSONResponse:
        return error_response(422, "invalid_request")  # never echo the input back

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "error")
        return error_response(exc.status_code, code, "Not found." if exc.status_code == 404 else "Request failed.")

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled error: %s", type(exc).__name__)
        return error_response(500, "internal_error")

    def live_off() -> JSONResponse | None:
        if live_enabled():
            return None
        # Configured but past ROADIE_LIVE_UNTIL: the hackathon key is gone, say so instead of "disabled".
        return error_response(503, "live_ended" if live_configured else "live_disabled")

    def ip_of(request: Request) -> str:
        return client_ip(request.client.host if request.client else None, request.headers.get("x-forwarded-for"), settings.trust_proxy, settings.trusted_proxy_hops)

    async def read_body(request: Request, model: type[BaseModel]) -> BaseModel | JSONResponse:
        raw = await request.body()
        if len(raw) > MAX_BODY:
            return error_response(413, "invalid_request")
        try:
            return model.model_validate_json(raw)
        except (ValidationError, ValueError):
            return error_response(422, "invalid_request")

    @app.get("/", include_in_schema=False)
    def index() -> Any:
        try:
            if FRONTEND_FILE.stat().st_size > MAX_FRONTEND:
                raise OSError
            page = FRONTEND_FILE.read_text(encoding="utf-8")
        except OSError:
            return error_response(404, "not_found")
        return HTMLResponse(page, headers={"Content-Security-Policy": CSP, "Cache-Control": "no-cache"})

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        on = live_enabled()
        # live_budget_remaining: live plan runs still allowed (smaller of today's and this month's); null when live is off
        # gallery_count: how many gallery files are on disk (0 means the secret files were not found)
        return {
            "status": "ok",
            "live_enabled": on,
            "live_budget_remaining": service.budget_remaining() if on else None,
            "gallery_count": len(_gallery_files(settings.gallery_dir)),
        }

    @app.get("/api/gallery")
    def gallery() -> list[dict[str, Any]]:
        out = []
        for slug, path in _gallery_files(settings.gallery_dir).items():
            data = _read_gallery(path)
            if data is not None:
                out.append(_summary(slug, data))
        return out

    @app.get("/api/plan/{slug}")
    def plan(slug: str) -> Any:
        if not SLUG_RE.fullmatch(slug):
            return error_response(422, "invalid_request")
        path = _gallery_files(settings.gallery_dir).get(slug)
        data = _read_gallery(path) if path else None
        if data is None:
            return error_response(404, "not_found")
        return data

    # ------------------------------------------------------------------ live mode

    @app.post("/api/live/search")
    async def live_search(request: Request) -> Any:
        if (off := live_off()) is not None:
            return off
        body = await read_body(request, SearchBody)
        if isinstance(body, JSONResponse):
            return body
        try:
            return await run_in_threadpool(service.search, ip_of(request), body.name)  # type: ignore[attr-defined]
        except LiveError as exc:
            return error_response(exc.status, exc.code, retry_after=exc.retry_after)

    @app.post("/api/live/plan", status_code=202)
    async def live_plan(request: Request) -> Any:
        if (off := live_off()) is not None:
            return off
        body = await read_body(request, PlanBody)
        if isinstance(body, JSONResponse):
            return body
        try:
            job, reused = service.start(ip_of(request), body.entity_id)  # type: ignore[attr-defined]
        except LiveError as exc:
            return error_response(exc.status, exc.code, retry_after=exc.retry_after)
        return {"job_id": job.id, "reused": reused, "stream": f"/api/live/stream/{job.id}"}

    @app.get("/api/live/stream/{job_id}")
    def live_stream(job_id: str) -> Any:
        if (off := live_off()) is not None:
            return off
        job = service.get_job(job_id) if JOB_ID_RE.fullmatch(job_id) else None
        if job is None:
            return error_response(404, "not_found")
        return StreamingResponse(
            job.stream(service.clock),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    return app


def _default_app() -> FastAPI:
    return create_app(Settings.from_env())


app = _default_app()
