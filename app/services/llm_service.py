import json
import logging
from typing import AsyncGenerator
import httpx

from app.config import settings

log = logging.getLogger("llm_service")

LLAMA_URL = settings.llama_url
TEMPERATURE = 0.1
MAX_TOKENS = 8192

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

async def generate_chapter_summary_stream(chapter_text: str) -> AsyncGenerator[str, None]:
    THINK_OPEN = "<|channel>thought\n"
    THINK_CLOSE = "<channel|>"
    LOOKAHEAD = max(len(THINK_OPEN), len(THINK_CLOSE)) - 1

    payload = {
        "model": "local",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT_SUMMARY},
            {"role": "user", "content": f"Resume el siguiente capítulo:\n\n{chapter_text}"},
        ],
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        "stream": True,
    }

    state = "waiting"
    buffer = ""

    def sse(data: dict) -> str:
        return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    try:
        async with httpx.AsyncClient() as client:
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
                        yield sse({"type": "think", "token": native_think})
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
                                    yield sse({"type": "text", "token": pre})
                                state = "thinking"
                            elif len(buffer) > LOOKAHEAD:
                                safe = len(buffer) - LOOKAHEAD
                                yield sse({"type": "text", "token": buffer[:safe]})
                                buffer = buffer[safe:]
                            else:
                                break
                        elif state == "thinking":
                            if THINK_CLOSE in buffer:
                                think_part, buffer = buffer.split(THINK_CLOSE, 1)
                                if think_part:
                                    yield sse({"type": "think", "token": think_part})
                                state = "xml"
                            elif len(buffer) > LOOKAHEAD:
                                safe = len(buffer) - LOOKAHEAD
                                yield sse({"type": "think", "token": buffer[:safe]})
                                buffer = buffer[safe:]
                            else:
                                break
                        else:
                            yield sse({"type": "text", "token": buffer})
                            buffer = ""
                            break

            if buffer.strip():
                yield sse({"type": "think" if state == "thinking" else "text", "token": buffer})
                
    except Exception as exc:
        log.error(f"Error streaming LLM: {exc}")
        yield sse({"type": "error", "message": str(exc)})
    finally:
        yield sse({"type": "done"})


async def call_llm_stream(
    system: str,
    user: str,
    temperature: float = TEMPERATURE,
    max_tokens: int = MAX_TOKENS,
) -> AsyncGenerator[dict, None]:
    """
    Versión genérica que yield dicts (type: 'think'|'text').
    NO emite SSE, solo datos crudos para que el servicio los procese/re-emita.
    """
    THINK_OPEN = "<|channel>thought\n"
    THINK_CLOSE = "<channel|>"
    LOOKAHEAD = max(len(THINK_OPEN), len(THINK_CLOSE)) - 1

    payload = {
        "model": "local",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }

    state = "waiting"
    buffer = ""

    try:
        async with httpx.AsyncClient() as client:
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
                        yield {"type": "think", "token": native_think}
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
                                    yield {"type": "text", "token": pre}
                                state = "thinking"
                            elif len(buffer) > LOOKAHEAD:
                                safe = len(buffer) - LOOKAHEAD
                                yield {"type": "text", "token": buffer[:safe]}
                                buffer = buffer[safe:]
                            else:
                                break
                        elif state == "thinking":
                            if THINK_CLOSE in buffer:
                                think_part, buffer = buffer.split(THINK_CLOSE, 1)
                                if think_part:
                                    yield {"type": "think", "token": think_part}
                                state = "xml"
                            elif len(buffer) > LOOKAHEAD:
                                safe = len(buffer) - LOOKAHEAD
                                yield {"type": "think", "token": buffer[:safe]}
                                buffer = buffer[safe:]
                            else:
                                break
                        else:
                            yield {"type": "text", "token": buffer}
                            buffer = ""
                            break

            if buffer.strip():
                yield {"type": "think" if state == "thinking" else "text", "token": buffer}

    except Exception as exc:
        log.error(f"Error in call_llm_stream: {exc}")
        raise exc


# ── Chat conversacional ──────────────────────────────────────────────────────

SYSTEM_PROMPT_CHAT = """\
Eres un asistente literario experto integrado en un editor de novelas.
Tu rol es ayudar al escritor con su obra. Puedes:
- Responder preguntas sobre la trama, personajes, consistencia narrativa
- Criticar y sugerir mejoras al texto
- Proponer texto alternativo o nuevos párrafos
- Analizar estilo, ritmo, diálogos y descripciones
- Editar fragmentos específicos del texto

El contexto de la novela que recibirás tiene fragmentos identificados con [F:ID],
donde ID es el número único del fragmento. Puedes referenciar y proponer ediciones
a fragmentos específicos usando su ID.

HERRAMIENTA — Editar fragmento existente:
Cuando quieras proponer reemplazar el contenido de un fragmento específico, usa:
%%FRAG_EDIT_START:ID%%
El nuevo contenido del fragmento aquí (solo el texto, sin HTML).
%%FRAG_EDIT_END%%

HERRAMIENTA — Insertar texto nuevo en el documento:
Cuando el usuario pida texto para insertar en el documento (sin reemplazar un
fragmento existente), usa:
%%SUGGESTION_START%%
El texto sugerido que se insertará en el editor va aquí.
%%SUGGESTION_END%%

Solo usa una herramienta a la vez por respuesta cuando sea relevante.
Responde siempre en español. Sé conciso pero útil.
Basa tus respuestas en el contexto de la novela proporcionado.
"""


async def generate_chat_response_stream(
    prompt: str,
    context_text: str,
    history: list[dict] | None = None,
) -> AsyncGenerator[str, None]:
    """Genera una respuesta de chat conversacional vía streaming SSE."""
    THINK_OPEN = "<|channel>thought\n"
    THINK_CLOSE = "<channel|>"
    LOOKAHEAD = max(len(THINK_OPEN), len(THINK_CLOSE)) - 1

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT_CHAT},
    ]

    # Inyectar contexto de la novela como mensaje del sistema
    if context_text:
        messages.append({
            "role": "system",
            "content": f"Contexto de la novela (fragmentos del capítulo/novela):\n\n{context_text}",
        })

    # Añadir historial de conversación previo
    if history:
        for msg in history:
            messages.append({"role": msg["role"], "content": msg["content"]})

    # Añadir el prompt actual del usuario
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": "local",
        "messages": messages,
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        "stream": True,
    }

    state = "waiting"
    buffer = ""

    def sse(data: dict) -> str:
        return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    try:
        async with httpx.AsyncClient() as client:
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
                        yield sse({"type": "think", "token": native_think})
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
                                    yield sse({"type": "text", "token": pre})
                                state = "thinking"
                            elif len(buffer) > LOOKAHEAD:
                                safe = len(buffer) - LOOKAHEAD
                                yield sse({"type": "text", "token": buffer[:safe]})
                                buffer = buffer[safe:]
                            else:
                                break
                        elif state == "thinking":
                            if THINK_CLOSE in buffer:
                                think_part, buffer = buffer.split(THINK_CLOSE, 1)
                                if think_part:
                                    yield sse({"type": "think", "token": think_part})
                                state = "xml"
                            elif len(buffer) > LOOKAHEAD:
                                safe = len(buffer) - LOOKAHEAD
                                yield sse({"type": "think", "token": buffer[:safe]})
                                buffer = buffer[safe:]
                            else:
                                break
                        else:
                            yield sse({"type": "text", "token": buffer})
                            buffer = ""
                            break

            if buffer.strip():
                yield sse({"type": "think" if state == "thinking" else "text", "token": buffer})

    except Exception as exc:
        log.error(f"Error streaming chat LLM: {exc}")
        yield sse({"type": "error", "message": str(exc)})
    finally:
        yield sse({"type": "done"})

