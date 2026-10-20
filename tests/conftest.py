from __future__ import annotations

from datetime import datetime, timezone

import pytest

from logpipe.enrich import enrich
from logpipe.generator import generate_lines
from logpipe.models import LogRecord
from logpipe.parsers import parse_nginx_combined
from logpipe.storage import Store

END = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)

NGINX_LINE = (
    '103.108.14.17 - - [20/Oct/2026:17:45:12 +0600] "GET /api/v1/orders/42?expand=items HTTP/1.1" '
    '200 1532 "https://shop.example.com.bd/cart" "Mozilla/5.0 (Linux; Android 13; SM-A546E) '
    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Mobile Safari/537.36" 0.123'
)


@pytest.fixture
def sample_lines() -> list[str]:
    return list(generate_lines(3000, seed=7, end=END, duration_minutes=180, incident=True))


@pytest.fixture
def sample_records(sample_lines) -> list[LogRecord]:
    records = []
    for line in sample_lines:
        rec = parse_nginx_combined(line)
        assert rec is not None
        records.append(enrich(rec))
    return records


@pytest.fixture
def store(sample_records) -> Store:
    s = Store(":memory:")
    s.insert(sample_records)
    yield s
    s.close()


@pytest.fixture
def sample_log(tmp_path, sample_lines):
    path = tmp_path / "access.log"
    path.write_text("\n".join(sample_lines) + "\n")
    return path
