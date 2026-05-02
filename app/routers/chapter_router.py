from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import (
    ChapterCreate, ChapterHistoryResponse, ChapterResponse,
    ChapterUpdate, PublishChapterRequest,
)
from app.services import chapter_service, fragment_service, llm_service, scene_generator_service, entity_generator_service, entity_service

router = APIRouter(prefix="/chapters", tags=["Chapters"])

@router.post("/{chapter_id}/summarize")
async def summarize_chapter(chapter_id: int, db: Session = Depends(get_db)):
    c = chapter_service.get_chapter(db, chapter_id)
    if not c:
        raise HTTPException(status_code=404, detail="Chapter not found")
        
    fragments = fragment_service.get_fragments_by_chapter(db, chapter_id)
    text_content = "\n\n".join([f.content for f in fragments])
    
    return StreamingResponse(
        llm_service.generate_chapter_summary_stream(text_content),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{chapter_id}/generate-scenes")
async def generate_scenes(chapter_id: int, db: Session = Depends(get_db)):
    c = chapter_service.get_chapter(db, chapter_id)
    if not c:
        raise HTTPException(status_code=404, detail="Chapter not found")

    return StreamingResponse(
        scene_generator_service.generate_scenes_stream(chapter_id, db),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{chapter_id}/generate-entities")
async def generate_entities(chapter_id: int, db: Session = Depends(get_db)):
    c = chapter_service.get_chapter(db, chapter_id)
    if not c:
        raise HTTPException(status_code=404, detail="Chapter not found")

    return StreamingResponse(
        entity_generator_service.generate_entities_stream(chapter_id, db),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{chapter_id}/entities")
def get_chapter_entities(chapter_id: int, db: Session = Depends(get_db)):
    c = chapter_service.get_chapter(db, chapter_id)
    if not c:
        raise HTTPException(status_code=404, detail="Chapter not found")
    return entity_service.get_entities_by_chapter(db, chapter_id)
# ── NovelChapter CRUD ─────────────────────────────────────────────────────────

@router.get("/by-novel/{novel_id}", response_model=list[ChapterResponse])
def list_chapters(novel_id: int, db: Session = Depends(get_db)):
    return chapter_service.get_chapters_by_novel(db, novel_id)


@router.post("/", response_model=ChapterResponse, status_code=status.HTTP_201_CREATED)
def create_chapter(data: ChapterCreate, db: Session = Depends(get_db)):
    return chapter_service.create_chapter(db, data)


@router.get("/{chapter_id}", response_model=ChapterResponse)
def get_chapter(chapter_id: int, db: Session = Depends(get_db)):
    c = chapter_service.get_chapter(db, chapter_id)
    if not c:
        raise HTTPException(status_code=404, detail="Chapter not found")
    return c


@router.patch("/{chapter_id}", response_model=ChapterResponse)
def update_chapter(chapter_id: int, data: ChapterUpdate, db: Session = Depends(get_db)):
    c = chapter_service.update_chapter(db, chapter_id, data)
    if not c:
        raise HTTPException(status_code=404, detail="Chapter not found")
    return c


@router.delete("/{chapter_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_chapter(chapter_id: int, db: Session = Depends(get_db)):
    if not chapter_service.delete_chapter(db, chapter_id):
        raise HTTPException(status_code=404, detail="Chapter not found")


# ── Publicación ───────────────────────────────────────────────────────────────

@router.post(
    "/{chapter_id}/publish",
    response_model=ChapterHistoryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Publica el capítulo creando un snapshot inmutable en el historial",
)
def publish_chapter(
    chapter_id: int,
    body: PublishChapterRequest,
    db: Session = Depends(get_db),
):
    try:
        return chapter_service.publish_chapter(db, chapter_id, body.content)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── ChapterHistory ────────────────────────────────────────────────────────────

@router.get("/{chapter_id}/history", response_model=list[ChapterHistoryResponse])
def list_history(chapter_id: int, db: Session = Depends(get_db)):
    """Devuelve todas las versiones publicadas, del más reciente al más antiguo."""
    return chapter_service.get_history_by_chapter(db, chapter_id)


@router.get("/history/{history_id}", response_model=ChapterHistoryResponse)
def get_history_entry(history_id: int, db: Session = Depends(get_db)):
    h = chapter_service.get_chapter_history(db, history_id)
    if not h:
        raise HTTPException(status_code=404, detail="History entry not found")
    return h
