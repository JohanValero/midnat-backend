from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import (
    EntityCreate, EntityFragmentCreate, EntityFragmentResponse,
    EntityRelationCreate, EntityRelationResponse, EntityRelationUpdate,
    EntityResponse, EntityUpdate, ChapterEntityResponse
)
from app.services import entity_service

router = APIRouter(prefix="/entities", tags=["Entities"])


# ── Entity CRUD ───────────────────────────────────────────────────────────────

@router.get("/by-project/{project_id}", response_model=list[EntityResponse])
def list_entities(
    project_id: int,
    entity_type: str | None = None,   # ?entity_type=character
    db: Session = Depends(get_db),
):
    """Filtra opcionalmente por tipo: character, location, worldbuilding, etc."""
    return entity_service.get_entities_by_project(db, project_id, entity_type)


@router.get("/by-novel/{novel_id}", response_model=list[ChapterEntityResponse])
def list_entities_by_novel(novel_id: int, db: Session = Depends(get_db)):
    """Devuelve las entidades de capítulo que aparecen en la novela con sus fragmentos."""
    return entity_service.get_entities_by_novel(db, novel_id)


@router.post("/", response_model=EntityResponse, status_code=status.HTTP_201_CREATED)
def create_entity(data: EntityCreate, db: Session = Depends(get_db)):
    return entity_service.create_entity(db, data)


@router.get("/{entity_id}", response_model=EntityResponse)
def get_entity(entity_id: int, db: Session = Depends(get_db)):
    e = entity_service.get_entity(db, entity_id)
    if not e:
        raise HTTPException(status_code=404, detail="Entity not found")
    return e


@router.patch("/{entity_id}", response_model=EntityResponse)
def update_entity(entity_id: int, data: EntityUpdate, db: Session = Depends(get_db)):
    e = entity_service.update_entity(db, entity_id, data)
    if not e:
        raise HTTPException(status_code=404, detail="Entity not found")
    return e


@router.delete("/{entity_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_entity(entity_id: int, db: Session = Depends(get_db)):
    if not entity_service.delete_entity(db, entity_id):
        raise HTTPException(status_code=404, detail="Entity not found")


# ── Entity ↔ Fragment links ───────────────────────────────────────────────────

@router.post("/links", response_model=EntityFragmentResponse, status_code=status.HTTP_201_CREATED)
def link_entity_fragment(data: EntityFragmentCreate, db: Session = Depends(get_db)):
    """Vincula una entidad a un fragmento. Devuelve 409 si el vínculo ya existe."""
    try:
        return entity_service.link_entity_fragment(db, data)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Link already exists")


@router.get("/{entity_id}/fragments", response_model=list[EntityFragmentResponse])
def get_entity_fragments(entity_id: int, db: Session = Depends(get_db)):
    """Todos los fragmentos donde aparece la entidad."""
    return entity_service.get_entity_fragments(db, entity_id)


@router.get("/fragment/{fragment_id}/entities", response_model=list[EntityFragmentResponse])
def get_fragment_entities(fragment_id: int, db: Session = Depends(get_db)):
    """Todas las entidades que aparecen en un fragmento dado."""
    return entity_service.get_fragment_entities(db, fragment_id)


@router.delete(
    "/{entity_id}/fragments/{fragment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def unlink_entity_fragment(entity_id: int, fragment_id: int, db: Session = Depends(get_db)):
    if not entity_service.unlink_entity_fragment(db, entity_id, fragment_id):
        raise HTTPException(status_code=404, detail="Link not found")


# ── Entity Relations ──────────────────────────────────────────────────────────

@router.post("/relations", response_model=EntityRelationResponse, status_code=status.HTTP_201_CREATED)
def create_relation(data: EntityRelationCreate, db: Session = Depends(get_db)):
    return entity_service.create_relation(db, data)


@router.get("/{entity_id}/relations", response_model=list[EntityRelationResponse])
def get_entity_relations(entity_id: int, db: Session = Depends(get_db)):
    """Devuelve relaciones donde la entidad aparece como A o como B."""
    return entity_service.get_entity_relations(db, entity_id)


@router.patch("/relations/{relation_id}", response_model=EntityRelationResponse)
def update_relation(relation_id: int, data: EntityRelationUpdate, db: Session = Depends(get_db)):
    r = entity_service.update_relation(db, relation_id, data)
    if not r:
        raise HTTPException(status_code=404, detail="Relation not found")
    return r


@router.delete("/relations/{relation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_relation(relation_id: int, db: Session = Depends(get_db)):
    if not entity_service.delete_relation(db, relation_id):
        raise HTTPException(status_code=404, detail="Relation not found")
