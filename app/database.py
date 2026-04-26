"""
Motor SQLAlchemy, fábrica de sesiones, Base declarativa y dependencia FastAPI.

Notas SQLite:
  - check_same_thread=False  → necesario porque FastAPI puede usar varios hilos.
  - PRAGMA foreign_keys=ON   → SQLite no impone FKs por defecto; lo activamos
                                en cada conexión mediante un event listener.
"""
from typing import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
    echo=settings.debug,  # Loguea SQL al stdout en modo debug
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_conn, _connection_record):
    """Activa el enforcement de foreign keys en cada nueva conexión SQLite."""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    """Clase base declarativa. Todos los modelos ORM heredan de aquí."""
    pass


def get_db() -> Generator[Session, None, None]:
    """
    Dependencia FastAPI: yield de una sesión DB que se cierra al terminar el request.
    Uso: `db: Session = Depends(get_db)`
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """
    Crea todas las tablas en la BD.
    El import inline evita importar modelos a nivel de módulo (riesgo de import circular).
    Se llama en el evento `startup` de FastAPI.
    """
    from app import models  # noqa: F401 — side-effect: registra los modelos en Base.metadata
    Base.metadata.create_all(bind=engine)
