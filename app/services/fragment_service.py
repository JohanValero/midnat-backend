"""
CRUD para TB_FRAGMENT con dos lógicas especiales:

1. Hash automático
   Cada vez que se crea o actualiza un fragmento se calcula un SHA-256
   del contenido (UTF-8). Sirve como huella única para detectar duplicados
   y como ID estable si en el futuro se implementa GraphRAG o embeddings.

2. Orden fraccionario (midpoint ordering)
   Los fragmentos viven en una "línea temporal" dentro del capítulo.
   En lugar de índices enteros consecutivos (difíciles de reordenar),
   se usan múltiplos de 1000:

     frag A → 1000
     frag B → 2000
     frag C → 3000

   Para insertar entre A y B:  order = (1000 + 2000) // 2 = 1500
   Para insertar entre A y 1500: order = (1000 + 1500) // 2 = 1250
   ...y así sucesivamente hasta que el espacio se agota.

   Cuando dos fragmentos adyacentes ya no tienen espacio entero entre ellos,
   `rebalance_fragments()` reasigna todos los órdenes en el capítulo
   volviendo a los múltiplos de 1000 sin cambiar el orden relativo.
"""
import hashlib
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Fragment
from app.schemas import FragmentCreate, FragmentInsertBetween, FragmentUpdate


# ── Utilidades internas ───────────────────────────────────────────────────────

def _hash(content: str) -> str:
    """SHA-256 hex del contenido en UTF-8."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _next_order(db: Session, chapter_id: int) -> int:
    """Calcula el orden para añadir al final: max_existente + 1000 (o 1000 si vacío)."""
    max_ord = (
        db.query(func.max(Fragment.order))
        .filter(Fragment.chapter_id == chapter_id)
        .scalar()
    )
    return (max_ord + 1000) if max_ord is not None else 1000


# ── CRUD ──────────────────────────────────────────────────────────────────────

def get_fragment(db: Session, fragment_id: int) -> Fragment | None:
    return db.query(Fragment).filter(Fragment.id == fragment_id).first()


def get_fragments_by_chapter(db: Session, chapter_id: int) -> list[Fragment]:
    """Devuelve todos los fragmentos del capítulo ordenados por `order` ascendente."""
    return (
        db.query(Fragment)
        .filter(Fragment.chapter_id == chapter_id)
        .order_by(Fragment.order)
        .all()
    )


def create_fragment(db: Session, data: FragmentCreate) -> Fragment:
    payload = data.model_dump()

    # Extraemos `order` porque puede ser None y necesitamos calcularlo
    explicit_order = payload.pop("order")
    order = explicit_order if explicit_order is not None else _next_order(
        db, payload["chapter_id"])

    fragment = Fragment(
        **payload,
        content_hash=_hash(payload["content"]),
        order=order,
        entities_dirty=True,
        scenes_dirty=True,
    )
    db.add(fragment)
    db.commit()
    db.refresh(fragment)
    return fragment


def insert_fragment_between(db: Session, data: FragmentInsertBetween) -> Fragment:
    """
    Inserta entre dos fragmentos existentes calculando el punto medio de sus órdenes.

    Ejemplo: prev_order=5000, next_order=6000 → order=5500
    Si prev_order=5000 y next_order=5001 (sin espacio entero) lanza ValueError
    y el cliente debería llamar a `rebalance_fragments` primero.
    """
    prev, nxt = data.prev_order, data.next_order
    if nxt <= prev:
        raise ValueError(
            f"next_order ({nxt}) debe ser mayor que prev_order ({prev})")

    mid = (prev + nxt) // 2
    if mid == prev:
        raise ValueError(
            f"Sin espacio entero entre órdenes {prev} y {nxt}. "
            "Llama a /fragments/rebalance/{chapter_id} para redistribuir."
        )

    payload = data.model_dump(exclude={"prev_order", "next_order"})
    fragment = Fragment(
        **payload,
        content_hash=_hash(payload["content"]),
        order=mid,
        entities_dirty=True,
        scenes_dirty=True,
    )
    db.add(fragment)
    db.commit()
    db.refresh(fragment)
    return fragment


def update_fragment(db: Session, fragment_id: int, data: FragmentUpdate) -> Fragment | None:
    fragment = get_fragment(db, fragment_id)
    if not fragment:
        return None

    updates = data.model_dump(exclude_unset=True)

    # Si el contenido cambia, invalidamos el hash anterior y marcamos como sucio para LLM
    if "content" in updates:
        updates["content_hash"] = _hash(updates["content"])
        updates["entities_dirty"] = True
        updates["scenes_dirty"] = True

    for field, value in updates.items():
        setattr(fragment, field, value)

    db.commit()
    db.refresh(fragment)
    return fragment


def delete_fragment(db: Session, fragment_id: int) -> bool:
    fragment = get_fragment(db, fragment_id)
    if not fragment:
        return False
    db.delete(fragment)
    db.commit()
    return True


import difflib

def bulk_sync_fragments(db: Session, chapter_id: int, blocks: list[str]) -> list[Fragment]:
    """Sincroniza los fragmentos usando diff para minimizar actualizaciones y usar orden intermedio."""
    existing_fragments = get_fragments_by_chapter(db, chapter_id)
    existing_texts = [f.content for f in existing_fragments]
    
    matcher = difflib.SequenceMatcher(None, existing_texts, blocks)
    final_sequence = []
    
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            final_sequence.extend(existing_fragments[i1:i2])
        elif tag == 'delete':
            for i in range(i1, i2):
                db.delete(existing_fragments[i])
        elif tag == 'replace':
            common_len = min(i2 - i1, j2 - j1)
            for k in range(common_len):
                frag = existing_fragments[i1 + k]
                new_content = blocks[j1 + k]
                new_hash = _hash(new_content)
                if frag.content != new_content or frag.content_hash != new_hash:
                    frag.content = new_content
                    frag.content_hash = new_hash
                    frag.entities_dirty = True
                    frag.scenes_dirty = True
                final_sequence.append(frag)
                
            if (i2 - i1) > common_len:
                for k in range(common_len, i2 - i1):
                    db.delete(existing_fragments[i1 + k])
            else:
                for k in range(common_len, j2 - j1):
                    final_sequence.append(blocks[j1 + k])
        elif tag == 'insert':
            for k in range(j1, j2):
                final_sequence.append(blocks[k])
                
    def assign_orders(seq):
        i = 0
        while i < len(seq):
            if isinstance(seq[i], str):
                start = i
                while i < len(seq) and isinstance(seq[i], str):
                    i += 1
                end = i
                
                prev_order = seq[start-1].order if start > 0 else 0
                if end < len(seq):
                    next_order = seq[end].order
                else:
                    next_order = prev_order + 1000 * ((end - start) + 1)
                
                space = next_order - prev_order
                num_inserts = end - start
                
                if space <= num_inserts:
                    return False
                
                step = space // (num_inserts + 1)
                for k in range(num_inserts):
                    order = prev_order + step * (k + 1)
                    new_frag = Fragment(
                        chapter_id=chapter_id,
                        content=seq[start+k],
                        content_hash=_hash(seq[start+k]),
                        order=order,
                        entities_dirty=True,
                        scenes_dirty=True,
                    )
                    db.add(new_frag)
                    seq[start+k] = new_frag
            else:
                i += 1
        return True

    if not assign_orders(final_sequence):
        db.commit()
        rebalance_fragments(db, chapter_id)
        if not assign_orders(final_sequence):
            raise Exception("Order space exhausted even after rebalancing.")
            
    db.commit()
    return get_fragments_by_chapter(db, chapter_id)


# ── Rebalanceo ────────────────────────────────────────────────────────────────

def rebalance_fragments(db: Session, chapter_id: int) -> list[Fragment]:
    """
    Redistribuye los órdenes de todos los fragmentos del capítulo
    asignando múltiplos de 1000 sin alterar el orden relativo.

    Útil cuando las inserciones por punto medio han agotado el espacio
    entre dos fragmentos adyacentes.

    Ejemplo antes:  [1000, 1500, 1750, 1875, 1937, 1968, 1984, 1992, 1996, 1998]
    Ejemplo después: [1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000, 10000]
    """
    fragments = get_fragments_by_chapter(db, chapter_id)  # ya vienen ordenados
    for i, frag in enumerate(fragments, start=1):
        frag.order = i * 1000
    db.commit()
    return fragments
