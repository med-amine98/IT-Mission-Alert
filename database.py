"""
Database module pour I-LEAD Mission Scraper
Gère : missions (offres + candidats) et CVs
SQLite avec WAL mode
"""
import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Dict, Any

# ============================================================
# CONFIGURATION
# ============================================================

DB_PATH = Path(__file__).resolve().parent / "missions.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


# ============================================================
# CONNEXION
# ============================================================

def get_connection():
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with get_connection() as conn:
        # Table missions (offres + candidats)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS missions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                title TEXT NOT NULL,
                company TEXT DEFAULT '',
                description TEXT DEFAULT '',
                location TEXT DEFAULT '',
                skills TEXT DEFAULT '',
                rate TEXT DEFAULT '',
                published_date TEXT DEFAULT '',
                url TEXT UNIQUE NOT NULL,
                collected_at TEXT NOT NULL,
                analysis TEXT,
                ai_score INTEGER
            )
        """)

        # Table CVs
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cvs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                title TEXT DEFAULT 'Consultant IT',
                email TEXT DEFAULT '',
                phone TEXT DEFAULT '',
                experience_years INTEGER DEFAULT 0,
                skills TEXT DEFAULT '',
                summary TEXT DEFAULT '',
                raw_text TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)

        # Index pour accélérer les recherches
        conn.execute("CREATE INDEX IF NOT EXISTS idx_missions_source ON missions(source)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_missions_url ON missions(url)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cvs_name ON cvs(name)")

        conn.commit()
    print(f"[DB] Base initialisée : {DB_PATH}")


# ============================================================
# MISSIONS
# ============================================================

def _row_to_dict(row) -> Dict[str, Any]:
    if row is None:
        return None
    return dict(row)


def save_mission(item: Dict[str, Any]) -> bool:
    """
    Sauvegarde une mission (offre ou candidat).
    Retourne True si inséré, False si déjà existant (URL unique).
    """
    try:
        with get_connection() as conn:
            cursor = conn.execute("""
                INSERT OR IGNORE INTO missions
                (source, title, company, description, location, skills, rate, published_date, url, collected_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                item.get("source", "inconnu"),
                item.get("title", ""),
                item.get("company", ""),
                item.get("description", ""),
                item.get("location", ""),
                item.get("skills", ""),
                item.get("rate", ""),
                item.get("published_date", ""),
                item.get("url", ""),
                datetime.now(timezone.utc).isoformat(),
            ))
            conn.commit()
            return cursor.rowcount > 0
    except Exception as e:
        print(f"[DB ERROR] save_mission: {e}")
        return False


def get_missions(source: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Retourne toutes les missions (ou filtrées par source)."""
    try:
        with get_connection() as conn:
            query = "SELECT * FROM missions"
            params = []
            if source:
                query += " WHERE source = ?"
                params.append(source)
            query += " ORDER BY collected_at DESC"
            if limit:
                query += " LIMIT ?"
                params.append(limit)

            rows = conn.execute(query, params).fetchall()
            return [_row_to_dict(r) for r in rows]
    except Exception as e:
        print(f"[DB ERROR] get_missions: {e}")
        return []


def get_mission(mission_id: int) -> Optional[Dict[str, Any]]:
    """Retourne une mission par son ID."""
    try:
        with get_connection() as conn:
            row = conn.execute("SELECT * FROM missions WHERE id = ?", (mission_id,)).fetchone()
            return _row_to_dict(row)
    except Exception as e:
        print(f"[DB ERROR] get_mission: {e}")
        return None


def delete_mission_by_url(url: str) -> bool:
    """Supprime une mission par son URL."""
    try:
        with get_connection() as conn:
            cursor = conn.execute("DELETE FROM missions WHERE url = ?", (url,))
            conn.commit()
            return cursor.rowcount > 0
    except Exception as e:
        print(f"[DB ERROR] delete_mission_by_url: {e}")
        return False


def delete_all_missions() -> int:
    """Supprime toutes les missions. Retourne le nombre supprimé."""
    try:
        with get_connection() as conn:
            cursor = conn.execute("DELETE FROM missions")
            conn.commit()
            return cursor.rowcount
    except Exception as e:
        print(f"[DB ERROR] delete_all_missions: {e}")
        return 0


def update_analysis(url: str, score: int, analysis_json: str) -> bool:
    """Met à jour l'analyse IA d'une mission."""
    try:
        with get_connection() as conn:
            cursor = conn.execute("""
                UPDATE missions SET analysis = ?, ai_score = ? WHERE url = ?
            """, (analysis_json, score, url))
            conn.commit()
            return cursor.rowcount > 0
    except Exception as e:
        print(f"[DB ERROR] update_analysis: {e}")
        return False


# ============================================================
# CVs
# ============================================================

def save_cv(cv_data: Dict[str, Any]) -> int:
    """
    Sauvegarde un CV. Retourne l'ID du CV créé.
    """
    try:
        with get_connection() as conn:
            cursor = conn.execute("""
                INSERT INTO cvs
                (name, title, email, phone, experience_years, skills, summary, raw_text, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                cv_data.get("name", "Sans nom"),
                cv_data.get("title", "Consultant IT"),
                cv_data.get("email", ""),
                cv_data.get("phone", ""),
                int(cv_data.get("experience_years", 0) or 0),
                cv_data.get("skills", ""),
                cv_data.get("summary", ""),
                cv_data.get("raw_text", ""),
                datetime.now(timezone.utc).isoformat(),
            ))
            conn.commit()
            return cursor.lastrowid
    except Exception as e:
        print(f"[DB ERROR] save_cv: {e}")
        return 0


def get_cvs() -> List[Dict[str, Any]]:
    """Retourne tous les CVs."""
    try:
        with get_connection() as conn:
            rows = conn.execute("SELECT * FROM cvs ORDER BY created_at DESC").fetchall()
            return [_row_to_dict(r) for r in rows]
    except Exception as e:
        print(f"[DB ERROR] get_cvs: {e}")
        return []


def get_cv(cv_id: int) -> Optional[Dict[str, Any]]:
    """Retourne un CV par son ID."""
    try:
        with get_connection() as conn:
            row = conn.execute("SELECT * FROM cvs WHERE id = ?", (cv_id,)).fetchone()
            return _row_to_dict(row)
    except Exception as e:
        print(f"[DB ERROR] get_cv: {e}")
        return None


def delete_cv(cv_id: int) -> bool:
    """Supprime un CV par son ID."""
    try:
        with get_connection() as conn:
            cursor = conn.execute("DELETE FROM cvs WHERE id = ?", (cv_id,))
            conn.commit()
            return cursor.rowcount > 0
    except Exception as e:
        print(f"[DB ERROR] delete_cv: {e}")
        return False


def delete_all_cvs() -> int:
    """Supprime tous les CVs. Retourne le nombre supprimé."""
    try:
        with get_connection() as conn:
            cursor = conn.execute("DELETE FROM cvs")
            conn.commit()
            return cursor.rowcount
    except Exception as e:
        print(f"[DB ERROR] delete_all_cvs: {e}")
        return 0


# ============================================================
# STATS
# ============================================================

def get_stats() -> Dict[str, Any]:
    """Retourne les statistiques globales."""
    try:
        with get_connection() as conn:
            missions_count = conn.execute("SELECT COUNT(*) FROM missions").fetchone()[0]
            cvs_count = conn.execute("SELECT COUNT(*) FROM cvs").fetchone()[0]

            sources = {}
            for row in conn.execute("SELECT source, COUNT(*) as count FROM missions GROUP BY source").fetchall():
                sources[row["source"]] = row["count"]

            return {
                "total_missions": missions_count,
                "total_candidates": cvs_count,
                "sources": sources,
            }
    except Exception as e:
        print(f"[DB ERROR] get_stats: {e}")
        return {"total_missions": 0, "total_candidates": 0, "sources": {}}


# ============================================================
# TEST RAPIDE
# ============================================================

if __name__ == "__main__":
    init_db()
    print(f"Stats : {get_stats()}")