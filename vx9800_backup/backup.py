"""Safe, recursive export of files exposed by an LG VX9800."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Protocol

from .protocol import FileEntry, ProtocolError

MANIFEST_NAME = ".vx9800-backup-manifest.json"
INVALID_WINDOWS_CHARACTERS = re.compile(r'[\x00-\x1f<>:"/\\|?*]')
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


class BackupClient(Protocol):
    def list_directory(self, path: str) -> list[FileEntry]: ...

    def copy_file_to(self, path: str, destination: BinaryIO) -> int: ...


def _safe_relative_path(remote_path: str) -> Path:
    parsed = PurePosixPath(remote_path)
    if parsed.is_absolute() or not parsed.parts or any(
        part in ("", ".", "..") for part in parsed.parts
    ):
        raise ValueError(f"Unsafe phone path: {remote_path!r}")

    safe_parts = []
    for part in parsed.parts:
        safe = INVALID_WINDOWS_CHARACTERS.sub("_", part).rstrip(" .")
        if not safe:
            safe = "_"
        if safe.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
            safe = f"_{safe}"
        safe_parts.append(safe)
    return Path(*safe_parts)


def backup_filesystem(
    client: BackupClient,
    output: Path,
    *,
    overwrite: bool = False,
    resume: bool = False,
    progress=print,
) -> dict:
    """Copy every readable exposed file, preserving the phone's directory tree."""
    if overwrite and resume:
        raise ValueError("--overwrite and --resume cannot be used together")
    output = output.expanduser().resolve()
    if output.exists() and not output.is_dir():
        raise NotADirectoryError(f"Backup destination is not a directory: {output}")
    output.mkdir(parents=True, exist_ok=True)
    if not overwrite and not resume and any(output.iterdir()):
        raise FileExistsError(
            f"Backup destination is not empty: {output}. "
            "Choose an empty folder or pass --overwrite."
        )

    manifest = {
        "device": "LG VX9800",
        "root": "/",
        "files": [],
        "directories": [],
        "errors": [],
    }
    visited_local_paths: dict[str, str] = {}
    pending = [""]

    while pending:
        directory = pending.pop()
        try:
            entries = client.list_directory(directory)
        except (OSError, ProtocolError, TimeoutError) as exc:
            manifest["errors"].append(
                {"path": directory or "/", "error": str(exc)}
            )
            progress(f"Skipped inaccessible directory: {directory or '/'}: {exc}")
            continue

        for entry in entries:
            try:
                relative = _safe_relative_path(entry.path)
            except ValueError as exc:
                manifest["errors"].append({"path": entry.path, "error": str(exc)})
                continue

            local_path = (output / relative).resolve()
            try:
                local_path.relative_to(output)
            except ValueError:
                manifest["errors"].append(
                    {"path": entry.path, "error": "Resolved outside the backup folder"}
                )
                continue

            local_key = str(local_path).casefold()
            if local_path == output / MANIFEST_NAME:
                manifest["errors"].append(
                    {"path": entry.path, "error": "Conflicts with the backup manifest"}
                )
                continue
            previous = visited_local_paths.get(local_key)
            if previous is not None and previous != entry.path:
                manifest["errors"].append(
                    {
                        "path": entry.path,
                        "error": f"Windows filename collides with {previous!r}",
                    }
                )
                continue
            visited_local_paths[local_key] = entry.path

            if entry.is_directory:
                if not local_path.exists():
                    local_path.mkdir(parents=True)
                elif not local_path.is_dir():
                    manifest["errors"].append(
                        {"path": entry.path, "error": "Conflicts with an existing file"}
                    )
                    continue
                manifest["directories"].append(entry.path)
                pending.append(entry.path)
                continue

            if resume and local_path.is_file() and local_path.stat().st_size == entry.size:
                manifest["files"].append(
                    {"path": entry.path, "bytes": entry.size, "resumed": True}
                )
                progress(f"Already saved {entry.path} ({entry.size} bytes)")
                continue

            local_path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary_name = tempfile.mkstemp(
                prefix=f".{local_path.name}.", suffix=".part", dir=local_path.parent
            )
            try:
                with os.fdopen(handle, "wb") as temporary_file:
                    copied = client.copy_file_to(entry.path, temporary_file)
                os.replace(temporary_name, local_path)
            except (OSError, ProtocolError, TimeoutError) as exc:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass
                manifest["errors"].append({"path": entry.path, "error": str(exc)})
                progress(f"Failed to read {entry.path}: {exc}")
                continue

            manifest["files"].append({"path": entry.path, "bytes": copied})
            progress(f"Saved {entry.path} ({copied} bytes)")

    manifest_path = output / MANIFEST_NAME
    with manifest_path.open("w", encoding="utf-8", newline="\n") as manifest_file:
        json.dump(manifest, manifest_file, indent=2, ensure_ascii=True)
        manifest_file.write("\n")
    return manifest
