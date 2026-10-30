"""Excel 导出：日记汇总 + 步骤评分 + 片段明细。"""

from __future__ import annotations

import io

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .schemas import DEPTH_NAMES, STEP_NAMES, Diary

_HEADER_FILL = PatternFill("solid", fgColor="DDEBF7")
_HEADER_FONT = Font(bold=True)

_SEG_STATUS_LABEL = {"unchanged": "未修改", "modified": "人工修改", "deleted": "人工删除"}
_SCORE_STATUS_LABEL = {"unchanged": "未修改", "modified": "人工修改"}
_SOURCE_LABEL = {"ai": "AI", "human_added": "人工新增"}
_DIARY_STATUS_LABEL = {
    "pending": "待处理",
    "processing": "处理中",
    "done": "已完成",
    "failed": "失败",
}


def build_workbook(diaries: list[Diary]) -> bytes:
    extra_keys = _collect_extra_keys(diaries)

    workbook = Workbook()
    _write_summary_sheet(workbook.active, diaries, extra_keys)
    _write_step_score_sheet(workbook.create_sheet("步骤评分"), diaries, extra_keys)
    _write_segment_sheet(workbook.create_sheet("片段明细"), diaries, extra_keys)

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _collect_extra_keys(diaries: list[Diary]) -> list[str]:
    keys: list[str] = []
    for diary in diaries:
        for key in diary.extra:
            if key not in keys:
                keys.append(key)
    return keys


def _blank(value) -> object:
    return "" if value is None else value


# ---------------------------------------------------------------------------
# 表 1：日记汇总（一行一篇）
# ---------------------------------------------------------------------------


def _write_summary_sheet(sheet, diaries: list[Diary], extra_keys: list[str]) -> None:
    sheet.title = "日记汇总"
    headers = (
        ["日记ID", "来源"]
        + extra_keys
        + ["字数", "处理状态"]
        + [f"步骤{i}({STEP_NAMES[i]})_AI深度分" for i in range(1, 6)]
        + [f"步骤{i}({STEP_NAMES[i]})_最终深度分" for i in range(1, 6)]
        + ["AI总深度分", "最终总深度分"]
        + [f"步骤{i}片段数" for i in range(1, 6)]
        + [
            "片段总数",
            "步骤分人工修改数",
            "片段人工修改数",
            "片段人工新增数",
            "片段人工删除数",
            "含模糊定位",
            "处理提示",
        ]
    )
    sheet.append(headers)

    for diary in diaries:
        active = diary.active_segments()
        per_step = {i: [s for s in active if s.step == i] for i in range(1, 6)}
        scores = {s.step: s for s in diary.step_scores}
        ai_values = [scores[i].ai_score if i in scores else None for i in range(1, 6)]
        final_values = [scores[i].score if i in scores else None for i in range(1, 6)]

        sheet.append(
            [
                diary.id,
                diary.source_name,
                *[diary.extra.get(key, "") for key in extra_keys],
                len(diary.text),
                _DIARY_STATUS_LABEL.get(diary.status.value, diary.status.value),
                *[_blank(v) for v in ai_values],
                *[_blank(v) for v in final_values],
                _sum_or_blank(ai_values),
                _sum_or_blank(final_values),
                *[len(per_step[i]) for i in range(1, 6)],
                len(active),
                sum(1 for s in diary.step_scores if s.status == "modified"),
                sum(1 for s in active if s.status == "modified"),
                sum(1 for s in active if s.source == "human_added"),
                sum(1 for s in diary.segments if s.status == "deleted"),
                "是" if any(s.fuzzy_located for s in active) else "",
                "；".join(diary.warnings + ([diary.error] if diary.error else [])),
            ]
        )

    _style_sheet(sheet, wrap_columns={len(headers)})


def _sum_or_blank(values: list[int | None]) -> object:
    if any(v is None for v in values):
        return ""
    return sum(v for v in values if v is not None)


# ---------------------------------------------------------------------------
# 表 2：步骤评分（一行一篇×一步，长表，含理由）
# ---------------------------------------------------------------------------


def _write_step_score_sheet(sheet, diaries: list[Diary], extra_keys: list[str]) -> None:
    headers = (
        ["日记ID", "来源"]
        + extra_keys
        + [
            "步骤",
            "步骤名称",
            "该步骤片段数",
            "AI深度分",
            "最终深度分",
            "最终深度分名称",
            "AI评分理由",
            "最终评分理由",
            "修改状态",
        ]
    )
    sheet.append(headers)

    for diary in diaries:
        active = diary.active_segments()
        for score in sorted(diary.step_scores, key=lambda s: s.step):
            sheet.append(
                [
                    diary.id,
                    diary.source_name,
                    *[diary.extra.get(key, "") for key in extra_keys],
                    score.step,
                    STEP_NAMES.get(score.step, ""),
                    sum(1 for s in active if s.step == score.step),
                    _blank(score.ai_score),
                    _blank(score.score),
                    DEPTH_NAMES.get(score.score, "") if score.score is not None else "",
                    score.ai_reason,
                    score.reason,
                    _SCORE_STATUS_LABEL.get(score.status, score.status),
                ]
            )

    reason_cols = {headers.index("AI评分理由") + 1, headers.index("最终评分理由") + 1}
    _style_sheet(sheet, wrap_columns=reason_cols)


# ---------------------------------------------------------------------------
# 表 3：片段明细（一行一个片段）
# ---------------------------------------------------------------------------


def _write_segment_sheet(sheet, diaries: list[Diary], extra_keys: list[str]) -> None:
    headers = (
        ["日记ID", "来源"]
        + extra_keys
        + [
            "片段ID",
            "片段文本",
            "起始位置",
            "结束位置",
            "AI步骤",
            "AI步骤名称",
            "最终步骤",
            "最终步骤名称",
            "步骤理由",
            "重叠片段",
            "片段来源",
            "修改状态",
            "模糊定位",
        ]
    )
    sheet.append(headers)

    for diary in diaries:
        for seg in diary.segments:
            sheet.append(
                [
                    diary.id,
                    diary.source_name,
                    *[diary.extra.get(key, "") for key in extra_keys],
                    seg.id,
                    seg.text,
                    seg.start,
                    seg.end,
                    _blank(seg.ai_step),
                    STEP_NAMES.get(seg.ai_step or 0, ""),
                    seg.step,
                    STEP_NAMES.get(seg.step, ""),
                    seg.step_reason,
                    ", ".join(seg.overlaps_with),
                    _SOURCE_LABEL.get(seg.source, seg.source),
                    _SEG_STATUS_LABEL.get(seg.status, seg.status),
                    "是" if seg.fuzzy_located else "",
                ]
            )

    _style_sheet(sheet, wrap_columns={headers.index("片段文本") + 1})


# ---------------------------------------------------------------------------
# 样式
# ---------------------------------------------------------------------------


def _style_sheet(sheet, wrap_columns: set[int]) -> None:
    for cell in sheet[1]:
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(vertical="center")
    sheet.freeze_panes = "A2"

    for col in range(1, sheet.max_column + 1):
        letter = get_column_letter(col)
        if col in wrap_columns:
            sheet.column_dimensions[letter].width = 60
            for row in range(2, sheet.max_row + 1):
                sheet.cell(row=row, column=col).alignment = Alignment(
                    wrap_text=True, vertical="top"
                )
            continue
        longest = max(
            (len(str(sheet.cell(row=row, column=col).value or "")) for row in range(1, sheet.max_row + 1)),
            default=8,
        )
        sheet.column_dimensions[letter].width = min(max(10, longest + 2), 40)
