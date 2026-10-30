"""Text chunking for long segments (Qwen works best on short chunks)."""

from __future__ import annotations

import re

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…;:])\s+|\n+")


def _split_long(piece: str, max_chars: int) -> list[str]:
    words = piece.split(" ")
    out: list[str] = []
    current = ""
    for word in words:
        if not word:
            continue
        if current and len(current) + 1 + len(word) > max_chars:
            out.append(current)
            current = word
            continue
        current = word if not current else f"{current} {word}"
    if current:
        out.append(current)
    # Extremely long single token: hard split, but never silently drop text.
    final: list[str] = []
    for chunk in out:
        while len(chunk) > max_chars:
            final.append(chunk[:max_chars])
            chunk = chunk[max_chars:]
        if chunk:
            final.append(chunk)
    return final


def split_chunks(text: str, max_chars: int = 240) -> list[str]:
    piece = (text or "").strip()
    if not piece:
        return []
    if len(piece) <= max_chars:
        return [piece]
    sentences = [part.strip() for part in _SENTENCE_SPLIT.split(piece) if part.strip()]
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if len(sentence) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_split_long(sentence, max_chars))
            continue
        if current and len(current) + 1 + len(sentence) > max_chars:
            chunks.append(current)
            current = sentence
        else:
            current = sentence if not current else f"{current} {sentence}"
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk]
