"""
Servicio de detección de entidades usando LLM local.

Proceso:
  1. Extraer entidades procesando el capítulo en bloques de 15 fragmentos.
  2. El LLM extrae entidades (base_name, alias_in_text). Se acumulan globalmente.
  3. Búsqueda exhaustiva: se busca cada alias detectado en TODO el corpus del capítulo.
  4. Vincular y guardar alias donde haya coincidencias exactas.
  5. Resumir la entidad usando una ventana de contexto de +/- 15 fragmentos.

Errores: cada llamada LLM se reintenta hasta 3 veces.
"""
import json
import logging
import re
from typing import AsyncGenerator
import httpx

from sqlalchemy.orm import Session

from app.models import Fragment, Entity, EntityFragment, NovelChapter, Novel
from app.services import entity_service, fragment_service, llm_service

log = logging.getLogger("entity_generator")

LLAMA_URL = "http://localhost:8081"
TEMPERATURE = 0.1
MAX_TOKENS = 8512
MAX_RETRIES = 3
BLOCK_SIZE = 15

ENTITY_TYPES = {"character", "object", "concept", "location", "faction", "creature", "event"}

SYSTEM_DETECT = """\
Eres un analista experto de literatura. Extrae SÓLO las entidades nombradas MÁS IMPORTANTES para la trama (personajes principales/secundarios, lugares clave, facciones importantes, objetos mágicos/únicos).
IGNORA conceptos genéricos o comunes como 'sol', 'cama', 'mesa', 'comida', 'huevos'.

Para cada entidad, indica:
- "base_name": el nombre base único (ej: "Krig")
- "alias_in_text": exactamente como aparece escrito en el texto provisto (ej: "Enana Krig", o "Håndværker Krig").
- "type": character, object, concept, location, faction, creature, event.

Si te proporciono una lista de 'Entidades conocidas', reusa el "base_name" exacto si detectas que el alias se refiere a una de ellas.

Responde ÚNICAMENTE con un JSON array válido. Ejemplo:
[
  {"base_name": "Krig", "alias_in_text": "Enana Krig", "type": "character"}
]

Si no hay entidades clave, responde: []
"""

SYSTEM_SUMMARIZE = """\
Eres un asistente literario. Basándote ÚNICAMENTE en el texto proporcionado (y no en conocimiento externo), resume en 1-2 frases concretas el rol o qué hace esta entidad.
Responde ÚNICAMENTE con el resumen, sin encabezados."""


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _strip_html(html: str) -> str:
    """Elimina tags HTML para enviar texto limpio al LLM."""
    clean = re.sub(r'<[^>]+>', '', html)
    return clean.strip()




def _parse_entities_json(text: str) -> list[dict]:
    """Extrae una lista JSON de entidades: base_name, alias_in_text, type."""
    match = re.search(r'\[.*\]', text, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group())
        if not isinstance(data, list):
            return []
        result = []
        for item in data:
            if isinstance(item, dict) and "base_name" in item and "alias_in_text" in item and "type" in item:
                etype = item["type"].lower().strip()
                if etype not in ENTITY_TYPES:
                    etype = "concept"
                result.append({
                    "base_name": item["base_name"].strip(),
                    "alias_in_text": item["alias_in_text"].strip(),
                    "type": etype
                })
        return result
    except json.JSONDecodeError:
        return []


def _get_project_id(db: Session, chapter_id: int) -> int | None:
    chapter = db.query(NovelChapter).filter(NovelChapter.id == chapter_id).first()
    if not chapter:
        return None
    novel = db.query(Novel).filter(Novel.id == chapter.novel_id).first()
    if not novel:
        return None
    return novel.project_id


async def generate_entities_stream(chapter_id: int, db: Session) -> AsyncGenerator[str, None]:
    project_id = _get_project_id(db, chapter_id)
    if project_id is None:
        yield _sse({"type": "error", "message": "Chapter or project not found."})
        yield _sse({"type": "done"})
        return

    # 1. Eliminar vínculos previos
    yield _sse({"type": "status", "step": "init", "message": "Removing previous entity links..."})
    deleted = entity_service.delete_entity_links_by_chapter(db, chapter_id)
    yield _sse({"type": "status", "step": "init_done", "message": f"{deleted} link(s) removed."})

    # 2. Cargar fragmentos
    fragments = fragment_service.get_fragments_by_chapter(db, chapter_id)
    if not fragments:
        yield _sse({"type": "error", "message": "Chapter has no fragments."})
        yield _sse({"type": "done"})
        return
        
    fragments.sort(key=lambda x: x.order)
    total = len(fragments)
    
    entity_map: dict[str, dict] = {}

    # --- FASE 1: EXTRACCIÓN (LLM) EN TODO EL CAPÍTULO ---
    yield _sse({"type": "status", "step": "detect",
                "message": f"Extracting entities from the entire chapter (this might take a while)..."})

    text_parts = []
    for i, f in enumerate(fragments):
        text_parts.append(f"[Fragment {i + 1}]: {_strip_html(f.content)}")
        
    chapter_text = "\n".join(text_parts)
    
    known_list = []
    # If there were known entities passed from other chapters, we could add them here
    # but for now it's empty at the start of the chapter.
    
    user_prompt = (
        f"Extrae las entidades importantes en todo este capítulo:\n\n{chapter_text}"
    )

    for attempt in range(MAX_RETRIES):
        full_text = ""
        try:
            async for event in llm_service.call_llm_stream(SYSTEM_DETECT, user_prompt):
                yield _sse(event)
                if event["type"] == "text":
                    full_text += event["token"]
            
            ok = True
            text = full_text.strip()
        except Exception as exc:
            log.error(f"LLM call failed in attempt {attempt}: {exc}")
            ok = False
            text = str(exc)

        if not ok:
            if attempt < MAX_RETRIES - 1:
                yield _sse({"type": "status", "step": "retry", "message": f"Retry {attempt + 1} for chapter extraction..."})
                continue
            else:
                yield _sse({"type": "error", "message": f"Failed 3 times on chapter extraction."})
                yield _sse({"type": "done"})
                return

        detected = _parse_entities_json(text)
        
        for ent in detected:
            b_name = ent["base_name"]
            alias = ent["alias_in_text"]
            etype = ent["type"]
            b_key = b_name.lower()
            
            if b_key not in entity_map:
                entity_map[b_key] = {
                    "name": b_name,
                    "type": etype,
                    "aliases": set()
                }
            
            # Acumular alias detectado
            entity_map[b_key]["aliases"].add(alias)

        yield _sse({"type": "text", "token": f"Chapter extraction: {len(detected)} entity mentions found.\n"})
        break

    # --- FASE 2: BÚSQUEDA GLOBAL EN TODO EL CORPUS ---
    yield _sse({"type": "status", "step": "search",
                "message": "Scanning all fragments across the chapter for detected aliases..."})
                
    # Inicializar el mapeo de fragmentos para cada entidad
    for b_key in entity_map:
        entity_map[b_key]["aliases_map"] = {} # fid -> set(alias_encontrados)
        
    for f in fragments:
        _PUNCT = "-—.,\"';:/\\!?()[]{}"
        clean_content = _strip_html(f.content).lower().translate(str.maketrans(_PUNCT, " " * len(_PUNCT)))
        for b_key, data in entity_map.items():
            for alias in data["aliases"]:
                if alias.lower() in clean_content:
                    if f.id not in data["aliases_map"]:
                        data["aliases_map"][f.id] = set()
                    data["aliases_map"][f.id].add(alias)

    # Filtrar entidades que al final no se encontraron en el corpus (alucinaciones puros)
    keys_to_delete = []
    for b_key, data in entity_map.items():
        if not data["aliases_map"]:
            keys_to_delete.append(b_key)
            
    for k in keys_to_delete:
        del entity_map[k]

    total_entities = len(entity_map)
    yield _sse({"type": "status", "step": "detect_done",
                "message": f"{total_entities} unique entities validated against corpus. Saving..."})

    # --- FASE 3: GUARDAR EN BD ---
    existing = entity_service.get_entities_by_project(db, project_id)
    existing_by_name = {e.name.lower(): e for e in existing}

    created_count = 0
    reused_count = 0
    entity_records = []

    for key, data in entity_map.items():
        if key in existing_by_name:
            entity = existing_by_name[key]
            reused_count += 1
        else:
            entity = Entity(
                project_id=project_id,
                name=data["name"],
                entity_type=data["type"],
            )
            db.add(entity)
            db.flush()
            existing_by_name[key] = entity
            created_count += 1

        entity_records.append((entity, data["aliases_map"]))

    # Guardar vínculos
    for entity, aliases_map in entity_records:
        for fid, aliases_set in aliases_map.items():
            for alias in aliases_set:
                ef = EntityFragment(
                    entity_id=entity.id,
                    fragment_id=fid,
                    alias_in_text=alias
                )
                db.add(ef)

    db.commit()
    yield _sse({"type": "status", "step": "saved", "message": f"Saved: {created_count} new, {reused_count} reused."})

    # --- FASE 4: GENERAR RESÚMENES (Ventana +/- 15) ---
    frag_by_id = {f.id: f for f in fragments}
    frag_index_by_id = {f.id: i for i, f in enumerate(fragments)}

    for i, (entity, aliases_map) in enumerate(entity_records):
        yield _sse({"type": "status", "step": "summarize",
                    "message": f"Summarizing {entity.name} ({i + 1}/{total_entities})..."})
                    
        fids = list(aliases_map.keys())
        
        context_indices = set()
        for fid in fids:
            idx = frag_index_by_id.get(fid)
            if idx is not None:
                start = max(0, idx - 15)
                end = min(len(fragments) - 1, idx + 15)
                for c_idx in range(start, end + 1):
                    context_indices.add(c_idx)
                    
        sorted_indices = sorted(list(context_indices))
        context_text_parts = []
        for c_idx in sorted_indices:
            context_text_parts.append(_strip_html(fragments[c_idx].content))
            
        context_text = "\n\n".join(context_text_parts)

        user_prompt = (
            f"Entidad: \"{entity.name}\" (tipo: {entity.entity_type})\n\n"
            f"Apariciones en el texto (con contexto):\n{context_text[:12000]}\n\n"
            f"Basado SÓLO en este contexto, resume en 1-2 frases concretas qué hace o qué rol tiene \"{entity.name}\"."
        )

        for attempt in range(MAX_RETRIES):
            full_text = ""
            try:
                async for event in llm_service.call_llm_stream(SYSTEM_SUMMARIZE, user_prompt):
                    yield _sse(event)
                    if event["type"] == "text":
                        full_text += event["token"]
                
                ok = True
                text = full_text.strip()
            except Exception as exc:
                log.error(f"LLM call failed in summary attempt {attempt}: {exc}")
                ok = False
                text = str(exc)

            if not ok:
                if attempt < MAX_RETRIES - 1:
                    continue
                else:
                    yield _sse({"type": "status", "step": "summary_error", "message": f"Failed to summarize {entity.name}."})
                    break

            db.refresh(entity)
            entity.description = text.strip()[:1000]
            db.commit()

            yield _sse({"type": "text", "token": f"{entity.name}: {text.strip()[:120]}\n"})
            break

    yield _sse({"type": "status", "step": "complete", "message": f"Done. {total_entities} entities processed."})
    yield _sse({"type": "entities_ready", "count": total_entities})
    yield _sse({"type": "done"})
