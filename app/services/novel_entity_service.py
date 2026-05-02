"""
Servicio para entidades canónicas a nivel de novela (TB_NOVEL_ENTITY).

Proceso de consolidación:
  1. Limpiar entidades huérfanas (sin fragmentos vinculados).
  2. Recoger todas las entidades de capítulo de la novela.
  3. Enviar al LLM la lista de entidades y pedir que agrupe las que
     representan la misma entidad narrativa.
  4. Crear TB_NOVEL_ENTITY por cada grupo canónico.
  5. Vincular TB_ENTITY.novel_entity_id a su padre.
  6. Generar descripción consolidada por entidad canónica.
"""
import json
import logging
import re
from typing import AsyncGenerator

from sqlalchemy.orm import Session

from app.models import (
    Entity, EntityFragment, Fragment, Novel,
    NovelChapter, NovelEntity,
)
from app.services.entity_generator_service import (
    _sse, _strip_html, MAX_RETRIES,
)
from app.services import llm_service

log = logging.getLogger("novel_entity_service")


# ── CRUD ──────────────────────────────────────────────────────────────────────

def get_novel_entity(db: Session, novel_entity_id: int) -> NovelEntity | None:
    return db.query(NovelEntity).filter(NovelEntity.id == novel_entity_id).first()


def get_novel_entities(db: Session, novel_id: int) -> list[NovelEntity]:
    return (
        db.query(NovelEntity)
        .filter(NovelEntity.novel_id == novel_id)
        .order_by(NovelEntity.canonical_name)
        .all()
    )


def delete_novel_entities(db: Session, novel_id: int) -> int:
    """Elimina todas las novel entities de una novela y desvincula sus entities hijas."""
    # First unlink child entities
    novel_ents = get_novel_entities(db, novel_id)
    ne_ids = [ne.id for ne in novel_ents]
    if ne_ids:
        db.query(Entity).filter(
            Entity.novel_entity_id.in_(ne_ids)
        ).update({Entity.novel_entity_id: None}, synchronize_session=False)

    deleted = (
        db.query(NovelEntity)
        .filter(NovelEntity.novel_id == novel_id)
        .delete(synchronize_session=False)
    )
    db.commit()
    return deleted


# ── Detail queries ────────────────────────────────────────────────────────────

def get_novel_entities_detail(db: Session, novel_id: int) -> list[dict]:
    """
    Devuelve las novel entities de una novela con todos sus fragmentos
    y aliases consolidados para el visor.
    """
    novel_ents = get_novel_entities(db, novel_id)
    if not novel_ents:
        return []

    # Get all chapter IDs for this novel
    chapter_ids = [
        cid for (cid,) in
        db.query(NovelChapter.id).filter(NovelChapter.novel_id == novel_id).all()
    ]

    result = []
    for ne in novel_ents:
        # Get child entities
        child_entities = (
            db.query(Entity)
            .filter(Entity.novel_entity_id == ne.id)
            .all()
        )
        child_entity_ids = [ce.id for ce in child_entities]

        # Get all entity_fragments for these child entities
        all_aliases: set[str] = set()
        all_aliases.add(ne.canonical_name)
        fragment_ids: set[int] = set()

        if child_entity_ids:
            links = (
                db.query(EntityFragment)
                .filter(EntityFragment.entity_id.in_(child_entity_ids))
                .all()
            )
            for link in links:
                fragment_ids.add(link.fragment_id)
                if link.alias_in_text:
                    all_aliases.add(link.alias_in_text)

        # Get fragment data
        fragments_data = []
        if fragment_ids:
            fragments = (
                db.query(Fragment)
                .filter(Fragment.id.in_(list(fragment_ids)))
                .all()
            )
            for f in fragments:
                fragments_data.append({
                    "id": f.id,
                    "content": f.content,
                    "chapter_id": f.chapter_id,
                })
            fragments_data.sort(key=lambda x: (x["chapter_id"], x["id"]))

        # Build child entity info
        children_info = []
        for ce in child_entities:
            # Count fragments for this child
            ce_frag_count = (
                db.query(EntityFragment)
                .filter(EntityFragment.entity_id == ce.id)
                .count()
            )
            # Get aliases for this child
            ce_aliases = set()
            ce_links = (
                db.query(EntityFragment)
                .filter(EntityFragment.entity_id == ce.id)
                .all()
            )
            for link in ce_links:
                if link.alias_in_text:
                    ce_aliases.add(link.alias_in_text)
            ce_aliases.add(ce.name)

            children_info.append({
                "id": ce.id,
                "name": ce.name,
                "entity_type": ce.entity_type,
                "fragment_count": ce_frag_count,
                "aliases": sorted(list(ce_aliases)),
            })

        result.append({
            "id": ne.id,
            "novel_id": ne.novel_id,
            "canonical_name": ne.canonical_name,
            "entity_type": ne.entity_type,
            "description": ne.description,
            "created_at": ne.created_at,
            "updated_at": ne.updated_at,
            "fragments": fragments_data,
            "all_aliases": sorted(list(all_aliases)),
            "child_entities": children_info,
            "total_fragments": len(fragments_data),
        })

    return result


# ── Consolidation ─────────────────────────────────────────────────────────────

SYSTEM_CONSOLIDATE = """\
Eres un analista literario experto. Se te dará una lista de entidades detectadas en distintos capítulos de una novela, cada una con su nombre y tipo.

Tu tarea es AGRUPAR las entidades que representan LA MISMA entidad narrativa (misma persona, lugar, objeto, etc.).
Por ejemplo: "Bror", "Håndværker Bror" y "hermano" podrían referirse a la misma persona.

Para cada grupo, indica:
- "canonical_name": el nombre más representativo y corto para esa entidad.
- "entity_type": el tipo más apropiado (character, location, object, concept, faction, creature, event).
- "members": lista de los nombres exactos de las entidades que pertenecen a este grupo.

NO inventes entidades nuevas. Solo agrupa las que se te proporcionan.
Si una entidad NO se puede agrupar con ninguna otra, ponla como grupo individual.

Responde ÚNICAMENTE con un JSON array válido. Ejemplo:
[
  {"canonical_name": "Bror", "entity_type": "character", "members": ["Bror", "Håndværker Bror"]},
  {"canonical_name": "Krig", "entity_type": "character", "members": ["Krig", "Enana Krig", "Håndværker Krig"]}
]
"""

SYSTEM_CONSOLIDATE_SUMMARY = """\
Eres un asistente literario. Basándote ÚNICAMENTE en los fragmentos de texto proporcionados, resume en 2-3 frases concretas el rol, personalidad y acciones principales de esta entidad en la novela.
Responde ÚNICAMENTE con el resumen, sin encabezados."""


def _parse_consolidation_json(text: str) -> list[dict]:
    """Parse LLM consolidation response."""
    match = re.search(r'\[.*\]', text, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group())
        if not isinstance(data, list):
            return []
        result = []
        for item in data:
            if isinstance(item, dict) and "canonical_name" in item and "members" in item:
                result.append({
                    "canonical_name": item["canonical_name"].strip(),
                    "entity_type": item.get("entity_type", "concept").strip().lower(),
                    "members": [m.strip() for m in item["members"] if isinstance(m, str)],
                })
        return result
    except json.JSONDecodeError:
        return []


def _cleanup_orphan_entities(db: Session, novel_id: int) -> int:
    """Elimina entidades sin fragmentos vinculados para los capítulos de la novela."""
    # Get chapters of this novel
    chapter_ids = [
        cid for (cid,) in
        db.query(NovelChapter.id).filter(NovelChapter.novel_id == novel_id).all()
    ]
    if not chapter_ids:
        return 0

    # Get fragment IDs of this novel's chapters
    frag_ids = [
        fid for (fid,) in
        db.query(Fragment.id).filter(Fragment.chapter_id.in_(chapter_ids)).all()
    ]

    # Get entity IDs that have links to these fragments
    linked_entity_ids = set()
    if frag_ids:
        links = (
            db.query(EntityFragment.entity_id)
            .filter(EntityFragment.fragment_id.in_(frag_ids))
            .distinct()
            .all()
        )
        linked_entity_ids = {eid for (eid,) in links}

    # Get the novel's project_id
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        return 0

    # Get all entities of this project
    all_entities = (
        db.query(Entity)
        .filter(Entity.project_id == novel.project_id)
        .all()
    )

    # Delete entities that have NO links at all (not just to this novel)
    orphan_ids = []
    for e in all_entities:
        total_links = (
            db.query(EntityFragment)
            .filter(EntityFragment.entity_id == e.id)
            .count()
        )
        if total_links == 0:
            orphan_ids.append(e.id)

    if orphan_ids:
        db.query(Entity).filter(Entity.id.in_(orphan_ids)).delete(synchronize_session=False)
        db.commit()

    return len(orphan_ids)


def _get_entities_for_novel(db: Session, novel_id: int) -> list[dict]:
    """Get all entities with fragment links for this novel's chapters."""
    chapter_ids = [
        cid for (cid,) in
        db.query(NovelChapter.id).filter(NovelChapter.novel_id == novel_id).all()
    ]
    if not chapter_ids:
        return []

    frag_ids = [
        fid for (fid,) in
        db.query(Fragment.id).filter(Fragment.chapter_id.in_(chapter_ids)).all()
    ]
    if not frag_ids:
        return []

    # Get entity IDs that appear in these fragments
    links = (
        db.query(EntityFragment)
        .filter(EntityFragment.fragment_id.in_(frag_ids))
        .all()
    )

    entity_map: dict[int, dict] = {}
    for link in links:
        if link.entity_id not in entity_map:
            entity_map[link.entity_id] = {
                "frag_ids": set(),
                "aliases": set(),
            }
        entity_map[link.entity_id]["frag_ids"].add(link.fragment_id)
        if link.alias_in_text:
            entity_map[link.entity_id]["aliases"].add(link.alias_in_text)

    if not entity_map:
        return []

    entities = db.query(Entity).filter(Entity.id.in_(list(entity_map.keys()))).all()

    result = []
    for e in entities:
        info = entity_map.get(e.id, {})
        aliases = info.get("aliases", set())
        aliases.add(e.name)
        result.append({
            "id": e.id,
            "name": e.name,
            "entity_type": e.entity_type,
            "frag_ids": info.get("frag_ids", set()),
            "aliases": aliases,
        })

    return sorted(result, key=lambda x: x["name"])


async def consolidate_entities_stream(
    novel_id: int, db: Session
) -> AsyncGenerator[str, None]:
    """
    Stream SSE que consolida entidades de capítulo en entidades canónicas
    a nivel de novela.
    """
    novel = db.query(Novel).filter(Novel.id == novel_id).first()
    if not novel:
        yield _sse({"type": "error", "message": "Novel not found."})
        yield _sse({"type": "done"})
        return

    # PASO 1: Limpiar novel entities anteriores
    yield _sse({"type": "status", "step": "init",
                "message": "Removing previous novel entities..."})
    deleted_ne = delete_novel_entities(db, novel_id)
    yield _sse({"type": "status", "step": "init_done",
                "message": f"{deleted_ne} previous novel entities removed."})

    # PASO 2: Limpiar entidades huérfanas
    yield _sse({"type": "status", "step": "cleanup",
                "message": "Cleaning up orphan entities..."})
    orphans_deleted = _cleanup_orphan_entities(db, novel_id)
    yield _sse({"type": "status", "step": "cleanup_done",
                "message": f"{orphans_deleted} orphan entities deleted."})

    # PASO 3: Recoger entidades de la novela
    yield _sse({"type": "status", "step": "collect",
                "message": "Collecting chapter entities..."})
    entities = _get_entities_for_novel(db, novel_id)
    if not entities:
        yield _sse({"type": "error",
                     "message": "No chapter entities found. Generate entities from chapters first."})
        yield _sse({"type": "done"})
        return

    yield _sse({"type": "status", "step": "collect_done",
                "message": f"Found {len(entities)} chapter entities."})

    # PASO 4: Agrupar con LLM
    yield _sse({"type": "status", "step": "consolidate",
                "message": "Asking LLM to group related entities..."})

    entity_list_text = "\n".join([
        f"- \"{e['name']}\" (type: {e['entity_type']}, aliases: {', '.join(e['aliases'])})"
        for e in entities
    ])

    user_prompt = (
        f"Novela: \"{novel.title}\"\n\n"
        f"Lista de entidades detectadas en los capítulos:\n{entity_list_text}\n\n"
        f"Agrupa las entidades que representan la misma entidad narrativa."
    )

    groups = []
    for attempt in range(MAX_RETRIES):
        full_text = ""
        try:
            async for event in llm_service.call_llm_stream(SYSTEM_CONSOLIDATE, user_prompt):
                yield _sse(event)
                if event["type"] == "text":
                    full_text += event["token"]
            
            ok = True
            text = full_text.strip()
        except Exception as exc:
            log.error(f"LLM call failed in consolidation attempt {attempt}: {exc}")
            ok = False
            text = str(exc)

        if not ok:
            if attempt < MAX_RETRIES - 1:
                yield _sse({"type": "status", "step": "retry",
                            "message": f"Retry {attempt + 1}..."})
                continue
            else:
                yield _sse({"type": "error",
                             "message": "Failed to consolidate entities after retries."})
                yield _sse({"type": "done"})
                return

        groups = _parse_consolidation_json(text)
        if groups:
            yield _sse({"type": "text",
                        "token": f"LLM grouped entities into {len(groups)} canonical entities.\n"})
            break
        elif attempt < MAX_RETRIES - 1:
            yield _sse({"type": "status", "step": "retry",
                        "message": f"Failed to parse response, retrying..."})
            continue
        else:
            yield _sse({"type": "error",
                         "message": "Failed to parse LLM consolidation response."})
            yield _sse({"type": "done"})
            return

    # PASO 5: Crear TB_NOVEL_ENTITY y vincular
    yield _sse({"type": "status", "step": "save",
                "message": "Saving novel entities..."})

    entity_by_name = {e["name"].lower(): e for e in entities}
    novel_entity_records = []

    for group in groups:
        ne = NovelEntity(
            novel_id=novel_id,
            canonical_name=group["canonical_name"],
            entity_type=group["entity_type"],
        )
        db.add(ne)
        db.flush()  # Get the ID

        # Link child entities
        member_entity_ids = []
        for member_name in group["members"]:
            entity_info = entity_by_name.get(member_name.lower())
            if entity_info:
                entity_obj = db.query(Entity).filter(Entity.id == entity_info["id"]).first()
                if entity_obj:
                    entity_obj.novel_entity_id = ne.id
                    member_entity_ids.append(entity_info["id"])

        novel_entity_records.append((ne, member_entity_ids))

    db.commit()
    yield _sse({"type": "status", "step": "save_done",
                "message": f"Created {len(novel_entity_records)} novel entities."})

    # PASO 6: Generar descripciones consolidadas
    # Collect all fragments for context
    chapter_ids = [
        cid for (cid,) in
        db.query(NovelChapter.id).filter(NovelChapter.novel_id == novel_id).all()
    ]
    all_fragments = (
        db.query(Fragment)
        .filter(Fragment.chapter_id.in_(chapter_ids))
        .order_by(Fragment.chapter_id, Fragment.order)
        .all()
    )
    frag_by_id = {f.id: f for f in all_fragments}
    frag_index_by_id = {f.id: i for i, f in enumerate(all_fragments)}

    total = len(novel_entity_records)
    for i, (ne, member_entity_ids) in enumerate(novel_entity_records):
        yield _sse({"type": "status", "step": "summarize",
                    "message": f"Summarizing {ne.canonical_name} ({i + 1}/{total})..."})

        # Get all fragment IDs linked to member entities
        if not member_entity_ids:
            continue

        links = (
            db.query(EntityFragment)
            .filter(EntityFragment.entity_id.in_(member_entity_ids))
            .all()
        )
        fids = list({link.fragment_id for link in links})

        if not fids:
            continue

        # Build context window (+/- 15 fragments around each occurrence)
        context_indices = set()
        for fid in fids:
            idx = frag_index_by_id.get(fid)
            if idx is not None:
                start = max(0, idx - 15)
                end = min(len(all_fragments) - 1, idx + 15)
                for c_idx in range(start, end + 1):
                    context_indices.add(c_idx)

        sorted_indices = sorted(list(context_indices))
        context_text_parts = []
        for c_idx in sorted_indices:
            context_text_parts.append(_strip_html(all_fragments[c_idx].content))

        context_text = "\n\n".join(context_text_parts)

        # Get all aliases
        all_aliases = set()
        all_aliases.add(ne.canonical_name)
        for link in links:
            if link.alias_in_text:
                all_aliases.add(link.alias_in_text)
        for eid in member_entity_ids:
            e = db.query(Entity).filter(Entity.id == eid).first()
            if e:
                all_aliases.add(e.name)

        user_prompt = (
            f"Entidad: \"{ne.canonical_name}\" (tipo: {ne.entity_type})\n"
            f"Aliases conocidos: {', '.join(sorted(all_aliases))}\n\n"
            f"Apariciones en el texto (con contexto):\n{context_text[:12000]}\n\n"
            f"Basado SÓLO en este contexto, resume en 2-3 frases concretas qué hace, "
            f"qué rol tiene y su personalidad \"{ne.canonical_name}\"."
        )

        for attempt in range(MAX_RETRIES):
            full_text = ""
            try:
                async for event in llm_service.call_llm_stream(SYSTEM_CONSOLIDATE_SUMMARY, user_prompt):
                    yield _sse(event)
                    if event["type"] == "text":
                        full_text += event["token"]
                
                ok = True
                text = full_text.strip()
            except Exception as exc:
                log.error(f"LLM call failed in novel entity summary attempt {attempt}: {exc}")
                ok = False
                text = str(exc)

            if not ok:
                if attempt < MAX_RETRIES - 1:
                    continue
                else:
                    yield _sse({"type": "status", "step": "summary_error",
                                "message": f"Failed to summarize {ne.canonical_name}."})
                    break

            db.refresh(ne)
            ne.description = text.strip()[:2000]
            db.commit()

            yield _sse({"type": "text",
                        "token": f"{ne.canonical_name}: {text.strip()[:150]}\n"})
            break

    yield _sse({"type": "status", "step": "complete",
                "message": f"Done. {total} novel entities consolidated."})
    yield _sse({"type": "novel_entities_ready", "count": total})
    yield _sse({"type": "done"})
