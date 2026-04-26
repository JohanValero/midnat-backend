"""
Semantic Annotation API  ·  FastAPI + llama.cpp-server
-------------------------------------------------------
v6.0 – Tres endpoints independientes:
  POST /analyze/refs          → character-ref, location-ref, object-ref
  POST /analyze/blocks        → narration, internal-thought, title, scene-break
  POST /analyze/conversations → dialogue + narration (atribuciones/incisos)

Arrancar:
    llama-server -m model.gguf --port 8080 -c 8192
    uvicorn main:app --reload --port 8000
"""
from __future__ import annotations
import json
import logging
import os
import re
import traceback
from dataclasses import dataclass, field
from typing import AsyncGenerator, Callable
from xml.etree import ElementTree as ET

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO,
                    format="%(levelname)s  %(name)s  %(message)s")
log = logging.getLogger("annotator")

LLAMA_URL = os.getenv("LLAMA_URL",         "http://llm-server:8080")
CHUNK_CHARS = int(os.getenv("CHUNK_CHARS",   "2000"))
TEMPERATURE = float(os.getenv("TEMPERATURE", "0.1"))
MAX_TOKENS = int(os.getenv("MAX_TOKENS",    "8192"))

# ── Mapas de etiquetas → tipos de anotación ────────────────────────────────────

# Inline refs (el texto encontrado se busca en el chunk y se anota)
INLINE_TAG_MAP: dict[str, str] = {
    "c": "character-ref",
    "l": "location-ref",
    "o": "object-ref",   # NUEVO en v6
}

# Bloques de estructura narrativa (sin diálogo)
BLOCK_TAG_MAP_BLOCKS: dict[str, str] = {
    "p":       "narration",
    "thought": "internal-thought",
}

# Bloques de conversación
BLOCK_TAG_MAP_CONV: dict[str, str] = {
    "dialogue": "dialogue",
    "p":        "narration",   # incisos de atribución («—dijo él»)
}


class AnalyzeRequest(BaseModel):
    fullText:        str = Field(..., min_length=1)
    enable_thinking: bool = Field(False)


@dataclass
class Chunk:
    text:   str
    offset: int
    index:  int
    total:  int = field(default=0)


def split_into_chunks(full_text: str) -> list[Chunk]:
    paragraphs = full_text.split("\n")
    chunks:  list[Chunk] = []
    parts:   list[str] = []
    cur_len = abs_off = chunk_start = 0

    for para in paragraphs:
        para_len = len(para) + 1
        if parts and cur_len + para_len > CHUNK_CHARS:
            chunks.append(Chunk("\n".join(parts), chunk_start, len(chunks)))
            chunk_start = abs_off
            parts, cur_len = [], 0
        parts.append(para)
        cur_len += para_len
        abs_off += para_len

    if parts:
        chunks.append(Chunk("\n".join(parts), chunk_start, len(chunks)))
    for c in chunks:
        c.total = len(chunks)

    log.info("Chunker: %d chars → %d chunks (máx %d c/chunk)",
             len(full_text), len(chunks), CHUNK_CHARS)
    return chunks


# ── Prompts ────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT_REFS = """\
Eres un extractor de referencias para narrativa literaria en español.
Tu tarea: identificar nombres de personajes, lugares y objetos importantes.

━━━ ETIQUETAS ━━━
  <c n="Nombre canónico">texto exacto</c>   Personaje     (c = character)
  <l n="Lugar canónico">texto exacto</l>    Lugar         (l = location)
  <o n="Nombre canónico">texto exacto</o>   Objeto/artefacto con relevancia narrativa
                                            (armas, reliquias, libros, vehículos, etc.)
                                            NO incluyas objetos genéricos («la puerta», «la mesa»)

━━━ REGLAS ━━━
1. Devuelve ÚNICAMENTE el XML entre <refs>…</refs>. Nada antes ni después.
2. Incluye cada nombre único UNA sola vez, aunque aparezca varias veces en el texto.
3. Usa exactamente el texto que aparece en el fragmento.
4. Solo nombres propios y objetos con relevancia narrativa clara.

━━━ EJEMPLO ━━━
ENTRADA:
  Marta encontró el Amuleto de Edda y se lo entregó a Ander junto a la Espada Rota.
  — Guárdalo bien —dijo Marta.

SALIDA:
<refs>
<c n="Marta">Marta</c>
<l n="Edda">Edda</l>
<o n="Amuleto de Edda">Amuleto de Edda</o>
<c n="Ander">Ander</c>
<o n="Espada Rota">Espada Rota</o>
</refs>
"""

SYSTEM_PROMPT_BLOCKS = """\
Eres un clasificador de estructura narrativa para literatura en español.
Tu tarea: etiquetar ÚNICAMENTE el contenido narrativo del fragmento.
Las líneas de diálogo puro (que empiezan por raya —) NO son tu responsabilidad: márcalas con <skip>.

━━━ ETIQUETAS ━━━
  <p>…</p>                  Narración, descripciones, acciones.
                            Las atribuciones de diálogo («—dijo él», «—preguntó María»)
                            también van aquí porque son narración intercalada.
  <thought>…</thought>      Pensamiento interno o monólogo interior de un personaje.
  <title>…</title>          Título de capítulo o sección.
  <br/>                     Corte de escena (línea completamente en blanco o cambio claro
                            de espacio/tiempo).
  <skip>…</skip>            Línea de diálogo puro — NO la clasifiques aquí.

━━━ REGLAS DE ORO PARA PENSAMIENTOS ━━━
1. MONÓLOGO INTERIOR EXPLÍCITO: verbos como «pensó», «reflexionó», «se dijo», «meditó».
   Texto: «No voy a llegar a tiempo», pensó Marta.
   XML:   <thought>«No voy a llegar a tiempo», pensó Marta.</thought>

2. ESTILO INDIRECTO LIBRE: voz del personaje fundida con el narrador (dudas, juicios).
   Texto: Caminaba por el pasillo oscuro. ¿Y si alguien la seguía? Imposible. Se calmó.
   XML:   <p>Caminaba por el pasillo oscuro.</p>
          <thought>¿Y si alguien la seguía? Imposible.</thought>
          <p>Se calmó.</p>

3. PREGUNTAS RETÓRICAS INTERNAS → <thought>
4. DECISIONES O JUICIOS INTERNOS → <thought>
5. DUDAS: ¿estas palabras están en la cabeza del personaje? → <thought>
          ¿las dice un narrador externo?                    → <p>

━━━ REGLAS GENERALES ━━━
- Devuelve ÚNICAMENTE el XML entre <annotations>…</annotations>.
- TODO el texto debe estar cubierto con alguna etiqueta.
- Las líneas que empiecen por — y contengan diálogo real → <skip>.
- Los incisos narrativos dentro de una línea de diálogo («—advirtió Juan—») → <p>.
- Respeta el texto literalmente (tildes, puntuación, mayúsculas).
- No anides bloques.

━━━ EJEMPLOS ━━━

EJEMPLO 1 — Líneas de diálogo → <skip>
ENTRADA:
  — No entiendo qué pasó.
  María no se movió.
  — ¿Y ahora qué? —preguntó en voz baja.

SALIDA:
<annotations>
<skip>— No entiendo qué pasó.</skip>
<p>María no se movió.</p>
<skip>— ¿Y ahora qué?</skip>
<p>—preguntó en voz baja.</p>
</annotations>

EJEMPLO 2 — Narración pura
ENTRADA:
  La puerta se cerró tras él.

  Afuera, la lluvia comenzaba a caer.

SALIDA:
<annotations>
<p>La puerta se cerró tras él.</p>
<br/>
<br/>
<p>Afuera, la lluvia comenzaba a caer.</p>
</annotations>

EJEMPLO 3 — Pensamiento + narración + diálogo
ENTRADA:
  Abrió el cajón. ¿Dónde estaba la llave? No podía haberse evaporado.
  —¿Has visto mi llave? —preguntó a su hermana.

SALIDA:
<annotations>
<p>Abrió el cajón.</p>
<thought>¿Dónde estaba la llave? No podía haberse evaporado.</thought>
<skip>—¿Has visto mi llave?</skip>
<p>—preguntó a su hermana.</p>
</annotations>

EJEMPLO 4 — Título
ENTRADA:
  Capítulo 1: El viaje comienza

SALIDA:
<annotations>
<title>Capítulo 1: El viaje comienza</title>
</annotations>
"""

SYSTEM_PROMPT_CONVERSATIONS = """\
Eres un extractor de conversaciones para literatura en español.
Tu tarea: identificar y clasificar ÚNICAMENTE las líneas de diálogo y sus atribuciones/incisos.
El texto narrativo puro que no forme parte de conversaciones debes marcarlo con <skip>.

━━━ ETIQUETAS ━━━
  <dialogue>…</dialogue>    SOLO las palabras pronunciadas en voz alta (con su raya —).
                            Nunca incluyas el inciso narrativo.
  <p>…</p>                  Inciso de atribución («—dijo él», «—advirtió Juan—») y
                            narración intercalada DENTRO de una secuencia de diálogo.
  <skip>…</skip>            Narración pura que NO forma parte de la conversación en curso.

━━━ REGLAS DE ORO PARA DIÁLOGOS ━━━
1. Muchas líneas tienen esta estructura:
     — «Texto dicho» —inciso narrativo—. «Más texto dicho»
   Separa diálogo puro en <dialogue> y el inciso en <p>:
     <dialogue>—Si llegas tarde</dialogue>
     <p>—advirtió Juan—</p>
     <dialogue>, no te esperaremos.</dialogue>

2. Cada línea independiente de diálogo (empezando por raya) es un <dialogue> separado.

3. Las frases de atribución («—dijo él», «—preguntó María») JAMÁS van dentro de
   <dialogue>. Siempre en un <p>.

━━━ ¿CUÁNDO USAR <skip>? ━━━
- Párrafos de descripción pura alejados de cualquier conversación → <skip>
- Narración entre bloques de diálogo cuando es extensa (más de 2 frases) → <skip>
- Pensamientos internos → <skip>  (los clasifica el endpoint de estructura)
- Títulos → <skip>

Regla práctica: si el párrafo es inmediatamente adyacente a un intercambio de diálogo
y contribuye a ambientarlo o atribuirlo → <p>. Si es narración independiente → <skip>.

━━━ REGLAS GENERALES ━━━
- Devuelve ÚNICAMENTE el XML entre <annotations>…</annotations>.
- TODO el texto debe estar cubierto: <dialogue>, <p> o <skip>.
- Respeta el texto literalmente (tildes, puntuación, mayúsculas).
- No anides bloques.

━━━ EJEMPLOS ━━━

EJEMPLO 1 — Diálogo simple
ENTRADA:
  — No deberías estar aquí —dijo Elena en voz baja.

SALIDA:
<annotations>
<dialogue>— No deberías estar aquí</dialogue>
<p>—dijo Elena en voz baja.</p>
</annotations>

EJEMPLO 2 — Inciso en medio
ENTRADA:
  —Si llegas tarde —advirtió Juan—, no te esperaremos.

SALIDA:
<annotations>
<dialogue>—Si llegas tarde</dialogue>
<p>—advirtió Juan—</p>
<dialogue>, no te esperaremos.</dialogue>
</annotations>

EJEMPLO 3 — Narración + diálogo + más narración
ENTRADA:
  María no se movió. No podía creer lo que pasaba.
  —¡Ahora! —insistió él.
  La puerta se abrió de par en par y la luz inundó la habitación.
  Corrió hacia la salida. No había tiempo para pensar. El suelo temblaba bajo sus pies.

SALIDA:
<annotations>
<p>María no se movió. No podía creer lo que pasaba.</p>
<dialogue>—¡Ahora!</dialogue>
<p>—insistió él.</p>
<skip>La puerta se abrió de par en par y la luz inundó la habitación.
Corrió hacia la salida. No había tiempo para pensar. El suelo temblaba bajo sus pies.</skip>
</annotations>

EJEMPLO 4 — Múltiples turnos
ENTRADA:
  —¿Vienes conmigo? —preguntó Clara.
  Él dudó un instante.
  —No —respondió finalmente mientras se daba la vuelta—. No puedo.

SALIDA:
<annotations>
<dialogue>—¿Vienes conmigo?</dialogue>
<p>—preguntó Clara.
Él dudó un instante.</p>
<dialogue>—No</dialogue>
<p>—respondió finalmente mientras se daba la vuelta—.</p>
<dialogue> No puedo.</dialogue>
</annotations>
"""


def user_prompt_refs(chunk_text: str) -> str:
    return (
        "Extrae los nombres de personajes, lugares y objetos importantes "
        "del siguiente fragmento:\n\n"
        f"---\n{chunk_text}\n---\n\n"
        "Responde SOLO con el XML entre <refs>…</refs>."
    )


def user_prompt_blocks(chunk_text: str) -> str:
    return (
        "Clasifica la estructura narrativa del siguiente fragmento "
        "(narración, pensamientos, títulos, cortes). "
        "Usa <skip> para las líneas de diálogo puro:\n\n"
        f"---\n{chunk_text}\n---\n\n"
        "Responde SOLO con el XML entre <annotations>…</annotations>. "
        "Cubre todo el texto."
    )


def user_prompt_conversations(chunk_text: str) -> str:
    return (
        "Extrae y clasifica las conversaciones del siguiente fragmento. "
        "Usa <skip> para la narración que no forme parte de conversaciones:\n\n"
        f"---\n{chunk_text}\n---\n\n"
        "Responde SOLO con el XML entre <annotations>…</annotations>. "
        "Cubre todo el texto."
    )


# ── Streaming genérico (idéntico al de v5) ─────────────────────────────────────

async def process_chunk_pass(
    chunk: Chunk,
    client: httpx.AsyncClient,
    enable_thinking: bool,
    system_prompt: str,
    user_prompt_text: str,
) -> AsyncGenerator[tuple[str, str], None]:
    THINK_OPEN = "<|channel>thought\n"
    THINK_CLOSE = "<channel|>"
    LOOKAHEAD = max(len(THINK_OPEN), len(THINK_CLOSE)) - 1

    payload: dict = {
        "model": "local",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt_text},
        ],
        "temperature": TEMPERATURE,
        "max_tokens":  MAX_TOKENS,
        "stream":      True,
    }

    state = "waiting"
    buffer = ""

    async with client.stream(
        "POST", f"{LLAMA_URL}/v1/chat/completions", json=payload,
        timeout=httpx.Timeout(connect=10.0, read=300.0, write=10.0, pool=10.0),
    ) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line.startswith("data: "):
                continue
            raw = line[6:].strip()
            if raw == "[DONE]":
                break
            try:
                delta_obj = json.loads(raw)["choices"][0]["delta"]
            except (json.JSONDecodeError, KeyError, IndexError):
                continue

            native_think = delta_obj.get("reasoning_content") or ""
            native_xml = delta_obj.get("content") or ""

            if native_think:
                yield ("think", native_think)
                if state == "waiting":
                    state = "thinking"
                continue
            if not native_xml:
                continue
            if state == "thinking":
                state = "xml"

            buffer += native_xml
            while True:
                if state == "waiting":
                    if THINK_OPEN in buffer:
                        pre, buffer = buffer.split(THINK_OPEN, 1)
                        if pre.strip():
                            yield ("xml", pre)
                        state = "thinking"
                    elif len(buffer) > LOOKAHEAD:
                        safe = len(buffer) - LOOKAHEAD
                        yield ("xml", buffer[:safe])
                        buffer = buffer[safe:]
                    else:
                        break
                elif state == "thinking":
                    if THINK_CLOSE in buffer:
                        think_part, buffer = buffer.split(THINK_CLOSE, 1)
                        if think_part:
                            yield ("think", think_part)
                        state = "xml"
                    elif len(buffer) > LOOKAHEAD:
                        safe = len(buffer) - LOOKAHEAD
                        yield ("think", buffer[:safe])
                        buffer = buffer[safe:]
                    else:
                        break
                else:
                    yield ("xml", buffer)
                    buffer = ""
                    break

    if buffer.strip():
        yield ("think" if state == "thinking" else "xml", buffer)


# ── Parsers XML → anotaciones ─────────────────────────────────────────────────

def _sanitize_xml(raw: str, root_tag: str = "annotations") -> str:
    raw = re.sub(r"```(?:xml)?\s*", "", raw)
    m = re.search(rf"(<{root_tag}\b[^>]*>.*?</{root_tag}>)", raw, re.DOTALL)
    raw = m.group(1) if m else f"<{root_tag}>{raw.strip()}</{root_tag}>"
    return re.sub(
        r"&(?!(?:amp|lt|gt|apos|quot|#\d+|#x[0-9a-fA-F]+);)", "&amp;", raw
    )


def resolve_refs_xml(xml_str: str, chunk: Chunk, id_offset: int) -> list[dict]:
    """Endpoint /refs — anota todas las ocurrencias de cada ref única."""
    annotations: list[dict] = []
    ann_id = id_offset
    xml_clean = _sanitize_xml(xml_str, "refs")
    try:
        root = ET.fromstring(xml_clean)
    except ET.ParseError as exc:
        log.warning("[chunk %d] Refs XML malformado (%s)", chunk.index, exc)
        return []

    seen: set[tuple[str, str]] = set()
    for elem in root.iter():
        if elem.tag not in INLINE_TAG_MAP:
            continue
        ref_text = (elem.text or "").strip()
        if not ref_text:
            continue
        key = (elem.tag, ref_text)
        if key in seen:
            continue
        seen.add(key)

        ann_type = INLINE_TAG_MAP[elem.tag]
        # El atributo de metadata varía según el tipo
        if elem.tag == "c":
            meta_key, name_val = "name",     elem.get("n", ref_text)
        elif elem.tag == "l":
            meta_key, name_val = "location", elem.get("n", ref_text)
        else:  # "o"
            meta_key, name_val = "object",   elem.get("n", ref_text)

        search_pos = 0
        while True:
            idx = chunk.text.find(ref_text, search_pos)
            if idx == -1:
                break
            annotations.append({
                "id":       f"llm_{ann_id}",
                "type":     ann_type,
                "start":    chunk.offset + idx,
                "end":      chunk.offset + idx + len(ref_text),
                "metadata": {meta_key: name_val},
            })
            ann_id += 1
            search_pos = idx + len(ref_text)

    log.info("[chunk %d] Refs: %d anotaciones", chunk.index, len(annotations))
    return annotations


def _resolve_block_xml_generic(
    xml_str: str,
    chunk: Chunk,
    id_offset: int,
    tag_map: dict[str, str],
    fill_gaps: bool,
) -> list[dict]:
    """
    Resuelve etiquetas de bloque genéricas.

    tag_map   – qué etiquetas XML mapean a qué tipos de anotación.
    fill_gaps – si True, los huecos entre bloques se rellenan como 'narration'.
                Para conversations, False: los huecos son <skip> implícitos.
    Las etiquetas <skip> y etiquetas desconocidas se ignoran silenciosamente.
    """
    annotations: list[dict] = []
    ann_id = id_offset

    xml_clean = _sanitize_xml(xml_str, "annotations")
    try:
        root = ET.fromstring(xml_clean)
    except ET.ParseError as exc:
        log.warning("[chunk %d] Blocks XML malformado (%s)", chunk.index, exc)
        return _resolve_blocks_fallback(xml_str, chunk, id_offset, tag_map, fill_gaps)

    last_pos = 0

    for elem in root:
        tag = elem.tag

        # Ignorar <skip> y etiquetas desconocidas
        if tag == "skip" or (tag not in tag_map and tag not in ("br", "title")):
            # Aun así avanzamos last_pos si podemos localizar el texto
            skip_text = "".join(elem.itertext()).strip()
            if skip_text:
                idx = chunk.text.find(skip_text, last_pos)
                if idx == -1:
                    idx = chunk.text.find(skip_text)
                if idx != -1:
                    last_pos = idx + len(skip_text)
            continue

        if tag == "br":
            annotations.append({
                "id":    f"llm_{ann_id}",
                "type":  "scene-break",
                "start": chunk.offset + last_pos,
                "end":   chunk.offset + last_pos,
            })
            ann_id += 1
            continue

        if tag == "title":
            title_text = "".join(elem.itertext()).strip()
            if title_text:
                idx = chunk.text.find(title_text, last_pos)
                if idx == -1:
                    idx = chunk.text.find(title_text)
                if idx != -1:
                    annotations.append({
                        "id":    f"llm_{ann_id}",
                        "type":  "title",
                        "start": chunk.offset + idx,
                        "end":   chunk.offset + idx + len(title_text),
                    })
                    ann_id += 1
                    last_pos = idx + len(title_text)
            continue

        full_text = "".join(elem.itertext()).strip()
        block_type = tag_map.get(tag)
        if not full_text or block_type is None:
            continue

        idx = chunk.text.find(full_text, last_pos)
        if idx == -1:
            idx = chunk.text.find(full_text)
        if idx == -1:
            log.debug("[chunk %d] Sin match <%s>: %r",
                      chunk.index, tag, full_text[:80])
            continue

        # Rellenar hueco previo si corresponde
        if fill_gaps and idx > last_pos:
            gap_text = chunk.text[last_pos:idx].strip()
            if gap_text and not gap_text.startswith("—"):
                annotations.append({
                    "id":    f"llm_{ann_id}",
                    "type":  "narration",
                    "start": chunk.offset + last_pos,
                    "end":   chunk.offset + idx,
                })
                ann_id += 1

        annotations.append({
            "id":    f"llm_{ann_id}",
            "type":  block_type,
            "start": chunk.offset + idx,
            "end":   chunk.offset + idx + len(full_text),
        })
        ann_id += 1
        last_pos = idx + len(full_text)

    # Texto residual al final del chunk
    if fill_gaps and last_pos < len(chunk.text):
        gap_text = chunk.text[last_pos:].strip()
        if gap_text and not gap_text.startswith("—"):
            annotations.append({
                "id":    f"llm_{ann_id}",
                "type":  "narration",
                "start": chunk.offset + last_pos,
                "end":   chunk.offset + len(chunk.text),
            })

    log.info("[chunk %d] Bloques (%s): %d anotaciones",
             chunk.index, "/".join(tag_map.keys()), len(annotations))
    return annotations


def resolve_blocks_xml(xml_str: str, chunk: Chunk, id_offset: int) -> list[dict]:
    """Endpoint /blocks — narration, internal-thought, title, scene-break."""
    return _resolve_block_xml_generic(
        xml_str, chunk, id_offset,
        tag_map=BLOCK_TAG_MAP_BLOCKS,
        fill_gaps=True,
    )


def resolve_conversations_xml(xml_str: str, chunk: Chunk, id_offset: int) -> list[dict]:
    """Endpoint /conversations — dialogue, narration (atribuciones)."""
    return _resolve_block_xml_generic(
        xml_str, chunk, id_offset,
        tag_map=BLOCK_TAG_MAP_CONV,
        fill_gaps=False,   # los huecos son <skip> implícitos, no se rellenan
    )


def _resolve_blocks_fallback(
    xml_str: str,
    chunk: Chunk,
    id_offset: int,
    tag_map: dict[str, str],
    fill_gaps: bool,
) -> list[dict]:
    """Fallback regex cuando el XML está malformado."""
    annotations: list[dict] = []
    ann_id = id_offset

    def strip_inline(s: str) -> str:
        return re.sub(r"<[^>]+>", "", s).strip()

    # Construimos el patrón con las etiquetas que nos interesan
    tags_re = "|".join(re.escape(t) for t in list(tag_map.keys()) + ["title"])
    pattern = re.compile(rf"<({tags_re})>(.*?)</\1>", re.DOTALL)
    last_pos = 0

    for m in pattern.finditer(xml_str):
        tag = m.group(1)
        content = strip_inline(m.group(2))
        if not content:
            continue

        idx = chunk.text.find(content, last_pos)
        if idx == -1:
            idx = chunk.text.find(content)
        if idx == -1:
            continue

        if fill_gaps and idx > last_pos:
            gap = chunk.text[last_pos:idx].strip()
            if gap and not gap.startswith("—"):
                annotations.append({
                    "id": f"llm_{ann_id}", "type": "narration",
                    "start": chunk.offset + last_pos,
                    "end":   chunk.offset + idx,
                })
                ann_id += 1

        block_type = "title" if tag == "title" else tag_map.get(tag, tag)
        annotations.append({
            "id":    f"llm_{ann_id}",
            "type":  block_type,
            "start": chunk.offset + idx,
            "end":   chunk.offset + idx + len(content),
        })
        ann_id += 1
        last_pos = idx + len(content)

    if fill_gaps and last_pos < len(chunk.text):
        gap = chunk.text[last_pos:].strip()
        if gap and not gap.startswith("—"):
            annotations.append({
                "id": f"llm_{ann_id}", "type": "narration",
                "start": chunk.offset + last_pos,
                "end":   chunk.offset + len(chunk.text),
            })

    return annotations


# ── Lógica SSE común ───────────────────────────────────────────────────────────

app = FastAPI(title="Semantic Annotation API", version="6.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"],
)


def sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _make_stream(
    req: AnalyzeRequest,
    system_prompt: str,
    user_prompt_fn: Callable[[str], str],
    resolver_fn:    Callable[[str, Chunk, int], list[dict]],
) -> StreamingResponse:
    """
    Factoriza la lógica de streaming compartida por los tres endpoints.
    Cada endpoint hace UNA pasada LLM por chunk (a diferencia de v5 que hacía dos).
    Los eventos SSE son los mismos en los tres casos; el cliente los distingue
    por el endpoint al que llamó.
    """
    chunks = split_into_chunks(req.fullText)

    async def stream() -> AsyncGenerator[str, None]:
        ann_id = 0
        yield sse({"type": "start", "total_chunks": len(chunks)})

        async with httpx.AsyncClient() as client:
            for chunk in chunks:
                preview = chunk.text[:120].replace("\n", " ").strip()
                yield sse({
                    "type":         "chunk_start",
                    "chunk":        chunk.index,
                    "total_chunks": chunk.total,
                    "preview":      preview,
                    "inputText":    chunk.text,
                })

                xml_content = ""
                error_msg: str | None = None

                try:
                    async for kind, text in process_chunk_pass(
                        chunk, client, req.enable_thinking,
                        system_prompt, user_prompt_fn(chunk.text),
                    ):
                        if kind == "think":
                            yield sse({"type": "think_token", "chunk": chunk.index, "token": text})
                        else:
                            xml_content += text
                            yield sse({"type": "token",       "chunk": chunk.index, "token": text})
                except Exception as exc:
                    error_msg = f"{type(exc).__name__}: {exc!r}"
                    log.error("[chunk %d] %s\n%s",
                              chunk.index, error_msg, traceback.format_exc())

                if error_msg:
                    yield sse({"type": "error", "chunk": chunk.index, "message": error_msg})
                    continue

                resolved = resolver_fn(xml_content, chunk, ann_id)
                ann_id += len(resolved)

                log.info("[chunk %d/%d] %d anotaciones",
                         chunk.index + 1, chunk.total, len(resolved))
                yield sse({
                    "type":         "progress",
                    "chunk":        chunk.index,
                    "total_chunks": chunk.total,
                    "annotations":  resolved,
                })

        yield sse({"type": "done", "total_annotations": ann_id})

    return StreamingResponse(
        stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{LLAMA_URL}/health", timeout=5.0)
            llm_ok = r.is_success
    except Exception:
        llm_ok = False
    return {"api": "ok", "llm": "ok" if llm_ok else "unreachable"}


@app.post("/analyze/refs")
async def analyze_refs(req: AnalyzeRequest):
    """
    Extrae referencias inline: personajes, lugares y objetos con relevancia narrativa.
    Produce: character-ref, location-ref, object-ref.
    """
    return _make_stream(req, SYSTEM_PROMPT_REFS, user_prompt_refs, resolve_refs_xml)


@app.post("/analyze/blocks")
async def analyze_blocks(req: AnalyzeRequest):
    """
    Clasifica la estructura narrativa: narración, pensamiento, títulos y cortes de escena.
    Produce: narration, internal-thought, title, scene-break.
    Las líneas de diálogo puro se omiten deliberadamente (las maneja /analyze/conversations).
    """
    return _make_stream(req, SYSTEM_PROMPT_BLOCKS, user_prompt_blocks, resolve_blocks_xml)


@app.post("/analyze/conversations")
async def analyze_conversations(req: AnalyzeRequest):
    """
    Clasifica conversaciones: diálogo puro y atribuciones/incisos narrativos.
    Produce: dialogue, narration (para incisos como «—dijo él»).
    Funciona mejor cuando /analyze/blocks ya se ha ejecutado (contexto de escenas).
    """
    return _make_stream(
        req, SYSTEM_PROMPT_CONVERSATIONS,
        user_prompt_conversations, resolve_conversations_xml,
    )
