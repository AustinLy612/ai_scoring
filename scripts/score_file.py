"""用指定版本的评分 prompt 给一个 Excel（ID、Text 两列）里的全部日记评深度分，输出三页 Excel。

运行：python -m scripts.score_file <输入.xlsx> <输出.xlsx> --prompt=v7
输出：评分prompt / AI深度分 / 评分理由。ID 重复的日记依次加 #2、#3 区分。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.config import get_settings  # noqa: E402
from backend.importers import normalize_diary_text  # noqa: E402
from backend.llm_client import get_client  # noqa: E402
from backend.pipeline import process_diary  # noqa: E402
from backend.schemas import STEP_NAMES, Diary  # noqa: E402
from scripts.build_agreement_report import _style_header, write_prompt_sheet  # noqa: E402
from tests.agreement_check import parse_args  # noqa: E402


def load_diaries(path: Path) -> list[Diary]:
    sheet = load_workbook(path, data_only=True).active
    diaries: list[Diary] = []
    seen: dict[str, int] = {}
    for row_no, row in enumerate(sheet.iter_rows(min_row=2, values_only=True), start=2):
        raw_id, raw_text = (row + (None, None))[:2]
        text = str(raw_text or "").strip()
        if raw_id is None and not text:
            continue
        base_id = str(raw_id).strip() if raw_id is not None else f"row{row_no}"
        seen[base_id] = seen.get(base_id, 0) + 1
        diary_id = base_id if seen[base_id] == 1 else f"{base_id}#{seen[base_id]}"
        normalized = normalize_diary_text(text)
        diaries.append(
            Diary(
                id=diary_id,
                text=normalized,
                source_name=path.name,
                extra={"原始ID": base_id, "行号": row_no, "类型": "问答型" if "【问答1" in normalized else "非问答型"},
            )
        )
    return diaries


def write_workbook(diaries: list[Diary], version: str, out_path: Path) -> None:
    workbook = Workbook()
    write_prompt_sheet(workbook.active, version)

    scores_sheet = workbook.create_sheet("AI深度分")
    scores_sheet.append(
        ["行号", "日记ID", "原始ID", "类型"] + [f"步骤{i}({STEP_NAMES[i]})" for i in range(1, 6)] + ["总深度分", "处理状态"]
    )
    reasons_sheet = workbook.create_sheet("评分理由")
    reasons_sheet.append(["日记ID", "步骤", "步骤名称", "AI深度分", "评分理由"])

    for diary in diaries:
        scores = [s.score for s in sorted(diary.step_scores, key=lambda s: s.step)]
        done = diary.status.value == "done" and all(v is not None for v in scores)
        scores_sheet.append(
            [diary.extra["行号"], diary.id, diary.extra["原始ID"], diary.extra["类型"], *scores,
             sum(scores) if done else None, "完成" if done else f"失败：{diary.error or '有步骤未评分'}"]
        )
        for score in sorted(diary.step_scores, key=lambda s: s.step):
            reasons_sheet.append([diary.id, score.step, STEP_NAMES[score.step], score.score, score.reason])

    _style_header(scores_sheet, [8, 12, 12, 10] + [16] * 5 + [10, 14])
    _style_header(reasons_sheet, [12, 6, 10, 10, 100])
    for row in reasons_sheet.iter_rows(min_row=2, min_col=5, max_col=5):
        row[0].alignment = Alignment(wrap_text=True, vertical="top")
    workbook.save(out_path)


async def main() -> None:
    args, version = parse_args(sys.argv[1:])
    if not version:
        raise SystemExit("请用 --prompt=版本 指定评分 prompt，例如 --prompt=v6")
    os.environ["STAGE2_PROMPT_VERSION"] = version
    get_settings.cache_clear()
    in_path, out_path = Path(args[0]), Path(args[1])

    diaries = load_diaries(in_path)
    print(f"评分 prompt 版本：{version}  日记数：{len(diaries)}", flush=True)

    client = get_client()
    sem = asyncio.Semaphore(get_settings().max_concurrency)
    finished = 0

    async def run(diary: Diary) -> None:
        nonlocal finished
        async with sem:
            await process_diary(diary, client)
        finished += 1
        if finished % 20 == 0 or finished == len(diaries):
            print(f"进度 {finished}/{len(diaries)}", flush=True)

    await asyncio.gather(*(run(d) for d in diaries))

    def unfinished() -> list[Diary]:
        return [d for d in diaries if d.status.value != "done" or any(s.score is None for s in d.step_scores)]

    for attempt in range(2):
        retry = unfinished()
        if not retry:
            break
        print(f"第 {attempt + 1} 次重试 {len(retry)} 篇：{[d.id for d in retry]}", flush=True)
        finished = 0
        await asyncio.gather(*(run(d) for d in retry))

    write_workbook(diaries, version, out_path)
    left = unfinished()
    print(f"完成 {len(diaries) - len(left)}/{len(diaries)}，失败 {[d.id for d in left]}")
    print("已写出：", out_path)


if __name__ == "__main__":
    asyncio.run(main())
