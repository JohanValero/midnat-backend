from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import (
    FragmentCreate, FragmentInsertBetween,
    FragmentResponse, FragmentUpdate, FragmentBulkSync
)
from app.services import fragment_service

router = APIRouter(prefix="/fragments", tags=["Fragments"])

@router.post("/bulk-sync", response_model=list[FragmentResponse], status_code=status.HTTP_200_OK)
def bulk_sync_fragments(data: FragmentBulkSync, db: Session = Depends(get_db)):
    """Reemplaza/actualiza todos los fragmentos del capítulo según la lista de bloques HTML enviados."""
    return fragment_service.bulk_sync_fragments(db, data.chapter_id, data.blocks)



@router.get("/by-chapter/{chapter_id}", response_model=list[FragmentResponse])
def list_fragments(chapter_id: int, db: Session = Depends(get_db)):
    """Devuelve los fragmentos del capítulo ordenados por `order` ascendente."""
    return fragment_service.get_fragments_by_chapter(db, chapter_id)


@router.post("/", response_model=FragmentResponse, status_code=status.HTTP_201_CREATED)
def create_fragment(data: FragmentCreate, db: Session = Depends(get_db)):
    """Crea un fragmento. Si no se envía `order`, se añade al final (max+1000)."""
    return fragment_service.create_fragment(db, data)


@router.post(
    "/insert-between",
    response_model=FragmentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Inserta un fragmento entre dos existentes (midpoint ordering)",
)
def insert_fragment_between(data: FragmentInsertBetween, db: Session = Depends(get_db)):
    """
    Calcula automáticamente order = (prev_order + next_order) // 2.
    Si no hay espacio entero entre ambos valores, devuelve 400 con sugerencia
    de llamar al endpoint de rebalanceo.
    """
    try:
        return fragment_service.insert_fragment_between(db, data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{fragment_id}", response_model=FragmentResponse)
def get_fragment(fragment_id: int, db: Session = Depends(get_db)):
    f = fragment_service.get_fragment(db, fragment_id)
    if not f:
        raise HTTPException(status_code=404, detail="Fragment not found")
    return f


@router.patch("/{fragment_id}", response_model=FragmentResponse)
def update_fragment(fragment_id: int, data: FragmentUpdate, db: Session = Depends(get_db)):
    """Si se actualiza `content`, el hash se recalcula automáticamente."""
    f = fragment_service.update_fragment(db, fragment_id, data)
    if not f:
        raise HTTPException(status_code=404, detail="Fragment not found")
    return f


@router.delete("/{fragment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_fragment(fragment_id: int, db: Session = Depends(get_db)):
    if not fragment_service.delete_fragment(db, fragment_id):
        raise HTTPException(status_code=404, detail="Fragment not found")


@router.post(
    "/rebalance/{chapter_id}",
    response_model=list[FragmentResponse],
    summary="Redistribuye los órdenes del capítulo en múltiplos de 1000",
)
def rebalance_fragments(chapter_id: int, db: Session = Depends(get_db)):
    """
    Usa cuando las inserciones por midpoint han agotado el espacio entero
    entre dos fragmentos adyacentes. No altera el orden relativo.
    """
    return fragment_service.rebalance_fragments(db, chapter_id)
