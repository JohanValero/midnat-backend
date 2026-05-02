from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import (
    NovelCreate, NovelResponse, NovelUpdate,
    NovelEntityDetailResponse, NovelEntitySummaryResponse,
)
from app.services import novel_service, novel_entity_service, export_service


router: APIRouter = APIRouter(prefix="/novels", tags=["Novels"])


@router.get("/", response_model=list[NovelResponse])
def list_novels(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    return novel_service.get_novels(db, skip, limit)


@router.get("/by-project/{project_id}", response_model=list[NovelResponse])
def list_novels_by_project(project_id: int, db: Session = Depends(get_db)):
    return novel_service.get_novels_by_project(
        db,
        project_id
    )


@router.post("/", response_model=NovelResponse, status_code=status.HTTP_201_CREATED)
def create_novel(data: NovelCreate, db: Session = Depends(get_db)):
    return novel_service.create_novel(db, data)


@router.get("/{novel_id}", response_model=NovelResponse)
def get_novel(novel_id: int, db: Session = Depends(get_db)):
    n = novel_service.get_novel(db, novel_id)
    if not n:
        raise HTTPException(status_code=404, detail="Novel not found")
    return n


@router.patch("/{novel_id}", response_model=NovelResponse)
def update_novel(novel_id: int, data: NovelUpdate, db: Session = Depends(get_db)):
    n = novel_service.update_novel(db, novel_id, data)
    if not n:
        raise HTTPException(status_code=404, detail="Novel not found")
    return n


@router.delete("/{novel_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_novel(novel_id: int, db: Session = Depends(get_db)):
    if not novel_service.delete_novel(db, novel_id):
        raise HTTPException(status_code=404, detail="Novel not found")


@router.get("/{novel_id}/export/{format}")
def export_novel(novel_id: int, format: str, db: Session = Depends(get_db)):
    """Exporta la novela completa en Markdown, docx o PDF."""
    try:
        file_stream, filename, media_type = export_service.export_novel(db, novel_id, format)
        return StreamingResponse(
            file_stream,
            media_type=media_type,
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Novel Entities (entidades canónicas consolidadas) ─────────────────────────

@router.post("/{novel_id}/consolidate-entities")
async def consolidate_entities(novel_id: int, db: Session = Depends(get_db)):
    """
    Consolida las entidades de capítulo en entidades canónicas a nivel de novela.
    Limpia huérfanas, agrupa con LLM, genera descripciones. SSE stream.
    """
    n = novel_service.get_novel(db, novel_id)
    if not n:
        raise HTTPException(status_code=404, detail="Novel not found")

    return StreamingResponse(
        novel_entity_service.consolidate_entities_stream(novel_id, db),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{novel_id}/novel-entities")
def get_novel_entities(novel_id: int, db: Session = Depends(get_db)):
    """Devuelve las entidades canónicas con fragmentos y aliases consolidados."""
    n = novel_service.get_novel(db, novel_id)
    if not n:
        raise HTTPException(status_code=404, detail="Novel not found")
    return novel_entity_service.get_novel_entities_detail(db, novel_id)

