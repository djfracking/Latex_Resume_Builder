"""Minimal RFC 6455 WebSocket framing (server side, stdlib only)."""

import base64
import hashlib
import struct

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

MAX_FRAME = 4 * 1024 * 1024


def accept_key(client_key: str) -> str:
    digest = hashlib.sha1((client_key.strip() + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def encode_frame(opcode: int, payload: bytes) -> bytes:
    header = bytearray()
    header.append(0x80 | opcode)
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header += struct.pack(">H", n)
    else:
        header.append(127)
        header += struct.pack(">Q", n)
    return bytes(header) + payload


class FrameReader:
    """Reads masked client frames. `reader` is any callable(n) -> bytes that
    returns up to n bytes and raises TimeoutError when no data is ready."""

    def __init__(self, reader):
        self.reader = reader
        self.buf = b""

    def _exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.reader(n - len(self.buf))
            if not chunk:
                raise ConnectionError("socket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def read_frame(self):
        b1, b2 = self._exact(2)
        fin = bool(b1 & 0x80)
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._exact(8))[0]
        if length > MAX_FRAME:
            raise ValueError("frame too large")
        mask = self._exact(4) if masked else b"\x00\x00\x00\x00"
        payload = bytearray(self._exact(length)) if length else bytearray()
        for i in range(length):
            payload[i] ^= mask[i % 4]
        return fin, opcode, bytes(payload)
