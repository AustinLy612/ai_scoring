"""FastAPI 应用：导入日记、经 SSH 调用本地 Qwen 编码评分、人工修正、导出 Excel。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from fastapi import Body, FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import exporter, importers
from .config import FRONTEND_DIR, get_settings
from .llm_client import LLMError, get_client
from .pipeline import assign_overlaps, process_diary
from .schemas import (
    DEPTH_NAMES,
    Diary,
    DiaryStatus,
    ImportTextRequest,
    ProcessRequest,
    ReviewUpdate,
    STEP_NAMES,
    Segment,
    SegmentUpdate,
    StepScoreUpdate,
)
from .store import store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="日记自我反思 AI 编码评分工具")


@app.middleware("http")
async def no_cache_static(request, call_next):
    """前端是本地静态文件，禁止浏览器缓存，避免改完代码后页面仍是旧版。"""
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------------------
# 配置与前端
# ---------------------------------------------------------------------------


@app.get("/api/config")
def read_config() -> dict:
    settings = get_settings()
    return {
        "model": settings.qwen_model if settings.qwen_model and not any(ch in settings.qwen_model for ch in r"/\:") else "qwen",
        "ssh_host": settings.llm_ssh_host,
        "ssh_configured": bool(settings.llm_ssh_user and settings.llm_ssh_password),
        "max_concurrency": settings.max_concurrency,
        "step_names": STEP_NAMES,
        "depth_names": DEPTH_NAMES,
    }


# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------


@app.post("/api/import")
async def import_files(files: list[UploadFile]) -> dict:
    added: list[str] = []
    errors: list[str] = []

    for upload in files:
        data = await upload.read()
        try:
            diaries = importers.parse_upload(upload.filename or "未命名", data)
        except Exception as exc:  # noqa: BLE001 - 单个文件失败不影响其他文件
            errors.append(f"{upload.filename}: {exc}")
            continue
        for diary in diaries:
            added.append(store.add(diary).id)

    return {"added": added, "errors": errors, "diaries": store.summaries()}


@app.post("/api/import/text")
def import_text(payload: ImportTextRequest) -> dict:
    try:
        diary = importers.parse_pasted_text(payload.id, payload.text, store.next_paste_index())
    except importers.ImportError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    added = store.add(diary)
    return {"added": [added.id], "errors": [], "diaries": store.summaries()}


@app.delete("/api/diaries")
def clear_diaries() -> dict:
    if store.is_processing():
        raise HTTPException(status_code=409, detail="正在处理中，请等待处理结束后再清空。")
    store.clear()
    return {"diaries": store.summaries()}


@app.delete("/api/diary/{diary_id}")
def delete_diary(diary_id: str) -> dict:
    if not store.remove(diary_id):
        raise HTTPException(status_code=404, detail=f"找不到日记 {diary_id}")
    return {"diaries": store.summaries()}


# ---------------------------------------------------------------------------
# 批量处理
# ---------------------------------------------------------------------------


@app.post("/api/process")
async def start_processing(payload: ProcessRequest) -> dict:
    if store.is_processing():
        raise HTTPException(status_code=409, detail="已有批量处理在进行中。")

    targets = store.targets_for_processing(payload.diary_ids, payload.force)
    if not targets:
        return {"started": 0, "diaries": store.summaries()}

    try:
        client = get_client()
    except LLMError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    for diary in targets:
        diary.status = DiaryStatus.pending
        diary.error = ""

    async def run_all() -> None:
        await asyncio.gather(*(process_diary(d, client) for d in targets))

    store.processing_task = asyncio.create_task(run_all())
    return {"started": len(targets), "diaries": store.summaries()}


@app.get("/api/status")
def read_status() -> dict:
    return {"processing": store.is_processing(), "diaries": store.summaries()}


# ---------------------------------------------------------------------------
# 读取与人工修正
# ---------------------------------------------------------------------------


@app.get("/api/diary/{diary_id}")
def read_diary(diary_id: str) -> Diary:
    diary = store.get(diary_id)
    if diary is None:
        raise HTTPException(status_code=404, detail=f"找不到日记 {diary_id}")
    return diary


@app.put("/api/diary/{diary_id}/review")
def update_review(diary_id: str, review: ReviewUpdate = Body(...)) -> Diary:
    """保存一次人工修正：片段（改步骤 / 改区间 / 删除 / 新增）+ 五个步骤的深度分。"""
    diary = store.get(diary_id)
    if diary is None:
        raise HTTPException(status_code=404, detail=f"找不到日记 {diary_id}")

    _apply_segment_updates(diary, review.segments)
    _apply_step_score_updates(diary, review.step_scores)
    diary.reviewed = True
    return diary


def _apply_segment_updates(diary: Diary, updates: list[SegmentUpdate]) -> None:
    existing = {seg.id: seg for seg in diary.segments}
    kept: list[Segment] = []
    new_index = 0

    for update in updates:
        start, end = _validate_span(diary, update)
        text = diary.text[start:end]
        original = existing.get(update.id)

        if original is None:
            new_index += 1
            kept.append(
                Segment(
                    id=_new_segment_id(diary, new_index),
                    start=start,
                    end=end,
                    text=text,
                    step=update.step,
                    step_reason=update.step_reason,
                    source="human_added",
                    status="deleted" if update.deleted else "unchanged",
                )
            )
            continue

        # 与 AI 原始结果对比判断是否被人工改动
        baseline_step = original.ai_step if original.source == "ai" else original.step
        changed = (
            update.step != baseline_step
            or start != original.start
            or end != original.end
            or update.step_reason != original.step_reason
        )

        original.start = start
        original.end = end
        original.text = text
        original.step = update.step
        original.step_reason = update.step_reason
        if update.deleted:
            original.status = "deleted"
        else:
            original.status = "modified" if changed else "unchanged"
        kept.append(original)

    # 未在提交列表中出现的 AI 片段视为被删除，保留记录
    submitted_ids = {seg.id for seg in kept}
    for seg in diary.segments:
        if seg.id not in submitted_ids:
            seg.status = "deleted"
            kept.append(seg)

    kept.sort(key=lambda s: (s.start, s.end, s.step))
    assign_overlaps(kept)
    diary.segments = kept


def _apply_step_score_updates(diary: Diary, updates: list[StepScoreUpdate]) -> None:
    for update in updates:
        score = diary.step_score(update.step)
        score.score = update.score
        score.reason = update.reason
        changed = update.score != score.ai_score or update.reason != score.ai_reason
        score.status = "modified" if changed else "unchanged"


def _validate_span(diary: Diary, update: SegmentUpdate) -> tuple[int, int]:
    start, end = update.start, update.end
    if not (0 <= start < end <= len(diary.text)):
        raise HTTPException(
            status_code=400,
            detail=f"片段位置非法：[{start}, {end})，日记长度 {len(diary.text)}",
        )
    return start, end


def _new_segment_id(diary: Diary, new_index: int) -> str:
    base = f"{diary.id}#h{new_index}"
    taken = {seg.id for seg in diary.segments}
    candidate, suffix = base, 1
    while candidate in taken:
        suffix += 1
        candidate = f"{base}_{suffix}"
    return candidate


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------


@app.get("/api/export.xlsx")
def export_xlsx() -> Response:
    diaries = store.all()
    if not diaries:
        raise HTTPException(status_code=400, detail="没有可导出的日记。")

    content = exporter.build_workbook(diaries)
    filename = f"self_reflection_coding_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# 静态前端（放在最后，避免覆盖 /api 路由）
# ---------------------------------------------------------------------------


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
