"""
Router de chat conversacional con asistente de IA.

El endpoint recibe un prompt, IDs de capítulos para contexto,
y un historial de conversación. Devuelve una respuesta SSE en streaming.
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import ChatRequest, PlanRequest, ExecuteStepRequest
from app.services import (
    chapter_service,
    fragment_service,
    llm_service,
    novel_service,
)

router = APIRouter(prefix="/chat", tags=["Chat"])


def _strip_html(html: str) -> str:
    """Quita tags HTML básicos para pasar texto plano al LLM."""
    import re
    return re.sub(r"<[^>]+>", "", html).strip()


def _build_context(db: Session, novel_id: int, chapter_ids: list[int]) -> str:
    """
    Construye el texto de contexto para el LLM.

    Si chapter_ids está vacío, se usa toda la novela.
    Si tiene IDs, solo se usan esos capítulos.
    """
    if chapter_ids:
        # Solo los capítulos seleccionados
        chapters = []
        for cid in chapter_ids:
            ch = chapter_service.get_chapter(db, cid)
            if ch and ch.novel_id == novel_id:
                chapters.append(ch)
    else:
        # Toda la novela
        chapters = chapter_service.get_chapters_by_novel(db, novel_id)

    if not chapters:
        return ""

    parts = []
    for ch in sorted(chapters, key=lambda c: c.chapter_number):
        frags = fragment_service.get_fragments_by_chapter(db, ch.id)
        frags_sorted = sorted(frags, key=lambda f: f.order)
        text = "\n\n".join(f"[F:{f.id}] {_strip_html(f.content)}" for f in frags_sorted if f.content)
        if text.strip():
            parts.append(f"--- Capítulo {ch.chapter_number}: {ch.title} ---\n{text}")

    return "\n\n".join(parts)


@router.post("/")
async def chat(data: ChatRequest, db: Session = Depends(get_db)):
    """Endpoint de chat conversacional con contexto de la novela."""
    # Validar que la novela existe
    novel = novel_service.get_novel(db, data.novel_id)
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")

    # Construir contexto
    context = _build_context(db, data.novel_id, data.chapter_ids)

    # Convertir historial
    history = [{"role": m.role, "content": m.content} for m in data.history]

    return StreamingResponse(
        llm_service.generate_chat_response_stream(data.prompt, context, history),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/plan")
async def generate_plan_endpoint(data: PlanRequest, db: Session = Depends(get_db)):
    """Genera un plan multi-paso para una tarea compleja (modo planificado)."""
    novel = novel_service.get_novel(db, data.novel_id)
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")

    context = _build_context(db, data.novel_id, data.chapter_ids)

    try:
        plan = await llm_service.generate_plan(data.prompt, context)
    except ValueError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error generating plan: {e}")

    return plan


@router.post("/execute-step")
async def execute_step_endpoint(data: ExecuteStepRequest, db: Session = Depends(get_db)):
    """Ejecuta un paso individual de un plan con streaming SSE."""
    novel = novel_service.get_novel(db, data.novel_id)
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")

    context = _build_context(db, data.novel_id, data.chapter_ids)
    prev = [{"title": r.title, "summary": r.summary} for r in data.previous_results]

    return StreamingResponse(
        llm_service.execute_plan_step_stream(
            step_prompt=data.step_prompt,
            step_title=data.step_title,
            objective=data.objective,
            context_text=context,
            previous_results=prev,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
