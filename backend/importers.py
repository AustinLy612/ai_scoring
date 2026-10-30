"""日记导入：txt 单文件、xlsx/csv 表格、以及网页粘贴文本。"""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from .schemas import Diary

TEXT_COLUMN_CANDIDATES = ("text", "文本", "内容", "日记", "日记文本", "content")
ID_COLUMN_CANDIDATES = ("id", "编号", "日记id", "日记编号", "diary_id")

_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "utf-16")


class ImportError_(ValueError):
    """导入失败（文件格式或缺列）。"""


def decode_bytes(data: bytes) -> str:
    for encoding in _ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def parse_upload(filename: str, data: bytes) -> list[Diary]:
    suffix = Path(filename).suffix.lower()
    if suffix == ".txt":
        return _parse_txt(filename, data)
    if suffix == ".csv":
        return _parse_csv(filename, data)
    if suffix in (".xlsx", ".xlsm"):
        return _parse_xlsx(filename, data)
    raise ImportError_(f"不支持的文件类型：{filename}（仅支持 .txt / .csv / .xlsx）")


def _parse_txt(filename: str, data: bytes) -> list[Diary]:
    text = normalize_diary_text(decode_bytes(data).strip())
    if not text:
        raise ImportError_(f"{filename} 内容为空")
    diary_id = Path(filename).stem.strip() or filename
    return [Diary(id=diary_id, text=text, source_name=filename)]


def _parse_csv(filename: str, data: bytes) -> list[Diary]:
    stream = io.StringIO(decode_bytes(data), newline="")
    reader = csv.DictReader(stream)
    if not reader.fieldnames:
        raise ImportError_(f"{filename} 没有表头行")
    rows = [dict(row) for row in reader]
    return _rows_to_diaries(filename, list(reader.fieldnames), rows)


def _parse_xlsx(filename: str, data: bytes) -> list[Diary]:
    workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    sheet = workbook.active
    if sheet is None:
        raise ImportError_(f"{filename} 没有可读的工作表")

    iterator = sheet.iter_rows(values_only=True)
    try:
        header_row = next(iterator)
    except StopIteration:
        raise ImportError_(f"{filename} 是空表") from None

    headers = [str(cell).strip() if cell is not None else "" for cell in header_row]
    rows: list[dict[str, Any]] = []
    for values in iterator:
        if values is None or all(v is None or str(v).strip() == "" for v in values):
            continue
        row = {
            headers[i]: values[i] if i < len(values) else None
            for i in range(len(headers))
            if headers[i]
        }
        rows.append(row)
    workbook.close()
    return _rows_to_diaries(filename, [h for h in headers if h], rows)


def _rows_to_diaries(filename: str, headers: list[str], rows: list[dict[str, Any]]) -> list[Diary]:
    text_col = _match_column(headers, TEXT_COLUMN_CANDIDATES)
    if text_col is None:
        raise ImportError_(
            f"{filename} 缺少日记文本列。请把存放日记正文的列命名为 text"
            f"（或 {'/'.join(TEXT_COLUMN_CANDIDATES[1:])}）。当前表头：{', '.join(headers)}"
        )
    id_col = _match_column(headers, ID_COLUMN_CANDIDATES)

    diaries: list[Diary] = []
    for line_no, row in enumerate(rows, start=2):
        text = normalize_diary_text(_clean(row.get(text_col)))
        if not text:
            continue
        diary_id = _clean(row.get(id_col)) if id_col else ""
        if not diary_id:
            diary_id = f"{Path(filename).stem}-{line_no - 1}"
        extra = {
            key: _clean(value)
            for key, value in row.items()
            if key not in {text_col, id_col} and _clean(value)
        }
        diaries.append(
            Diary(id=diary_id, text=text, extra=extra, source_name=f"{filename}:{line_no}")
        )

    if not diaries:
        raise ImportError_(f"{filename} 中没有有效的日记行（{text_col} 列全为空）")
    return diaries


def parse_pasted_text(diary_id: str, text: str, fallback_index: int) -> Diary:
    cleaned = normalize_diary_text(text.strip())
    if not cleaned:
        raise ImportError_("粘贴的日记内容为空")
    final_id = diary_id.strip() or f"粘贴-{fallback_index}"
    return Diary(id=final_id, text=cleaned, source_name="粘贴输入")


QA_ORIGINAL_HEADER = "【原始日记】（仅用于理解背景，不作为评分对象）"
QA_BLOCK_HEADER = "【问答{i}｜对应步骤{i}】"

_JSON_STRING = r'"((?:[^"\\]|\\.)*)"'


def _json_field(raw: str, key: str) -> str:
    match = re.search(rf'"{re.escape(key)}"\s*:\s*{_JSON_STRING}', raw)
    if not match:
        return ""
    try:
        return json.loads(f'"{match.group(1)}"').strip()
    except json.JSONDecodeError:
        return match.group(1).strip()


def normalize_diary_text(text: str) -> str:
    """问答型日记（含 originalDiary 与 问题1-5/答案1-5 的 JSON）转为结构化纯文本；其余原样返回。

    导出的 JSON 常被截断或带多余逗号，因此按字段用正则抽取，而不是整体 json.loads。
    """
    if not text.lstrip().startswith("{") or '"答案1"' not in text:
        return text

    blocks: list[str] = []
    original = _json_field(text, "originalDiary")
    if original:
        blocks.append(f"{QA_ORIGINAL_HEADER}\n{original}")
    for i in range(1, 6):
        question = _json_field(text, f"问题{i}")
        answer = _json_field(text, f"答案{i}")
        if question or answer:
            blocks.append(f"{QA_BLOCK_HEADER.format(i=i)}\n问：{question}\n答：{answer}")

    return "\n\n".join(blocks) if blocks else text


def _match_column(headers: list[str], candidates: tuple[str, ...]) -> str | None:
    lowered = {h.strip().lower(): h for h in headers}
    for candidate in candidates:
        if candidate in lowered:
            return lowered[candidate]
    return None


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()
