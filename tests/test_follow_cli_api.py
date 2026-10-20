from __future__ import annotations

import asyncio
import json
from datetime import timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from logpipe.anomaly import AnomalyMonitor
from logpipe.api import create_app
from logpipe.checkpoint import Checkpoint
from logpipe.cli import app
from logpipe.follow import Follower
from logpipe.generator import generate_lines
from logpipe.storage import Store
from tests.conftest import END

runner = CliRunner()


def _nginx(ts, status=200):
    stamp = ts.astimezone(timezone(timedelta(hours=6))).strftime("%d/%b/%Y:%H:%M:%S %z")
    return f'10.0.0.1 - - [{stamp}] "GET /x HTTP/1.1" {status} 10 "-" "ua" 0.010\n'


async def test_follower_ingests_incrementally_and_alerts(tmp_path):
    log = tmp_path / "live.log"
    store = Store(":memory:")
    monitor = AnomalyMonitor(window=10, threshold=3.0, min_points=3)
    seen_alerts = []
    follower = Follower(
        log, store, Checkpoint.load(tmp_path / "cp.json"), monitor,
        fmt="nginx", poll_interval=0.01, on_alert=seen_alerts.append,
    )  # fmt: skip
    assert await follower.poll_once() == 0  # file does not exist yet

    base = END
    with log.open("w") as fh:
        for minute in range(6):
            for second in range(0, 60, 6):  # 10 req/min baseline
                fh.write(_nginx(base + timedelta(minutes=minute, seconds=second)))
    assert await follower.poll_once() == 60
    assert store.count() == 60 and not follower.alerts

    with log.open("a") as fh:
        for second in range(60):  # 60 req/min with 50% 5xx, then one more minute to close it
            fh.write(
                _nginx(base + timedelta(minutes=6, seconds=second), 503 if second % 2 else 200)
            )
        fh.write(_nginx(base + timedelta(minutes=7)))
    assert await follower.poll_once() == 61
    assert {a.metric for a in seen_alerts} == {"request_rate", "error_rate"}
    assert store.recent_alerts()[0]["severity"] == "critical"

    # run() honours max_polls and stop()
    task = asyncio.create_task(follower.run(max_polls=2))
    await task
    follower.stop()
    await follower.run()
    store.close()


def test_cli_end_to_end(tmp_path):
    db = tmp_path / "t.duckdb"
    cp = tmp_path / "cp.json"
    log = tmp_path / "access.log"
    res = runner.invoke(
        app,
        [
            "generate-sample",
            "--lines",
            "1500",
            "--minutes",
            "120",
            "--out",
            str(log),
            "--end",
            END.isoformat(),
        ],
    )
    assert res.exit_code == 0, res.output
    assert log.read_text().count("\n") == 1500

    res = runner.invoke(app, ["ingest", str(log), "--db", str(db), "--checkpoint-file", str(cp)])
    assert res.exit_code == 0, res.output
    assert "ingested 1,500 records" in res.output

    res = runner.invoke(app, ["ingest", str(log), "--db", str(db), "--checkpoint-file", str(cp)])
    assert "ingested 0 records" in res.output  # checkpoint resume

    res = runner.invoke(app, ["report", "--db", str(db), "--since", "1h", "--window", "20"])
    assert res.exit_code == 0, res.output
    assert "Requests" in res.output and "Top endpoints" in res.output

    html_out = tmp_path / "r.html"
    res = runner.invoke(app, ["report", "--db", str(db), "--html", str(html_out)])
    assert res.exit_code == 0 and html_out.exists() and "<svg" in html_out.read_text()

    res = runner.invoke(app, ["report", "--db", str(db), "--json", "--since", "30m"])
    assert res.exit_code == 0
    payload = json.loads(res.output[res.output.index("{") :])
    assert payload["total_requests"] > 0 and len(payload["per_minute"]) in (30, 31)

    pq = tmp_path / "out.parquet"
    res = runner.invoke(app, ["export", str(pq), "--db", str(db), "--since", "10m"])
    assert res.exit_code == 0 and pq.exists()

    res = runner.invoke(app, ["report", "--db", str(tmp_path / "missing.duckdb")])
    assert res.exit_code == 1
    res = runner.invoke(app, ["report", "--db", str(db), "--since", "soon"])
    assert res.exit_code != 0
    res = runner.invoke(app, ["--version"])
    assert res.exit_code == 0 and "0.1.0" in res.output


def test_cli_json_format_and_rejects(tmp_path):
    log = tmp_path / "app.json.log"
    lines = list(generate_lines(300, fmt="json", seed=5, end=END, duration_minutes=30))
    log.write_text("\n".join(lines) + "\nnot json at all\n")
    res = runner.invoke(
        app, ["ingest", str(log), "--db", str(tmp_path / "j.duckdb"), "--no-checkpoint"]
    )
    assert res.exit_code == 0, res.output
    assert "format=json" in res.output and "rejected=1" in res.output
    assert "rejected: not json at all" in res.output


@pytest.fixture
def api_client(store):
    return TestClient(create_app(store=store))


def test_api_health_and_stats(api_client):
    assert api_client.get("/health").json()["status"] == "ok"
    body = api_client.get("/v1/stats", params={"since": "1h", "top": 3}).json()
    assert body["total_requests"] > 0
    assert len(body["top_endpoints"]) == 3
    assert 60 <= len(body["per_minute"]) <= 61
    assert {"minute", "requests", "errors_5xx"} <= set(body["per_minute"][0])
    assert api_client.get("/v1/stats", params={"since": "bogus"}).status_code == 422
    assert api_client.get("/v1/stats", params={"anchor": "yesterday"}).status_code == 422


def test_api_alerts_and_missing_db(api_client, tmp_path):
    assert api_client.get("/v1/alerts").json() == []
    missing = TestClient(create_app(db_path=tmp_path / "nope.duckdb"))
    assert missing.get("/v1/stats").status_code == 503
