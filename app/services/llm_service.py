import json
import logging
import re
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


# ── Planning Mode ────────────────────────────────────────────────────────────

SYSTEM_PROMPT_PLANNER = """\
Eres un planificador estratégico para tareas complejas de escritura de novelas.
Tu trabajo es descomponer el objetivo del usuario en pasos atómicos y secuenciales.
Cada paso debe tener su propio prompt enfocado y específico.

PRINCIPIOS:
1. Cada paso debe ser ATÓMICO: una sola tarea clara
2. Los pasos posteriores pueden referenciar resultados de pasos anteriores
3. Considera la secuencia: análisis → extracción de información → generación → síntesis
4. Genera entre 3 y 6 pasos
5. Cada prompt debe ser específico y accionable

TIPOS DE PASOS COMUNES:
- "Analizar X" — identificar problemas, patrones, oportunidades
- "Extraer perfiles de Y" — sacar info estructurada (personajes, lugares, etc.)
- "Generar Z" — producir contenido nuevo
- "Sintetizar" — resumir, combinar, recomendar acciones concretas

FORMATO DE RESPUESTA — SOLO JSON, sin markdown, sin texto adicional:
{
  "objective": "objetivo del usuario en una frase",
  "steps": [
    {
      "title": "Título corto (3-6 palabras)",
      "description": "Qué hace este paso (1 frase)",
      "prompt": "Prompt detallado y específico para el LLM. Si depende de pasos anteriores, mencionalo explícitamente (ej: 'Basándote en el análisis anterior, ...')."
    }
  ]
}

Responde ÚNICAMENTE con el JSON. No agregues texto antes ni después.
"""


async def generate_plan(prompt: str, context_text: str) -> dict:
    """Genera un plan multi-paso a partir del objetivo del usuario."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT_PLANNER}]
    if context_text:
        truncated = context_text[:6000] + ("\n\n[...contexto truncado...]" if len(context_text) > 6000 else "")
        messages.append({
            "role": "system",
            "content": f"Contexto resumido de la novela:\n\n{truncated}",
        })
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": "local",
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 4096,
        "stream": False,
    }

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=180.0, write=10.0, pool=10.0)
        ) as client:
            resp = await client.post(f"{LLAMA_URL}/v1/chat/completions", json=payload)
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]

        # Strip thinking blocks (XML and native formats)
        content = re.sub(r"<\|channel>thought.*?<channel\|>", "", content, flags=re.DOTALL)
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)
        content = content.strip()

        # Strip markdown code fences
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```\s*$", "", content)
        content = content.strip()

        # Locate JSON object boundaries (resilient to surrounding noise)
        start_idx = content.find("{")
        end_idx = content.rfind("}")
        if start_idx >= 0 and end_idx > start_idx:
            content = content[start_idx:end_idx + 1]

        plan = json.loads(content)

        if "objective" not in plan or "steps" not in plan:
            raise ValueError("Plan missing required fields (objective, steps)")
        if not isinstance(plan["steps"], list) or len(plan["steps"]) == 0:
            raise ValueError("Plan must include at least one step")
        for s in plan["steps"]:
            if not all(k in s for k in ("title", "description", "prompt")):
                raise ValueError("Each step must have title, description, prompt")

        return plan
    except json.JSONDecodeError as e:
        log.error(f"Plan JSON parse failed: {e}")
        raise ValueError(f"El LLM devolvió un JSON inválido: {e}")


SYSTEM_PROMPT_STEP_EXECUTOR = """\
Eres un asistente literario experto ejecutando un paso específico de un plan de trabajo.

Tienes acceso al contexto de la novela (con fragmentos identificados con [F:ID]) y a los
resultados de los pasos previos del plan, si existen.

HERRAMIENTAS DISPONIBLES:

— Editar fragmento existente (usa el ID exacto del contexto):
%%FRAG_EDIT_START:ID%%
nuevo contenido del fragmento (texto plano, sin HTML)
%%FRAG_EDIT_END%%

— Insertar texto nuevo en el documento:
%%SUGGESTION_START%%
texto a insertar
%%SUGGESTION_END%%

REGLAS:
- Enfócate ÚNICAMENTE en el paso actual. No hagas más de lo que pide.
- Sé conciso y útil. Responde en español.
- Si el paso requiere análisis o extracción, devuelve resultados estructurados (listas, puntos numerados).
- Si referencias fragmentos, usa siempre su ID [F:ID].
"""


async def execute_plan_step_stream(
    step_prompt: str,
    step_title: str,
    objective: str,
    context_text: str,
    previous_results: list[dict],
) -> AsyncGenerator[str, None]:
    """Ejecuta un paso individual de un plan con streaming SSE."""
    THINK_OPEN = "<|channel>thought\n"
    THINK_CLOSE = "<channel|>"
    LOOKAHEAD = max(len(THINK_OPEN), len(THINK_CLOSE)) - 1

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT_STEP_EXECUTOR},
        {"role": "system", "content": f"Objetivo general del plan: {objective}"},
    ]
    if context_text:
        messages.append({
            "role": "system",
            "content": f"Contexto de la novela (fragmentos identificados con [F:ID]):\n\n{context_text}",
        })
    if previous_results:
        prev_text = "\n\n".join(
            f"### Paso previo: {r['title']}\n{r['summary']}" for r in previous_results
        )
        messages.append({
            "role": "system",
            "content": f"Resultados de pasos previos del plan:\n\n{prev_text}",
        })
    messages.append({
        "role": "user",
        "content": f"Paso actual a ejecutar — {step_title}\n\n{step_prompt}",
    })

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
        log.error(f"Error streaming plan step LLM: {exc}")
        yield sse({"type": "error", "message": str(exc)})
    finally:
        yield sse({"type": "done"})

