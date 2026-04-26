"""
database.py – Capa de persistencia SQLite para la API de anotaciones.

Diseño deliberado: funciones síncronas puras (sin async).
FastAPI ejecuta automáticamente los endpoints `def` en un threadpool,
por lo que no necesitamos aiosqlite ni ningún ORM.

Schema:
  novels      → metadatos de cada novela (título, autor, color de portada…)
  chapters    → capítulos de una novela, con texto plano + HTML para el editor
  annotations → anotaciones generadas por el LLM, ligadas a un capítulo
"""

from __future__ import annotations
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

DB_PATH = Path("novels.db")


# ── Conexión ───────────────────────────────────────────────────────────────────

@contextmanager
def db_conn():
    """Context manager que garantiza commit/rollback y cierre limpio."""
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")   # Mejor concurrencia en lectura
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _row(row: sqlite3.Row) -> dict:
    """Convierte un sqlite3.Row en dict Python plano."""
    return dict(row)


# ── Inicialización ─────────────────────────────────────────────────────────────

def init_db() -> None:
    """Crea las tablas si no existen. Idem potente, seguro llamar en cada arranque."""
    with db_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS novels (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                title       TEXT NOT NULL,
                author      TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                cover_color TEXT NOT NULL DEFAULT '#4c1d95',
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            -- Cada fila es un capítulo (o prólogo, epílogo, etc.).
            -- content_text es el texto plano que ve el LLM.
            -- content_html es el HTML rico que renderiza TipTap.
            CREATE TABLE IF NOT EXISTS chapters (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                novel_id     INTEGER NOT NULL REFERENCES novels(id) ON DELETE CASCADE,
                title        TEXT NOT NULL DEFAULT 'Capítulo',
                content_text TEXT NOT NULL DEFAULT '',
                content_html TEXT NOT NULL DEFAULT '',
                order_index  INTEGER NOT NULL DEFAULT 0,
                summary      TEXT NOT NULL DEFAULT '',
                word_count   INTEGER NOT NULL DEFAULT 0,
                created_at   TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
            );

            -- analysis_type = 'refs' | 'blocks' | 'conversations'
            -- La clave primaria compuesta evita duplicados al re-analizar.
            CREATE TABLE IF NOT EXISTS annotations (
                id            TEXT    NOT NULL,
                chapter_id    INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
                type          TEXT    NOT NULL,
                start_offset  INTEGER NOT NULL,
                end_offset    INTEGER NOT NULL,
                metadata      TEXT    NOT NULL DEFAULT '{}',
                analysis_type TEXT    NOT NULL,
                PRIMARY KEY (id, chapter_id)
            );

            CREATE INDEX IF NOT EXISTS idx_chapters_novel
                ON chapters(novel_id, order_index);
            CREATE INDEX IF NOT EXISTS idx_annotations_chapter
                ON annotations(chapter_id, analysis_type);
        """)


# ── Novels ─────────────────────────────────────────────────────────────────────

def list_novels() -> list[dict]:
    with db_conn() as conn:
        rows = conn.execute("""
            SELECT n.*,
                   COUNT(c.id)          AS chapter_count,
                   SUM(c.word_count)    AS total_words
            FROM novels n
            LEFT JOIN chapters c ON c.novel_id = n.id
            GROUP BY n.id
            ORDER BY n.updated_at DESC
        """).fetchall()
        return [_row(r) for r in rows]


def get_novel(novel_id: int) -> Optional[dict]:
    with db_conn() as conn:
        row = conn.execute(
            "SELECT * FROM novels WHERE id = ?", (novel_id,)
        ).fetchone()
        return _row(row) if row else None


def create_novel(
    title: str,
    author: str = "",
    description: str = "",
    cover_color: str = "#4c1d95",
) -> dict:
    with db_conn() as conn:
        cur = conn.execute(
            "INSERT INTO novels (title, author, description, cover_color) VALUES (?,?,?,?)",
            (title, author, description, cover_color),
        )
        row = conn.execute(
            "SELECT * FROM novels WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return _row(row)


def update_novel(novel_id: int, **fields) -> Optional[dict]:
    allowed = {"title", "author", "description", "cover_color"}
    updates = {k: v for k, v in fields.items(
    ) if k in allowed and v is not None}
    if not updates:
        return get_novel(novel_id)
    cols = ", ".join(f"{k} = ?" for k in updates)
    vals = list(updates.values()) + [novel_id]
    with db_conn() as conn:
        conn.execute(
            f"UPDATE novels SET {cols}, updated_at = datetime('now') WHERE id = ?", vals
        )
    return get_novel(novel_id)


def delete_novel(novel_id: int) -> bool:
    with db_conn() as conn:
        cur = conn.execute("DELETE FROM novels WHERE id = ?", (novel_id,))
        return cur.rowcount > 0


# ── Chapters ───────────────────────────────────────────────────────────────────

def list_chapters(novel_id: int) -> list[dict]:
    """Lista ligera: sin content_text/html para no saturar la red."""
    with db_conn() as conn:
        rows = conn.execute("""
            SELECT
                c.id, c.novel_id, c.title, c.order_index,
                c.summary, c.word_count, c.created_at, c.updated_at,
                (c.summary != '')                              AS has_summary,
                (SELECT COUNT(*) FROM annotations a
                 WHERE a.chapter_id = c.id)                   AS annotation_count
            FROM chapters c
            WHERE c.novel_id = ?
            ORDER BY c.order_index ASC
        """, (novel_id,)).fetchall()
        return [_row(r) for r in rows]


def get_chapter(chapter_id: int, include_annotations: bool = True) -> Optional[dict]:
    with db_conn() as conn:
        row = conn.execute(
            "SELECT * FROM chapters WHERE id = ?", (chapter_id,)
        ).fetchone()
        if not row:
            return None
        chapter = _row(row)
        if include_annotations:
            ann_rows = conn.execute(
                """SELECT * FROM annotations
                   WHERE chapter_id = ?
                   ORDER BY start_offset""",
                (chapter_id,),
            ).fetchall()
            chapter["annotations"] = [
                {**_row(a), "metadata": json.loads(a["metadata"] or "{}")}
                for a in ann_rows
            ]
        return chapter


def create_chapter(
    novel_id: int,
    title: str,
    content_text: str = "",
    content_html: str = "",
    order_index: Optional[int] = None,
) -> dict:
    with db_conn() as conn:
        if order_index is None:
            row = conn.execute(
                "SELECT COALESCE(MAX(order_index), -1) + 1 AS nxt FROM chapters WHERE novel_id = ?",
                (novel_id,),
            ).fetchone()
            order_index = row["nxt"]
        wc = len(content_text.split()) if content_text.strip() else 0
        cur = conn.execute(
            """INSERT INTO chapters
               (novel_id, title, content_text, content_html, order_index, word_count)
               VALUES (?,?,?,?,?,?)""",
            (novel_id, title, content_text, content_html, order_index, wc),
        )
        chapter_id = cur.lastrowid
        conn.execute(
            "UPDATE novels SET updated_at = datetime('now') WHERE id = ?", (
                novel_id,)
        )
        row = conn.execute(
            "SELECT * FROM chapters WHERE id = ?", (chapter_id,)).fetchone()
        return _row(row)


def update_chapter(chapter_id: int, **fields) -> Optional[dict]:
    allowed = {"title", "content_text",
               "content_html", "order_index", "summary"}
    updates = {k: v for k, v in fields.items(
    ) if k in allowed and v is not None}
    if not updates:
        return get_chapter(chapter_id)
    if "content_text" in updates:
        txt = updates["content_text"]
        updates["word_count"] = len(txt.split()) if txt.strip() else 0
    cols = ", ".join(f"{k} = ?" for k in updates)
    vals = list(updates.values()) + [chapter_id]
    with db_conn() as conn:
        conn.execute(
            f"UPDATE chapters SET {cols}, updated_at = datetime('now') WHERE id = ?", vals
        )
        conn.execute(
            """UPDATE novels SET updated_at = datetime('now')
               WHERE id = (SELECT novel_id FROM chapters WHERE id = ?)""",
            (chapter_id,),
        )
    return get_chapter(chapter_id)


def reorder_chapters(novel_id: int, ordered_ids: list[int]) -> list[dict]:
    """Actualiza order_index según la lista de IDs recibida."""
    with db_conn() as conn:
        for idx, ch_id in enumerate(ordered_ids):
            conn.execute(
                "UPDATE chapters SET order_index = ? WHERE id = ? AND novel_id = ?",
                (idx, ch_id, novel_id),
            )
        conn.execute(
            "UPDATE novels SET updated_at = datetime('now') WHERE id = ?", (
                novel_id,)
        )
    return list_chapters(novel_id)


def delete_chapter(chapter_id: int) -> bool:
    with db_conn() as conn:
        cur = conn.execute("DELETE FROM chapters WHERE id = ?", (chapter_id,))
        return cur.rowcount > 0


# ── Annotations ────────────────────────────────────────────────────────────────

def save_annotations(
    chapter_id: int, annotations: list[dict], analysis_type: str
) -> int:
    """
    Reemplaza las anotaciones de un analysis_type concreto para este capítulo.
    Estrategia: DELETE + INSERT para que re-analizar sea idempotente.
    """
    with db_conn() as conn:
        conn.execute(
            "DELETE FROM annotations WHERE chapter_id = ? AND analysis_type = ?",
            (chapter_id, analysis_type),
        )
        for ann in annotations:
            conn.execute(
                """INSERT OR REPLACE INTO annotations
                   (id, chapter_id, type, start_offset, end_offset, metadata, analysis_type)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    ann["id"],
                    chapter_id,
                    ann["type"],
                    ann["start"],
                    ann["end"],
                    json.dumps(ann.get("metadata", {})),
                    analysis_type,
                ),
            )
        return len(annotations)


def delete_annotations(
    chapter_id: int, analysis_type: Optional[str] = None
) -> int:
    with db_conn() as conn:
        if analysis_type:
            cur = conn.execute(
                "DELETE FROM annotations WHERE chapter_id = ? AND analysis_type = ?",
                (chapter_id, analysis_type),
            )
        else:
            cur = conn.execute(
                "DELETE FROM annotations WHERE chapter_id = ?", (chapter_id,)
            )
        return cur.rowcount
