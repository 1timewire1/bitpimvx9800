"""Upload BREW game packages under a selected BREW app ID."""

from __future__ import annotations

from pathlib import Path

from .protocol import BrewClient, ProtocolError


def install_brew_game(
    client: BrewClient,
    source: Path,
    app_id: int,
    *,
    replace_existing: bool = False,
    progress=print,
) -> dict[str, int]:
    if not 1 <= app_id <= 0xFFFFFFFF:
        raise ValueError("BREW app ID must be a positive 32-bit integer")
    root = source.expanduser().resolve()
    mif_directory = root / "mif"
    mod_directory = root / "mod"
    if not mif_directory.is_dir() or not mod_directory.is_dir():
        raise ValueError("Game folder must contain mif and mod directories")
    mif_files = [path for path in mif_directory.iterdir() if path.is_file()]
    mod_directories = [path for path in mod_directory.iterdir() if path.is_dir()]
    if len(mif_files) != 1 or len(mod_directories) != 1:
        raise ValueError("Game package must contain one MIF file and one module directory")
    module_root = mod_directories[0]
    files = []
    for local_path in sorted(module_root.rglob("*")):
        if local_path.is_symlink():
            raise ValueError(f"Game package cannot contain symbolic links: {local_path}")
        if local_path.is_file():
            files.append((local_path, local_path.relative_to(module_root).as_posix()))
    if not files:
        raise ValueError("Game package contains no module files")

    module_path = f"brew/mod/{app_id}"
    mif_path = f"brew/mif/{app_id}.mif"
    existing_modules = {
        entry.path.rsplit("/", 1)[-1] for entry in client.list_directory("brew/mod")
    }
    existing_mifs = {
        entry.path.rsplit("/", 1)[-1] for entry in client.list_directory("brew/mif")
    }
    module_exists = str(app_id) in existing_modules
    mif_exists = f"{app_id}.mif" in existing_mifs
    if (module_exists or mif_exists) and not replace_existing:
        raise FileExistsError(f"BREW app ID {app_id} is already present on the phone")

    if module_exists:
        existing_entries = client.list_directory(module_path)
        if any(entry.is_directory for entry in existing_entries):
            raise ValueError("Replacing an app with subdirectories is not supported")
        for entry in existing_entries:
            progress(f"Removing existing {entry.path}")
            client._remove_file(entry.path)
    else:
        client._create_directory(module_path)

    total_bytes = 0
    uploaded = []
    for local_path, relative_path in files:
        phone_path = f"{module_path}/{relative_path}"
        size = local_path.stat().st_size
        progress(f"Uploading {phone_path} ({size} bytes)")
        with local_path.open("rb") as source_file:
            copied = client.write_file_from(phone_path, source_file, size)
        _, phone_size = client._stat_file(phone_path)
        if phone_size != size:
            raise ProtocolError(
                f"Uploaded {phone_path} but phone reports {phone_size} of {size} bytes"
            )
        total_bytes += copied
        uploaded.append(phone_path)

    mif_source = mif_files[0]
    mif_size = mif_source.stat().st_size
    if mif_exists:
        progress(f"Removing existing {mif_path}")
        client._remove_file(mif_path)
    progress(f"Uploading {mif_path} ({mif_size} bytes; registering last)")
    with mif_source.open("rb") as source_file:
        copied = client.write_file_from(mif_path, source_file, mif_size)
    _, phone_size = client._stat_file(mif_path)
    if phone_size != mif_size:
        raise ProtocolError(
            f"Uploaded {mif_path} but phone reports {phone_size} of {mif_size} bytes"
        )
    total_bytes += copied
    uploaded.append(mif_path)
    return {"files": len(uploaded), "bytes": total_bytes}
