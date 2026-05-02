"""
Novel Analyzer API — Entry point.

Arrancar en desarrollo:
    uvicorn app.main:app --reload

Documentación interactiva disponible en:
    http://localhost:8000/docs   (Swagger UI)
    http://localhost:8000/redoc  (ReDoc)
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import init_db
from app.routers import (
    chapter_router,
    chat_router,
    entity_router,
    fragment_router,
    novel_router,
    project_router,
    scene_router,
)

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    debug=settings.debug,
)

# CORS abierto durante desarrollo para el servidor Angular (ng serve → :4200)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup() -> None:
    """Crea las tablas en SQLite si no existen todavía."""
    init_db()


# Registro de routers — el orden afecta la visualización en /docs
app.include_router(project_router.router)
app.include_router(novel_router.router)
app.include_router(chapter_router.router)
app.include_router(scene_router.router)
app.include_router(fragment_router.router)
app.include_router(entity_router.router)
app.include_router(chat_router.router)


@app.get("/", tags=["Health"])
def health_check():
    return {"status": "ok", "app": settings.app_name, "version": settings.app_version}
