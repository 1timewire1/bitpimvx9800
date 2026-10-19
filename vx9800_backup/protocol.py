"""Minimal BREW v2 filesystem client for the LG VX9800."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter
from typing import BinaryIO, Callable, Iterator, Protocol

FLAG = 0x7E
ESCAPE = 0x7D
READ_SIZE = 0xEB
WRITE_SIZE = 0xEA
MAX_DIRECTORY_ENTRIES = 10_000


class Transport(Protocol):
    def write(self, data: bytes) -> int: ...

    def read(self, size: int = 1) -> bytes: ...


class ProtocolError(Exception):
    """The phone returned an invalid or unsuccessful BREW response."""


class ChecksumError(ProtocolError):
    """A received PPP frame failed its checksum and can be safely retried."""


class PhoneAccessError(ProtocolError):
    """The phone denied access to a filesystem entry."""


@dataclass(frozen=True)
class FileEntry:
    path: str
    is_directory: bool
    size: int = 0


@dataclass(frozen=True)
class HandsetScreen:
    backlight_on: bool
    rows: tuple[str, ...]
    annunciators: int
    blink_rows: tuple[str, ...]
    blink_annunciators: int
    rssi: int
    blink_rate: int
    nonzero_character_bytes: int


def crc16_ppp(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0x8408 if crc & 1 else 0)
    return crc ^ 0xFFFF


def escape_packet(data: bytes) -> bytes:
    return data.replace(b"\x7d", b"\x7d\x5d").replace(b"\x7e", b"\x7d\x5e")


def unescape_packet(data: bytes) -> bytes:
    result = bytearray()
    index = 0
    while index < len(data):
        byte = data[index]
        if byte == ESCAPE:
            index += 1
            if index == len(data):
                raise ProtocolError("Truncated PPP escape sequence")
            byte = data[index] ^ 0x20
        result.append(byte)
        index += 1
    return bytes(result)


def frame(payload: bytes) -> bytes:
    checksum = struct.pack("<H", crc16_ppp(payload))
    return escape_packet(payload + checksum) + bytes((FLAG,))


def decode_frame(packet: bytes) -> bytes:
    if len(packet) < 3 or packet[-1] != FLAG:
        raise ProtocolError("Invalid PPP frame delimiter")
    unescaped = unescape_packet(packet[:-1])
    if len(unescaped) < 3:
        raise ProtocolError("PPP frame is shorter than its checksum")
    payload, checksum = unescaped[:-2], unescaped[-2:]
    if struct.pack("<H", crc16_ppp(payload)) != checksum:
        raise ChecksumError("BREW packet failed its CRC check")
    return payload


def encode_path(path: str) -> bytes:
    try:
        encoded = path.encode("iso8859_1")
    except UnicodeEncodeError as exc:
        raise ValueError("Phone paths must be representable in ISO-8859-1") from exc
    if b"\0" in encoded:
        raise ValueError("Phone paths cannot contain NUL bytes")
    return encoded + b"\0"


def response_body(packet: bytes) -> bytes:
    if len(packet) >= 3 and packet[0] == 0x59 and packet[2] != 0:
        error = packet[2]
        if error == 0x04:
            raise PhoneAccessError("The phone denied access to this path")
        raise ProtocolError(f"BREW command failed with error 0x{error:02x}")
    if packet and packet[0] in (0x13, 0x14):
        if packet[0] == 0x13:
            raise PhoneAccessError("The phone denied access to this path")
        raise ProtocolError("The phone rejected the BREW command")
    if len(packet) >= 3 and packet[0] == 0x4B and packet[2] == 0x1C:
        raise PhoneAccessError("The phone denied access to this path")
    return packet


class BrewClient:
    """Speaks the VX9800's BREW v2 filesystem protocol on a serial port."""

    def __init__(
        self,
        transport: Transport,
        timeout: float = 15.0,
        *,
        trace: Callable[[dict[str, object]], None] | None = None,
        trace_payloads: bool = False,
    ) -> None:
        self.transport = transport
        self.timeout = timeout
        self.trace = trace
        self.trace_payloads = trace_payloads
        self.stats = {
            "exchanges": 0,
            "requests": 0,
            "retries": 0,
            "crc_failures": 0,
            "timeouts": 0,
            "protocol_errors": 0,
            "failures": 0,
        }

    @staticmethod
    def _command_name(payload: bytes) -> str:
        if payload.startswith(b"\x4b\x13") and len(payload) >= 3:
            return f"brew_v2_0x{payload[2]:02x}"
        if payload.startswith(b"\x59\x0c"):
            return "enter_brew_mode"
        if payload.startswith(b"\x21"):
            return "diagnostic_mode"
        if payload.startswith(b"\x20"):
            return "diagnostic_key"
        if payload:
            return f"command_0x{payload[0]:02x}"
        return "empty"

    def _trace_event(
        self,
        event: str,
        payload: bytes,
        *,
        attempt: int,
        started: float,
        command_payload: bytes | None = None,
        frame_bytes: int | None = None,
        crc_valid: bool | None = None,
        error: Exception | None = None,
    ) -> None:
        if self.trace is None:
            return
        record: dict[str, object] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "command": self._command_name(command_payload or payload),
            "attempt": attempt,
            "payload_bytes": len(payload),
            "elapsed_ms": round((perf_counter() - started) * 1000, 3),
        }
        if frame_bytes is not None:
            record["frame_bytes"] = frame_bytes
        if crc_valid is not None:
            record["crc_valid"] = crc_valid
        if error is not None:
            record["error_type"] = type(error).__name__
            record["error"] = str(error)
        if self.trace_payloads:
            record["payload_hex"] = payload.hex()
        self.trace(record)

    def _read_frame(self) -> bytes:
        packet = bytearray()
        while True:
            byte = self.transport.read(1)
            if not byte:
                raise TimeoutError("Timed out waiting for the phone")
            if byte == bytes((FLAG,)):
                if not packet:
                    continue
                packet.append(FLAG)
                return bytes(packet)
            packet.extend(byte)

    def exchange(self, payload: bytes, *, checksum_retries: int = 0) -> bytes:
        self.stats["exchanges"] += 1
        for attempt in range(checksum_retries + 1):
            started = perf_counter()
            request_frame = frame(payload)
            self.stats["requests"] += 1
            self._trace_event(
                "request", payload, attempt=attempt + 1, started=started,
                frame_bytes=len(request_frame),
            )
            raw_response = b""
            try:
                self.transport.write(request_frame)
                raw_response = self._read_frame()
                response = decode_frame(raw_response)
                self._trace_event(
                    "response", response, attempt=attempt + 1, started=started,
                    command_payload=payload, frame_bytes=len(raw_response),
                    crc_valid=True,
                )
                return response_body(response)
            except ChecksumError as exc:
                self.stats["crc_failures"] += 1
                self._trace_event(
                    "response", b"", attempt=attempt + 1, started=started,
                    command_payload=payload, frame_bytes=len(raw_response),
                    crc_valid=False, error=exc,
                )
                if attempt == checksum_retries:
                    self.stats["protocol_errors"] += 1
                    self.stats["failures"] += 1
                    raise
                self.stats["retries"] += 1
            except (OSError, TimeoutError, ProtocolError) as exc:
                self.stats["failures"] += 1
                if isinstance(exc, TimeoutError):
                    self.stats["timeouts"] += 1
                if isinstance(exc, ProtocolError):
                    self.stats["protocol_errors"] += 1
                self._trace_event(
                    "failure", payload, attempt=attempt + 1, started=started,
                    frame_bytes=len(request_frame), error=exc,
                )
                raise
        raise AssertionError("Unreachable retry state")

    def enter_brew_mode(self) -> None:
        response = self.exchange(b"\x59\x0c")
        if response[:3] != b"\x59\x0c\x00" or len(response) < 7:
            raise ProtocolError("Phone returned an incomplete BREW mode response")

    def enter_diagnostic_mode(self) -> None:
        self.exchange(b"\x21\x00\x00")
        for key in b"\x06\x513733929\x51":
            self.exchange(b"\x20\x00" + bytes((key,)))
        self.exchange(b"\x21\x01\x00")

    def connect(self) -> None:
        self.enter_brew_mode()
        self.enter_diagnostic_mode()

    def read_diagnostic_version(self) -> int:
        response = self.exchange(b"\x1c")
        if not response or response[0] != 0x1C:
            raise ProtocolError("Phone returned an unexpected diagnostic-version response")
        if len(response) != 3:
            raise ProtocolError(
                f"Phone returned a diagnostic-version response of {len(response)} bytes; "
                "expected 3"
            )
        return struct.unpack_from("<H", response, 1)[0]

    def read_handset_screen(self) -> HandsetScreen:
        response = self.exchange(b"\x22")
        expected_size = 107
        if not response or response[0] != 0x22:
            raise ProtocolError("Phone returned an unexpected handset-screen response")
        if len(response) != expected_size:
            raise ProtocolError(
                f"Phone returned a handset-screen response of {len(response)} bytes; "
                f"expected {expected_size}"
            )

        def read_rows(offset: int) -> tuple[str, ...]:
            return tuple(
                "".join(chr(byte) if 0x20 <= byte <= 0x7E else " " for byte in row)
                for row in (
                    response[offset + start : offset + start + 12]
                    for start in range(0, 48, 12)
                )
            )

        return HandsetScreen(
            backlight_on=bool(response[1]),
            rows=read_rows(2),
            annunciators=struct.unpack_from("<H", response, 51)[0],
            blink_rows=read_rows(53),
            blink_annunciators=struct.unpack_from("<H", response, 101)[0],
            rssi=response[103],
            blink_rate=struct.unpack_from("<H", response, 105)[0],
            nonzero_character_bytes=sum(byte != 0 for byte in response[2:50]),
        )

    def _open_directory(self, path: str) -> int:
        response = self.exchange(b"\x4b\x13\x0b\x00" + encode_path(path or "/"))
        self._check_response_header(response, 0x0B)
        if len(response) < 12:
            raise ProtocolError("Phone returned an incomplete open-directory response")
        handle, error = struct.unpack_from("<II", response, 4)
        self._check_error(error, path)
        return handle

    @staticmethod
    def _check_response_header(response: bytes, command: int) -> None:
        expected = b"\x4b\x13" + bytes((command, 0))
        if not response.startswith(expected):
            raise ProtocolError(
                f"Unexpected response header for BREW command 0x{command:02x}"
            )

    @staticmethod
    def _check_error(error: int, path: str) -> None:
        if error == 0:
            return
        if error in (2, 0x13):
            raise PhoneAccessError(f"Phone could not access {path!r} (error 0x{error:x})")
        raise ProtocolError(f"Phone returned error 0x{error:x} for {path!r}")

    def _close_directory(self, handle: int) -> None:
        response = self.exchange(b"\x4b\x13\x0d\x00" + struct.pack("<I", handle))
        self._check_response_header(response, 0x0D)
        if len(response) < 8:
            raise ProtocolError("Phone returned an incomplete close-directory response")
        self._check_error(struct.unpack_from("<I", response, 4)[0], "directory")

    def list_directory(self, path: str) -> list[FileEntry]:
        handle = self._open_directory(path)
        entries: list[FileEntry] = []
        try:
            for entry_number in range(1, MAX_DIRECTORY_ENTRIES + 1):
                response = self.exchange(
                    b"\x4b\x13\x0c\x00" + struct.pack("<II", handle, entry_number),
                    checksum_retries=2,
                )
                self._check_response_header(response, 0x0C)
                if len(response) < 40:
                    raise ProtocolError("Phone returned an incomplete directory entry")
                returned_handle, returned_number = struct.unpack_from("<II", response, 4)
                if returned_handle != handle or returned_number != entry_number:
                    raise ProtocolError("Phone returned a mismatched directory entry")
                entry_type = struct.unpack_from("<I", response, 16)[0]
                size = struct.unpack_from("<I", response, 24)[0]
                name_bytes = response[40:].split(b"\0", 1)[0]
                if not name_bytes:
                    return entries
                try:
                    name = name_bytes.decode("iso8859_1")
                except UnicodeDecodeError as exc:
                    raise ProtocolError("Phone returned an invalid filename") from exc
                if name in (".", "..") or "/" in name or "\\" in name:
                    raise ProtocolError(f"Phone returned an unsafe filename: {name!r}")
                if entry_type == 0x0F or entry_type == 0:
                    entries.append(FileEntry(f"{path}/{name}".strip("/"), False, size))
                else:
                    entries.append(FileEntry(f"{path}/{name}".strip("/"), True))
            raise ProtocolError(
                f"Directory {path or '/'} exceeded {MAX_DIRECTORY_ENTRIES} entries"
            )
        finally:
            self._close_directory(handle)

    def walk(self, path: str = "") -> Iterator[FileEntry]:
        for entry in self.list_directory(path):
            yield entry
            if entry.is_directory:
                yield from self.walk(entry.path)

    def _open_file(self, path: str) -> tuple[int, int]:
        response = self.exchange(
            b"\x4b\x13\x02\x00" + struct.pack("<II", 0, 0) + encode_path(path)
        )
        self._check_response_header(response, 0x02)
        if len(response) < 12:
            raise ProtocolError("Phone returned an incomplete open-file response")
        handle, error = struct.unpack_from("<II", response, 4)
        self._check_error(error, path)
        return handle, error

    def _close_file(self, handle: int, path: str) -> None:
        response = self.exchange(b"\x4b\x13\x03\x00" + struct.pack("<I", handle))
        self._check_response_header(response, 0x03)
        if len(response) < 8:
            raise ProtocolError("Phone returned an incomplete close-file response")
        self._check_error(struct.unpack_from("<I", response, 4)[0], path)

    def _create_directory(self, path: str) -> None:
        response = self.exchange(
            b"\x4b\x13\x09\x00" + struct.pack("<H", 0x01FF) + encode_path(path)
        )
        self._check_response_header(response, 0x09)
        response = self.exchange(b"\x4b\x13\x13\x00\x2f\x00")
        self._check_response_header(response, 0x13)
        if len(response) < 48:
            raise ProtocolError("Phone returned an incomplete filesystem refresh response")

    def _remove_file(self, path: str) -> None:
        response = self.exchange(b"\x4b\x13\x08\x00" + encode_path(path) + b"\x01")
        self._check_response_header(response, 0x08)

    def write_file_from(self, path: str, source: BinaryIO, size: int) -> int:
        if size < 0:
            raise ValueError("File size cannot be negative")
        response = self.exchange(
            b"\x4b\x13\x02\x00"
            + struct.pack("<II", 0x41, 1)
            + encode_path(path)
        )
        self._check_response_header(response, 0x02)
        if len(response) < 12:
            raise ProtocolError("Phone returned an incomplete open-file response")
        handle, error = struct.unpack_from("<II", response, 4)
        self._check_error(error, path)
        total = 0
        try:
            while total < size:
                data = source.read(min(WRITE_SIZE, size - total))
                if not data:
                    raise ProtocolError(f"Local file ended before {size} bytes: {path!r}")
                response = self.exchange(
                    b"\x4b\x13\x05\x00"
                    + struct.pack("<II", handle, total)
                    + data
                )
                self._check_response_header(response, 0x05)
                if len(response) < 20:
                    raise ProtocolError("Phone returned an incomplete write response")
                returned_handle, position, written, error = struct.unpack_from(
                    "<IIII", response, 4
                )
                self._check_error(error, path)
                if returned_handle != handle or position != total or written != len(data):
                    raise ProtocolError(f"Phone returned an invalid write response for {path!r}")
                total += written
            if source.read(1):
                raise ProtocolError(f"Local file grew while being uploaded: {path!r}")
        finally:
            self._close_file(handle, path)
        return total

    def _stat_file(self, path: str) -> tuple[int, int]:
        response = self.exchange(
            b"\x4b\x13\x0f\x00" + encode_path(path), checksum_retries=2
        )
        self._check_response_header(response, 0x0F)
        if len(response) < 16:
            raise ProtocolError("Phone returned an incomplete stat response")
        error = struct.unpack_from("<I", response, 4)[0]
        size = struct.unpack_from("<I", response, 12)[0]
        self._check_error(error, path)
        return error, size

    def copy_file_to(self, path: str, destination: BinaryIO) -> int:
        _, size = self._stat_file(path)
        handle, _ = self._open_file(path)
        total = 0
        block_size = READ_SIZE
        try:
            while total < size:
                count = min(block_size, size - total)
                try:
                    response = self.exchange(
                        b"\x4b\x13\x04\x00"
                        + struct.pack("<III", handle, count, total),
                        checksum_retries=2,
                    )
                except ChecksumError:
                    if count == 1:
                        raise
                    block_size = max(1, count // 2)
                    continue
                self._check_response_header(response, 0x04)
                if len(response) < 20:
                    raise ProtocolError("Phone returned an incomplete file block")
                returned_handle, position, actual, error = struct.unpack_from(
                    "<IIII", response, 4
                )
                self._check_error(error, path)
                data = response[20:]
                if returned_handle != handle or position != total:
                    raise ProtocolError(f"Phone returned an out-of-order block for {path!r}")
                if actual != len(data) or not actual:
                    raise ProtocolError(f"Phone returned an invalid block for {path!r}")
                destination.write(data)
                total += actual
            if total != size:
                raise ProtocolError(f"Downloaded {total} of {size} bytes from {path!r}")
        finally:
            self._close_file(handle, path)
        return total
