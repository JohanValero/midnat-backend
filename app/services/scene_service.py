from sqlalchemy.orm import Session, joinedload

from app.models import Scene
from app.schemas import SceneCreate, SceneUpdate


def get_scene(db: Session, scene_id: int) -> Scene | None:
    return db.query(Scene).options(joinedload(Scene.fragments)).filter(Scene.id == scene_id).first()


def get_scenes_by_chapter(db: Session, chapter_id: int) -> list[Scene]:
    return db.query(Scene).options(joinedload(Scene.fragments)).filter(Scene.chapter_id == chapter_id).all()


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


def delete_scenes_by_chapter(db: Session, chapter_id: int) -> int:
    """Elimina todas las escenas de un capítulo y desvincula los fragmentos. Devuelve el número de escenas eliminadas."""
    from app.models import Fragment
    # Desvincular fragmentos
    db.query(Fragment).filter(Fragment.chapter_id == chapter_id).update({"scene_id": None})
    scenes = db.query(Scene).filter(Scene.chapter_id == chapter_id).all()
    count = len(scenes)
    for s in scenes:
        db.delete(s)
    db.commit()
    return count
