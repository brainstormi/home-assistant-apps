"""G.711 µ-law (PCMU) codec in pure Python, no dependencies.

AudioSocket delivers and expects slin (16-bit little-endian, 8 kHz), while the
Realtime API accepts and produces ``audio/pcmu`` (µ-law, 8 kHz). Converting
between the two is a table lookup, so no resampling and no ``audioop`` (which
was removed in Python 3.13) are needed.
"""

from __future__ import annotations

import array
import sys

_BIAS = 0x84
_CLIP = 32635


def _encode_sample(sample: int) -> int:
    sign = 0x80 if sample < 0 else 0x00
    if sample < 0:
        sample = -sample
    if sample > _CLIP:
        sample = _CLIP
    sample += _BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (sample & mask):
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa)) & 0xFF


def _decode_byte(value: int) -> int:
    value = ~value & 0xFF
    sign = value & 0x80
    exponent = (value >> 4) & 0x07
    mantissa = value & 0x0F
    sample = (((mantissa << 3) + _BIAS) << exponent) - _BIAS
    return -sample if sign else sample


# Lookup table indexed by the sample read as an unsigned 16-bit integer.
_ENCODE = bytes(
    _encode_sample(u - 65536 if u >= 32768 else u) for u in range(65536)
)
_DECODE = array.array("h", (_decode_byte(b) for b in range(256)))

_BIG_ENDIAN = sys.byteorder == "big"


def slin_to_ulaw(pcm: bytes) -> bytes:
    """16-bit little-endian signed linear PCM -> µ-law (one byte per sample)."""
    if len(pcm) % 2:
        pcm = pcm[:-1]
    samples = array.array("h")
    samples.frombytes(pcm)
    if _BIG_ENDIAN:
        samples.byteswap()
    return bytes(_ENCODE[s & 0xFFFF] for s in samples)


def ulaw_to_slin(data: bytes) -> bytes:
    """µ-law -> 16-bit little-endian signed linear PCM."""
    samples = array.array("h", (_DECODE[b] for b in data))
    if _BIG_ENDIAN:
        samples.byteswap()
    return samples.tobytes()
