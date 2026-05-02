"""
CRUD para TB_ENTITY, TB_ENTITY_FRAGMENT y TB_ENTITY_RELATION.

Agrupa los tres dominios porque están estrechamente relacionados:
el flujo típico es crear entidades → vincularlas a fragmentos →
definir las relaciones entre ellas.
"""
from sqlalchemy.orm import Session, joinedload

from app.models import Entity, EntityFragment, EntityRelation, Fragment, NovelChapter
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


# ── Chapter-level operations ──────────────────────────────────────────────────

def delete_entity_links_by_chapter(db: Session, chapter_id: int) -> int:
    """
    Borra todos los vínculos EntityFragment para fragmentos del capítulo.
    No borra las entidades en sí (son a nivel de proyecto y pueden estar
    vinculadas a otros capítulos).
    Retorna el número de vínculos eliminados.
    """
    # Primero obtenemos los IDs de los fragmentos del capítulo
    frag_ids = [
        fid for (fid,) in
        db.query(Fragment.id).filter(Fragment.chapter_id == chapter_id).all()
    ]
    if not frag_ids:
        return 0

    # Borramos los vínculos que apunten a esos fragmentos
    deleted = (
        db.query(EntityFragment)
        .filter(EntityFragment.fragment_id.in_(frag_ids))
        .delete(synchronize_session=False)
    )
    db.commit()
    return deleted


def get_entities_by_chapter(db: Session, chapter_id: int) -> list[dict]:
    """
    Devuelve las entidades que aparecen en fragmentos del capítulo dado,
    junto con la lista de fragment_ids donde aparecen.

    Returns list of dicts:
        [{id, name, entity_type, description, project_id, fragment_ids: [int]}]
    """
    # Fragmentos del capítulo
    frag_ids = [
        fid for (fid,) in
        db.query(Fragment.id).filter(Fragment.chapter_id == chapter_id).all()
    ]
    if not frag_ids:
        return []

    # EntityFragments para esos fragmentos
    links = (
        db.query(EntityFragment)
        .filter(EntityFragment.fragment_id.in_(frag_ids))
        .all()
    )

    # Agrupar por entity_id
    entity_frag_map: dict[int, list[int]] = {}
    entity_aliases_map: dict[int, set[str]] = {}
    entity_ids = set()
    for link in links:
        entity_ids.add(link.entity_id)
        entity_frag_map.setdefault(link.entity_id, []).append(link.fragment_id)
        if link.alias_in_text:
            entity_aliases_map.setdefault(link.entity_id, set()).add(link.alias_in_text)

    if not entity_ids:
        return []

    # Cargar entidades
    entities = db.query(Entity).filter(Entity.id.in_(entity_ids)).all()

    result = []
    for e in entities:
        # Añadir el nombre base también a los alias por defecto para asegurar que se resalta
        aliases = entity_aliases_map.get(e.id, set())
        aliases.add(e.name)
        
        result.append({
            "id": e.id,
            "name": e.name,
            "entity_type": e.entity_type,
            "description": e.description,
            "project_id": e.project_id,
            "fragment_ids": sorted(list(set(entity_frag_map.get(e.id, [])))),
            "aliases": sorted(list(aliases))
        })

    return sorted(result, key=lambda x: x["name"])


def get_entities_by_novel(db: Session, novel_id: int) -> list[dict]:
    """
    Devuelve las entidades que aparecen en todos los fragmentos de la novela dada,
    junto con la lista de fragmentos y su contenido donde aparecen.

    Returns list of dicts:
        [{id, name, entity_type, description, project_id, fragments: [{id, content, chapter_id}]}]
    """
    # 1. Get all chapter IDs for the novel
    chapter_ids = [
        cid for (cid,) in
        db.query(NovelChapter.id).filter(NovelChapter.novel_id == novel_id).all()
    ]
    if not chapter_ids:
        return []

    # 2. Get all fragments for these chapters
    fragments = db.query(Fragment).filter(Fragment.chapter_id.in_(chapter_ids)).all()
    if not fragments:
        return []
    
    frag_map = {f.id: {"id": f.id, "content": f.content, "chapter_id": f.chapter_id, "order": f.order} for f in fragments}
    frag_ids = list(frag_map.keys())

    # 3. Get EntityFragments for those fragments
    links = (
        db.query(EntityFragment)
        .filter(EntityFragment.fragment_id.in_(frag_ids))
        .all()
    )

    # Agrupar por entity_id
    entity_frag_map: dict[int, set[int]] = {}
    entity_aliases_map: dict[int, set[str]] = {}
    entity_ids = set()
    for link in links:
        entity_ids.add(link.entity_id)
        entity_frag_map.setdefault(link.entity_id, set()).add(link.fragment_id)
        if link.alias_in_text:
            entity_aliases_map.setdefault(link.entity_id, set()).add(link.alias_in_text)

    if not entity_ids:
        return []

    # Cargar entidades
    entities = db.query(Entity).filter(Entity.id.in_(entity_ids)).all()

    result = []
    for e in entities:
        aliases = entity_aliases_map.get(e.id, set())
        aliases.add(e.name)
        
        # Get fragment objects
        e_frag_ids = entity_frag_map.get(e.id, set())
        e_fragments = [frag_map[fid] for fid in e_frag_ids if fid in frag_map]
        
        # Sort fragments by chapter_id then order to keep narrative order
        e_fragments.sort(key=lambda x: (x["chapter_id"], x["order"]))

        result.append({
            "id": e.id,
            "name": e.name,
            "entity_type": e.entity_type,
            "description": e.description,
            "project_id": e.project_id,
            "fragments": e_fragments,
            "aliases": sorted(list(aliases))
        })

    return sorted(result, key=lambda x: x["name"])
