import pytest

from logpipe.enrich import classify_ua, enrich, normalize_path, status_class
from logpipe.parsers import parse_nginx_combined
from tests.conftest import NGINX_LINE


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/", "/"),
        ("", "/"),
        ("/users/123", "/users/{id}"),
        ("/users/123/orders/456/", "/users/{id}/orders/{id}"),
        ("/api/v1/items/9f1c2d3e-1a2b-4c5d-8e9f-0a1b2c3d4e5f", "/api/v1/items/{uuid}"),
        ("/sessions/3fa85f6457174562b3fc2c963f66afa6", "/sessions/{hash}"),
        ("/products/eid-offer-2026-1234", "/products/{slug}"),
        ("/static/app.8f3a9c1d.js", "/static/{file}.js"),
        ("/static/img/4711.webp", "/static/img/{file}.webp"),
        ("//double//slash/", "/double/slash"),
        (
            "/" + "/".join(f"s{i}" for i in range(15)),
            "/" + "/".join(f"s{i}" for i in range(12)) + "/...",
        ),
    ],
)
def test_normalize_path(raw, expected):
    assert normalize_path(raw) == expected


@pytest.mark.parametrize(
    ("status", "cls"),
    [(200, "2xx"), (304, "3xx"), (404, "4xx"), (503, "5xx"), (0, "other"), (999, "other")],
)
def test_status_class(status, cls):
    assert status_class(status) == cls


def test_classify_ua():
    assert classify_ua(None) == ("unknown", "Unknown", False)
    assert classify_ua("-") == ("unknown", "Unknown", False)
    assert classify_ua("Mozilla/5.0 (compatible; Googlebot/2.1)") == ("bot", "Bot", True)
    assert classify_ua("python-requests/2.31.0")[2] is True
    device, browser, is_bot = classify_ua(
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1"
    )
    assert (device, browser, is_bot) == ("mobile", "Safari", False)
    assert classify_ua("Mozilla/5.0 (iPad; CPU OS 16_6 like Mac OS X) Safari/604.1")[0] == "tablet"
    edge = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Edg/124.0"
    assert classify_ua(edge)[1] == "Edge"


def test_enrich_fills_derived_fields():
    rec = enrich(parse_nginx_combined(NGINX_LINE))
    assert rec.path_normalized == "/api/v1/orders/{id}"
    assert rec.status_class == "2xx"
    assert (rec.device, rec.browser, rec.is_bot) == ("mobile", "Chrome", False)
