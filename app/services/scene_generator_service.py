"""
Servicio de generación de escenas usando LLM local.

Proceso holístico (una sola llamada al LLM):
  1. Borrar escenas existentes del capítulo.
  2. Enviar TODO el texto del capítulo al LLM y pedirle que identifique
     y describa cada escena, devolviendo un JSON estructurado.
  3. Mapear cada escena a los fragmentos del capítulo usando el campo
     `start_text` (las primeras palabras donde inicia la escena).
  4. Crear registros Scene en BD y asignar scene_id a los fragmentos.

Errores: la llamada LLM se reintenta hasta 3 veces. Si falla, aborta.
Todos los eventos se emiten como SSE.
"""
import json
import logging
import re
from typing import AsyncGenerator
import httpx

from sqlalchemy.orm import Session

from app.models import Fragment, Scene
from app.config import settings
from app.services import scene_service, fragment_service, llm_service

log = logging.getLogger("scene_generator")

LLAMA_URL = settings.llama_url
TEMPERATURE = 0.1
MAX_TOKENS = 8512
MAX_RETRIES = 3

SYSTEM_SCENES = """\
Eres un analizador de estructura narrativa experto. Tu tarea es leer el capítulo completo y dividirlo en ESCENAS LARGAS E IMPORTANTES.
Evita fragmentar la narrativa en demasiadas escenas pequeñas; buscamos bloques narrativos significativos que mantengan la continuidad dramática y el peso emocional.

Un cambio de escena SOLAMENTE ocurre cuando hay una ruptura clara en la continuidad situacional:
- Cambio físico de ubicación (ej. de una oficina a un bosque).
- Salto temporal significativo (ej. "al día siguiente", "semanas después").
- Cambio total o casi total de los personajes presentes.
- Un cambio drástico en la perspectiva narrativa o el tono que actúe como un corte cinematográfico.

IMPORTANTE SOBRE LA CONTINUIDAD:
- Si la "cámara" narrativa fluye sin cortes, mantén la escena unida aunque la acción evolucione.
- Ejemplo: Si un personaje está realizando un stream y de repente ocurre un terremoto en el mismo lugar, eso es UNA SOLA ESCENA continua. No dividas escenas solo porque la acción cambie o el conflicto escale; divídelas solo cuando el contexto situacional cambie por completo.
- Se prefieren escenas extensas y cohesionadas sobre múltiples fragmentos cortos de acción.

Para cada escena identificada, proporciona:
- "title": título corto de la escena (máx. 8 palabras)
- "description": resumen breve (2-3 frases)
- "start_text": copia textual las primeras 15-25 palabras del fragmento donde INICIA la escena (esto se usará para localizar la escena en el texto)
- "key_points": lista de 3-5 puntos clave breves

REGLAS DE FORMATO:
- La primera escena siempre inicia al comienzo del capítulo.
- El "start_text" debe ser una COPIA EXACTA del texto original.
- Responde ÚNICAMENTE con un JSON array válido.

Ejemplo:
[
  {
    "title": "El stream interrumpido",
    "description": "Alex presenta su canal a los seguidores cuando un terremoto violento sacude el edificio, forzándolo a buscar refugio bajo el escritorio.",
    "start_text": "Bienvenidos de nuevo a mi canal hoy vamos a hablar de algo especial",
    "key_points": ["Presentación del personaje", "Inicio del sismo", "Lucha por la supervivencia"]
  }
]

No añadas texto fuera del JSON."""


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"




def _strip_html(html: str) -> str:
    """Elimina tags HTML para enviar texto limpio al LLM."""
    clean = re.sub(r'<[^>]+>', '', html)
    return clean.strip()


def _normalize_for_match(text: str) -> str:
    """Normaliza texto para comparación difusa: minúsculas, sin puntuación extra, espacios colapsados."""
    text = text.lower()
    # Reemplazar puntuación por espacios
    text = re.sub(r'[^\w\sáéíóúüñàèìòùâêîôûäëïöü]', ' ', text)
    # Colapsar espacios múltiples
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _parse_scenes_json(text: str) -> list[dict]:
    """Extrae una lista JSON de escenas de la respuesta del LLM."""
    match = re.search(r'\[.*\]', text, re.DOTALL)
    if not match:
        log.warning(f"No JSON array found in LLM response: {text[:200]!r}")
        return []
    try:
        data = json.loads(match.group())
        if not isinstance(data, list):
            return []
        result = []
        for item in data:
            if not isinstance(item, dict):
                continue
            scene = {
                "title": str(item.get("title", "")).strip()[:255],
                "description": str(item.get("description", "")).strip(),
                "start_text": str(item.get("start_text", "")).strip(),
                "key_points": item.get("key_points", []),
            }
            # Incluir key_points en la descripción
            if scene["key_points"] and isinstance(scene["key_points"], list):
                points_text = "\n".join(f"• {p}" for p in scene["key_points"] if isinstance(p, str))
                if points_text:
                    scene["description"] = f"{scene['description']}\n\n{points_text}"
            result.append(scene)
        return result
    except json.JSONDecodeError as e:
        log.warning(f"Invalid JSON in LLM response: {e}")
        return []


def _match_scenes_to_fragments(
    scenes_data: list[dict],
    fragments: list[Fragment],
) -> list[tuple[dict, int]]:
    """
    Mapea cada escena a su fragmento de inicio usando el campo start_text.

    Retorna una lista de (scene_data, fragment_start_index).
    La primera escena siempre empieza en el fragmento 0.
    """
    if not scenes_data or not fragments:
        return []

    # Pre-procesar fragmentos: texto limpio normalizado
    frag_texts = [_normalize_for_match(_strip_html(f.content)) for f in fragments]

    matched: list[tuple[dict, int]] = []

    for scene_idx, scene in enumerate(scenes_data):
        if scene_idx == 0:
            # La primera escena siempre empieza al inicio
            matched.append((scene, 0))
            continue

        start_text = scene.get("start_text", "")
        if not start_text:
            log.warning(f"Scene '{scene.get('title')}' has no start_text, skipping")
            continue

        normalized_start = _normalize_for_match(start_text)
        if not normalized_start:
            continue

        # Buscar en qué fragmento aparece el start_text
        best_frag_idx = -1
        best_score = 0

        # Intentar matching directo primero
        for frag_idx, frag_text in enumerate(frag_texts):
            if normalized_start in frag_text:
                best_frag_idx = frag_idx
                break

        # Si no hay match directo, intentar con las primeras N palabras progresivamente
        if best_frag_idx == -1:
            words = normalized_start.split()
            # Intentar con 10, 8, 6, 4 palabras
            for word_count in [min(10, len(words)), min(8, len(words)), min(6, len(words)), min(4, len(words))]:
                if word_count < 3:
                    continue
                partial = " ".join(words[:word_count])
                for frag_idx, frag_text in enumerate(frag_texts):
                    if partial in frag_text:
                        best_frag_idx = frag_idx
                        break
                if best_frag_idx != -1:
                    break

        if best_frag_idx != -1:
            # Evitar que dos escenas apunten al mismo fragmento
            existing_indices = [m[1] for m in matched]
            if best_frag_idx in existing_indices:
                # Mover al siguiente fragmento disponible
                for candidate in range(best_frag_idx + 1, len(fragments)):
                    if candidate not in existing_indices:
                        best_frag_idx = candidate
                        break
            matched.append((scene, best_frag_idx))
        else:
            log.warning(
                f"Could not match scene '{scene.get('title')}' start_text to any fragment. "
                f"start_text: {start_text[:80]!r}"
            )

    # Ordenar por índice de fragmento
    matched.sort(key=lambda x: x[1])
    return matched


async def generate_scenes_stream(chapter_id: int, db: Session) -> AsyncGenerator[str, None]:
    """Generador SSE principal."""

    # ── Paso 0: borrar escenas existentes ─────────────────────────────────────
    yield _sse({"type": "status", "step": "init", "message": "Eliminando escenas anteriores..."})
    deleted = scene_service.delete_scenes_by_chapter(db, chapter_id)
    yield _sse({"type": "status", "step": "init_done",
                "message": f"{deleted} escena(s) eliminada(s)."})

    # ── Cargar fragmentos ──────────────────────────────────────────────────────
    fragments: list[Fragment] = fragment_service.get_fragments_by_chapter(db, chapter_id)
    if not fragments:
        yield _sse({"type": "error", "message": "El capítulo no tiene fragmentos."})
        yield _sse({"type": "done"})
        return

    total = len(fragments)
    yield _sse({"type": "status", "step": "fragments_loaded",
                "message": f"{total} fragmento(s) cargados. Enviando capítulo completo al LLM..."})

    # ── Paso 1: Análisis global del capítulo (1 sola llamada LLM) ─────────────
    # Construir el texto completo del capítulo con marcadores de fragmento
    text_parts = []
    for i, f in enumerate(fragments):
        text_parts.append(f"[Fragmento {i + 1}]: {_strip_html(f.content)}")

    chapter_text = "\n".join(text_parts)

    user_prompt = (
        f"Analiza el siguiente capítulo completo e identifica todas las escenas narrativas distintas.\n\n"
        f"{chapter_text}"
    )

    scenes_data: list[dict] = []

    for attempt in range(MAX_RETRIES):
        full_text = ""
        try:
            async for event in llm_service.call_llm_stream(SYSTEM_SCENES, user_prompt):
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
                yield _sse({"type": "status", "step": "retry",
                            "message": f"Error en análisis del capítulo (intento {attempt + 1}/{MAX_RETRIES}). Reintentando..."})
                continue
            else:
                yield _sse({"type": "error",
                            "message": "LLM falló 3 veces al analizar el capítulo. Proceso cancelado."})
                yield _sse({"type": "done"})
                return

        scenes_data = _parse_scenes_json(text)

        if not scenes_data:
            if attempt < MAX_RETRIES - 1:
                yield _sse({"type": "status", "step": "retry",
                            "message": f"No se pudieron parsear las escenas (intento {attempt + 1}/{MAX_RETRIES}). Reintentando..."})
                continue
            else:
                yield _sse({"type": "error",
                            "message": "No se pudieron extraer escenas del análisis del LLM tras 3 intentos."})
                yield _sse({"type": "done"})
                return

        yield _sse({"type": "text",
                    "token": f"LLM identificó {len(scenes_data)} escena(s) en el capítulo.\n"})
        break

    # ── Paso 2: Mapear escenas a fragmentos ───────────────────────────────────
    yield _sse({"type": "status", "step": "matching",
                "message": "Mapeando escenas a fragmentos del capítulo..."})

    matched_scenes = _match_scenes_to_fragments(scenes_data, fragments)

    if not matched_scenes:
        # Fallback: crear una sola escena con todos los fragmentos
        yield _sse({"type": "status", "step": "fallback",
                    "message": "No se pudieron mapear escenas. Creando escena única para todo el capítulo."})
        matched_scenes = [(scenes_data[0] if scenes_data else {"title": "Escena 1", "description": ""}, 0)]

    num_scenes = len(matched_scenes)

    # Reportar el mapeo
    for i, (scene, frag_idx) in enumerate(matched_scenes):
        yield _sse({"type": "text",
                    "token": f"Escena {i + 1}: \"{scene['title']}\" → inicia en fragmento {frag_idx + 1}/{total}\n"})

    yield _sse({"type": "status", "step": "boundaries_done",
                "message": f"{num_scenes} escena(s) mapeada(s). Guardando en base de datos..."})

    # ── Paso 3: Crear escenas en BD y asignar scene_id a fragmentos ───────────
    # Construir los rangos de fragmentos para cada escena
    boundary_indices = [m[1] for m in matched_scenes]

    created_scenes: list[Scene] = []
    for i, (scene_data, start_idx) in enumerate(matched_scenes):
        # El rango va desde start_idx hasta el inicio de la siguiente escena (o el final)
        end_idx = matched_scenes[i + 1][1] if i + 1 < num_scenes else total

        scene = Scene(
            chapter_id=chapter_id,
            title=scene_data.get("title", f"Escena {i + 1}")[:255],
            description=scene_data.get("description", ""),
        )
        db.add(scene)
        db.flush()

        # Asignar scene_id a los fragmentos de este rango
        frag_count = 0
        word_count = 0
        for frag in fragments[start_idx:end_idx]:
            frag.scene_id = scene.id
            db.add(frag)
            frag_count += 1
            word_count += len(_strip_html(frag.content).split())

        created_scenes.append(scene)

        yield _sse({"type": "text",
                    "token": f"Escena {i + 1}: \"{scene.title}\" ({frag_count} frags, ~{word_count} palabras)\n"})

    db.commit()

    # Limpiar flag de scenes_dirty para todos los fragmentos del capítulo
    db.query(Fragment).filter(Fragment.chapter_id == chapter_id).update({"scenes_dirty": False})
    db.commit()

    yield _sse({"type": "status", "step": "complete",
                "message": f"Proceso completado. {num_scenes} escena(s) generada(s)."})
    yield _sse({"type": "scenes_ready", "count": num_scenes})
    yield _sse({"type": "done"})
