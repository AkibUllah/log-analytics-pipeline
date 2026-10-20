from datetime import datetime, timezone

from logpipe.parsers import (
    detect_format,
    parse_json_line,
    parse_lines,
    parse_nginx_combined,
)
from tests.conftest import NGINX_LINE


def test_nginx_combined_with_request_time():
    rec = parse_nginx_combined(NGINX_LINE)
    assert rec is not None
    assert rec.ip == "103.108.14.17"
    assert rec.ts == datetime(2026, 10, 20, 11, 45, 12, tzinfo=timezone.utc)  # +0600 -> UTC
    assert (rec.method, rec.path, rec.query) == ("GET", "/api/v1/orders/42", "expand=items")
    assert rec.protocol == "HTTP/1.1"
    assert (rec.status, rec.bytes_sent) == (200, 1532)
    assert rec.referrer == "https://shop.example.com.bd/cart"
    assert rec.user_agent.startswith("Mozilla/5.0 (Linux; Android 13")
    assert rec.latency_ms == 123.0


def test_nginx_combined_without_request_time_and_dash_fields():
    line = '203.0.113.9 - - [20/Oct/2026:10:15:32 +0000] "POST /login HTTP/2.0" 302 - "-" "-"'
    rec = parse_nginx_combined(line)
    assert rec is not None
    assert rec.bytes_sent == 0
    assert rec.referrer is None
    assert rec.user_agent is None
    assert rec.latency_ms is None
    assert rec.protocol == "HTTP/2.0"


def test_nginx_common_format_without_referrer_and_ua():
    line = '127.0.0.1 - frank [10/Oct/2026:13:55:36 -0700] "GET /apache_pb.gif HTTP/1.0" 200 2326'
    rec = parse_nginx_combined(line)
    assert rec is not None
    assert rec.path == "/apache_pb.gif"
    assert rec.ts.hour == 20  # -0700 -> UTC


def test_nginx_malformed_request_line_is_kept_as_unknown():
    line = '1.2.3.4 - - [20/Oct/2026:10:15:32 +0000] "-" 400 0 "-" "-"'
    rec = parse_nginx_combined(line)
    assert rec is not None
    assert rec.method == "UNKNOWN"
    assert rec.status == 400


def test_nginx_garbage_returns_none():
    assert parse_nginx_combined("this is not a log line") is None
    assert parse_nginx_combined("") is None
    assert parse_nginx_combined('1.2.3.4 - - [not a date] "GET / HTTP/1.1" 200 1') is None


def test_json_line_nginx_style_keys():
    line = (
        '{"time":"2026-10-20T11:45:12.250Z","remote_addr":"10.0.0.5","method":"GET",'
        '"path":"/api/v1/products","query":"page=2","status":200,"body_bytes_sent":812,'
        '"http_referer":null,"http_user_agent":"curl/8.5.0","request_time":0.045,'
        '"host":"shop.example.com.bd","upstream":"app-1"}'
    )
    rec = parse_json_line(line)
    assert rec is not None
    assert rec.ts == datetime(2026, 10, 20, 11, 45, 12, 250000, tzinfo=timezone.utc)
    assert (rec.path, rec.query) == ("/api/v1/products", "page=2")
    assert rec.latency_ms == 45.0
    assert rec.host == "shop.example.com.bd"
    assert rec.extra == {"upstream": "app-1"}


def test_json_line_app_style_keys_and_epoch_ms():
    line = (
        '{"timestamp": 1760960712250, "client_ip": "1.1.1.1", '
        '"request": "POST /orders?x=1 HTTP/1.1", "status_code": "201", "bytes": "99", '
        '"duration_ms": 12.5, "ua": "test-agent"}'
    )
    rec = parse_json_line(line)
    assert rec is not None
    assert rec.ts.year == 2025
    assert (rec.method, rec.path, rec.query) == ("POST", "/orders", "x=1")
    assert (rec.status, rec.bytes_sent, rec.latency_ms) == (201, 99, 12.5)
    assert rec.user_agent == "test-agent"


def test_json_line_rejects_invalid_or_timestampless():
    assert parse_json_line("{not json") is None
    assert parse_json_line('{"path": "/x", "status": 200}') is None
    assert parse_json_line("[1, 2]") is None
    assert parse_json_line("") is None


def test_detect_format_and_parse_lines():
    assert detect_format(['{"time": "2026-01-01T00:00:00Z"}']) == "json"
    assert detect_format(["", NGINX_LINE]) == "nginx"
    assert detect_format([]) == "nginx"
    parsed = list(parse_lines([NGINX_LINE, "", "garbage"], fmt="nginx"))
    assert len(parsed) == 2 and parsed[0] is not None and parsed[1] is None
