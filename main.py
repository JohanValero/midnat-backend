"""
Semantic Annotation API  ·  FastAPI + llama.cpp-server + SQLite
---------------------------------------------------------------
v7.0 – Tres análisis independientes + persistencia de novelas/capítulos.

Endpoints:
  GET  /health
  GET  /novels                                    → listar novelas
  POST /novels                                    → crear novela vacía
  POST /novels/import                             → crear novela desde capítulos (importar .docx)
  GET  /novels/{novel_id}                         → detalle de novela
  PUT  /novels/{novel_id}                         → actualizar metadatos
  DELETE /novels/{novel_id}                       → eliminar novela
  GET  /novels/{novel_id}/chapters                → listar capítulos (sin contenido)
  POST /novels/{novel_id}/chapters                → añadir capítulo
  GET  /novels/{novel_id}/chapters/{chapter_id}   → capítulo + anotaciones
  PUT  /novels/{novel_id}/chapters/{chapter_id}   → guardar contenido + anotaciones
  DELETE /novels/{novel_id}/chapters/{chapter_id} → eliminar capítulo
  POST /novels/{novel_id}/chapters/reorder        → reordenar capítulos
  POST /novels/{novel_id}/chapters/{chapter_id}/summarize  → generar resumen LLM (SSE)
  POST /analyze/refs                              → análisis de referencias (SSE)
  POST /analyze/blocks                            → análisis de estructura (SSE)
  POST /analyze/conversations                     → análisis de conversaciones (SSE)

Arrancar:
    llama-server -m model.gguf --port 8080 -c 8192
    uvicorn main:app --reload --port 8000 --host 0.0.0.0
"""
from __future__ import annotations
import json
import logging
import os
import re
import traceback
from dataclasses import dataclass, field
from typing import AsyncGenerator, Callable, Optional
from xml.etree import ElementTree as ET

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import database as db

logging.basicConfig(level=logging.INFO,
                    format="%(levelname)s  %(name)s  %(message)s")
log = logging.getLogger("annotator")

LLAMA_URL = os.getenv("LLAMA_URL",         "http://llm-server:8080")
CHUNK_CHARS = int(os.getenv("CHUNK_CHARS",   "2000"))
TEMPERATURE = float(os.getenv("TEMPERATURE", "0.1"))
MAX_TOKENS = int(os.getenv("MAX_TOKENS",    "8192"))

# ── Tag maps (sin cambios respecto a v6) ──────────────────────────────────────

INLINE_TAG_MAP: dict[str, str] = {
    "c": "character-ref",
    "l": "location-ref",
    "o": "object-ref",
}
BLOCK_TAG_MAP_BLOCKS: dict[str, str] = {
    "p":       "narration",
    "thought": "internal-thought",
}
BLOCK_TAG_MAP_CONV: dict[str, str] = {
    "dialogue": "dialogue",
    "p":        "narration",
}


# ── Pydantic models (request/response) ────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    fullText:        str = Field(..., min_length=1)
    enable_thinking: bool = Field(False)
    # Si se proporciona, las anotaciones se guardan automáticamente en la BD.
    chapter_id: Optional[int] = Field(None)


class NovelCreateRequest(BaseModel):
    title:       str = Field(..., min_length=1)
    author:      str = Field("")
    description: str = Field("")
    cover_color: str = Field("#4c1d95")


class NovelUpdateRequest(BaseModel):
    title:       Optional[str] = None
    author:      Optional[str] = None
    description: Optional[str] = None
    cover_color: Optional[str] = None


class ChapterImportItem(BaseModel):
    title:        str = Field(..., min_length=1)
    content_text: str = Field("")
    content_html: str = Field("")


class NovelImportRequest(BaseModel):
    """Usado por el endpoint /novels/import — el cliente envía los capítulos ya divididos."""
    title:       str = Field(..., min_length=1)
    author:      str = Field("")
    description: str = Field("")
    cover_color: str = Field("#4c1d95")
    chapters:    list[ChapterImportItem] = Field(default_factory=list)


class ChapterCreateRequest(BaseModel):
    title:        str = Field(..., min_length=1)
    content_text: str = Field("")
    content_html: str = Field("")
    order_index:  Optional[int] = None


class ChapterUpdateRequest(BaseModel):
    title:        Optional[str] = None
    content_text: Optional[str] = None
    content_html: Optional[str] = None
    summary:      Optional[str] = None
    # Si se proporciona, se reemplazan las anotaciones del tipo indicado.
    annotations:  Optional[list[dict]] = None
    analysis_type_for_annotations: Optional[str] = None


class ReorderRequest(BaseModel):
    ordered_ids: list[int]


# ── Chunker ────────────────────────────────────────────────────────────────────

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


# ── System Prompts (idénticos a v6) ───────────────────────────────────────────

SYSTEM_PROMPT_REFS = """\
Eres un extractor de referencias para narrativa literaria en español.
Tu tarea: identificar nombres de personajes, lugares y objetos importantes.

━━━ ETIQUETAS ━━━
  <c n="Nombre canónico">texto exacto</c>   Personaje     (c = character)
  <l n="Lugar canónico">texto exacto</l>    Lugar         (l = location)
  <o n="Nombre canónico">texto exacto</o>   Objeto/artefacto con relevancia narrativa
                                            NO incluyas objetos genéricos («la puerta»)

━━━ REGLAS ━━━
1. Devuelve ÚNICAMENTE el XML entre <refs>…</refs>. Nada antes ni después.
2. Incluye cada nombre único UNA sola vez aunque aparezca varias veces en el texto.
3. Usa exactamente el texto que aparece en el fragmento.

━━━ EJEMPLO ━━━
ENTRADA:
  Marta encontró el Amuleto de Edda y se lo entregó a Ander.
SALIDA:
<refs>
<c n="Marta">Marta</c>
<l n="Edda">Edda</l>
<o n="Amuleto de Edda">Amuleto de Edda</o>
<c n="Ander">Ander</c>
</refs>
"""

SYSTEM_PROMPT_BLOCKS = """\
Eres un clasificador de estructura narrativa para literatura en español.
Etiqueta TODO el fragmento. Las líneas de diálogo puro (que empiezan por —) → <skip>.

━━━ ETIQUETAS ━━━
  <p>…</p>          Narración, descripciones, acciones, atribuciones de diálogo.
  <thought>…</thought> Pensamiento interno o monólogo interior.
  <title>…</title>  Título de capítulo o sección.
  <br/>             Corte de escena (línea en blanco / cambio de espacio-tiempo).
  <skip>…</skip>    Línea de diálogo puro — NO la clasifiques aquí.

━━━ REGLAS ━━━
- Devuelve SOLO el XML entre <annotations>…</annotations>.
- TODO el texto debe estar cubierto. No anides bloques.
- ¿Es la voz del personaje hablando para sí? → <thought>. ¿Narrador externo? → <p>.
"""

SYSTEM_PROMPT_CONVERSATIONS = """\
Eres un extractor de conversaciones para literatura en español.
Identifica diálogos y sus atribuciones. El resto → <skip>.

━━━ ETIQUETAS ━━━
  <dialogue>…</dialogue>  Palabras pronunciadas en voz alta (con su raya —).
  <p>…</p>                Inciso/atribución («—dijo él», narración adyacente al diálogo).
  <skip>…</skip>          Narración pura sin relación con la conversación en curso.

━━━ REGLAS ━━━
- Devuelve SOLO el XML entre <annotations>…</annotations>.
- TODO el texto debe estar cubierto.
- Las frases de atribución NUNCA van dentro de <dialogue>.
"""

SYSTEM_PROMPT_SUMMARY = """\
Eres un asistente literario especializado en resumir capítulos de novelas en español.
Genera un resumen conciso (3-5 frases) que capture:
- Los eventos principales de la trama
- El desarrollo de personajes relevantes
- El tono emocional predominante
- Cualquier revelación o giro importante

Estilo: neutro, tercera persona, presente narrativo.
Devuelve ÚNICAMENTE el resumen. Sin títulos, prefijos ni explicaciones.
"""


def _up_refs(chunk_text: str) -> str:
    return (
        "Extrae los nombres de personajes, lugares y objetos importantes "
        f"del siguiente fragmento:\n\n---\n{chunk_text}\n---\n\n"
        "Responde SOLO con el XML entre <refs>…</refs>."
    )


def _up_blocks(chunk_text: str) -> str:
    return (
        "Clasifica la estructura narrativa del siguiente fragmento. "
        f"Usa <skip> para las líneas de diálogo puro:\n\n---\n{chunk_text}\n---\n\n"
        "Responde SOLO con el XML entre <annotations>…</annotations>. Cubre todo el texto."
    )


def _up_conversations(chunk_text: str) -> str:
    return (
        "Extrae y clasifica las conversaciones del siguiente fragmento. "
        f"Usa <skip> para la narración:\n\n---\n{chunk_text}\n---\n\n"
        "Responde SOLO con el XML entre <annotations>…</annotations>. Cubre todo el texto."
    )


# ── Streaming genérico ─────────────────────────────────────────────────────────

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
        "model":       "local",
        "messages":    [
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


# ── Parsers XML → anotaciones (idénticos a v6) ────────────────────────────────

def _sanitize_xml(raw: str, root_tag: str = "annotations") -> str:
    raw = re.sub(r"```(?:xml)?\s*", "", raw)
    m = re.search(rf"(<{root_tag}\b[^>]*>.*?</{root_tag}>)", raw, re.DOTALL)
    raw = m.group(1) if m else f"<{root_tag}>{raw.strip()}</{root_tag}>"
    return re.sub(r"&(?!(?:amp|lt|gt|apos|quot|#\d+|#x[0-9a-fA-F]+);)", "&amp;", raw)


def resolve_refs_xml(xml_str: str, chunk: Chunk, id_offset: int) -> list[dict]:
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
        if not ref_text or (elem.tag, ref_text) in seen:
            continue
        seen.add((elem.tag, ref_text))

        ann_type = INLINE_TAG_MAP[elem.tag]
        meta_key = {"c": "name", "l": "location", "o": "object"}[elem.tag]
        name_val = elem.get("n", ref_text)

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

    return annotations


def _resolve_block_xml_generic(
    xml_str: str,
    chunk: Chunk,
    id_offset: int,
    tag_map: dict[str, str],
    fill_gaps: bool,
) -> list[dict]:
    annotations: list[dict] = []
    ann_id = id_offset
    xml_clean = _sanitize_xml(xml_str, "annotations")
    try:
        root = ET.fromstring(xml_clean)
    except ET.ParseError as exc:
        log.warning("[chunk %d] Blocks XML malformado (%s)", chunk.index, exc)
        return _blocks_fallback(xml_str, chunk, id_offset, tag_map, fill_gaps)

    last_pos = 0
    for elem in root:
        tag = elem.tag

        if tag == "skip" or (tag not in tag_map and tag not in ("br", "title")):
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

        if fill_gaps and idx > last_pos:
            gap = chunk.text[last_pos:idx].strip()
            if gap:
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

    if fill_gaps and last_pos < len(chunk.text):
        gap = chunk.text[last_pos:].strip()
        if gap:
            annotations.append({
                "id":    f"llm_{ann_id}",
                "type":  "narration",
                "start": chunk.offset + last_pos,
                "end":   chunk.offset + len(chunk.text),
            })

    return annotations


def resolve_blocks_xml(xml_str: str, chunk: Chunk, id_offset: int) -> list[dict]:
    return _resolve_block_xml_generic(xml_str, chunk, id_offset, BLOCK_TAG_MAP_BLOCKS, True)


def resolve_conversations_xml(xml_str: str, chunk: Chunk, id_offset: int) -> list[dict]:
    return _resolve_block_xml_generic(xml_str, chunk, id_offset, BLOCK_TAG_MAP_CONV, False)


def _blocks_fallback(
    xml_str: str,
    chunk: Chunk,
    id_offset: int,
    tag_map: dict[str, str],
    fill_gaps: bool,
) -> list[dict]:
    annotations: list[dict] = []
    ann_id = id_offset

    def strip_inline(s: str) -> str:
        return re.sub(r"<[^>]+>", "", s).strip()

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
            if gap:
                annotations.append({
                    "id": f"llm_{ann_id}", "type": "narration",
                    "start": chunk.offset + last_pos,
                    "end":   chunk.offset + idx,
                })
                ann_id += 1
        bt = "title" if tag == "title" else tag_map.get(tag, tag)
        annotations.append({
            "id":    f"llm_{ann_id}",
            "type":  bt,
            "start": chunk.offset + idx,
            "end":   chunk.offset + idx + len(content),
        })
        ann_id += 1
        last_pos = idx + len(content)

    if fill_gaps and last_pos < len(chunk.text):
        gap = chunk.text[last_pos:].strip()
        if gap:
            annotations.append({
                "id": f"llm_{ann_id}", "type": "narration",
                "start": chunk.offset + last_pos,
                "end":   chunk.offset + len(chunk.text),
            })
    return annotations


# ── Lógica SSE compartida ──────────────────────────────────────────────────────

def sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def _make_analysis_stream(
    req: AnalyzeRequest,
    analysis_type_name: str,          # 'refs' | 'blocks' | 'conversations'
    system_prompt: str,
    user_prompt_fn: Callable[[str], str],
    resolver_fn:    Callable[[str, Chunk, int], list[dict]],
) -> StreamingResponse:
    """
    Factoriza el ciclo de vida SSE para los tres endpoints de análisis.
    Diferencia clave respecto a v6: si req.chapter_id está definido,
    las anotaciones resueltas se guardan automáticamente en la BD.
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
                    log.error("[chunk %d] %s\n%s", chunk.index,
                              error_msg, traceback.format_exc())

                if error_msg:
                    yield sse({"type": "error", "chunk": chunk.index, "message": error_msg})
                    continue

                resolved = resolver_fn(xml_content, chunk, ann_id)
                ann_id += len(resolved)

                # Persistencia automática cuando el cliente especifica chapter_id
                if req.chapter_id and resolved:
                    try:
                        db.save_annotations(
                            req.chapter_id, resolved, analysis_type_name)
                        log.info(
                            "[chunk %d] %d anotaciones (%s) guardadas en capítulo %d",
                            chunk.index, len(
                                resolved), analysis_type_name, req.chapter_id,
                        )
                    except Exception as exc:
                        log.error("Error guardando anotaciones en BD: %s", exc)

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


# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(title="Semantic Annotation API", version="7.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


@app.on_event("startup")
def startup():
    db.init_db()
    log.info("Base de datos inicializada en %s", db.DB_PATH)


# ── Health ─────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{LLAMA_URL}/health", timeout=5.0)
            llm_ok = r.is_success
    except Exception:
        llm_ok = False
    return {"api": "ok", "llm": "ok" if llm_ok else "unreachable"}


# ── Novels ─────────────────────────────────────────────────────────────────────

@app.get("/novels")
def novels_list():
    return db.list_novels()


@app.post("/novels", status_code=201)
def novels_create(req: NovelCreateRequest):
    return db.create_novel(req.title, req.author, req.description, req.cover_color)


@app.post("/novels/import", status_code=201)
def novels_import(req: NovelImportRequest):
    """
    Crea una novela con todos sus capítulos en una sola operación transaccional.
    El cliente (frontend) es responsable de extraer el texto del .docx y dividirlo
    en capítulos por heading. Este endpoint simplemente persiste lo que recibe.
    """
    novel = db.create_novel(req.title, req.author,
                            req.description, req.cover_color)
    created_chapters = []
    for idx, ch in enumerate(req.chapters):
        chapter = db.create_chapter(
            novel_id=novel["id"],
            title=ch.title,
            content_text=ch.content_text,
            content_html=ch.content_html,
            order_index=idx,
        )
        created_chapters.append(chapter)
    return {**novel, "chapters": created_chapters}


@app.get("/novels/{novel_id}")
def novels_get(novel_id: int):
    novel = db.get_novel(novel_id)
    if not novel:
        raise HTTPException(404, "Novela no encontrada")
    novel["chapters"] = db.list_chapters(novel_id)
    return novel


@app.put("/novels/{novel_id}")
def novels_update(novel_id: int, req: NovelUpdateRequest):
    updated = db.update_novel(novel_id, **req.model_dump(exclude_none=True))
    if not updated:
        raise HTTPException(404, "Novela no encontrada")
    return updated


@app.delete("/novels/{novel_id}", status_code=204)
def novels_delete(novel_id: int):
    if not db.delete_novel(novel_id):
        raise HTTPException(404, "Novela no encontrada")


# ── Chapters ───────────────────────────────────────────────────────────────────

@app.get("/novels/{novel_id}/chapters")
def chapters_list(novel_id: int):
    return db.list_chapters(novel_id)


@app.post("/novels/{novel_id}/chapters", status_code=201)
def chapters_create(novel_id: int, req: ChapterCreateRequest):
    return db.create_chapter(
        novel_id, req.title, req.content_text, req.content_html, req.order_index
    )


@app.post("/novels/{novel_id}/chapters/reorder")
def chapters_reorder(novel_id: int, req: ReorderRequest):
    return db.reorder_chapters(novel_id, req.ordered_ids)


@app.get("/novels/{novel_id}/chapters/{chapter_id}")
def chapters_get(novel_id: int, chapter_id: int):
    chapter = db.get_chapter(chapter_id, include_annotations=True)
    if not chapter or chapter["novel_id"] != novel_id:
        raise HTTPException(404, "Capítulo no encontrado")
    return chapter


@app.put("/novels/{novel_id}/chapters/{chapter_id}")
def chapters_update(novel_id: int, chapter_id: int, req: ChapterUpdateRequest):
    """
    Actualiza el contenido del capítulo y opcionalmente reemplaza anotaciones
    de un tipo de análisis específico.
    """
    chapter = db.get_chapter(chapter_id, include_annotations=False)
    if not chapter or chapter["novel_id"] != novel_id:
        raise HTTPException(404, "Capítulo no encontrado")

    fields = req.model_dump(exclude_none=True, exclude={
                            "annotations", "analysis_type_for_annotations"})
    updated = db.update_chapter(chapter_id, **fields)

    if req.annotations is not None and req.analysis_type_for_annotations:
        db.save_annotations(chapter_id, req.annotations,
                            req.analysis_type_for_annotations)

    return db.get_chapter(chapter_id, include_annotations=True)


@app.delete("/novels/{novel_id}/chapters/{chapter_id}", status_code=204)
def chapters_delete(novel_id: int, chapter_id: int):
    chapter = db.get_chapter(chapter_id, include_annotations=False)
    if not chapter or chapter["novel_id"] != novel_id:
        raise HTTPException(404, "Capítulo no encontrado")
    db.delete_chapter(chapter_id)


# ── Resumen LLM (streaming) ────────────────────────────────────────────────────

@app.post("/novels/{novel_id}/chapters/{chapter_id}/summarize")
async def chapters_summarize(novel_id: int, chapter_id: int):
    """
    Genera un resumen del capítulo vía LLM y lo guarda en la BD al finalizar.
    Emite eventos SSE: { type: 'token', token: '...' } mientras genera,
    luego { type: 'done', summary: '...' } al terminar.
    """
    chapter = db.get_chapter(chapter_id, include_annotations=False)
    if not chapter or chapter["novel_id"] != novel_id:
        raise HTTPException(404, "Capítulo no encontrado")

    content_text = chapter.get("content_text", "").strip()
    if not content_text:
        raise HTTPException(422, "El capítulo no tiene contenido de texto")

    # Limitamos a los primeros 6000 chars para no sobrecargar el contexto
    excerpt = content_text[:6000]
    user_prompt = (
        f"Capítulo: «{chapter['title']}»\n\n"
        f"---\n{excerpt}\n---\n\n"
        "Genera el resumen del capítulo según tus instrucciones."
    )

    async def stream() -> AsyncGenerator[str, None]:
        full_summary = ""
        payload = {
            "model":       "local",
            "messages":    [
                {"role": "system", "content": SYSTEM_PROMPT_SUMMARY},
                {"role": "user",   "content": user_prompt},
            ],
            "temperature": 0.3,
            "max_tokens":  512,
            "stream":      True,
        }
        try:
            async with httpx.AsyncClient() as client:
                async with client.stream(
                    "POST", f"{LLAMA_URL}/v1/chat/completions", json=payload,
                    timeout=httpx.Timeout(
                        connect=10.0, read=120.0, write=10.0, pool=10.0),
                ) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        raw = line[6:].strip()
                        if raw == "[DONE]":
                            break
                        try:
                            delta = json.loads(raw)["choices"][0]["delta"]
                            token = delta.get("content") or ""
                            if token:
                                full_summary += token
                                yield sse({"type": "token", "token": token})
                        except (json.JSONDecodeError, KeyError, IndexError):
                            continue
        except Exception as exc:
            yield sse({"type": "error", "message": str(exc)})
            return

        # Guardamos en BD y notificamos al cliente
        summary_clean = full_summary.strip()
        if summary_clean:
            db.update_chapter(chapter_id, summary=summary_clean)
        yield sse({"type": "done", "summary": summary_clean})

    return StreamingResponse(
        stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Análisis (SSE) ─────────────────────────────────────────────────────────────

@app.post("/analyze/refs")
async def analyze_refs(req: AnalyzeRequest):
    return _make_analysis_stream(
        req, "refs",
        SYSTEM_PROMPT_REFS, _up_refs, resolve_refs_xml,
    )


@app.post("/analyze/blocks")
async def analyze_blocks(req: AnalyzeRequest):
    return _make_analysis_stream(
        req, "blocks",
        SYSTEM_PROMPT_BLOCKS, _up_blocks, resolve_blocks_xml,
    )


@app.post("/analyze/conversations")
async def analyze_conversations(req: AnalyzeRequest):
    return _make_analysis_stream(
        req, "conversations",
        SYSTEM_PROMPT_CONVERSATIONS, _up_conversations, resolve_conversations_xml,
    )
