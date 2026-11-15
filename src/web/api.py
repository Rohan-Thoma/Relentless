"""Zero-dependency ASGI 3.0 Web & API Server for Relentless Command Center."""

from __future__ import annotations

import asyncio
from http import HTTPStatus
import json
import logging
import mimetypes
import os
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote

from src.hitl import service
from src.knowledge.distiller import run_distill
from src.logging_config import clear_log_buffer, configure_logging, get_recent_logs
from src.readiness import collect_readiness
from src.settings_store import load_example_profile
from src.tailor.resume_tailor import ensure_tailored_pdf
from src.web.auth import (
    LOCALHOST_IPS,
    SESSION_COOKIE_NAME,
    authenticate_request,
    clear_failed_attempts,
    create_session_token,
    get_configured_secret,
    is_ip_locked_out,
    record_failed_attempt,
    verify_secret,
)

logger = logging.getLogger(__name__)


def _base_headers(extra: list[tuple[bytes, bytes]] | None = None) -> list[tuple[bytes, bytes]]:
    cors_origin = os.getenv("WEB_CORS_ORIGIN", "*").strip() or "*"
    headers: list[tuple[bytes, bytes]] = [
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"SAMEORIGIN"),
        (b"referrer-policy", b"strict-origin-when-cross-origin"),
        (b"access-control-allow-origin", cors_origin.encode("latin-1")),
        (b"access-control-allow-headers", b"Content-Type, Authorization, X-Relentless-Api-Key"),
        (b"access-control-allow-methods", b"GET, POST, PUT, DELETE, OPTIONS"),
    ]
    if extra:
        headers.extend(extra)
    return headers


async def _send_bytes(
    send: Any,
    status: int,
    body: bytes,
    content_type: str,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
    head_only: bool = False,
) -> None:
    headers = _base_headers(
        [
            (b"content-type", content_type.encode("latin-1")),
            (b"content-length", str(len(body)).encode("latin-1")),
            *(extra_headers or []),
        ]
    )
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": b"" if head_only else body, "more_body": False})


async def _send_json(
    send: Any,
    status: int,
    payload: Any,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
    head_only: bool = False,
) -> None:
    raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    await _send_bytes(
        send,
        status,
        raw,
        "application/json; charset=utf-8",
        extra_headers=extra_headers,
        head_only=head_only,
    )


async def _read_body(receive: Any) -> bytes:
    chunks: list[bytes] = []
    while True:
        event = await receive()
        etype = event.get("type")
        if etype == "http.request":
            body = event.get("body", b"")
            if body:
                chunks.append(body)
            if not event.get("more_body", False):
                break
        elif etype == "http.disconnect":
            break
    return b"".join(chunks)


def _parse_json_body(raw: bytes) -> tuple[Any, str | None]:
    if not raw or not raw.strip():
        return {}, None
    try:
        return json.loads(raw.decode("utf-8")), None
    except Exception:
        return None, "INVALID_JSON"


def _cookie_attrs(scope: dict[str, Any], headers: dict[str, str]) -> str:
    is_https = (
        scope.get("scheme") == "https"
        or headers.get("x-forwarded-proto", "").lower() == "https"
    )
    return "; HttpOnly; Path=/; SameSite=Lax" + ("; Secure" if is_https else "")


async def _handle_auth_routes(
    method: str,
    path: str,
    scope: dict[str, Any],
    receive: Any,
    send: Any,
    headers: dict[str, str],
    client_ip: str,
) -> bool:
    if path == "/api/health" and method in ("GET", "HEAD"):
        await _send_json(
            send, 200, {"ok": True, "service": "relentless-web"}, head_only=(method == "HEAD")
        )
        return True

    if path == "/api/auth/status" and method in ("GET", "HEAD"):
        auth_state = authenticate_request(headers, client_ip)
        await _send_json(send, 200, auth_state, head_only=(method == "HEAD"))
        return True

    if path == "/api/auth/login" and method == "POST":
        if is_ip_locked_out(client_ip):
            await _send_json(send, 429, {"ok": False, "error": "TOO_MANY_ATTEMPTS"})
            return True

        raw = await _read_body(receive)
        body, err = _parse_json_body(raw)
        if err:
            await _send_json(send, 400, {"ok": False, "error": err})
            return True
        if not isinstance(body, dict):
            await _send_json(send, 400, {"ok": False, "error": "INVALID_BODY"})
            return True

        candidate = str(body.get("password") or body.get("api_key") or "")
        attrs = _cookie_attrs(scope, headers)

        if (
            not get_configured_secret()
            and client_ip in LOCALHOST_IPS
            and not headers.get("x-forwarded-for")
        ):
            token = create_session_token()
            cookie = f"{SESSION_COOKIE_NAME}={token}{attrs}; Max-Age=604800"
            await _send_json(
                send,
                200,
                {"ok": True, "token": token, "mode": "local_dev"},
                extra_headers=[(b"set-cookie", cookie.encode("latin-1"))],
            )
            return True

        if verify_secret(candidate):
            clear_failed_attempts(client_ip)
            token = create_session_token()
            cookie = f"{SESSION_COOKIE_NAME}={token}{attrs}; Max-Age=604800"
            await _send_json(
                send,
                200,
                {"ok": True, "token": token, "mode": "session"},
                extra_headers=[(b"set-cookie", cookie.encode("latin-1"))],
            )
            return True

        record_failed_attempt(client_ip)
        await _send_json(send, 401, {"ok": False, "error": "INVALID_CREDENTIALS"})
        return True

    if path == "/api/auth/logout" and method == "POST":
        attrs = _cookie_attrs(scope, headers)
        cookie = f"{SESSION_COOKIE_NAME}={attrs}; Max-Age=0"
        await _send_json(
            send,
            200,
            {"ok": True},
            extra_headers=[(b"set-cookie", cookie.encode("latin-1"))],
        )
        return True

    return False


async def _handle_job_routes(
    method: str,
    path: str,
    query: dict[str, list[str]],
    receive: Any,
    send: Any,
    head_only: bool,
) -> bool:
    if path == "/api/jobs" and method in ("GET", "HEAD"):
        try:
            limit = int(query.get("limit", ["50"])[0])
            offset = int(query.get("offset", ["0"])[0])
        except ValueError:
            await _send_json(send, 400, {"ok": False, "error": "INVALID_QUERY_PARAM"})
            return True

        res = await service.async_list_jobs(
            stage=query.get("stage", [None])[0],
            fit_tier=query.get("fit_tier", [None])[0],
            source_ats=query.get("source_ats", [None])[0],
            search=query.get("search", [None])[0],
            limit=limit,
            offset=offset,
        )
        await _send_json(send, 200, res, head_only=head_only)
        return True

    if not path.startswith("/api/jobs/"):
        return False

    parts = [p for p in path.removeprefix("/api/jobs/").split("/") if p]
    if not parts:
        return False

    try:
        job_id = int(parts[0])
    except ValueError:
        await _send_json(send, 400, {"ok": False, "error": "INVALID_JOB_ID"})
        return True

    # GET /api/jobs/{id}
    if len(parts) == 1 and method in ("GET", "HEAD"):
        detail = await service.async_get_job_detail(job_id)
        if detail is None:
            await _send_json(send, 404, {"ok": False, "error": "NOT_FOUND"}, head_only=head_only)
            return True
        await _send_json(send, 200, detail, head_only=head_only)
        return True

    # GET /api/jobs/{id}/pdf
    if len(parts) == 2 and parts[1] == "pdf" and method in ("GET", "HEAD"):
        detail = await service.async_get_job_detail(job_id)
        if detail is None:
            await _send_json(send, 404, {"ok": False, "error": "NOT_FOUND"}, head_only=head_only)
            return True
        pdf_path = await ensure_tailored_pdf(detail)
        if not pdf_path or not Path(pdf_path).is_file():
            await _send_json(send, 404, {"ok": False, "error": "PDF_NOT_FOUND"}, head_only=head_only)
            return True
        data = await asyncio.to_thread(Path(pdf_path).read_bytes)
        await _send_bytes(
            send,
            200,
            data,
            "application/pdf",
            extra_headers=[
                (
                    b"content-disposition",
                    f'inline; filename="resume_{job_id}.pdf"'.encode("latin-1"),
                )
            ],
            head_only=head_only,
        )
        return True

    # GET /api/jobs/{id}/screenshot/{idx}
    if len(parts) == 3 and parts[1] == "screenshot" and method in ("GET", "HEAD"):
        try:
            idx = int(parts[2])
        except ValueError:
            await _send_json(send, 400, {"ok": False, "error": "INVALID_SCREENSHOT_INDEX"})
            return True
        detail = await service.async_get_job_detail(job_id)
        if detail is None:
            await _send_json(send, 404, {"ok": False, "error": "NOT_FOUND"}, head_only=head_only)
            return True
        screenshots = detail.get("screenshots") or []
        if idx < 0 or idx >= len(screenshots):
            await _send_json(
                send, 404, {"ok": False, "error": "SCREENSHOT_NOT_FOUND"}, head_only=head_only
            )
            return True
        shot_path = Path(screenshots[idx])
        if not shot_path.is_file():
            await _send_json(
                send, 404, {"ok": False, "error": "SCREENSHOT_NOT_FOUND"}, head_only=head_only
            )
            return True
        mime = mimetypes.guess_type(str(shot_path))[0] or "image/png"
        data = await asyncio.to_thread(shot_path.read_bytes)
        await _send_bytes(send, 200, data, mime, head_only=head_only)
        return True

    if len(parts) != 2 or method != "POST":
        return False

    sub = parts[1]
    raw = await _read_body(receive)
    body, err = _parse_json_body(raw)
    if err:
        await _send_json(send, 400, {"ok": False, "error": err})
        return True
    if not isinstance(body, dict):
        await _send_json(send, 400, {"ok": False, "error": "INVALID_BODY"})
        return True

    if sub == "interest":
        res = await service.start_job_interest(
            job_id,
            custom_note=str(body.get("custom_note") or ""),
            is_edit=bool(body.get("is_edit", False)),
            actor_source="web",
        )
        status = 409 if res.get("error") == "BUSY" else (404 if res.get("error") == "NOT_FOUND" else 200)
        await _send_json(send, status, res)
        return True

    if sub == "edit":
        note = str(body.get("custom_note") or "").strip()
        if not note:
            await _send_json(send, 400, {"ok": False, "error": "MISSING_EDIT_NOTE"})
            return True
        res = await service.start_job_interest(
            job_id,
            custom_note=note,
            is_edit=True,
            actor_source="web",
        )
        status = 409 if res.get("error") == "BUSY" else (404 if res.get("error") == "NOT_FOUND" else 200)
        await _send_json(send, status, res)
        return True

    if sub == "action":
        action = str(body.get("action") or "").strip()
        try:
            res = await service.record_job_action(job_id, action, actor_source="web")
        except ValueError as e:
            await _send_json(send, 400, {"ok": False, "error": str(e)})
            return True
        status = 404 if res.get("error") == "NOT_FOUND" else 200
        await _send_json(send, status, res)
        return True

    if sub == "submit":
        token = body.get("token")
        res = await service.execute_job_submit(
            job_id,
            token=str(token) if token is not None else None,
            actor_source="web",
        )
        status = 409 if res.get("error") == "BUSY" else (404 if res.get("error") == "NOT_FOUND" else 200)
        await _send_json(send, status, res)
        return True

    return False


async def _handle_okf_eval_settings_routes(
    method: str,
    path: str,
    query: dict[str, list[str]],
    receive: Any,
    send: Any,
    head_only: bool,
) -> bool:
    if path == "/api/okf" and method in ("GET", "HEAD"):
        data = await service.async_get_okf_brain_data(
            query=query.get("query", [""])[0] or "",
            concept_type=query.get("type", [None])[0],
        )
        await _send_json(send, 200, data, head_only=head_only)
        return True

    if path == "/api/okf/distill" and method == "POST":
        count = await asyncio.to_thread(run_distill)
        await _send_json(send, 200, {"ok": True, "distilled": count})
        return True

    if path.startswith("/api/okf/"):
        concept_path = unquote(path.removeprefix("/api/okf/"))
        if method == "PUT":
            raw = await _read_body(receive)
            body, err = _parse_json_body(raw)
            if err:
                await _send_json(send, 400, {"ok": False, "error": err})
                return True
            if not isinstance(body, dict):
                await _send_json(send, 400, {"ok": False, "error": "INVALID_BODY"})
                return True
            try:
                saved = await service.async_save_okf_concept(
                    path=concept_path,
                    concept_type=str(body.get("concept_type") or ""),
                    body=str(body.get("body") or ""),
                    name=str(body.get("name") or ""),
                    description=str(body.get("description") or ""),
                    tags=body.get("tags"),
                    actor="human:owner",
                )
            except ValueError as e:
                await _send_json(send, 400, {"ok": False, "error": str(e)})
                return True
            await _send_json(send, 200, {"ok": True, "concept": saved})
            return True

        if method == "DELETE":
            try:
                res = await service.async_delete_okf_concept(concept_path)
            except ValueError as e:
                await _send_json(send, 400, {"ok": False, "error": str(e)})
                return True
            await _send_json(send, 200, res)
            return True

    if path == "/api/eval" and method in ("GET", "HEAD"):
        data = await service.async_get_evaluation_data()
        await _send_json(send, 200, data, head_only=head_only)
        return True

    if path == "/api/settings" and method in ("GET", "HEAD"):
        data = await service.async_get_user_settings()
        await _send_json(send, 200, data, head_only=head_only)
        return True

    if path == "/api/settings/profile-template" and method in ("GET", "HEAD"):
        try:
            tmpl = load_example_profile()
        except Exception as e:
            await _send_json(send, 404, {"ok": False, "error": str(e)}, head_only=head_only)
            return True
        await _send_json(send, 200, {"ok": True, "template": tmpl}, head_only=head_only)
        return True

    if path == "/api/logs" and method in ("GET", "HEAD"):
        limit_raw = (query.get("limit") or ["100"])[0]
        since_raw = (query.get("since_id") or ["0"])[0]
        level_raw = (query.get("level") or [""])[0]
        q_raw = (query.get("q") or [""])[0]
        try:
            limit_val = int(limit_raw)
        except ValueError:
            limit_val = 100
        try:
            since_val = int(since_raw)
        except ValueError:
            since_val = 0

        logs_data = get_recent_logs(
            limit=limit_val, level=level_raw or None, query=q_raw or None, since_id=since_val
        )
        readiness = await asyncio.to_thread(collect_readiness, True)
        await _send_json(
            send,
            200,
            {"ok": True, **logs_data, "readiness": readiness},
            head_only=head_only,
        )
        return True

    if path == "/api/logs/clear" and method == "POST":
        cleared = clear_log_buffer()
        await _send_json(send, 200, {"ok": True, "cleared": cleared})
        return True

    if path == "/api/settings/profile" and method == "PUT":
        raw = await _read_body(receive)
        body, err = _parse_json_body(raw)
        if err:
            await _send_json(send, 400, {"ok": False, "error": err})
            return True
        payload = (
            body["master_profile"]
            if isinstance(body, dict)
            and "master_profile" in body
            and isinstance(body["master_profile"], dict)
            else body
        )
        try:
            res = await service.async_save_master_profile(payload)
        except (TypeError, ValueError) as e:
            await _send_json(send, 400, {"ok": False, "error": str(e)})
            return True
        await _send_json(send, 200, res)
        return True

    if path == "/api/settings/config" and method == "PUT":
        raw = await _read_body(receive)
        body, err = _parse_json_body(raw)
        if err:
            await _send_json(send, 400, {"ok": False, "error": err})
            return True
        payload = (
            body["config"]
            if isinstance(body, dict)
            and "config" in body
            and isinstance(body["config"], dict)
            else body
        )
        try:
            res = await service.async_save_config_yaml(payload)
        except (TypeError, ValueError) as e:
            await _send_json(send, 400, {"ok": False, "error": str(e)})
            return True
        await _send_json(send, 200, res)
        return True

    if path == "/api/actions/pause" and method == "POST":
        raw = await _read_body(receive)
        body, err = _parse_json_body(raw)
        if err:
            await _send_json(send, 400, {"ok": False, "error": err})
            return True
        if not isinstance(body, dict):
            await _send_json(send, 400, {"ok": False, "error": "INVALID_BODY"})
            return True
        hours_raw = body.get("hours", 24)
        hours_val = None if hours_raw is None else float(hours_raw)
        res = await service.async_set_automation_pause(hours_val)
        await _send_json(send, 200, res)
        return True

    if path == "/api/actions/digest" and method == "POST":
        raw = await _read_body(receive)
        body, err = _parse_json_body(raw)
        if err:
            await _send_json(send, 400, {"ok": False, "error": err})
            return True
        limit = int(body.get("limit", 5)) if isinstance(body, dict) else 5
        from src.hitl.telegram_bot import send_pending_digests

        try:
            await send_pending_digests(limit=limit)
        except ValueError as e:
            await _send_json(send, 400, {"ok": False, "error": str(e)})
            return True
        await _send_json(send, 200, {"ok": True})
        return True

    if path == "/api/actions/reset" and method == "POST":
        raw = await _read_body(receive)
        body, err = _parse_json_body(raw)
        if err:
            await _send_json(send, 400, {"ok": False, "error": err})
            return True
        if not isinstance(body, dict) or body.get("confirm") is not True:
            await _send_json(send, 400, {"ok": False, "error": "CONFIRMATION_REQUIRED"})
            return True
        mode = str(body.get("mode") or "labels").strip().lower()
        try:
            res = await service.async_reset_database_action(mode)
        except ValueError as e:
            await _send_json(send, 400, {"ok": False, "error": str(e)})
            return True
        await _send_json(send, 200, res)
        return True

    return False


async def _serve_static(path: str, send: Any, head_only: bool) -> None:
    web_root = Path("web").resolve()
    index_file = web_root / "index.html"
    if not index_file.is_file():
        if path == "/":
            await _send_json(
                send,
                200,
                {"ok": True, "service": "Relentless Web Command Center API"},
                head_only=head_only,
            )
            return
        await _send_json(send, 404, {"ok": False, "error": "NOT_FOUND"}, head_only=head_only)
        return

    rel = path.lstrip("/") or "index.html"
    candidate = (web_root / rel).resolve()
    if candidate != web_root and web_root not in candidate.parents:
        await _send_json(send, 403, {"ok": False, "error": "FORBIDDEN"}, head_only=head_only)
        return

    if candidate.is_file():
        mime = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        data = await asyncio.to_thread(candidate.read_bytes)
        await _send_bytes(send, 200, data, mime, head_only=head_only)
        return

    if "." not in Path(rel).name:
        data = await asyncio.to_thread(index_file.read_bytes)
        await _send_bytes(send, 200, data, "text/html; charset=utf-8", head_only=head_only)
        return

    await _send_json(send, 404, {"ok": False, "error": "NOT_FOUND"}, head_only=head_only)


async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """ASGI 3.0 entrypoint for Relentless Web Command Center API and static UI."""
    scope_type = scope.get("type")
    if scope_type == "lifespan":
        while True:
            msg = await receive()
            mtype = msg.get("type")
            if mtype == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif mtype == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
        return

    if scope_type != "http":
        return

    method = str(scope.get("method", "GET")).upper()
    path = str(scope.get("path", "/"))
    raw_qs = scope.get("query_string", b"")
    query = parse_qs(raw_qs.decode("utf-8", errors="replace") if raw_qs else "")
    headers = {
        k.decode("latin-1").lower(): v.decode("latin-1")
        for k, v in (scope.get("headers") or [])
    }
    client = scope.get("client")
    client_ip = str(client[0]) if client else "127.0.0.1"
    head_only = method == "HEAD"

    if method == "OPTIONS":
        await _send_bytes(send, 204, b"", "text/plain")
        return

    if await _handle_auth_routes(method, path, scope, receive, send, headers, client_ip):
        return

    if path.startswith("/api/"):
        auth_state = authenticate_request(headers, client_ip)
        if not auth_state.get("authenticated"):
            await _send_json(
                send, 401, {"ok": False, "error": "UNAUTHORIZED"}, head_only=head_only
            )
            return

        if path == "/api/status" and method in ("GET", "HEAD"):
            data = await service.async_get_system_status()
            await _send_json(send, 200, data, head_only=head_only)
            return

        if await _handle_job_routes(method, path, query, receive, send, head_only):
            return

        if await _handle_okf_eval_settings_routes(method, path, query, receive, send, head_only):
            return

        await _send_json(send, 404, {"ok": False, "error": "NOT_FOUND"}, head_only=head_only)
        return

    if method in ("GET", "HEAD"):
        await _serve_static(path, send, head_only=head_only)
        return

    await _send_json(send, 405, {"ok": False, "error": "METHOD_NOT_ALLOWED"})


async def _handle_http_connection(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    try:
        raw_head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=15.0)
    except Exception:
        writer.close()
        return

    try:
        head_text = raw_head.decode("latin-1")
        lines = head_text.split("\r\n")
        request_line = lines[0].split(" ")
        if len(request_line) < 2:
            writer.close()
            return
        method = request_line[0].upper()
        raw_target = request_line[1]
        if "?" in raw_target:
            raw_path, raw_qs = raw_target.split("?", 1)
        else:
            raw_path, raw_qs = raw_target, ""

        asgi_headers: list[tuple[bytes, bytes]] = []
        content_length = 0
        for line in lines[1:]:
            if not line or ":" not in line:
                continue
            k, v = line.split(":", 1)
            k_low = k.strip().lower()
            v_str = v.strip()
            asgi_headers.append((k_low.encode("latin-1"), v_str.encode("latin-1")))
            if k_low == "content-length":
                try:
                    content_length = max(0, int(v_str))
                except ValueError:
                    content_length = 0

        body_bytes = b""
        if content_length > 0:
            body_bytes = await asyncio.wait_for(reader.readexactly(content_length), timeout=30.0)

        peer = writer.get_extra_info("peername")
        client = (str(peer[0]), int(peer[1])) if isinstance(peer, tuple) and len(peer) >= 2 else ("127.0.0.1", 0)
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": unquote(raw_path),
            "raw_path": raw_path.encode("latin-1"),
            "query_string": raw_qs.encode("latin-1"),
            "headers": asgi_headers,
            "client": client,
        }

        body_delivered = False

        async def _receive() -> dict[str, Any]:
            nonlocal body_delivered
            if not body_delivered:
                body_delivered = True
                return {"type": "http.request", "body": body_bytes, "more_body": False}
            return {"type": "http.disconnect"}

        resp_status = 500
        resp_headers: list[tuple[bytes, bytes]] = []
        resp_chunks: list[bytes] = []

        async def _send(event: dict[str, Any]) -> None:
            nonlocal resp_status, resp_headers
            if event.get("type") == "http.response.start":
                resp_status = int(event.get("status", 200))
                resp_headers = list(event.get("headers") or [])
            elif event.get("type") == "http.response.body":
                chunk = event.get("body", b"")
                if chunk:
                    resp_chunks.append(chunk)

        await app(scope, _receive, _send)

        full_body = b"".join(resp_chunks)
        try:
            reason = HTTPStatus(resp_status).phrase
        except ValueError:
            reason = "OK"

        header_lines = [f"HTTP/1.1 {resp_status} {reason}\r\n".encode("latin-1")]
        has_cl = False
        for hk, hv in resp_headers:
            if hk.lower() == b"content-length":
                has_cl = True
            header_lines.append(hk + b": " + hv + b"\r\n")
        if not has_cl:
            header_lines.append(f"content-length: {len(full_body)}\r\n".encode("latin-1"))
        header_lines.append(b"connection: close\r\n\r\n")

        writer.write(b"".join(header_lines))
        if method != "HEAD" and full_body:
            writer.write(full_body)
        await writer.drain()
    except Exception as e:
        logger.debug("HTTP connection error: %s", e)
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _serve_stdlib_asgi(host: str, port: int) -> None:
    server = await asyncio.start_server(_handle_http_connection, host=host, port=port)
    print(f"🌐 Relentless Web Command Center API listening on http://{host}:{port}", flush=True)
    async with server:
        await server.serve_forever()


async def run_web_server(host: str | None = None, port: int | None = None) -> None:
    """Runs the Relentless Web API server using uvicorn if available, else stdlib asyncio HTTP."""
    configure_logging(capture_stdio=True)
    bind_host = (host or os.getenv("WEB_HOST") or "0.0.0.0").strip()
    bind_port = int(port or os.getenv("PORT", "8000"))

    try:
        import uvicorn  # type: ignore[import-not-found]

        config = uvicorn.Config(app, host=bind_host, port=bind_port, log_level="info")
        server = uvicorn.Server(config)
        await server.serve()
    except ImportError:
        await _serve_stdlib_asgi(bind_host, bind_port)
