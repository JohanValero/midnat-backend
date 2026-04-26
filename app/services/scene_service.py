"""CRUD para TB_SCENE."""
from sqlalchemy.orm import Session

from app.models import Scene
from app.schemas import SceneCreate, SceneUpdate


def get_scene(db: Session, scene_id: int) -> Scene | None:
    return db.query(Scene).filter(Scene.id == scene_id).first()


def get_scenes_by_chapter(db: Session, chapter_id: int) -> list[Scene]:
    return db.query(Scene).filter(Scene.chapter_id == chapter_id).all()


def create_scene(db: Session, data: SceneCreate) -> Scene:
    scene = Scene(**data.model_dump())
    db.add(scene)
    db.commit()
    db.refresh(scene)
    return scene


def update_scene(db: Session, scene_id: int, data: SceneUpdate) -> Scene | None:
    scene = get_scene(db, scene_id)
    if not scene:
        return None
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(scene, field, value)
    db.commit()
    db.refresh(scene)
    return scene


def delete_scene(db: Session, scene_id: int) -> bool:
    scene = get_scene(db, scene_id)
    if not scene:
        return False
    db.delete(scene)
    db.commit()
    return True
