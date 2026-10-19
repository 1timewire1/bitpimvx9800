"""Command-line interface for the VX9800 backup utility."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path


def _list_ports() -> int:
    try:
        from serial.tools import list_ports
    except ImportError:
        print(
            "pyserial is required. Install the project dependencies first.",
            file=sys.stderr,
        )
        return 2

    ports = list(list_ports.comports())
    if not ports:
        print("No serial ports found. Pair the phone and enable its PC Bluetooth connection.")
        return 0

    for port in ports:
        details = [port.device, port.description]
        if port.manufacturer:
            details.append(port.manufacturer)
        if port.hwid and port.hwid != "n/a":
            details.append(port.hwid)
        print(" | ".join(details))
    return 0


def _backup(port: str, output: Path | None, baud: int, timeout: float,
            overwrite: bool, resume: bool) -> int:
    try:
        import serial
    except ImportError:
        print(
            "pyserial is required. Install the project dependencies first.",
            file=sys.stderr,
        )
        return 2

    from .backup import backup_filesystem
    from .protocol import BrewClient, ProtocolError

    destination = output or Path(
        f"VX9800-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    )
    try:
        with serial.Serial(
            port=port,
            baudrate=baud,
            timeout=timeout,
            write_timeout=timeout,
            rtscts=False,
            xonxoff=False,
        ) as connection:
            client = BrewClient(connection, timeout=timeout)
            print(f"Connecting to the LG VX9800 on {port}...")
            client.connect()
            print(f"Reading phone files into {destination.resolve()}...")
            manifest = backup_filesystem(
                client, destination, overwrite=overwrite, resume=resume
            )
    except (OSError, serial.SerialException, TimeoutError, ProtocolError, ValueError) as exc:
        print(f"Backup failed: {exc}", file=sys.stderr)
        return 1

    bytes_copied = sum(item["bytes"] for item in manifest["files"])
    print(
        f"Backup finished: {len(manifest['files'])} files, "
        f"{bytes_copied} bytes, {len(manifest['errors'])} errors."
    )
    print(f"Manifest: {(destination / '.vx9800-backup-manifest.json').resolve()}")
    return 1 if manifest["errors"] else 0


def _install_game(
    port: str,
    source: Path,
    app_id: int,
    baud: int,
    timeout: float,
    replace_existing: bool,
) -> int:
    try:
        import serial
    except ImportError:
        print(
            "pyserial is required. Install the project dependencies first.",
            file=sys.stderr,
        )
        return 2

    from .install import install_brew_game
    from .protocol import BrewClient, ProtocolError

    try:
        with serial.Serial(
            port=port,
            baudrate=baud,
            timeout=timeout,
            write_timeout=timeout,
            rtscts=False,
            xonxoff=False,
        ) as connection:
            client = BrewClient(connection, timeout=timeout)
            print(f"Connecting to the LG VX9800 on {port}...")
            client.connect()
            result = install_brew_game(
                client,
                source,
                app_id,
                replace_existing=replace_existing,
                progress=print,
            )
    except (OSError, serial.SerialException, TimeoutError, ProtocolError, ValueError) as exc:
        print(f"Game upload failed: {exc}", file=sys.stderr)
        return 1

    print(
        f"Upload finished: {result['files']} files, {result['bytes']} bytes "
        f"installed under BREW app ID {app_id}."
    )
    return 0


def _diagnose(
    port: str,
    baud: int,
    timeout: float,
    output: Path | None,
    trace_path: Path | None,
    trace_payloads: bool,
) -> int:
    try:
        import serial
    except ImportError:
        print(
            "pyserial is required. Install the project dependencies first.",
            file=sys.stderr,
        )
        return 2

    from .diagnostics import JsonlTrace, run_diagnostics, write_report
    from .protocol import BrewClient, ProtocolError

    trace_file = None
    try:
        if output is not None and trace_path is not None:
            if output.expanduser().resolve() == trace_path.expanduser().resolve():
                raise ValueError("Report output and trace log must be different files")
        if output is not None and output.expanduser().exists():
            raise FileExistsError(f"Refusing to overwrite diagnostic report: {output}")
        if trace_path is not None:
            trace_file = trace_path.expanduser().open("x", encoding="utf-8", newline="\n")
        if trace_payloads:
            print(
                "WARNING: protocol payload logging is enabled; trace may contain "
                "phone file contents, personal data, and device control data.",
                file=sys.stderr,
            )
        with serial.Serial(
            port=port,
            baudrate=baud,
            timeout=timeout,
            write_timeout=timeout,
            rtscts=False,
            xonxoff=False,
        ) as connection:
            client = BrewClient(
                connection,
                timeout=timeout,
                trace=JsonlTrace(trace_file) if trace_file is not None else None,
                trace_payloads=trace_payloads,
            )
            print(f"Connecting to the LG VX9800 on {port}...")
            client.connect()
            print("BREW and diagnostic-mode handshakes succeeded; scanning directories...")
            report = run_diagnostics(
                client, progress=lambda path: print(f"Scanning {path}")
            )
        report["connection"] = {
            "port": port,
            "baud": baud,
            "timeout_seconds": timeout,
        }
        if output is not None:
            with output.expanduser().open("x", encoding="utf-8", newline="\n") as report_file:
                write_report(report_file, report)
        summary = report["summary"]
        print(
            "Diagnostics finished: "
            f"{summary['directories_scanned']} directories, "
            f"{summary['files_listed']} files "
            f"({summary['file_bytes_reported']} bytes reported), "
            f"{summary['errors']} access/protocol errors."
        )
        print(f"Protocol statistics: {report['protocol_stats']}")
        for error in report["errors"]:
            print(f"{error['category']}: {error['path']}: {error['error']}")
        if output is not None:
            print(f"Report: {output.expanduser().resolve()}")
        if trace_path is not None:
            print(f"Protocol trace: {trace_path.expanduser().resolve()}")
        return 0
    except (OSError, serial.SerialException, TimeoutError, ProtocolError, ValueError) as exc:
        print(f"Diagnostics failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if trace_file is not None:
            trace_file.close()


def _screen(port: str, baud: int, timeout: float) -> int:
    try:
        import serial
    except ImportError:
        print(
            "pyserial is required. Install the project dependencies first.",
            file=sys.stderr,
        )
        return 2

    from .protocol import BrewClient, ProtocolError

    try:
        with serial.Serial(
            port=port,
            baudrate=baud,
            timeout=timeout,
            write_timeout=timeout,
            rtscts=False,
            xonxoff=False,
        ) as connection:
            client = BrewClient(connection, timeout=timeout)
            print(f"Connecting to the LG VX9800 on {port}...")
            client.connect()
            diagnostic_version = client.read_diagnostic_version()
            screen = client.read_handset_screen()
    except (OSError, serial.SerialException, TimeoutError, ProtocolError, ValueError) as exc:
        print(f"Screen read failed: {exc}", file=sys.stderr)
        return 1

    print("Handset text display (4 rows x 12 columns):")
    for row in screen.rows:
        print(row)
    print(
        f"Diagnostic version: {diagnostic_version}; "
        f"nonzero character bytes: {screen.nonzero_character_bytes}/48"
    )
    print(
        f"Backlight: {'on' if screen.backlight_on else 'off'}; "
        f"annunciators: 0x{screen.annunciators:04x}; RSSI: {screen.rssi}; "
        f"blink rate: {screen.blink_rate}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vx9800-backup",
        description="Back up or experimentally upload files over Bluetooth serial.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("ports", help="list serial ports (including paired Bluetooth COM ports)")

    backup = commands.add_parser("backup", help="recursively back up readable phone files")
    backup.add_argument("--port", required=True, help="paired phone COM port, for example COM7")
    backup.add_argument("--output", type=Path, help="destination folder (default: timestamped folder)")
    backup.add_argument("--baud", type=int, default=115200)
    backup.add_argument("--timeout", type=float, default=15.0)
    destination_mode = backup.add_mutually_exclusive_group()
    destination_mode.add_argument(
        "--overwrite",
        action="store_true",
        help="allow writing into a non-empty destination folder",
    )
    destination_mode.add_argument(
        "--resume",
        action="store_true",
        help="continue an existing backup, skipping files with matching sizes",
    )

    diagnose = commands.add_parser(
        "diagnose",
        help="run a read-only BREW connection and filesystem diagnostics scan",
    )
    diagnose.add_argument("--port", required=True, help="paired phone COM port, for example COM7")
    diagnose.add_argument("--baud", type=int, default=115200)
    diagnose.add_argument("--timeout", type=float, default=15.0)
    diagnose.add_argument(
        "--output",
        type=Path,
        help="write the diagnostic inventory as JSON (refuses to overwrite)",
    )
    diagnose.add_argument(
        "--trace",
        type=Path,
        help="write a timestamped JSONL protocol trace (refuses to overwrite)",
    )
    diagnose.add_argument(
        "--trace-payloads",
        action="store_true",
        help="include raw frame payloads in the trace (may contain personal data)",
    )

    screen = commands.add_parser(
        "screen",
        help="read the handset's 4x12 text display (read-only)",
    )
    screen.add_argument("--port", required=True, help="paired phone COM port, for example COM7")
    screen.add_argument("--baud", type=int, default=115200)
    screen.add_argument("--timeout", type=float, default=15.0)

    install = commands.add_parser(
        "install-game",
        help="upload a BREW game package to a new, unused app ID",
    )
    install.add_argument("--port", required=True, help="paired phone COM port, for example COM7")
    install.add_argument("--source", type=Path, required=True, help="game package folder")
    install.add_argument("--app-id", type=int, required=True, help="unused numeric BREW app ID")
    install.add_argument("--baud", type=int, default=115200)
    install.add_argument("--timeout", type=float, default=15.0)
    install.add_argument(
        "--replace-existing",
        action="store_true",
        help="replace files for this app ID (the previous game must be backed up)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "ports":
        return _list_ports()
    if args.command == "backup":
        return _backup(
            args.port, args.output, args.baud, args.timeout, args.overwrite, args.resume
        )
    if args.command == "diagnose":
        if args.trace_payloads and args.trace is None:
            parser.error("--trace-payloads requires --trace")
        return _diagnose(
            args.port,
            args.baud,
            args.timeout,
            args.output,
            args.trace,
            args.trace_payloads,
        )
    if args.command == "screen":
        return _screen(args.port, args.baud, args.timeout)
    if args.command == "install-game":
        return _install_game(
            args.port,
            args.source,
            args.app_id,
            args.baud,
            args.timeout,
            args.replace_existing,
        )
    parser.error(f"unknown command: {args.command}")
    return 2
