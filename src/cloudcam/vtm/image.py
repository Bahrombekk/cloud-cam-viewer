"""Shifrlangan hodisa (alarm) rasmlarini ochish — `hikencodepicture` formati.

`pyezvizapi` 1.0.5.0 (`pyezvizapi/utils.py: decrypt_image`) dan ko'chirilgan
(Apache-2.0, (c) Renier Moorcroft; litsenziya — `LICENSE-pyezvizapi`).
O'zgarishlar: xatolar `ImageKeyError` / `ValueError`; [[code_hash]] umumiy.

Format: `hikencodepicture` (16 bayt) + md5(md5(kod)) hex (32 bayt) + AES-CBC
shifrmatn (kalit = kod, 16 baytgacha nol bilan; IV = b"01234567" + 8 nol).
Bir faylda bir nechta shunday blok ketma-ket kelishi mumkin.
"""

from __future__ import annotations

from hashlib import md5

from Crypto.Cipher import AES

HEADER = b"hikencodepicture"
_HASH_LEN = 32
_IV = bytes([48, 49, 50, 51, 52, 53, 54, 55, 0, 0, 0, 0, 0, 0, 0, 0])


class ImageKeyError(ValueError):
    """Kod (tasdiqlash kodi) rasmga mos kelmadi."""


def code_hash(code: str) -> str:
    """md5(md5(kod)) — rasm sarlavhasidagi kod tekshiruvi."""
    return md5(md5(code.encode()).hexdigest().encode()).hexdigest()


def is_encrypted(data: bytes) -> bool:
    return HEADER in data[:4096]


def decrypt_image(data: bytes, code: str) -> bytes:
    """Rasmni ochadi. Shifrlanmagan bo'lsa o'zini qaytaradi.

    Xato kodda [[ImageKeyError]], buzilgan faylda `ValueError`."""
    min_len = len(HEADER) + _HASH_LEN
    start = data.find(HEADER)
    if start == -1:
        return data
    data = data[start:]
    if len(data) < min_len:
        raise ValueError("Invalid image data")
    blocks = _split_blocks(data, min_len)
    if not blocks:
        raise ValueError("Invalid image data")
    return b"".join(_decrypt_block(b, code) for b in blocks)


def _split_blocks(data: bytes, min_len: int) -> list[bytes]:
    blocks: list[bytes] = []
    cursor = 0
    while cursor <= len(data) - min_len:
        if data[cursor:cursor + len(HEADER)] != HEADER:
            nxt = data.find(HEADER, cursor + 1)
            if nxt == -1:
                break
            cursor = nxt
            continue
        nxt = data.find(HEADER, cursor + len(HEADER))
        block = data[cursor:nxt if nxt != -1 else len(data)]
        if len(block) < min_len:
            break
        blocks.append(block)
        if nxt == -1:
            break
        cursor = nxt
    return blocks


def _decrypt_block(block: bytes, code: str) -> bytes:
    hl = len(HEADER)
    if block[hl:hl + _HASH_LEN] != code_hash(code).encode():
        raise ImageKeyError("Verification code does not match the image")
    ct = block[hl + _HASH_LEN:]
    ct = ct[:len(ct) - len(ct) % AES.block_size]
    if not ct:
        raise ValueError("Missing ciphertext payload")
    cipher = AES.new(code.ljust(16, "\0")[:16].encode(), AES.MODE_CBC, _IV)
    out = bytearray()
    step = 1024 * AES.block_size
    for i in range(0, len(ct), step):
        chunk = cipher.decrypt(ct[i:i + step])
        if i + step >= len(ct):
            chunk = chunk[:-chunk[-1]] if chunk and 0 < chunk[-1] <= len(chunk) else chunk
        out.extend(chunk)
    return bytes(out)
