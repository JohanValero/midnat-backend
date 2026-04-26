"""CRUD para TB_NOVEL."""
from sqlalchemy.orm import Session

from app.models import Novel
from app.schemas import NovelCreate, NovelUpdate


def get_novel(db: Session, novel_id: int) -> Novel | None:
    return db.query(Novel).filter(Novel.id == novel_id).first()


def get_novels(db: Session, skip: int = 0, limit: int = 100) -> list[Novel]:
    return db.query(Novel).offset(skip).limit(limit).all()


def get_novels_by_project(db: Session, project_id: int) -> list[Novel]:
    return db.query(Novel).filter(Novel.project_id == project_id).all()


def create_novel(db: Session, data: NovelCreate) -> Novel:
    novel = Novel(**data.model_dump())
    db.add(novel)
    db.commit()
    db.refresh(novel)
    return novel


def update_novel(db: Session, novel_id: int, data: NovelUpdate) -> Novel | None:
    novel = get_novel(db, novel_id)
    if not novel:
        return None
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(novel, field, value)
    db.commit()
    db.refresh(novel)
    return novel


def delete_novel(db: Session, novel_id: int) -> bool:
    novel = get_novel(db, novel_id)
    if not novel:
        return False
    db.delete(novel)
    db.commit()
    return True
