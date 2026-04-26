"""
Schemas Pydantic v2 para validación de requests y serialización de responses.

Patrón por dominio:
  Base   → campos compartidos (heredado por Create y Response)
  Create → lo que el cliente envía al crear un recurso
  Update → todos los campos opcionales (PATCH semántico)
  Response → modelo completo incluyendo campos generados por el servidor

`model_config = ConfigDict(from_attributes=True)` permite construir
un schema directamente desde un objeto ORM (necesario en los Response).
"""
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


# ═══════════════════════════════════════════════════════════════════
#  Project
# ═══════════════════════════════════════════════════════════════════

class ProjectBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None


class ProjectCreate(ProjectBase):
    pass


class ProjectUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None


class ProjectResponse(ProjectBase):
    id: int
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)


# ═══════════════════════════════════════════════════════════════════
#  Novel
# ═══════════════════════════════════════════════════════════════════

class NovelBase(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None


class NovelCreate(NovelBase):
    project_id: int


class NovelUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None


class NovelResponse(NovelBase):
    id: int
    project_id: int
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)


# ═══════════════════════════════════════════════════════════════════
#  ChapterHistory  (se declara antes de ChapterResponse porque lo referencia)
# ═══════════════════════════════════════════════════════════════════

class ChapterHistoryResponse(BaseModel):
    id: int
    chapter_id: int
    content: str
    version: int
    published_at: Optional[datetime]
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


# ═══════════════════════════════════════════════════════════════════
#  NovelChapter
# ═══════════════════════════════════════════════════════════════════

class ChapterBase(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    chapter_number: int = Field(..., ge=1)


class ChapterCreate(ChapterBase):
    novel_id: int


class ChapterUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=255)
    chapter_number: Optional[int] = Field(None, ge=1)


class ChapterResponse(ChapterBase):
    id: int
    novel_id: int
    current_history_id: Optional[int]
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)


class PublishChapterRequest(BaseModel):
    """
    Petición para publicar un capítulo.
    Si `content` es None, el service ensambla el texto a partir de los
    fragmentos del capítulo ordenados por `Fragment.order`.
    """
    content: Optional[str] = None


# ═══════════════════════════════════════════════════════════════════
#  Scene
# ═══════════════════════════════════════════════════════════════════

class SceneBase(BaseModel):
    title: Optional[str] = Field(None, max_length=255)
    location: Optional[str] = Field(None, max_length=255)
    description: Optional[str] = None


class SceneCreate(SceneBase):
    chapter_id: int


class SceneUpdate(SceneBase):
    pass  # Todos los campos ya son opcionales en la base


class SceneResponse(SceneBase):
    id: int
    chapter_id: int
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)


# ═══════════════════════════════════════════════════════════════════
#  Fragment
# ═══════════════════════════════════════════════════════════════════

class FragmentBase(BaseModel):
    content: str = Field(..., min_length=1)
    scene_id: Optional[int] = None
    fragment_metadata: Optional[dict[str, Any]] = None


class FragmentCreate(FragmentBase):
    chapter_id: int
    # Si se omite, el service lo calcula como max(order)+1000
    order: Optional[int] = Field(None, ge=0)


class FragmentInsertBetween(FragmentBase):
    """
    Inserta un fragmento entre dos existentes.
    El service calcula order = (prev_order + next_order) // 2.
    """
    chapter_id: int
    prev_order: int = Field(..., ge=0)
    next_order: int = Field(..., ge=1)


class FragmentUpdate(BaseModel):
    content: Optional[str] = Field(None, min_length=1)
    scene_id: Optional[int] = None
    fragment_metadata: Optional[dict[str, Any]] = None
    order: Optional[int] = Field(None, ge=0)


class FragmentResponse(FragmentBase):
    id: int
    chapter_id: int
    content_hash: str
    order: int
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)


# ═══════════════════════════════════════════════════════════════════
#  Entity
# ═══════════════════════════════════════════════════════════════════

class EntityBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    entity_type: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = None


class EntityCreate(EntityBase):
    project_id: int


class EntityUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    entity_type: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = None


class EntityResponse(EntityBase):
    id: int
    project_id: int
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)


# ═══════════════════════════════════════════════════════════════════
#  EntityFragment
# ═══════════════════════════════════════════════════════════════════

class EntityFragmentCreate(BaseModel):
    entity_id: int
    fragment_id: int


class EntityFragmentResponse(BaseModel):
    id: int
    entity_id: int
    fragment_id: int
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


# ═══════════════════════════════════════════════════════════════════
#  EntityRelation
# ═══════════════════════════════════════════════════════════════════

class EntityRelationBase(BaseModel):
    entity_a_id: int
    entity_b_id: int
    relation_type: str = Field(..., min_length=1, max_length=100)
    summary_a_to_b: Optional[str] = None
    summary_b_to_a: Optional[str] = None


class EntityRelationCreate(EntityRelationBase):
    pass


class EntityRelationUpdate(BaseModel):
    relation_type: Optional[str] = Field(None, min_length=1, max_length=100)
    summary_a_to_b: Optional[str] = None
    summary_b_to_a: Optional[str] = None


class EntityRelationResponse(EntityRelationBase):
    id: int
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)
