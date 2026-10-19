# LG VX9800 backup utility

This is a focused Python 3 utility for Windows 11. It uses the VX9800's
Bluetooth Serial Port Profile and BREW v2 filesystem protocol to copy readable
files into a local folder, run read-only diagnostics, and experimentally upload
a BREW game package.

## Prepare the phone and Windows

1. On the phone, open **Settings & Tools > PC Connection > BT**.
2. Enable Bluetooth visibility, then pair the phone in Windows Bluetooth settings.
3. In Windows Bluetooth device settings, find the phone's outgoing serial
   (SPP) COM port.
4. Keep the phone powered on and near the PC. Bluetooth transfers can be slow.

## Install and run

Install Python 3.11 or later, then create a project-local environment and
install the utility from the repository root:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\python.exe -m vx9800_backup ports
.\.venv\Scripts\python.exe -m vx9800_backup backup --port COM7 --output .\VX9800-backup
```

Replace `COM7` with the outgoing COM port shown by the `ports` command or
Windows. If the destination folder already contains files, the utility refuses
to write there unless `--overwrite` or `--resume` is supplied. Use `--resume`
to continue an interrupted backup; it skips files already present with the
same size reported by the phone and retries missing or incomplete files.
`--resume` and `--overwrite` cannot be used together.

## Read-only diagnostics and protocol tracing

The `diagnose` command checks the Bluetooth serial connection and BREW
handshakes, then recursively inventories accessible directories and file sizes.
It does not read file contents or write to the phone. The JSON report includes
accessible directories, listed files and sizes, access-denied/time-out paths,
and protocol retry/error counters. A full directory scan can take time over
Bluetooth.

```powershell
.\.venv\Scripts\python.exe -m vx9800_backup diagnose --port COM7 --output .\vx9800-diagnostics.json
```

Use `--trace` to write a timestamped JSONL protocol trace containing command
names, frame lengths, CRC results, timings, and errors. Raw payloads are omitted
by default. `--trace-payloads` explicitly includes them; traces may then contain
phone file contents, personal data, and device control data. Both output paths
refuse to overwrite existing files.

```powershell
.\.venv\Scripts\python.exe -m vx9800_backup diagnose --port COM7 `
  --output .\vx9800-diagnostics.json --trace .\vx9800-trace.jsonl
```

Only enable raw payload logging when needed and store the trace securely.

## Read the handset text display

The `screen` command reads the standard DIAG version and then sends Qualcomm's
handset-display request. It prints the phone's 4-by-12 character display,
basic status, and the number of nonzero character bytes without exposing raw
screen data. It is read-only; it does not emulate keypresses, capture LCD
pixels, or write files.

```powershell
.\.venv\Scripts\python.exe -m vx9800_backup screen --port COM7
```

Support depends on the phone's firmware and whether its Bluetooth diagnostic
channel exposes this command. A rejected or unsupported response is reported
as an error; it does not fall back to sending other commands.

## Experimental game upload

The `install-game` command writes to the phone. By default it requires a new
numeric app ID absent from both `brew/mod` and `brew/mif`, and refuses to
overwrite an existing ID. `--replace-existing` instead deletes files under the
selected app ID before uploading; use it only after verifying a backup. The
module files are uploaded under `brew/mod/<app-id>/`, and the MIF is written to
`brew/mif/<app-id>.mif` last. This minimizes the chance of a partially uploaded
game appearing as installed, but does not guarantee that the phone will list or
run an unofficial package.

```powershell
.\.venv\Scripts\python.exe -m vx9800_backup install-game --port COM7 --source C:\path\to\game --app-id 50000
```

Any game save files created by the installed app are expected to be in or near
`brew/mod/<app-id>/`, but the game may use a different location.

The backup includes a `.vx9800-backup-manifest.json` file. Any directories or
files the phone refuses to expose are listed there and make the command return
an error status, so a partial backup is not presented as complete.

This backs up files the phone exposes through its BREW filesystem, including
the microSD tree when the phone exposes it (usually `mmc1`). It cannot image
protected internal flash or firmware. The backup utility has been used with a
physical VX9800 over Bluetooth; access varies by path, and experimental game
uploads are not guaranteed to appear or run.
