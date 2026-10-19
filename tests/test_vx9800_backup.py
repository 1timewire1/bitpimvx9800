from __future__ import annotations

import io
import json
import struct
import tempfile
import unittest
from pathlib import Path

from vx9800_backup.backup import MANIFEST_NAME, backup_filesystem
from vx9800_backup.diagnostics import JsonlTrace, run_diagnostics
from vx9800_backup.install import install_brew_game
from vx9800_backup.protocol import (
    BrewClient,
    FileEntry,
    PhoneAccessError,
    ProtocolError,
    crc16_ppp,
    decode_frame,
    encode_path,
    escape_packet,
    frame,
    unescape_packet,
)


class FakeTransport:
    def __init__(self, response: bytes = b"") -> None:
        self.response = bytearray(response)
        self.written = bytearray()

    def write(self, data: bytes) -> int:
        self.written.extend(data)
        return len(data)

    def read(self, size: int = 1) -> bytes:
        data = bytes(self.response[:size])
        del self.response[:size]
        return data


class FakeBackupClient:
    def __init__(self) -> None:
        self.directories = {
            "": [FileEntry("brew", True), FileEntry("readme.txt", False, 5)],
            "brew": [
                FileEntry("brew/empty", True),
                FileEntry("brew/data.bin", False, 3),
            ],
            "brew/empty": [],
        }
        self.files = {"readme.txt": b"hello", "brew/data.bin": b"\x00\x01\xff"}

    def list_directory(self, path: str) -> list[FileEntry]:
        return self.directories[path]

    def copy_file_to(self, path: str, destination: io.BufferedIOBase) -> int:
        data = self.files[path]
        destination.write(data)
        return len(data)


class ProtocolTests(unittest.TestCase):
    def test_crc_matches_ppp_fcs_reference_vector(self) -> None:
        self.assertEqual(crc16_ppp(b"123456789"), 0x906E)

    def test_frame_round_trip_escapes_reserved_bytes(self) -> None:
        payload = b"\x59\x7d\x7e\x00"
        encoded = frame(payload)
        self.assertNotIn(b"\x7d\x7e", encoded)
        self.assertEqual(decode_frame(encoded), payload)

    def test_escape_and_unescape_round_trip(self) -> None:
        payload = bytes(range(256))
        self.assertEqual(unescape_packet(escape_packet(payload)), payload)

    def test_decode_rejects_corrupt_crc(self) -> None:
        with self.assertRaisesRegex(ProtocolError, "CRC"):
            decode_frame(frame(b"payload")[:-2] + b"\x00\x00\x7e")

    def test_path_encoding_uses_phone_encoding_and_nul_terminator(self) -> None:
        self.assertEqual(encode_path("mmc1/café"), b"mmc1/caf\xe9\0")
        with self.assertRaises(ValueError):
            encode_path("bad\0path")
        with self.assertRaises(ValueError):
            encode_path("snowman-\u2603")

    def test_exchange_writes_ppp_frame_and_reads_phone_response(self) -> None:
        response = b"\x4b\x13\x0c\x00"
        transport = FakeTransport(frame(response))
        client = BrewClient(transport)
        self.assertEqual(client.exchange(b"\x59\x0c"), response)
        self.assertEqual(decode_frame(bytes(transport.written)), b"\x59\x0c")

    def test_exchange_traces_timestamp_and_crc_without_payload_by_default(self) -> None:
        events: list[dict[str, object]] = []
        response = b"\x4b\x13\x0c\x00"
        client = BrewClient(FakeTransport(frame(response)), trace=events.append)

        self.assertEqual(client.exchange(b"\x59\x0c"), response)

        self.assertEqual([event["event"] for event in events], ["request", "response"])
        self.assertEqual(events[0]["command"], "enter_brew_mode")
        self.assertEqual(events[1]["command"], "enter_brew_mode")
        self.assertTrue(events[1]["crc_valid"])
        self.assertEqual(events[0]["frame_bytes"], len(frame(b"\x59\x0c")))
        self.assertEqual(events[1]["frame_bytes"], len(frame(response)))
        self.assertTrue(events[0]["timestamp"])
        self.assertNotIn("payload_hex", events[0])
        self.assertNotIn("payload_hex", events[1])
        self.assertEqual(client.stats["exchanges"], 1)
        self.assertEqual(client.stats["requests"], 1)

    def test_exchange_trace_payloads_are_included_only_when_enabled(self) -> None:
        events: list[dict[str, object]] = []
        response = b"\x4b\x13\x0c\x00"
        client = BrewClient(
            FakeTransport(frame(response)),
            trace=events.append,
            trace_payloads=True,
        )

        client.exchange(b"\x59\x0c")

        self.assertEqual(events[0]["payload_hex"], "590c")
        self.assertEqual(events[1]["payload_hex"], response.hex())

    def test_exchange_trace_counts_crc_retry(self) -> None:
        events: list[dict[str, object]] = []
        response = b"\x4b\x13\x0c\x00"
        corrupt_frame = bytearray(frame(response))
        corrupt_frame[-2] ^= 1
        client = BrewClient(
            FakeTransport(bytes(corrupt_frame) + b"\x7e" + frame(response)),
            trace=events.append,
        )

        self.assertEqual(client.exchange(b"\x59\x0c", checksum_retries=1), response)

        self.assertEqual(client.stats["requests"], 2)
        self.assertEqual(client.stats["retries"], 1)
        self.assertEqual(client.stats["crc_failures"], 1)
        self.assertEqual(events[1]["crc_valid"], False)
        self.assertEqual(events[3]["crc_valid"], True)

    def test_exchange_trace_and_stats_report_timeout(self) -> None:
        events: list[dict[str, object]] = []
        client = BrewClient(FakeTransport(), trace=events.append)

        with self.assertRaises(TimeoutError):
            client.exchange(b"\x59\x0c")

        self.assertEqual(client.stats["timeouts"], 1)
        self.assertEqual(client.stats["failures"], 1)
        self.assertEqual(events[-1]["event"], "failure")
        self.assertEqual(events[-1]["error_type"], "TimeoutError")

    def test_exchange_skips_redundant_ppp_opening_flag(self) -> None:
        response = b"\x4b\x13\x0c\x00"
        transport = FakeTransport(b"\x7e" + frame(response))
        self.assertEqual(BrewClient(transport).exchange(b"\x59\x0c"), response)

    def test_brew_mode_accepts_legacy_memory_config_response(self) -> None:
        response = b"\x59\x0c\x00\x00\x04\xfd\x03"
        transport = FakeTransport(frame(response))
        BrewClient(transport).enter_brew_mode()

    def test_diagnostic_version_reads_little_endian_version(self) -> None:
        response = b"\x1c\x34\x12"
        transport = FakeTransport(frame(response))

        version = BrewClient(transport).read_diagnostic_version()

        self.assertEqual(version, 0x1234)
        self.assertEqual(decode_frame(bytes(transport.written)), b"\x1c")

    def test_diagnostic_version_rejects_invalid_response(self) -> None:
        for response in (b"\x13\x00\x00", b"\x1c\x00"):
            with self.subTest(response=response):
                client = BrewClient(FakeTransport(frame(response)))
                with self.assertRaises(ProtocolError):
                    client.read_diagnostic_version()

    def test_handset_screen_reads_text_and_status_fields(self) -> None:
        screen = bytearray(107)
        screen[0:2] = b"\x22\x01"
        screen[2:50] = (
            b"HELLO WORLD!"
            b"ROW TWO     "
            b"ROW THREE   "
            b"ROW FOUR    "
        )
        struct.pack_into("<H", screen, 51, 0x1234)
        screen[53:101] = b"BLINK       " * 4
        struct.pack_into("<H", screen, 101, 0x5678)
        screen[103] = 4
        struct.pack_into("<H", screen, 105, 250)
        transport = FakeTransport(frame(bytes(screen)))

        result = BrewClient(transport).read_handset_screen()

        self.assertEqual(
            decode_frame(bytes(transport.written)),
            b"\x22",
        )
        self.assertTrue(result.backlight_on)
        self.assertEqual(
            result.rows,
            ("HELLO WORLD!", "ROW TWO     ", "ROW THREE   ", "ROW FOUR    "),
        )
        self.assertEqual(result.annunciators, 0x1234)
        self.assertEqual(result.blink_rows, ("BLINK       ",) * 4)
        self.assertEqual(result.blink_annunciators, 0x5678)
        self.assertEqual(result.rssi, 4)
        self.assertEqual(result.blink_rate, 250)
        self.assertEqual(result.nonzero_character_bytes, 48)

    def test_handset_screen_rejects_unexpected_or_truncated_response(self) -> None:
        for response in (b"\x13\x00\x00", b"\x22" + bytes(105)):
            with self.subTest(response_length=len(response)):
                client = BrewClient(FakeTransport(frame(response)))
                with self.assertRaises(ProtocolError):
                    client.read_handset_screen()

    def test_directory_entry_decoding_and_handle_close(self) -> None:
        entries = (
            b"\x4b\x13\x0b\x00"
            + struct.pack("<II", 7, 0)
        )
        first = (
            b"\x4b\x13\x0c\x00"
            + struct.pack("<IIIIIIII", 7, 1, 0, 0, 0, 5, 0, 0)
            + struct.pack("<I", 0)
            + b"file.txt\0"
        )
        end = (
            b"\x4b\x13\x0c\x00"
            + struct.pack("<IIIIIIII", 7, 2, 0, 0, 0, 0, 0, 0)
            + struct.pack("<I", 0)
            + b"\0"
        )
        closed = b"\x4b\x13\x0d\x00" + struct.pack("<I", 0)
        transport = FakeTransport(
            frame(entries)
            + b"\x7e"
            + frame(first)
            + b"\x7e"
            + frame(end)
            + b"\x7e"
            + frame(closed)
        )
        result = BrewClient(transport).list_directory("brew")
        self.assertEqual(result, [FileEntry("brew/file.txt", False, 5)])
        requests = [
            decode_frame(packet + b"\x7e")
            for packet in bytes(transport.written).split(b"\x7e")
            if packet
        ]
        self.assertEqual(
            requests[0],
            b"\x4b\x13\x0b\x00" + encode_path("brew"),
        )
        self.assertEqual(requests[-1], b"\x4b\x13\x0d\x00" + struct.pack("<I", 7))

    def test_file_copy_reads_in_blocks_and_closes_the_handle(self) -> None:
        stat = (
            b"\x4b\x13\x0f\x00"
            + struct.pack("<IIIIIIII", 0, 0, 3, 1, 0, 0, 0, 0)
        )
        opened = b"\x4b\x13\x02\x00" + struct.pack("<II", 42, 0)
        read = (
            b"\x4b\x13\x04\x00"
            + struct.pack("<IIII", 42, 0, 3, 0)
            + b"\x00\x7d\x7e"
        )
        corrupt_read = bytearray(frame(read))
        corrupt_read[-2] ^= 1
        closed = b"\x4b\x13\x03\x00" + struct.pack("<I", 0)
        transport = FakeTransport(
            frame(stat)
            + b"\x7e"
            + frame(opened)
            + b"\x7e"
            + bytes(corrupt_read)
            + b"\x7e"
            + frame(read)
            + b"\x7e"
            + frame(closed)
        )
        destination = io.BytesIO()

        copied = BrewClient(transport).copy_file_to("brew/data.bin", destination)

        self.assertEqual(copied, 3)
        self.assertEqual(destination.getvalue(), b"\x00\x7d\x7e")
        self.assertEqual(bytes(transport.response), b"")
        requests = [
            decode_frame(packet + b"\x7e")
            for packet in bytes(transport.written).split(b"\x7e")
            if packet
        ]
        self.assertEqual(requests[0], b"\x4b\x13\x0f\x00" + encode_path("brew/data.bin"))
        self.assertEqual(requests[1][:4], b"\x4b\x13\x02\x00")
        self.assertEqual(requests[2], b"\x4b\x13\x04\x00" + struct.pack("<III", 42, 3, 0))
        self.assertEqual(requests[2], requests[3])
        self.assertEqual(requests[4], b"\x4b\x13\x03\x00" + struct.pack("<I", 42))

    def test_file_copy_reduces_block_size_after_repeated_crc_errors(self) -> None:
        stat = (
            b"\x4b\x13\x0f\x00"
            + struct.pack("<IIIIIIII", 0, 0, 4, 1, 0, 0, 0, 0)
        )
        opened = b"\x4b\x13\x02\x00" + struct.pack("<II", 42, 0)
        corrupt_read = (
            b"\x4b\x13\x04\x00"
            + struct.pack("<IIII", 42, 0, 4, 0)
            + b"data"
        )
        first_half = (
            b"\x4b\x13\x04\x00"
            + struct.pack("<IIII", 42, 0, 2, 0)
            + b"da"
        )
        second_half = (
            b"\x4b\x13\x04\x00"
            + struct.pack("<IIII", 42, 2, 2, 0)
            + b"ta"
        )
        closed = b"\x4b\x13\x03\x00" + struct.pack("<I", 0)
        bad_frames = bytearray(frame(corrupt_read))
        bad_frames[-2] ^= 1
        transport = FakeTransport(
            frame(stat)
            + b"\x7e"
            + frame(opened)
            + b"\x7e"
            + bytes(bad_frames) * 3
            + b"\x7e"
            + frame(first_half)
            + b"\x7e"
            + frame(second_half)
            + b"\x7e"
            + frame(closed)
        )
        destination = io.BytesIO()

        copied = BrewClient(transport).copy_file_to("brew/data.bin", destination)

        self.assertEqual(copied, 4)
        self.assertEqual(destination.getvalue(), b"data")
        requests = [
            decode_frame(packet + b"\x7e")
            for packet in bytes(transport.written).split(b"\x7e")
            if packet
        ]
        self.assertEqual(requests[2:5], [requests[2]] * 3)
        self.assertEqual(
            requests[2],
            b"\x4b\x13\x04\x00" + struct.pack("<III", 42, 4, 0),
        )
        self.assertEqual(
            requests[5],
            b"\x4b\x13\x04\x00" + struct.pack("<III", 42, 2, 0),
        )
        self.assertEqual(
            requests[6],
            b"\x4b\x13\x04\x00" + struct.pack("<III", 42, 2, 2),
        )

    def test_file_upload_writes_in_blocks_and_closes_the_handle(self) -> None:
        opened = b"\x4b\x13\x02\x00" + struct.pack("<II", 42, 0)
        written = b"\x4b\x13\x05\x00" + struct.pack("<IIII", 42, 0, 3, 0)
        closed = b"\x4b\x13\x03\x00" + struct.pack("<I", 0)
        transport = FakeTransport(frame(opened) + b"\x7e" + frame(written) + b"\x7e" + frame(closed))

        copied = BrewClient(transport).write_file_from(
            "brew/mod/50000/test.bin", io.BytesIO(b"\x00\x7d\x7e"), 3
        )

        self.assertEqual(copied, 3)
        requests = [
            decode_frame(packet + b"\x7e")
            for packet in bytes(transport.written).split(b"\x7e")
            if packet
        ]
        self.assertEqual(
            requests[0],
            b"\x4b\x13\x02\x00"
            + struct.pack("<II", 0x41, 1)
            + encode_path("brew/mod/50000/test.bin"),
        )
        self.assertEqual(
            requests[1],
            b"\x4b\x13\x05\x00" + struct.pack("<II", 42, 0) + b"\x00\x7d\x7e",
        )
        self.assertEqual(requests[2], b"\x4b\x13\x03\x00" + struct.pack("<I", 42))

    def test_file_removal_uses_brew_v2_remove_command(self) -> None:
        transport = FakeTransport(frame(b"\x4b\x13\x08\x00"))

        BrewClient(transport)._remove_file("brew/mod/37470/rs.dat")

        request = decode_frame(bytes(transport.written))
        self.assertEqual(
            request,
            b"\x4b\x13\x08\x00" + encode_path("brew/mod/37470/rs.dat") + b"\x01",
        )

    def test_game_install_uses_new_id_and_registers_mif_after_module_files(self) -> None:
        class InstallClient(BrewClient):
            def __init__(self) -> None:
                super().__init__(FakeTransport())
                self.created: list[str] = []
                self.uploaded: dict[str, bytes] = {}
                self.removed: list[str] = []

            def list_directory(self, path: str) -> list[FileEntry]:
                if path == "brew/mod":
                    return [FileEntry("brew/mod/50000", True)]
                if path == "brew/mif":
                    return [FileEntry("brew/mif/50000.mif", False, 2)]
                if path == "brew/mod/50000":
                    return [FileEntry("brew/mod/50000/old.bin", False, 3)]
                return []

            def _create_directory(self, path: str) -> None:
                self.created.append(path)

            def _remove_file(self, path: str) -> None:
                self.removed.append(path)

            def write_file_from(self, path: str, source: io.BufferedIOBase, size: int) -> int:
                data = source.read()
                self.uploaded[path] = data
                return len(data)

            def _stat_file(self, path: str) -> tuple[int, int]:
                return 0, len(self.uploaded[path])

        client = InstallClient()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "mif").mkdir()
            (root / "mod" / "SHS").mkdir(parents=True)
            (root / "mif" / "SHS.mif").write_bytes(b"mif")
            (root / "mod" / "SHS" / "game.mod").write_bytes(b"module")

            result = install_brew_game(
                client, root, 50000, replace_existing=True, progress=lambda _: None
            )

        self.assertEqual(client.created, [])
        self.assertEqual(client.removed, ["brew/mod/50000/old.bin", "brew/mif/50000.mif"])
        self.assertEqual(
            list(client.uploaded),
            ["brew/mod/50000/game.mod", "brew/mif/50000.mif"],
        )
        self.assertEqual(result, {"files": 2, "bytes": 9})

    def test_game_install_refuses_an_app_id_already_in_use(self) -> None:
        class OccupiedIdClient(BrewClient):
            def __init__(self) -> None:
                super().__init__(FakeTransport())
                self.created: list[str] = []

            def list_directory(self, path: str) -> list[FileEntry]:
                return [FileEntry("brew/mod/50000", True)] if path == "brew/mod" else []

            def _create_directory(self, path: str) -> None:
                self.created.append(path)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "mif").mkdir()
            (root / "mod" / "SHS").mkdir(parents=True)
            (root / "mif" / "SHS.mif").write_bytes(b"mif")
            (root / "mod" / "SHS" / "game.mod").write_bytes(b"module")
            client = OccupiedIdClient()

            with self.assertRaises(FileExistsError):
                install_brew_game(client, root, 50000, progress=lambda _: None)

        self.assertEqual(client.created, [])


class BackupTests(unittest.TestCase):
    def test_backup_preserves_tree_binary_files_and_empty_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "backup"
            manifest = backup_filesystem(FakeBackupClient(), destination, progress=lambda _: None)

            self.assertEqual((destination / "readme.txt").read_bytes(), b"hello")
            self.assertEqual((destination / "brew" / "data.bin").read_bytes(), b"\x00\x01\xff")
            self.assertTrue((destination / "brew" / "empty").is_dir())
            self.assertEqual(len(manifest["files"]), 2)
            self.assertEqual(manifest["errors"], [])
            self.assertTrue((destination / MANIFEST_NAME).is_file())
            saved_manifest = json.loads((destination / MANIFEST_NAME).read_text())
            self.assertEqual(saved_manifest["device"], "LG VX9800")

    def test_backup_refuses_a_non_empty_destination_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp)
            (destination / "keep.txt").write_text("preserve me")
            with self.assertRaises(FileExistsError):
                backup_filesystem(FakeBackupClient(), destination, progress=lambda _: None)
            self.assertEqual((destination / "keep.txt").read_text(), "preserve me")

    def test_resume_skips_files_with_matching_sizes_and_reads_missing_files(self) -> None:
        class CountingClient(FakeBackupClient):
            def __init__(self) -> None:
                super().__init__()
                self.copied: list[str] = []

            def copy_file_to(self, path: str, destination: io.BufferedIOBase) -> int:
                self.copied.append(path)
                return super().copy_file_to(path, destination)

        client = CountingClient()
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "backup"
            destination.mkdir()
            (destination / "readme.txt").write_bytes(b"hello")
            (destination / "brew").mkdir()
            (destination / "brew" / "empty").mkdir()

            manifest = backup_filesystem(
                client, destination, resume=True, progress=lambda _: None
            )

            self.assertEqual(client.copied, ["brew/data.bin"])
            self.assertEqual((destination / "brew" / "data.bin").read_bytes(), b"\x00\x01\xff")
            self.assertEqual(
                {item["path"] for item in manifest["files"]},
                {"readme.txt", "brew/data.bin"},
            )
            self.assertEqual(
                [item["path"] for item in manifest["files"] if item.get("resumed")],
                ["readme.txt"],
            )

    def test_resume_and_overwrite_are_mutually_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "cannot be used together"):
                backup_filesystem(
                    FakeBackupClient(),
                    Path(temp),
                    overwrite=True,
                    resume=True,
                    progress=lambda _: None,
                )

    def test_phone_filename_is_sanitized_for_windows(self) -> None:
        class InvalidNameClient:
            def list_directory(self, path: str) -> list[FileEntry]:
                return [FileEntry("bad:name?.txt", False)] if not path else []

            def copy_file_to(self, path: str, destination: io.BufferedIOBase) -> int:
                destination.write(b"data")
                return 4

        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "backup"
            backup_filesystem(InvalidNameClient(), destination, progress=lambda _: None)
            self.assertEqual((destination / "bad_name_.txt").read_bytes(), b"data")


class DiagnosticTests(unittest.TestCase):
    def test_diagnostics_scan_sizes_and_report_access_and_timeout_errors(self) -> None:
        class DiagnosticClient(BrewClient):
            def __init__(self) -> None:
                super().__init__(FakeTransport())

            def list_directory(self, path: str) -> list[FileEntry]:
                if path == "":
                    return [
                        FileEntry("brew", True),
                        FileEntry("protected", True),
                        FileEntry("slow", True),
                        FileEntry("root.bin", False, 5),
                    ]
                if path == "brew":
                    return [FileEntry("brew/data.bin", False, 3)]
                if path == "protected":
                    raise PhoneAccessError("access denied")
                if path == "slow":
                    raise TimeoutError("timed out")
                raise AssertionError(f"Unexpected path: {path}")

        report = run_diagnostics(DiagnosticClient())

        self.assertTrue(report["read_only"])
        self.assertEqual(
            report["summary"],
            {
                "directories_scanned": 4,
                "files_listed": 2,
                "file_bytes_reported": 8,
                "errors": 2,
                "access_denied": 1,
                "timeout": 1,
                "protocol_error": 0,
            },
        )
        self.assertEqual(
            {item["path"] for item in report["errors"]},
            {"protected", "slow"},
        )

    def test_jsonl_trace_writes_one_json_event_per_line(self) -> None:
        output = io.StringIO()
        trace = JsonlTrace(output)

        trace({"event": "request", "command": "enter_brew_mode"})

        self.assertEqual(
            json.loads(output.getvalue()),
            {"event": "request", "command": "enter_brew_mode"},
        )


if __name__ == "__main__":
    unittest.main()
