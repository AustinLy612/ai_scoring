"""把当前评分 prompt、最近一次一致率检验的 AI 深度分和一致率统计写成三页 Excel，不调用 API。

运行：python -m scripts.build_agreement_report <人工编码表.xlsx> <输出.xlsx> [--prompt=v1|v2]
AI 分数读自 tests/_agreement_reasons_<版本>.json（该版本 tests.agreement_check 最近一次运行的结果）。
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import prompts  # noqa: E402
from backend.schemas import STEP_NAMES  # noqa: E402
from tests.agreement_check import load_gold, parse_args, quadratic_kappa  # noqa: E402

_HEADER_FILL = PatternFill("solid", fgColor="DDEBF7")
_BOLD = Font(bold=True)


def _style_header(sheet, widths: list[int]) -> None:
    for cell in sheet[1]:
        cell.fill = _HEADER_FILL
        cell.font = _BOLD
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.freeze_panes = "A2"


def write_prompt_sheet(sheet, version: str) -> None:
    sheet.title = "评分prompt"
    sheet.append(["部分", "内容"])
    user_template = prompts.build_stage2_user_prompt("{日记编号}", "{日记全文}", [{"step": "…", "segments": "…"}])
    rows = (
        ("版本", version),
        ("系统提示词（system）", prompts.get_stage2_system(version)),
        ("用户提示词模板（user）", user_template),
    )
    for part, text in rows:
        sheet.append([part, text])
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    _style_header(sheet, [22, 140])


def write_score_sheet(sheet, ids: list[str], pred: dict[str, list[int]]) -> None:
    sheet.title = "AI深度分"
    sheet.append(["日记ID"] + [f"步骤{i}({STEP_NAMES[i]})" for i in range(1, 6)] + ["总深度分"])
    for diary_id in ids:
        scores = pred[diary_id]
        sheet.append([diary_id, *scores, sum(scores)])
    _style_header(sheet, [12] + [16] * 5 + [10])


def write_stats_sheet(sheet, ids: list[str], pred: dict[str, list[int]], gold: dict[str, list[int]]) -> None:
    sheet.title = "一致率统计"
    sheet.append(["范围", "完全一致", "完全一致率", "相差≤1比例", "二次加权kappa", "不一致明细（人工final→AI）"])

    def add_row(label: str, g: list[int], p: list[int]) -> None:
        conf = Counter((x, y) for x, y in zip(g, p) if x != y)
        detail = "；".join(f"人{x}→AI{y} ×{c}" for (x, y), c in sorted(conf.items()))
        exact = sum(x == y for x, y in zip(g, p))
        near = sum(abs(x - y) <= 1 for x, y in zip(g, p))
        sheet.append([label, f"{exact}/{len(g)}", round(exact / len(g), 3), round(near / len(g), 3),
                      round(quadratic_kappa(g, p), 3), detail])

    all_g = [gold[i][s] for i in ids for s in range(5)]
    all_p = [pred[i][s] for i in ids for s in range(5)]
    add_row("总体（20篇×5步）", all_g, all_p)
    for s in range(5):
        add_row(f"步骤{s + 1}({STEP_NAMES[s + 1]})", [gold[i][s] for i in ids], [pred[i][s] for i in ids])

    for row in sheet.iter_rows(min_row=2, max_col=5):
        for cell in row[2:5]:
            cell.number_format = "0.00"

    totals = [abs(sum(pred[i]) - sum(gold[i])) for i in ids]
    sheet.append([])
    sheet.append(["总分平均绝对差", round(sum(totals) / len(totals), 2)])
    sheet.append(["总分完全相同的日记数", f"{sum(t == 0 for t in totals)}/{len(totals)}"])
    sheet.append([])
    sheet.append(["说明", "对照对象为人工编码表中的 Final Score（两位编码员的一致结果）；"
                          "kappa 在某步骤分数高度集中时（如步骤2大多为2分）会偏低，此时以完全一致率为准。"])
    _style_header(sheet, [22, 12, 12, 12, 14, 60])


def main() -> None:
    args, version = parse_args(sys.argv[1:])
    version = version or "v1"
    gold_path, out_path = Path(args[0]), Path(args[1])
    _, gold = load_gold(gold_path)
    saved = json.loads((ROOT / "tests" / f"_agreement_reasons_{version}.json").read_text(encoding="utf-8"))
    pred = {k: [int(v) for v in scores] for k, scores in saved["pred"].items()}
    ids = [i for i in gold if i in pred]

    workbook = Workbook()
    write_prompt_sheet(workbook.active, version)
    write_score_sheet(workbook.create_sheet(), ids, pred)
    write_stats_sheet(workbook.create_sheet(), ids, pred, gold)
    workbook.save(out_path)
    print("已写出：", out_path)


if __name__ == "__main__":
    main()
