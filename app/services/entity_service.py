"""
CRUD para TB_ENTITY, TB_ENTITY_FRAGMENT y TB_ENTITY_RELATION.

Agrupa los tres dominios porque están estrechamente relacionados:
el flujo típico es crear entidades → vincularlas a fragmentos →
definir las relaciones entre ellas.
"""
from sqlalchemy.orm import Session

from app.models import Entity, EntityFragment, EntityRelation
from app.schemas import (
    EntityCreate, EntityUpdate,
    EntityFragmentCreate,
    EntityRelationCreate, EntityRelationUpdate,
)


# ── Entity CRUD ───────────────────────────────────────────────────────────────

def get_entity(db: Session, entity_id: int) -> Entity | None:
    return db.query(Entity).filter(Entity.id == entity_id).first()


def get_entities_by_project(
    db: Session,
    project_id: int,
    entity_type: str | None = None,
) -> list[Entity]:
    """
    Devuelve todas las entidades de un proyecto.
    `entity_type` permite filtrar por categoría (character, location, etc.).
    """
    q = db.query(Entity).filter(Entity.project_id == project_id)
    if entity_type:
        q = q.filter(Entity.entity_type == entity_type)
    return q.order_by(Entity.name).all()


def create_entity(db: Session, data: EntityCreate) -> Entity:
    entity = Entity(**data.model_dump())
    db.add(entity)
    db.commit()
    db.refresh(entity)
    return entity


def update_entity(db: Session, entity_id: int, data: EntityUpdate) -> Entity | None:
    entity = get_entity(db, entity_id)
    if not entity:
        return None
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(entity, field, value)
    db.commit()
    db.refresh(entity)
    return entity


def delete_entity(db: Session, entity_id: int) -> bool:
    entity = get_entity(db, entity_id)
    if not entity:
        return False
    db.delete(entity)
    db.commit()
    return True


# ── EntityFragment (vínculos entidad ↔ fragmento) ────────────────────────────

def link_entity_fragment(db: Session, data: EntityFragmentCreate) -> EntityFragment:
    """
    Vincula una entidad a un fragmento donde aparece.
    La constraint UNIQUE (entity_id, fragment_id) en la tabla evita duplicados;
    si ya existe, SQLAlchemy lanzará IntegrityError que el router convierte en 409.
    """
    ef = EntityFragment(**data.model_dump())
    db.add(ef)
    db.commit()
    db.refresh(ef)
    return ef


def get_entity_fragments(db: Session, entity_id: int) -> list[EntityFragment]:
    """Fragmentos donde aparece una entidad dada."""
    return (
        db.query(EntityFragment)
        .filter(EntityFragment.entity_id == entity_id)
        .all()
    )


def get_fragment_entities(db: Session, fragment_id: int) -> list[EntityFragment]:
    """Entidades que aparecen en un fragmento dado."""
    return (
        db.query(EntityFragment)
        .filter(EntityFragment.fragment_id == fragment_id)
        .all()
    )


def unlink_entity_fragment(db: Session, entity_id: int, fragment_id: int) -> bool:
    ef = (
        db.query(EntityFragment)
        .filter(
            EntityFragment.entity_id == entity_id,
            EntityFragment.fragment_id == fragment_id,
        )
        .first()
    )
    if not ef:
        return False
    db.delete(ef)
    db.commit()
    return True


# ── EntityRelation ────────────────────────────────────────────────────────────

def get_relation(db: Session, relation_id: int) -> EntityRelation | None:
    return db.query(EntityRelation).filter(EntityRelation.id == relation_id).first()


def get_entity_relations(db: Session, entity_id: int) -> list[EntityRelation]:
    """
    Devuelve todas las relaciones en las que la entidad aparece,
    ya sea como entidad A o como entidad B.
    """
    return (
        db.query(EntityRelation)
        .filter(
            (EntityRelation.entity_a_id == entity_id)
            | (EntityRelation.entity_b_id == entity_id)
        )
        .all()
    )


def create_relation(db: Session, data: EntityRelationCreate) -> EntityRelation:
    rel = EntityRelation(**data.model_dump())
    db.add(rel)
    db.commit()
    db.refresh(rel)
    return rel


def update_relation(
    db: Session, relation_id: int, data: EntityRelationUpdate
) -> EntityRelation | None:
    rel = get_relation(db, relation_id)
    if not rel:
        return None
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(rel, field, value)
    db.commit()
    db.refresh(rel)
    return rel


def delete_relation(db: Session, relation_id: int) -> bool:
    rel = get_relation(db, relation_id)
    if not rel:
        return False
    db.delete(rel)
    db.commit()
    return True
