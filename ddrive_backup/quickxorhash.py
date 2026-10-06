"""QuickXorHash: the checksum OneDrive for work or school stores for every file.

Microsoft's reference algorithm XORs byte number k of the file into a 160-bit
circular register at bit offset (11 * k) mod 160, then XORs the file length
into the last 8 bytes. Because 11 * 160 is a multiple of 160, every byte whose
position is the same modulo 160 lands on the same offset. So we can first XOR
the whole file down to one 160-byte row (fast, using Python big integers) and
only then do the per-byte rotation for those 160 bytes.
"""

from __future__ import annotations

import base64

WIDTH_BITS = 160
SHIFT = 11
_ROW = 160                      # bytes per row; byte offsets repeat every 160 bytes
_ROW_BITS = _ROW * 8
_MASK160 = (1 << WIDTH_BITS) - 1

EMPTY_HASH = base64.b64encode(bytes(20)).decode("ascii")


def _fold_rows(value: int, rows: int) -> int:
    """XOR together `rows` consecutive 160-byte rows packed little-endian in `value`."""
    while rows > 1:
        half = rows // 2
        shift = half * _ROW_BITS
        value = (value & ((1 << shift) - 1)) ^ (value >> shift)
        rows -= half
    return value


class QuickXorHash:
    def __init__(self) -> None:
        self._fold = 0          # XOR of all complete rows seen so far
        self._pending = b""     # start of an incomplete row (always row-aligned)
        self.length = 0

    def update(self, data) -> None:
        if not data:
            return
        view = memoryview(data).cast("B")
        self.length += len(view)
        if self._pending:
            need = _ROW - len(self._pending)
            if len(view) < need:
                self._pending += bytes(view)
                return
            self._fold ^= int.from_bytes(self._pending + bytes(view[:need]), "little")
            self._pending = b""
            view = view[need:]
        full = len(view) - len(view) % _ROW
        if full:
            self._fold ^= _fold_rows(int.from_bytes(view[:full], "little"), full // _ROW)
        if full < len(view):
            self._pending = bytes(view[full:])

    def digest(self) -> bytes:
        row = self._fold
        if self._pending:
            row ^= int.from_bytes(self._pending, "little")
        state = 0
        for position, byte in enumerate(row.to_bytes(_ROW, "little")):
            if byte:
                bits = byte << ((SHIFT * position) % WIDTH_BITS)
                state ^= (bits | (bits >> WIDTH_BITS)) & _MASK160
        out = bytearray(state.to_bytes(WIDTH_BITS // 8, "little"))
        for i, length_byte in enumerate(self.length.to_bytes(8, "little")):
            out[len(out) - 8 + i] ^= length_byte
        return bytes(out)

    def b64digest(self) -> str:
        return base64.b64encode(self.digest()).decode("ascii")


def hash_bytes(data: bytes) -> str:
    h = QuickXorHash()
    h.update(data)
    return h.b64digest()
