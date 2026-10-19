"""Read-only diagnostics for an LG VX9800 BREW filesystem connection."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Callable, TextIO

from .protocol import BrewClient, PhoneAccessError, ProtocolError


class JsonlTrace:
    """Write timestamped protocol events without payloads unless explicitly enabled."""

    def __init__(self, output: TextIO) -> None:
        self.output = output

    def __call__(self, event: dict[str, object]) -> None:
        self.output.write(json.dumps(event, sort_keys=True) + "\n")
        self.output.flush()


def run_diagnostics(
    client: BrewClient,
    *,
    progress: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """Inventory directory entries and file sizes without reading file contents."""
    directories: list[str] = []
    files: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []
    pending = [""]
    visited = {""}

    while pending:
        directory = pending.pop()
        directories.append(directory or "/")
        if progress is not None:
            progress(directory or "/")
        try:
            entries = client.list_directory(directory)
        except (OSError, ProtocolError, TimeoutError) as exc:
            if isinstance(exc, TimeoutError):
                category = "timeout"
            elif isinstance(exc, PhoneAccessError):
                category = "access_denied"
            else:
                category = "protocol_error"
            errors.append(
                {"path": directory or "/", "category": category, "error": str(exc)}
            )
            continue

        for entry in entries:
            if entry.is_directory:
                if entry.path not in visited:
                    visited.add(entry.path)
                    pending.append(entry.path)
            else:
                files.append({"path": entry.path, "size": entry.size})

    error_counts = {
        category: sum(error["category"] == category for error in errors)
        for category in ("access_denied", "timeout", "protocol_error")
    }
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "device": "LG VX9800",
        "protocol": "BREW v2 over Bluetooth serial",
        "handshake": "success",
        "read_only": True,
        "summary": {
            "directories_scanned": len(directories),
            "files_listed": len(files),
            "file_bytes_reported": sum(int(file["size"]) for file in files),
            "errors": len(errors),
            **error_counts,
        },
        "directories": directories,
        "files": files,
        "errors": errors,
        "protocol_stats": dict(client.stats),
    }


def write_report(output: TextIO, report: dict[str, object]) -> None:
    json.dump(report, output, indent=2, sort_keys=True)
    output.write("\n")
