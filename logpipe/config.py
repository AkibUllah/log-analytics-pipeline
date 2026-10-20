"""Settings from environment variables (prefix ``LOGPIPE_``). CLI flags take precedence."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(f"LOGPIPE_{name}", default)


@dataclass
class Settings:
    db_path: Path = field(default_factory=lambda: Path(_env("DB_PATH", "./logpipe.duckdb")))
    checkpoint_path: Path = field(
        default_factory=lambda: Path(_env("CHECKPOINT_PATH", "./logpipe.checkpoint.json"))
    )
    batch_size: int = field(default_factory=lambda: int(_env("BATCH_SIZE", "5000")))
    anomaly_window_minutes: int = field(
        default_factory=lambda: int(_env("ANOMALY_WINDOW_MINUTES", "30"))
    )
    anomaly_z_threshold: float = field(
        default_factory=lambda: float(_env("ANOMALY_Z_THRESHOLD", "3.0"))
    )
    anomaly_min_points: int = field(default_factory=lambda: int(_env("ANOMALY_MIN_POINTS", "10")))
    api_host: str = field(default_factory=lambda: _env("API_HOST", "0.0.0.0"))
    api_port: int = field(default_factory=lambda: int(_env("API_PORT", "8080")))
