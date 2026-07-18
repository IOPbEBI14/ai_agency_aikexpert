"""Фильтры тихого poll-логирования."""
import logging

from core.logging_setup import QuietPollFilter, is_quiet_http_path


def test_quiet_paths():
    assert is_quiet_http_path("/api/agency/status")
    assert is_quiet_http_path("/static/brand/logo.png")
    assert not is_quiet_http_path("/api/agency/resume")


def test_quiet_poll_filter_drops_status_access():
    f = QuietPollFilter()
    rec = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='192.168.0.123:0 - "GET /api/agency/status HTTP/1.1" 200 OK',
        args=(),
        exc_info=None,
    )
    assert f.filter(rec) is False
    rec2 = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='192.168.0.123:0 - "POST /api/agency/resume HTTP/1.1" 200 OK',
        args=(),
        exc_info=None,
    )
    assert f.filter(rec2) is True
