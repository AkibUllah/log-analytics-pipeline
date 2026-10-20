"""``logpipe`` command line interface."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import typer
from rich.console import Console

from logpipe import __version__
from logpipe.anomaly import AnomalyMonitor
from logpipe.checkpoint import Checkpoint
from logpipe.config import Settings
from logpipe.follow import Follower
from logpipe.generator import generate_lines
from logpipe.pipeline import ingest_file
from logpipe.report import build_report, render_html, render_text
from logpipe.storage import Store
from logpipe.timeutil import resolve_window

app = typer.Typer(
    help="Access-log analytics: ingest nginx/JSON logs into DuckDB, report, follow, alert.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console(soft_wrap=True)
err_console = Console(stderr=True, soft_wrap=True)
settings = Settings()


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _version(value: bool) -> None:
    if value:
        console.print(f"logpipe {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool | None = typer.Option(  # noqa: UP007
        None, "--version", callback=_version, is_eager=True, help="Show version and exit."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging."),
) -> None:
    _configure_logging(verbose)


DbOption = typer.Option(
    None, "--db", help="DuckDB file (default: LOGPIPE_DB_PATH or ./logpipe.duckdb)."
)


@app.command()
def ingest(
    path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    fmt: str = typer.Option("auto", "--format", "-f", help="nginx | json | auto"),
    db: Path | None = DbOption,  # noqa: UP007
    checkpoint: bool = typer.Option(
        True, "--checkpoint/--no-checkpoint", help="Resume from the last offset for this file."
    ),
    checkpoint_path: Path | None = typer.Option(None, "--checkpoint-file"),  # noqa: UP007
    batch_size: int = typer.Option(settings.batch_size, "--batch-size", min=100),
) -> None:
    """Parse a log file incrementally and store enriched requests in DuckDB."""
    cp = Checkpoint.load(checkpoint_path or settings.checkpoint_path) if checkpoint else None
    with Store(db or settings.db_path) as store:
        result = ingest_file(path, store, fmt=fmt, checkpoint=cp, batch_size=batch_size)
        total = store.count()
    console.print(
        f"[green]ingested[/] {result.parsed:,} records from {path} "
        f"(format={result.fmt}, rejected={result.rejected:,}, batches={result.batches}); "
        f"store now holds {total:,} rows"
    )
    for sample in result.sample_rejects:
        err_console.print(f"  [yellow]rejected:[/] {sample}")


@app.command()
def follow(
    path: Path = typer.Argument(..., help="Log file to tail (may not exist yet)."),
    fmt: str = typer.Option("auto", "--format", "-f"),
    db: Path | None = DbOption,  # noqa: UP007
    interval: float = typer.Option(1.0, "--interval", min=0.1, help="Poll interval in seconds."),
    checkpoint_path: Path | None = typer.Option(None, "--checkpoint-file"),  # noqa: UP007
    window: int = typer.Option(settings.anomaly_window_minutes, "--window", min=2),
    threshold: float = typer.Option(settings.anomaly_z_threshold, "--z", min=1.0),
    min_points: int = typer.Option(settings.anomaly_min_points, "--min-points", min=2),
) -> None:
    """Tail a file like ``tail -F``, ingest new lines and emit anomaly alerts per minute."""
    cp = Checkpoint.load(checkpoint_path or settings.checkpoint_path)
    monitor = AnomalyMonitor(window=window, threshold=threshold, min_points=min_points)

    def on_alert(alert) -> None:  # noqa: ANN001
        err_console.print(f"[bold red]ALERT[/] {alert.describe()}")

    def on_records(records) -> None:  # noqa: ANN001
        console.print(f"[dim]{datetime.now():%H:%M:%S}[/] +{len(records):,} records")

    with Store(db or settings.db_path) as store:
        follower = Follower(
            path, store, cp, monitor, fmt=fmt, poll_interval=interval,
            on_alert=on_alert, on_records=on_records,
        )  # fmt: skip
        console.print(f"following {path} (poll every {interval}s, z>{threshold}); Ctrl-C to stop")
        try:
            asyncio.run(follower.run())
        except KeyboardInterrupt:
            console.print(
                f"\nstopped after {follower.records_seen:,} records, {len(follower.alerts)} alerts"
            )


@app.command()
def report(
    since: str | None = typer.Option(None, "--since", help="e.g. 1h, 30m, 2d or ISO timestamp"),  # noqa: UP007
    until: str | None = typer.Option(None, "--until", help="e.g. 10m or ISO timestamp"),  # noqa: UP007
    anchor: str = typer.Option(
        "latest", "--anchor", help="What '--since' is relative to: 'latest' record or 'now'."
    ),
    db: Path | None = DbOption,  # noqa: UP007
    top: int = typer.Option(10, "--top", min=1, max=100),
    html_out: Path | None = typer.Option(None, "--html", help="Write an HTML report here."),  # noqa: UP007
    json_out: bool = typer.Option(False, "--json", help="Print the report as JSON."),
    window: int = typer.Option(settings.anomaly_window_minutes, "--window", min=2),
    threshold: float = typer.Option(settings.anomaly_z_threshold, "--z", min=1.0),
) -> None:
    """Summarise traffic, latency percentiles, errors, top endpoints/IPs and anomalies."""
    if anchor not in ("latest", "now"):
        raise typer.BadParameter("anchor must be 'latest' or 'now'")
    db_path = db or settings.db_path
    if not Path(db_path).exists():
        err_console.print(f"[red]no database at {db_path}; run `logpipe ingest` first[/]")
        raise typer.Exit(code=1)
    with Store(db_path, read_only=True) as store:
        rng = store.time_range()
        anchor_dt = rng[1] if (anchor == "latest" and rng) else datetime.now(timezone.utc)
        try:
            since_dt, until_dt = resolve_window(since, until, anchor_dt)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        rep = build_report(
            store, since_dt, until_dt, top=top, anomaly_window=window, anomaly_threshold=threshold,
            anomaly_min_points=min(settings.anomaly_min_points, window),
        )  # fmt: skip
    if html_out:
        html_out.write_text(render_html(rep))
        console.print(f"[green]wrote[/] {html_out}")
    if json_out:
        console.print_json(json.dumps(rep.to_dict(), default=str))
    elif not html_out or sys.stdout.isatty():
        typer.echo(render_text(rep))


@app.command("generate-sample")
def generate_sample(
    lines: int = typer.Option(50_000, "--lines", "-n", min=1),
    out: Path = typer.Option(Path("data/access.log"), "--out", "-o"),
    fmt: str = typer.Option("nginx", "--format", "-f", help="nginx | json"),
    seed: int = typer.Option(42, "--seed"),
    minutes: int = typer.Option(24 * 60, "--minutes", min=2, help="Time span covered."),
    end: str | None = typer.Option(
        None, "--end", help="ISO timestamp of the last line (default now)."
    ),  # noqa: UP007
    no_incident: bool = typer.Option(
        False, "--no-incident", help="Skip the 5xx burst and flash crowd."
    ),
) -> None:
    """Write a realistic synthetic access log (daily curve, bots, a 5xx incident, a flash crowd)."""
    end_dt = None
    if end:
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for line in generate_lines(
            lines,
            fmt=fmt,
            seed=seed,
            end=end_dt,
            duration_minutes=minutes,
            incident=not no_incident,
        ):
            fh.write(line + "\n")
    console.print(f"[green]wrote[/] {lines:,} {fmt} lines spanning {minutes} minutes to {out}")


@app.command()
def export(
    out: Path = typer.Argument(..., help="Destination .parquet file"),
    since: str | None = typer.Option(None, "--since"),  # noqa: UP007
    until: str | None = typer.Option(None, "--until"),  # noqa: UP007
    db: Path | None = DbOption,  # noqa: UP007
) -> None:
    """Export (a window of) the requests table to Parquet."""
    with Store(db or settings.db_path, read_only=True) as store:
        rng = store.time_range()
        anchor_dt = rng[1] if rng else datetime.now(timezone.utc)
        since_dt, until_dt = resolve_window(since, until, anchor_dt)
        rows = store.export_parquet(out, since_dt, until_dt)
    console.print(f"[green]exported[/] {rows:,} rows to {out}")


@app.command()
def serve(
    host: str = typer.Option(settings.api_host, "--host"),
    port: int = typer.Option(settings.api_port, "--port"),
    db: Path | None = DbOption,  # noqa: UP007
) -> None:
    """Serve the read-only stats API (requires the ``api`` extra)."""
    try:
        import uvicorn

        from logpipe.api import create_app
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        err_console.print(
            f"[red]install the api extra: pip install 'log-analytics-pipeline[api]' ({exc})[/]"
        )
        raise typer.Exit(code=1) from exc
    uvicorn.run(create_app(db_path=db or settings.db_path), host=host, port=port)


if __name__ == "__main__":  # pragma: no cover
    app()
