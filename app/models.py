"""
Modelos ORM SQLAlchemy para Novel Analyzer.

────────────────────────────────────────────────────────────────────
NOTA: Dependencia circular entre NovelChapter ↔ ChapterHistory
────────────────────────────────────────────────────────────────────
TB_NOVEL_CHAPTER.current_history_id apunta al último snapshot publicado
(TB_CHAPTER_HISTORY), mientras que TB_CHAPTER_HISTORY.chapter_id apunta
de vuelta al capítulo. Esto crea un ciclo en el grafo de FKs.

SQLite no soporta ALTER TABLE ADD CONSTRAINT, por lo que la opción
`use_alter=True` de SQLAlchemy no funciona aquí. La solución elegida es:

  • `current_history_id` se declara como Integer plano, SIN FK a nivel de BD.
  • La integridad se garantiza en `chapter_service.publish_chapter()`.
  • SQLite igualmente no impone FKs por defecto; el resto de relaciones
    sí tienen FK porque las activamos con PRAGMA foreign_keys=ON en database.py.
────────────────────────────────────────────────────────────────────
"""
from sqlalchemy import (
    Column, ForeignKey, Integer, JSON, String, Text,
    DateTime, UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class Project(Base):
    __tablename__ = "TB_PROJECT"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(),
                        onupdate=func.now())

    novels = relationship("Novel",  back_populates="project",
                          cascade="all, delete-orphan")
    entities = relationship(
        "Entity", back_populates="project", cascade="all, delete-orphan")


class Novel(Base):
    __tablename__ = "TB_NOVEL"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey(
        "TB_PROJECT.id", ondelete="CASCADE"), nullable=False)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(),
                        onupdate=func.now())

    project = relationship("Project", back_populates="novels")
    chapters = relationship(
        "NovelChapter", back_populates="novel", cascade="all, delete-orphan")


class NovelChapter(Base):
    __tablename__ = "TB_NOVEL_CHAPTER"

    id = Column(Integer, primary_key=True, index=True)
    novel_id = Column(Integer, ForeignKey(
        "TB_NOVEL.id", ondelete="CASCADE"), nullable=False)
    # ⚠ SIN FK DECLARADA — ver nota al inicio del módulo.
    # chapter_service.publish_chapter() es responsable de mantener este campo sincronizado.
    current_history_id = Column(Integer, nullable=True)
    title = Column(String(255), nullable=False)
    chapter_number = Column(Integer, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(),
                        onupdate=func.now())

    novel = relationship("Novel", back_populates="chapters")
    history_entries = relationship(
        "ChapterHistory",
        foreign_keys="ChapterHistory.chapter_id",
        back_populates="chapter",
        cascade="all, delete-orphan",
    )
    scenes = relationship(
        "Scene",    back_populates="chapter", cascade="all, delete-orphan")
    fragments = relationship(
        "Fragment", back_populates="chapter", cascade="all, delete-orphan")

    @property
    def current_history(self):
        """Acceso conveniente al snapshot actual sin necesidad de JOIN extra."""
        return next(
            (h for h in self.history_entries if h.id == self.current_history_id),
            None,
        )


class ChapterHistory(Base):
    __tablename__ = "TB_CHAPTER_HISTORY"

    id = Column(Integer, primary_key=True, index=True)
    chapter_id = Column(Integer, ForeignKey(
        "TB_NOVEL_CHAPTER.id", ondelete="CASCADE"), nullable=False)
    content = Column(Text, nullable=False)
    version = Column(Integer, nullable=False, default=1)
    published_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    chapter = relationship(
        "NovelChapter",
        foreign_keys=[chapter_id],
        back_populates="history_entries",
    )


class Scene(Base):
    __tablename__ = "TB_SCENE"

    id = Column(Integer, primary_key=True, index=True)
    chapter_id = Column(Integer, ForeignKey(
        "TB_NOVEL_CHAPTER.id", ondelete="CASCADE"), nullable=False)
    title = Column(String(255), nullable=True)
    location = Column(String(255), nullable=True)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(),
                        onupdate=func.now())

    chapter = relationship("NovelChapter", back_populates="scenes")
    fragments = relationship("Fragment", back_populates="scene")


class Fragment(Base):
    __tablename__ = "TB_FRAGMENT"

    id = Column(Integer, primary_key=True, index=True)
    chapter_id = Column(Integer, ForeignKey(
        "TB_NOVEL_CHAPTER.id", ondelete="CASCADE"), nullable=False)
    scene_id = Column(Integer, ForeignKey("TB_SCENE.id",
                      ondelete="SET NULL"), nullable=True)
    content = Column(Text, nullable=False)
    # SHA-256 hex del contenido — huella única, se recalcula si cambia content
    content_hash = Column(String(64), unique=True, nullable=False, index=True)
    # Ordenación en pasos de 1000. Inserción entre dos fragmentos → (prev+next)//2
    order = Column(Integer, nullable=False)
    # JSON flexible: anotaciones del LLM, entidades extraídas, tipo de fragmento, etc.
    fragment_metadata = Column(JSON, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(),
                        onupdate=func.now())

    chapter = relationship("NovelChapter", back_populates="fragments")
    scene = relationship("Scene", back_populates="fragments")
    entity_fragments = relationship(
        "EntityFragment", back_populates="fragment", cascade="all, delete-orphan")


class Entity(Base):
    __tablename__ = "TB_ENTITY"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey(
        "TB_PROJECT.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(255), nullable=False)
    # Valores sugeridos: character | object | worldbuilding | historical_moment | concept | location
    entity_type = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(),
                        onupdate=func.now())

    project = relationship("Project", back_populates="entities")
    entity_fragments = relationship(
        "EntityFragment", back_populates="entity", cascade="all, delete-orphan")
    relations_as_a = relationship(
        "EntityRelation", foreign_keys="EntityRelation.entity_a_id", back_populates="entity_a",
    )
    relations_as_b = relationship(
        "EntityRelation", foreign_keys="EntityRelation.entity_b_id", back_populates="entity_b",
    )


class EntityFragment(Base):
    """Relación M-N entre entidades y los fragmentos donde aparecen."""
    __tablename__ = "TB_ENTITY_FRAGMENT"

    id = Column(Integer, primary_key=True, index=True)
    entity_id = Column(Integer, ForeignKey(
        "TB_ENTITY.id",   ondelete="CASCADE"), nullable=False)
    fragment_id = Column(Integer, ForeignKey(
        "TB_FRAGMENT.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("entity_id", "fragment_id",
                         name="uq_entity_fragment"),
    )

    entity = relationship("Entity",   back_populates="entity_fragments")
    fragment = relationship("Fragment", back_populates="entity_fragments")


class EntityRelation(Base):
    """
    Relación dirigida entre dos entidades.
    summary_a_to_b: cómo A ve / se relaciona con B.
    summary_b_to_a: cómo B ve / se relaciona con A.
    """
    __tablename__ = "TB_ENTITY_RELATION"

    id = Column(Integer, primary_key=True, index=True)
    entity_a_id = Column(
        Integer,
        ForeignKey(
            "TB_ENTITY.id",
            ondelete="CASCADE"
        ),
        nullable=False
    )
    entity_b_id = Column(
        Integer,
        ForeignKey(
            "TB_ENTITY.id",
            ondelete="CASCADE"
        ),
        nullable=False
    )
    # Ej.: ally | rival | mentor | family | location_of | associated_with
    relation_type = Column(String(100), nullable=False)
    summary_a_to_b = Column(Text, nullable=True)
    summary_b_to_a = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now()
    )

    entity_a = relationship(
        "Entity",
        foreign_keys=[entity_a_id],
        back_populates="relations_as_a"
    )
    entity_b = relationship(
        "Entity",
        foreign_keys=[entity_b_id],
        back_populates="relations_as_b"
    )
