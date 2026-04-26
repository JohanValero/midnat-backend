from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import SceneCreate, SceneResponse, SceneUpdate
from app.services import scene_service

router = APIRouter(prefix="/scenes", tags=["Scenes"])


@router.get("/by-chapter/{chapter_id}", response_model=list[SceneResponse])
def list_scenes(chapter_id: int, db: Session = Depends(get_db)):
    return scene_service.get_scenes_by_chapter(db, chapter_id)


@router.post("/", response_model=SceneResponse, status_code=status.HTTP_201_CREATED)
def create_scene(data: SceneCreate, db: Session = Depends(get_db)):
    return scene_service.create_scene(db, data)


@router.get("/{scene_id}", response_model=SceneResponse)
def get_scene(scene_id: int, db: Session = Depends(get_db)):
    s = scene_service.get_scene(db, scene_id)
    if not s:
        raise HTTPException(status_code=404, detail="Scene not found")
    return s


@router.patch("/{scene_id}", response_model=SceneResponse)
def update_scene(scene_id: int, data: SceneUpdate, db: Session = Depends(get_db)):
    s = scene_service.update_scene(db, scene_id, data)
    if not s:
        raise HTTPException(status_code=404, detail="Scene not found")
    return s


@router.delete("/{scene_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_scene(scene_id: int, db: Session = Depends(get_db)):
    if not scene_service.delete_scene(db, scene_id):
        raise HTTPException(status_code=404, detail="Scene not found")
