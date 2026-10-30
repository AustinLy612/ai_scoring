"""把模型返回的片段文本定位回原文的字符区间。"""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import NamedTuple

# 定位时忽略的字符：空白与换行（模型常会丢掉换行或缩进）
_IGNORED = set(" \t\r\n\u3000\u00a0")

FUZZY_THRESHOLD = 0.6


class Located(NamedTuple):
    start: int
    end: int
    fuzzy: bool


def _normalize(text: str) -> tuple[str, list[int]]:
    """去掉空白字符，并返回每个保留字符在原文中的下标。"""
    chars: list[str] = []
    index_map: list[int] = []
    for i, ch in enumerate(text):
        if ch in _IGNORED:
            continue
        chars.append(ch)
        index_map.append(i)
    return "".join(chars), index_map


def locate(full_text: str, snippet: str, search_from: int = 0) -> Located | None:
    """在 full_text 中定位 snippet。

    依次尝试：从 search_from 起精确匹配 -> 全文精确匹配 -> 忽略空白匹配 -> 模糊匹配。
    返回 None 表示无法定位。
    """
    snippet = snippet.strip()
    if not snippet or not full_text:
        return None

    pos = full_text.find(snippet, max(0, search_from))
    if pos == -1:
        pos = full_text.find(snippet)
    if pos != -1:
        return Located(pos, pos + len(snippet), False)

    norm_full, full_map = _normalize(full_text)
    norm_snippet, _ = _normalize(snippet)
    if not norm_snippet or not norm_full:
        return None

    # 忽略空白后的精确匹配：位置准确，只是原文中夹了换行/空格
    norm_from = _normalized_offset(full_map, max(0, search_from))
    npos = norm_full.find(norm_snippet, norm_from)
    if npos == -1:
        npos = norm_full.find(norm_snippet)
    if npos != -1:
        return Located(full_map[npos], full_map[npos + len(norm_snippet) - 1] + 1, False)

    return _fuzzy_locate(norm_full, norm_snippet, full_map)


def _normalized_offset(full_map: list[int], original_offset: int) -> int:
    for i, orig in enumerate(full_map):
        if orig >= original_offset:
            return i
    return len(full_map)


def _fuzzy_locate(
    norm_full: str,
    norm_snippet: str,
    full_map: list[int],
) -> Located | None:
    matcher = SequenceMatcher(None, norm_full, norm_snippet, autojunk=False)
    blocks = [b for b in matcher.get_matching_blocks() if b.size > 0]
    if not blocks:
        return None

    # 取最长匹配块作为锚点，向两侧按片段长度扩展出候选窗口
    anchor = max(blocks, key=lambda b: b.size)
    span = len(norm_snippet)
    start = max(0, anchor.a - anchor.b)
    end = min(len(norm_full), start + span)
    start = max(0, end - span)

    ratio = SequenceMatcher(None, norm_full[start:end], norm_snippet, autojunk=False).ratio()
    if ratio < FUZZY_THRESHOLD or start >= end:
        return None

    return Located(full_map[start], full_map[end - 1] + 1, True)
