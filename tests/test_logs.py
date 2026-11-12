import json
import logging
import sys

import pytest

from app.core.logs import (
    JsonFormatter,
    TextFormatter,
    configure_logging,
    request_id_var,
)


def make_record(**kwargs: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=kwargs.pop("exc_info", None),  # type: ignore[arg-type]
    )
    for key, value in kwargs.items():
        setattr(record, key, value)
    return record


@pytest.fixture
def restore_logging():  # type: ignore[no-untyped-def]
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def test_json_line_has_core_fields() -> None:
    line = JsonFormatter().format(make_record())
    data = json.loads(line)
    assert data["message"] == "hello world"
    assert data["level"] == "INFO"
    assert data["logger"] == "app.test"
    assert data["ts"].endswith("+00:00")
    assert "request_id" not in data


def test_json_includes_request_id_and_fields() -> None:
    token = request_id_var.set("abc123")
    try:
        line = JsonFormatter().format(make_record(fields={"status": 200}))
    finally:
        request_id_var.reset(token)
    data = json.loads(line)
    assert data["request_id"] == "abc123"
    assert data["status"] == 200


def test_fields_cannot_overwrite_core_keys() -> None:
    line = JsonFormatter().format(make_record(fields={"level": "FAKE", "x": 1}))
    data = json.loads(line)
    assert data["level"] == "INFO"
    assert data["x"] == 1


def test_json_handles_exceptions_and_unserializable_values() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        exc_info = sys.exc_info()
    line = JsonFormatter().format(
        make_record(exc_info=exc_info, fields={"obj": object()})
    )
    data = json.loads(line)
    assert "ValueError: boom" in data["exc"]
    assert isinstance(data["obj"], str)


def test_text_formatter_appends_request_id_and_fields() -> None:
    token = request_id_var.set("rid9")
    try:
        text = TextFormatter().format(make_record(fields={"status": 200}))
    finally:
        request_id_var.reset(token)
    assert "hello world" in text
    assert "request_id=rid9" in text
    assert "status=200" in text


def test_configure_logging_is_idempotent(restore_logging) -> None:  # type: ignore[no-untyped-def]
    root = logging.getLogger()
    configure_logging(level="DEBUG", fmt="json")
    configure_logging(level="DEBUG", fmt="json")
    ours = [h for h in root.handlers if getattr(h, "_llmgw_handler", False)]
    assert len(ours) == 1
    assert root.level == logging.DEBUG


def test_configure_logging_reads_environment(monkeypatch, restore_logging) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("LOG_LEVEL", "warning")
    monkeypatch.setenv("LOG_FORMAT", "text")
    configure_logging()
    root = logging.getLogger()
    ours = [h for h in root.handlers if getattr(h, "_llmgw_handler", False)]
    assert root.level == logging.WARNING
    assert isinstance(ours[0].formatter, TextFormatter)


def test_unknown_level_falls_back_to_info(restore_logging) -> None:  # type: ignore[no-untyped-def]
    configure_logging(level="nonsense", fmt="json")
    assert logging.getLogger().level == logging.INFO
