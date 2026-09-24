import json
import os
import re
import sqlite3
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


CACHE_PATH = Path(os.getenv("ANALYSIS_CACHE_PATH", "/tmp/foodtech_analysis_cache.sqlite3"))
_lock = threading.Lock()


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def domain_key(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").casefold().rstrip(".")
    except ValueError:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return f"domain:{host}" if host else ""


def company_key(company_name: str) -> str:
    normalized = _normalize(company_name)
    return f"company:{normalized}" if len(normalized) >= 5 else ""


def cache_keys(company_name: str, original_url: str, resolved_url: str = "") -> list[str]:
    keys = [domain_key(original_url), domain_key(resolved_url), company_key(company_name)]
    return list(dict.fromkeys(key for key in keys if key))


def _connect() -> sqlite3.Connection:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(CACHE_PATH, timeout=30)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS conclusive_evaluations (
            cache_key TEXT PRIMARY KEY,
            company_name TEXT NOT NULL,
            original_url TEXT NOT NULL,
            resolved_url TEXT NOT NULL,
            evaluation_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            hits INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    return connection


def get_cached(company_name: str, original_url: str) -> dict | None:
    keys = cache_keys(company_name, original_url)
    if not keys:
        return None
    with _lock, _connect() as connection:
        for key in keys:
            row = connection.execute(
                "SELECT cache_key, evaluation_json FROM conclusive_evaluations WHERE cache_key = ?",
                (key,),
            ).fetchone()
            if not row:
                continue
            connection.execute(
                "UPDATE conclusive_evaluations SET hits = hits + 1, updated_at = ? WHERE cache_key = ?",
                (datetime.now(timezone.utc).isoformat(), row[0]),
            )
            result = json.loads(row[1])
            result["cache_hit"] = True
            result["cache_key"] = row[0]
            return result
    return None


def put_cached(company_name: str, original_url: str, evaluation: dict) -> None:
    if not evaluation.get("conclusive"):
        return
    resolved_url = str(evaluation.get("main_url") or "")
    keys = cache_keys(company_name, original_url, resolved_url)
    if not keys:
        return
    stored = dict(evaluation)
    stored["cache_hit"] = False
    payload = json.dumps(stored, ensure_ascii=False)
    now = datetime.now(timezone.utc).isoformat()
    with _lock, _connect() as connection:
        for key in keys:
            connection.execute(
                """
                INSERT INTO conclusive_evaluations
                    (cache_key, company_name, original_url, resolved_url,
                     evaluation_json, created_at, updated_at, hits)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(cache_key) DO UPDATE SET
                    company_name = excluded.company_name,
                    original_url = excluded.original_url,
                    resolved_url = excluded.resolved_url,
                    evaluation_json = excluded.evaluation_json,
                    updated_at = excluded.updated_at
                """,
                (key, company_name, original_url, resolved_url, payload, now, now),
            )


def cache_stats() -> dict:
    if not CACHE_PATH.exists():
        return {"aliases": 0, "path_configured": str(CACHE_PATH)}
    with _lock, _connect() as connection:
        count = connection.execute("SELECT COUNT(*) FROM conclusive_evaluations").fetchone()[0]
    return {"aliases": count, "path_configured": str(CACHE_PATH)}
