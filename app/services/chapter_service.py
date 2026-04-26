"""
CRUD para TB_NOVEL_CHAPTER + lógica de publicación (TB_CHAPTER_HISTORY).

Publicar un capítulo significa:
  1. Tomar el contenido (explícito o ensamblado desde fragmentos ordenados).
  2. Crear un nuevo registro en TB_CHAPTER_HISTORY con versión autoincremental.
  3. Actualizar TB_NOVEL_CHAPTER.current_history_id al nuevo snapshot.
     (Aquí se garantiza la integridad de la FK circular — ver models.py)
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import ChapterHistory, Fragment, NovelChapter
from app.schemas import ChapterCreate, ChapterUpdate


# ── NovelChapter CRUD ─────────────────────────────────────────────────────────

def get_chapter(db: Session, chapter_id: int) -> NovelChapter | None:
    return db.query(NovelChapter).filter(NovelChapter.id == chapter_id).first()


def get_chapters_by_novel(db: Session, novel_id: int) -> list[NovelChapter]:
    return (
        db.query(NovelChapter)
        .filter(NovelChapter.novel_id == novel_id)
        .order_by(NovelChapter.chapter_number)
        .all()
    )


def create_chapter(db: Session, data: ChapterCreate) -> NovelChapter:
    chapter = NovelChapter(**data.model_dump())
    db.add(chapter)
    db.commit()
    db.refresh(chapter)
    return chapter


def update_chapter(db: Session, chapter_id: int, data: ChapterUpdate) -> NovelChapter | None:
    chapter = get_chapter(db, chapter_id)
    if not chapter:
        return None
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(chapter, field, value)
    db.commit()
    db.refresh(chapter)
    return chapter


def delete_chapter(db: Session, chapter_id: int) -> bool:
    chapter = get_chapter(db, chapter_id)
    if not chapter:
        return False
    db.delete(chapter)
    db.commit()
    return True


# ── Publicación ───────────────────────────────────────────────────────────────

def publish_chapter(
    db: Session,
    chapter_id: int,
    content: Optional[str] = None,
) -> ChapterHistory:
    """
    Crea un snapshot inmutable del capítulo y lo marca como el publicado actual.

    Si `content` es None, el contenido se ensambla concatenando los fragmentos
    del capítulo en orden ascendente de `Fragment.order`, separados por \\n\\n.
    Esto es útil cuando el capítulo vive "vivo" en fragmentos y se quiere
    publicar el estado actual con un solo endpoint.
    """
    chapter = get_chapter(db, chapter_id)
    if not chapter:
        raise ValueError(f"Chapter {chapter_id} not found")

    if content is None:
        # Ensambla desde fragmentos si no se proporcionó texto explícito
        fragments = (
            db.query(Fragment)
            .filter(Fragment.chapter_id == chapter_id)
            .order_by(Fragment.order)
            .all()
        )
        content = "\n\n".join(f.content for f in fragments)

    # Determina la próxima versión; si no hay historial previo, arranca en 1
    last_version = (
        db.query(func.max(ChapterHistory.version))
        .filter(ChapterHistory.chapter_id == chapter_id)
        .scalar()
        or 0
    )

    history = ChapterHistory(
        chapter_id=chapter_id,
        content=content,
        version=last_version + 1,
        published_at=datetime.utcnow(),
    )
    db.add(history)
    # flush asigna el id antes de usarlo en el siguiente paso
    db.flush()

    # Actualiza el puntero del capítulo al snapshot recién creado
    # (aquí se garantiza la integridad de la "FK circular" a nivel de aplicación)
    chapter.current_history_id = history.id
    db.commit()
    db.refresh(history)
    return history


# ── ChapterHistory (solo lectura — se crea únicamente vía publish_chapter) ───

def get_chapter_history(db: Session, history_id: int) -> ChapterHistory | None:
    return db.query(ChapterHistory).filter(ChapterHistory.id == history_id).first()


def get_history_by_chapter(db: Session, chapter_id: int) -> list[ChapterHistory]:
    """Devuelve todo el historial del capítulo, del más reciente al más antiguo."""
    return (
        db.query(ChapterHistory)
        .filter(ChapterHistory.chapter_id == chapter_id)
        .order_by(ChapterHistory.version.desc())
        .all()
    )
