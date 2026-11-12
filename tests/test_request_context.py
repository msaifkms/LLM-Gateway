import io
import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.core.logs import JsonFormatter, request_id_var
from app.core.request_context import RequestContextMiddleware


def make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/ok")
    async def ok() -> JSONResponse:
        return JSONResponse(
            {"ok": True}, headers={"X-Cache": "HIT", "X-Provider": "mock"}
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("boom")

    @app.get("/log")
    async def write_log() -> dict[str, bool]:
        logging.getLogger("app.test").info("inside handler")
        return {"ok": True}

    return app


def request_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == "app.request" and r.getMessage() == "request"
    ]


def test_generates_request_id() -> None:
    response = TestClient(make_app()).get("/ok")
    request_id = response.headers["x-request-id"]
    assert len(request_id) == 32
    int(request_id, 16)


def test_honors_valid_incoming_request_id() -> None:
    response = TestClient(make_app()).get(
        "/ok", headers={"X-Request-ID": "abc-123_X.y"}
    )
    assert response.headers["x-request-id"] == "abc-123_X.y"


@pytest.mark.parametrize("bad", ["x" * 100, "has space", "semi;colon"])
def test_replaces_invalid_incoming_request_id(bad: str) -> None:
    response = TestClient(make_app()).get("/ok", headers={"X-Request-ID": bad})
    assert response.headers["x-request-id"] != bad
    assert len(response.headers["x-request-id"]) == 32


def test_summary_line_has_expected_fields(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="app.request")
    TestClient(make_app()).get("/ok")
    record = request_records(caplog)[0]
    fields = record.fields  # type: ignore[attr-defined]
    assert record.levelno == logging.INFO
    assert fields["method"] == "GET"
    assert fields["path"] == "/ok"
    assert fields["status"] == 200
    assert fields["cache"] == "HIT"
    assert fields["provider"] == "mock"
    assert fields["duration_ms"] >= 0


def test_health_checks_log_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="app.request")
    TestClient(make_app()).get("/healthz")
    assert request_records(caplog)[0].levelno == logging.DEBUG


def test_unhandled_error_is_logged_as_500(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="app.request")
    client = TestClient(make_app(), raise_server_exceptions=False)
    response = client.get("/boom")
    assert response.status_code == 500
    record = request_records(caplog)[0]
    assert record.levelno == logging.ERROR
    assert record.fields["status"] == 500  # type: ignore[attr-defined]


def test_request_id_reaches_logs_written_inside_the_handler() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("app.test")
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        response = TestClient(make_app()).get("/log")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
    data = json.loads(stream.getvalue().strip().splitlines()[0])
    assert data["message"] == "inside handler"
    assert data["request_id"] == response.headers["x-request-id"]


def test_request_id_is_cleared_after_the_request() -> None:
    TestClient(make_app()).get("/ok")
    assert request_id_var.get() is None


def test_real_app_is_wired_with_the_middleware() -> None:
    from app.main import app

    response = TestClient(app).get("/healthz")
    assert response.status_code == 200
    assert "x-request-id" in response.headers
