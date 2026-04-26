from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import NovelCreate, NovelResponse, NovelUpdate
from app.services import novel_service


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
