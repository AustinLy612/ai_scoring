"""AI 深度分与人工最终一致分（final）的一致率检验，会调用本地 Qwen。

运行：python -m tests.agreement_check <人工编码表.xlsx> [导出结果.xlsx] [--prompt=v1|v2]
人工编码表需含 match_ID、Text 两列，以及最后 5 列为 final 的五步分数。
AI 分数与理由写入 tests/_agreement_reasons_<版本>.json。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import Counter
from pathlib import Path

from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import get_settings  # noqa: E402
from backend.exporter import build_workbook  # noqa: E402
from backend.importers import normalize_diary_text  # noqa: E402
from backend.llm_client import get_client  # noqa: E402
from backend.pipeline import process_diary  # noqa: E402
from backend.schemas import Diary  # noqa: E402


def load_gold(path: Path) -> tuple[list[Diary], dict[str, list[int]]]:
    sheet = load_workbook(path, data_only=True).active
    diaries: list[Diary] = []
    gold: dict[str, list[int]] = {}
    for row in sheet.iter_rows(min_row=2, values_only=True):
        if row[0] is None:
            continue
        diary_id = str(row[0]).strip()
        diaries.append(Diary(id=diary_id, text=normalize_diary_text(str(row[1]).strip()), source_name=path.name))
        gold[diary_id] = [int(v) for v in row[-5:]]
    return diaries, gold


def quadratic_kappa(a: list[int], b: list[int], k: int = 3) -> float:
    n = len(a)
    observed = Counter(zip(a, b))
    hist_a, hist_b = Counter(a), Counter(b)
    num = den = 0.0
    for i in range(k):
        for j in range(k):
            weight = (i - j) ** 2 / (k - 1) ** 2
            num += weight * observed[(i, j)] / n
            den += weight * hist_a[i] * hist_b[j] / n / n
    return 1.0 if den == 0 else 1 - num / den


def report(pred: dict[str, list[int]], gold: dict[str, list[int]]) -> None:
    ids = [i for i in gold if i in pred]
    print(f"\n{'ID':10} {'final':16} {'AI':16} 差异步骤")
    for i in ids:
        diff = [s + 1 for s in range(5) if pred[i][s] != gold[i][s]]
        print(f"{i:10} {str(gold[i]):16} {str(pred[i]):16} {diff or ''}")

    all_g = [gold[i][s] for i in ids for s in range(5)]
    all_p = [pred[i][s] for i in ids for s in range(5)]
    exact = sum(g == p for g, p in zip(all_g, all_p)) / len(all_g)
    near = sum(abs(g - p) <= 1 for g, p in zip(all_g, all_p)) / len(all_g)
    print(f"\n总体：完全一致 {exact:.0%}  相差≤1 {near:.0%}  二次加权kappa {quadratic_kappa(all_g, all_p):.2f}  (n={len(all_g)})")
    for s in range(5):
        g = [gold[i][s] for i in ids]
        p = [pred[i][s] for i in ids]
        conf = Counter((x, y) for x, y in zip(g, p) if x != y)
        misses = ", ".join(f"人{x}→AI{y}×{c}" for (x, y), c in sorted(conf.items()))
        print(f"步骤{s + 1}：完全一致 {sum(x == y for x, y in zip(g, p))}/{len(g)}  kappa {quadratic_kappa(g, p):.2f}  {misses}")
    totals = [abs(sum(pred[i]) - sum(gold[i])) for i in ids]
    print(f"总分平均绝对差 {sum(totals) / len(totals):.2f}")


def parse_args(argv: list[str]) -> tuple[list[str], str | None]:
    positional = [a for a in argv if not a.startswith("--prompt=")]
    versions = [a.split("=", 1)[1] for a in argv if a.startswith("--prompt=")]
    return positional, (versions[-1] if versions else None)


async def main() -> None:
    args, version = parse_args(sys.argv[1:])
    if version:
        os.environ["STAGE2_PROMPT_VERSION"] = version
        get_settings.cache_clear()
    version = get_settings().stage2_prompt_version
    print("评分 prompt 版本：", version)

    gold_path = Path(args[0])
    out_path = Path(args[1]) if len(args) > 1 else None
    diaries, gold = load_gold(gold_path)

    client = get_client()
    sem = asyncio.Semaphore(get_settings().max_concurrency)

    async def run(diary: Diary) -> None:
        async with sem:
            await process_diary(diary, client)
            print(f"完成 {diary.id} {diary.status.value} {[s.score for s in diary.step_scores]}", flush=True)

    await asyncio.gather(*(run(d) for d in diaries))
    failed = [d.id for d in diaries if d.status.value != "done"]
    if failed:
        print("失败：", failed)

    pred = {d.id: [s.score or 0 for s in sorted(d.step_scores, key=lambda s: s.step)] for d in diaries if d.status.value == "done"}
    report(pred, gold)

    reasons = {d.id: {s.step: s.reason for s in d.step_scores} for d in diaries}
    Path(__file__).with_name(f"_agreement_reasons_{version}.json").write_text(
        json.dumps({"prompt_version": version, "pred": pred, "reasons": reasons}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if out_path:
        out_path.write_bytes(build_workbook(diaries))
        print("已导出：", out_path)


if __name__ == "__main__":
    asyncio.run(main())
