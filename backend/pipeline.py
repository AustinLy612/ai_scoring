"""单篇日记的两阶段处理流水线。"""

from __future__ import annotations

import logging

from . import prompts
from .config import get_settings
from .llm_client import LLMClient
from .locator import locate
from .schemas import STEP_NAMES, Diary, DiaryStatus, Segment, empty_step_scores

logger = logging.getLogger(__name__)

async def process_diary(diary: Diary, client: LLMClient) -> Diary:
    """就地更新 diary：写入 segments / step_scores / status / warnings。"""
    diary.status = DiaryStatus.processing
    diary.error = ""
    diary.warnings = []
    diary.segments = []
    diary.step_scores = empty_step_scores()

    try:
        raw_segments = await _stage1_annotate(diary, client)
        segments = _build_segments(diary, raw_segments)
        assign_overlaps(segments)
        diary.segments = segments
        await _stage2_score(diary, segments, client)
        diary.status = DiaryStatus.done
    except Exception as exc:  # noqa: BLE001 - 单篇失败不应中断批量
        logger.exception("处理日记 %s 失败", diary.id)
        diary.status = DiaryStatus.failed
        diary.error = str(exc)

    return diary


# ---------------------------------------------------------------------------
# 阶段一
# ---------------------------------------------------------------------------


async def _stage1_annotate(diary: Diary, client: LLMClient) -> list[dict]:
    data = await client.complete_json(
        prompts.STAGE1_SYSTEM,
        prompts.build_stage1_user_prompt(diary.id, diary.text),
    )
    raw = data.get("segments")
    if raw is None:
        raw = data.get("result") or data.get("data") or []
    if not isinstance(raw, list):
        raise ValueError(f"阶段一返回的 segments 不是列表：{raw!r}")
    return [item for item in raw if isinstance(item, dict)]


def _build_segments(diary: Diary, raw_segments: list[dict]) -> list[Segment]:
    segments: list[Segment] = []
    cursor = 0
    seen: set[tuple[int, int, int]] = set()

    for index, item in enumerate(raw_segments):
        step = _coerce_step(item.get("step"))
        snippet = str(item.get("text") or "").strip()
        reason = str(item.get("reason") or "").strip()

        if step is None:
            diary.warnings.append(f"第 {index + 1} 条标注的 step 非法（{item.get('step')!r}），已跳过")
            continue
        if not snippet:
            diary.warnings.append(f"第 {index + 1} 条标注没有文本内容，已跳过")
            continue

        located = locate(diary.text, snippet, cursor)
        if located is None:
            diary.warnings.append(
                f"片段无法在原文中定位，已跳过（步骤{step} {STEP_NAMES[step]}）：{snippet[:40]}"
            )
            continue

        key = (step, located.start, located.end)
        if key in seen:
            continue
        seen.add(key)

        if located.fuzzy:
            diary.warnings.append(
                f"片段为模糊定位，请人工核对（步骤{step} {STEP_NAMES[step]}）：{snippet[:40]}"
            )

        segments.append(
            Segment(
                id=f"{diary.id}#s{len(segments) + 1}",
                start=located.start,
                end=located.end,
                text=diary.text[located.start : located.end],
                step=step,
                step_reason=reason,
                ai_step=step,
                fuzzy_located=located.fuzzy,
            )
        )
        cursor = located.start

    segments.sort(key=lambda s: (s.start, s.end, s.step))
    return segments


def _coerce_step(value: object) -> int | None:
    try:
        step = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return step if step in STEP_NAMES else None


# ---------------------------------------------------------------------------
# 阶段二
# ---------------------------------------------------------------------------


async def _stage2_score(diary: Diary, segments: list[Segment], client: LLMClient) -> None:
    """通读全文为五个步骤各评 0/1/2；阶段一片段只作参考，没有片段的步骤也由模型判断。"""
    active = [s for s in segments if s.status != "deleted"]
    all_steps = sorted(STEP_NAMES)

    grouped = [
        {
            "step": step,
            "step_name": STEP_NAMES[step],
            "segments": [
                {"segment_id": s.id, "text": s.text, "annotation_reason": s.step_reason}
                for s in active
                if s.step == step
            ],
        }
        for step in all_steps
    ]

    system_prompt = prompts.get_stage2_system(get_settings().stage2_prompt_version)
    user_prompt = prompts.build_stage2_user_prompt(diary.id, diary.text, grouped)
    for _attempt in range(2):
        data = await client.complete_json(system_prompt, user_prompt)
        raw = data.get("step_scores")
        if raw is None:
            raw = data.get("scores") or data.get("result") or data.get("data") or []
        if not isinstance(raw, list):
            raise ValueError(f"阶段二返回的 step_scores 不是列表：{raw!r}")

        for item in raw:
            if not isinstance(item, dict):
                continue
            step = _coerce_step(item.get("step"))
            value = _coerce_depth(item.get("score"))
            if step is None or value is None or diary.step_score(step).score is not None:
                continue
            _set_ai_score(diary.step_score(step), value, str(item.get("reason") or "").strip())

        if all(diary.step_score(step).score is not None for step in all_steps):
            break

    missing = [
        f"步骤{step} {STEP_NAMES[step]}"
        for step in all_steps
        if diary.step_score(step).score is None
    ]
    if missing:
        diary.warnings.append(f"以下步骤未获得深度评分，需人工补填：{', '.join(missing)}")


def _set_ai_score(score, value: int, reason: str) -> None:
    score.score = value
    score.ai_score = value
    score.reason = reason
    score.ai_reason = reason
    score.status = "unchanged"


def _coerce_depth(value: object) -> int | None:
    try:
        depth = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return depth if depth in (0, 1, 2) else None


# ---------------------------------------------------------------------------
# 重叠检测
# ---------------------------------------------------------------------------


def assign_overlaps(segments: list[Segment]) -> None:
    """为区间相交的片段互相登记 overlaps_with（只考虑未删除的片段）。"""
    active = [s for s in segments if s.status != "deleted"]
    for seg in segments:
        seg.overlaps_with = []
    for i, a in enumerate(active):
        for b in active[i + 1 :]:
            if a.start < b.end and b.start < a.end:
                a.overlaps_with.append(b.id)
                b.overlaps_with.append(a.id)
