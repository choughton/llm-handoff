from __future__ import annotations

import codecs
from pathlib import Path


def read_dispatch_text(path: Path) -> str:
    data = path.read_bytes()
    text = _decode_dispatch_text(data)
    return _normalize_newlines(text)


def _decode_dispatch_text(data: bytes) -> str:
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16")
    if data.startswith(codecs.BOM_UTF8):
        return data.decode("utf-8-sig")
    return data.decode(_detect_utf16_without_bom(data) or "utf-8-sig")


def _detect_utf16_without_bom(data: bytes) -> str | None:
    sample = data[:4096]
    if len(sample) < 4:
        return None

    even_nulls = sample[0::2].count(0)
    odd_nulls = sample[1::2].count(0)
    pair_count = len(sample) // 2
    if pair_count == 0:
        return None

    if odd_nulls / pair_count > 0.3 and even_nulls / pair_count < 0.05:
        return "utf-16-le"
    if even_nulls / pair_count > 0.3 and odd_nulls / pair_count < 0.05:
        return "utf-16-be"
    return None


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


__all__ = ["read_dispatch_text"]
