"""Incremental file reading with a JSON checkpoint (offset + inode) for resume and rotation."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class FileState:
    inode: int
    offset: int
    size: int
    lines: int = 0
    updated_at: str = ""


@dataclass
class Checkpoint:
    path: Path
    files: dict[str, FileState] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Checkpoint:
        cp = cls(path=Path(path))
        if cp.path.exists():
            data = json.loads(cp.path.read_text() or "{}")
            cp.files = {k: FileState(**v) for k, v in data.get("files", {}).items()}
        return cp

    def save(self) -> None:
        payload = {"version": 1, "files": {k: asdict(v) for k, v in self.files.items()}}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, self.path)  # atomic on POSIX

    def state_for(self, log_path: Path) -> FileState | None:
        return self.files.get(str(Path(log_path).resolve()))

    def update(self, log_path: Path, inode: int, offset: int, size: int, lines: int) -> None:
        key = str(Path(log_path).resolve())
        previous = self.files.get(key)
        self.files[key] = FileState(
            inode=inode,
            offset=offset,
            size=size,
            lines=(previous.lines if previous else 0) + lines,
            updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    def reset(self, log_path: Path) -> None:
        self.files.pop(str(Path(log_path).resolve()), None)


def start_offset(log_path: Path, checkpoint: Checkpoint | None) -> int:
    """Where to resume. Rotation (new inode) or truncation (file shrank) restarts from 0."""
    if checkpoint is None:
        return 0
    state = checkpoint.state_for(log_path)
    if state is None:
        return 0
    stat = os.stat(log_path)
    if stat.st_ino != state.inode or stat.st_size < state.offset:
        return 0
    return state.offset


def read_new_lines(
    log_path: Path, checkpoint: Checkpoint | None, batch_size: int = 5000
) -> Iterator[list[str]]:
    """Yield batches of complete new lines since the checkpoint, updating it after each batch.

    A trailing partial line (no newline yet, typical while a writer is mid-line) is
    left unread so the next pass picks it up whole.
    """
    log_path = Path(log_path)
    offset = start_offset(log_path, checkpoint)
    stat = os.stat(log_path)
    with log_path.open("rb") as fh:
        fh.seek(offset)
        batch: list[str] = []
        consumed = offset
        while True:
            raw = fh.readline()
            if not raw:
                break
            if not raw.endswith(b"\n"):
                break  # partial line, wait for the writer to finish it
            consumed += len(raw)
            batch.append(raw.decode("utf-8", errors="replace"))
            if len(batch) >= batch_size:
                yield batch
                if checkpoint is not None:
                    checkpoint.update(log_path, stat.st_ino, consumed, stat.st_size, len(batch))
                    checkpoint.save()
                batch = []
        if batch:
            yield batch
            if checkpoint is not None:
                checkpoint.update(log_path, stat.st_ino, consumed, stat.st_size, len(batch))
                checkpoint.save()
