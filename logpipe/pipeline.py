"""Ingest glue: file -> parser -> enrich -> store, with optional checkpointing."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from logpipe.checkpoint import Checkpoint, read_new_lines
from logpipe.enrich import enrich
from logpipe.models import LogRecord
from logpipe.parsers import PARSERS, detect_format
from logpipe.storage import Store

logger = logging.getLogger(__name__)


@dataclass
class IngestResult:
    parsed: int = 0
    rejected: int = 0
    batches: int = 0
    fmt: str = "nginx"
    sample_rejects: list[str] = field(default_factory=list)


def resolve_format(path: Path, fmt: str) -> str:
    if fmt != "auto":
        if fmt not in PARSERS:
            raise ValueError(f"unknown format {fmt!r}; expected one of {sorted(PARSERS)} or 'auto'")
        return fmt
    with Path(path).open(encoding="utf-8", errors="replace") as fh:
        return detect_format(fh.readline() for _ in range(5))


def parse_batch(lines: list[str], fmt: str, result: IngestResult) -> list[LogRecord]:
    parser = PARSERS[fmt]
    records: list[LogRecord] = []
    for line in lines:
        if not line.strip():
            continue
        record = parser(line)
        if record is None:
            result.rejected += 1
            if len(result.sample_rejects) < 5:
                result.sample_rejects.append(line.rstrip("\n")[:200])
            continue
        records.append(enrich(record))
    result.parsed += len(records)
    return records


def ingest_file(
    path: Path,
    store: Store,
    fmt: str = "auto",
    checkpoint: Checkpoint | None = None,
    batch_size: int = 5000,
    on_batch=None,  # noqa: ANN001 - callable(list[LogRecord]) for follow-mode hooks
) -> IngestResult:
    path = Path(path)
    result = IngestResult(fmt=resolve_format(path, fmt))
    for lines in read_new_lines(path, checkpoint, batch_size=batch_size):
        records = parse_batch(lines, result.fmt, result)
        store.insert(records)
        if on_batch is not None and records:
            on_batch(records)
        result.batches += 1
    logger.info(
        "ingested %s: parsed=%d rejected=%d batches=%d",
        path,
        result.parsed,
        result.rejected,
        result.batches,
    )
    return result
