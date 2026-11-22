"""Asterisk AudioSocket protocol (TCP).

Every frame is: 1 byte kind + 2 bytes big-endian length + payload. Asterisk
(the ``AudioSocket()`` dialplan application) connects out to this server and
sends a UUID frame first. Audio is 16-bit signed linear PCM at 8 kHz, mono.
"""

from __future__ import annotations

import asyncio

KIND_HANGUP = 0x00
KIND_UUID = 0x01
KIND_DTMF = 0x03
KIND_AUDIO = 0x10
KIND_ERROR = 0xFF

SAMPLE_RATE = 8000
FRAME_MS = 20
FRAME_BYTES = SAMPLE_RATE * FRAME_MS // 1000 * 2  # 320 bytes = 20 ms


def pack_frame(kind: int, payload: bytes = b"") -> bytes:
    if len(payload) > 0xFFFF:
        raise ValueError("payload too large for an AudioSocket frame")
    return bytes([kind]) + len(payload).to_bytes(2, "big") + payload


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    """Read one frame. Raises ``asyncio.IncompleteReadError`` if the peer closes."""
    header = await reader.readexactly(3)
    length = int.from_bytes(header[1:3], "big")
    payload = await reader.readexactly(length) if length else b""
    return header[0], payload
