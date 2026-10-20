"""Optional read-only stats API over the DuckDB store (``pip install '.[api]'``)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query

from logpipe import __version__
from logpipe.report import build_report
from logpipe.storage import Store
from logpipe.timeutil import resolve_window


def create_app(db_path: str | Path | None = None, store: Store | None = None) -> FastAPI:
    """Build the API. Pass ``store`` to share an in-memory store (tests); otherwise a
    fresh read-only connection to ``db_path`` is opened per request so the file can be
    written by ``logpipe ingest``/``follow`` at the same time."""
    app = FastAPI(title="logpipe stats", version=__version__)

    def open_store() -> Store:
        if store is not None:
            return store
        if db_path is None or not Path(db_path).exists():
            raise HTTPException(status_code=503, detail="database not available yet")
        return Store(db_path, read_only=True)

    def close_store(s: Store) -> None:
        if s is not store:
            s.close()

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/v1/stats")
    def stats(
        since: str | None = Query(default=None, description="e.g. 1h, 30m, 2d"),
        until: str | None = Query(default=None),
        top: int = Query(default=10, ge=1, le=100),
        anchor: str = Query(default="latest", pattern="^(latest|now)$"),
    ) -> dict:
        s = open_store()
        try:
            rng = s.time_range()
            anchor_dt = rng[1] if (anchor == "latest" and rng) else datetime.now(timezone.utc)
            try:
                since_dt, until_dt = resolve_window(since, until, anchor_dt)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            report = build_report(s, since_dt, until_dt, top=top)
        finally:
            close_store(s)
        data = report.to_dict()
        data["per_minute"] = [
            {"minute": m.isoformat(), "requests": c, "errors_5xx": e}
            for m, c, e in report.per_minute
        ]
        return data

    @app.get("/v1/alerts")
    def alerts(
        since: str | None = Query(default=None), limit: int = Query(default=50, ge=1, le=500)
    ) -> list[dict]:
        s = open_store()
        try:
            rng = s.time_range()
            anchor_dt = rng[1] if rng else datetime.now(timezone.utc)
            since_dt, _ = resolve_window(since, None, anchor_dt)
            return s.recent_alerts(since_dt, limit)
        finally:
            close_store(s)

    return app


app = create_app(db_path=Path("./logpipe.duckdb"))
