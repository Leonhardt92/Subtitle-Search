from __future__ import annotations

import argparse
import math
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import threading
from io import BytesIO
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote, unquote, urlparse
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from sqlite_vec_utils import delete_vec_rows


ROOT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = ROOT_DIR / "subtitles.db"
CLIPS_DIR = ROOT_DIR / "clips"
THUMBNAILS_DIR = ROOT_DIR / "thumbnails"
CLIP_LAB_EXPORTS_DIR = ROOT_DIR / "clip_lab_exports"
SEMANTIC_SCRIPT = ROOT_DIR / "scripts" / "semantic-search.py"
SEMANTIC_PYTHON = ROOT_DIR / ".venv" / "bin" / "python"
LOCAL_EMBED_MODEL = "BAAI/bge-small-zh-v1.5"
BUILD_SQLITE_SCRIPT = ROOT_DIR / "scripts" / "build-sqlite.py"
EMBED_SQLITE_SCRIPT = ROOT_DIR / "scripts" / "embed-sqlite.py"
CATEGORY_PREFIXES = {
    "humor": "data/幽默/%",
    "writing": "data/文笔/%",
}
FALLBACK_PAD_SECONDS = 8.0
MAX_SMART_SEGMENT_SECONDS = 120.0
SEMANTIC_MIN_SCORE = 0.6
CLIP_REQUEST_PAD_SECONDS = 1.0


def normalize_query(value: str) -> str:
    """Normalize a user query for feedback keys and comparisons."""
    return " ".join(str(value or "").strip().split()).lower()


def ensure_search_feedback_schema(connection: sqlite3.Connection) -> None:
    """Create or migrate the search feedback table."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS search_feedback (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          query TEXT NOT NULL,
          normalized_query TEXT NOT NULL,
          category TEXT NOT NULL,
          search_mode TEXT NOT NULL,
          subtitle_id INTEGER NOT NULL REFERENCES subtitles(id) ON DELETE CASCADE,
          video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
          rank_index INTEGER,
          score REAL,
          feedback TEXT NOT NULL,
          created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        DELETE FROM search_feedback
        WHERE id NOT IN (
          SELECT MAX(id)
          FROM search_feedback
          GROUP BY normalized_query, category, search_mode, subtitle_id, COALESCE(rank_index, -1)
        )
        """
    )
    connection.execute("DROP INDEX IF EXISTS idx_search_feedback_lookup")
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_search_feedback_lookup
        ON search_feedback(normalized_query, category, search_mode, subtitle_id, rank_index)
        """
    )


def ensure_query_group_schema(connection: sqlite3.Connection) -> None:
    """Create the query group tables and seed the built-in group."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS query_groups (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL UNIQUE,
          description TEXT,
          created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS query_group_terms (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          group_id INTEGER NOT NULL REFERENCES query_groups(id) ON DELETE CASCADE,
          term TEXT NOT NULL,
          weight REAL NOT NULL DEFAULT 1.0,
          created_at TEXT NOT NULL,
          UNIQUE(group_id, term)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_query_group_terms_term
        ON query_group_terms(term)
        """
    )

    created_at = datetime.now(timezone.utc).isoformat()
    connection.execute(
        """
        INSERT OR IGNORE INTO query_groups (name, description, created_at)
        VALUES (?, ?, ?)
        """,
        ("praise", "夸赞、认同、点赞相关表达", created_at),
    )
    praise_group = connection.execute("SELECT id FROM query_groups WHERE name = ?", ("praise",)).fetchone()
    if praise_group is None:
        return

    for term, weight in [
        ("很棒", 1.0),
        ("真棒", 0.95),
        ("厉害", 0.95),
        ("赞", 0.95),
        ("点赞", 1.0),
        ("大拇指", 1.0),
        ("优秀", 0.85),
        ("夸", 0.75),
        ("认可", 0.75),
        ("牛", 0.8),
    ]:
        connection.execute(
            """
            INSERT OR IGNORE INTO query_group_terms (group_id, term, weight, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (int(praise_group["id"]), normalize_query(term), weight, created_at),
        )


def load_feedback_counts(
    connection: sqlite3.Connection,
    *,
    normalized_query: str,
    category: str,
    search_mode: str,
) -> dict[int, dict[str, int]]:
    """Load per-subtitle feedback counts for one query."""
    ensure_search_feedback_schema(connection)
    rows = connection.execute(
        """
        SELECT
          subtitle_id,
          SUM(CASE WHEN feedback = 'useful' THEN 1 ELSE 0 END) AS useful_count,
          SUM(CASE WHEN feedback = 'bad' THEN 1 ELSE 0 END) AS bad_count
        FROM search_feedback
        WHERE normalized_query = ?
          AND category = ?
          AND search_mode = ?
        GROUP BY subtitle_id
        """,
        (normalized_query, category, search_mode),
    ).fetchall()

    feedback_by_subtitle: dict[int, dict[str, int]] = {}
    for row in rows:
        feedback_by_subtitle[int(row["subtitle_id"])] = {
            "usefulCount": int(row["useful_count"] or 0),
            "badCount": int(row["bad_count"] or 0),
        }
    return feedback_by_subtitle


def load_related_query_terms(connection: sqlite3.Connection, *, normalized_query: str) -> dict[str, float]:
    """Load related terms from the query-group table."""
    ensure_query_group_schema(connection)
    rows = connection.execute(
        """
        SELECT DISTINCT related.term, related.weight
        FROM query_group_terms base
        JOIN query_group_terms related ON related.group_id = base.group_id
        WHERE base.term = ?
          AND related.term != ?
        ORDER BY related.weight DESC, related.term
        """,
        (normalized_query, normalized_query),
    ).fetchall()

    related_terms: dict[str, float] = {}
    for row in rows:
        term = str(row["term"] or "").strip()
        if not term:
            continue
        related_terms[term] = max(related_terms.get(term, 0.0), float(row["weight"] or 0.0))
    return related_terms


def load_related_feedback_counts(
    connection: sqlite3.Connection,
    *,
    related_terms: dict[str, float],
    category: str,
    search_mode: str,
) -> dict[int, dict[str, float]]:
    """Aggregate feedback counts for related queries."""
    ensure_search_feedback_schema(connection)
    if not related_terms:
        return {}

    placeholders = ", ".join("?" for _ in related_terms)
    rows = connection.execute(
        f"""
        SELECT
          normalized_query,
          subtitle_id,
          SUM(CASE WHEN feedback = 'useful' THEN 1 ELSE 0 END) AS useful_count,
          SUM(CASE WHEN feedback = 'bad' THEN 1 ELSE 0 END) AS bad_count
        FROM search_feedback
        WHERE normalized_query IN ({placeholders})
          AND category = ?
          AND search_mode = ?
        GROUP BY normalized_query, subtitle_id
        """,
        (*related_terms.keys(), category, search_mode),
    ).fetchall()

    feedback_by_subtitle: dict[int, dict[str, float]] = {}
    for row in rows:
        subtitle_id = int(row["subtitle_id"])
        weight = float(related_terms.get(str(row["normalized_query"]), 0.0))
        entry = feedback_by_subtitle.setdefault(subtitle_id, {"usefulCount": 0.0, "badCount": 0.0})
        entry["usefulCount"] += int(row["useful_count"] or 0) * weight
        entry["badCount"] += int(row["bad_count"] or 0) * weight
    return feedback_by_subtitle


def load_related_term_candidates(
    connection: sqlite3.Connection,
    *,
    related_terms: dict[str, float],
    category: str,
    exclude_query: str,
    limit: int = 80,
) -> list[dict]:
    """Find subtitle candidates that match related query-group terms."""
    if not related_terms:
        return []

    scored_candidates: dict[int, dict] = {}
    for term, weight in related_terms.items():
        if not term or term == exclude_query:
            continue

        rows = connection.execute(
            """
            SELECT
              s.id,
              s.video_id,
              s.cue_index,
              s.start_seconds,
              s.end_seconds,
              s.text,
              v.title AS video_title,
              v.video_path,
              (
                SELECT sp.start_seconds
                FROM subtitles sp
                WHERE sp.video_id = s.video_id
                  AND sp.cue_index = s.cue_index - 1
              ) AS prev_start_seconds,
              (
                SELECT sp.text
                FROM subtitles sp
                WHERE sp.video_id = s.video_id
                  AND sp.cue_index = s.cue_index - 1
              ) AS prev_text,
              (
                SELECT sn.text
                FROM subtitles sn
                WHERE sn.video_id = s.video_id
                  AND sn.cue_index = s.cue_index + 1
              ) AS next_text
            FROM subtitles s
            JOIN videos v ON v.id = s.video_id
            WHERE v.has_video = 1
              AND v.has_subtitle = 1
              AND v.folder_path LIKE ?
              AND s.text NOT LIKE ?
              AND s.text LIKE ?
            ORDER BY s.start_seconds
            LIMIT 30
            """,
            (CATEGORY_PREFIXES[category], f"%{exclude_query}%", f"%{term}%"),
        ).fetchall()

        for row in rows:
            subtitle_id = int(row["id"])
            existing = scored_candidates.get(subtitle_id)
            base_score = 0.48 + float(weight) * 0.22
            if existing is not None and float(existing["score"]) >= base_score:
                continue

            scored_candidates[subtitle_id] = {
                "id": str(row["id"]),
                "videoId": int(row["video_id"]),
                "videoTitle": row["video_title"],
                "videoPath": row["video_path"],
                "startSeconds": row["start_seconds"],
                "endSeconds": row["end_seconds"],
                "text": row["text"],
                "prevStartSeconds": row["prev_start_seconds"],
                "prevText": row["prev_text"] or "",
                "nextText": row["next_text"] or "",
                "score": round(base_score, 6),
                "matchSource": "query-group",
                "matchedTerm": term,
            }

    return sorted(
        scored_candidates.values(),
        key=lambda item: (float(item.get("score") or 0.0), int(item["videoId"]), float(item["startSeconds"])),
        reverse=True,
    )[:limit]


def load_feedback_seed_candidates(
    connection: sqlite3.Connection,
    *,
    normalized_query: str,
    related_terms: dict[str, float],
    category: str,
    search_mode: str,
    limit: int = 40,
) -> list[dict]:
    """Seed semantic search results from positive feedback history."""
    ensure_search_feedback_schema(connection)
    query_weights = {normalized_query: 1.0, **related_terms}
    placeholders = ", ".join("?" for _ in query_weights)
    rows = connection.execute(
        f"""
        SELECT
          sf.normalized_query,
          sf.subtitle_id,
          SUM(CASE WHEN sf.feedback = 'useful' THEN 1 ELSE 0 END) AS useful_count,
          SUM(CASE WHEN sf.feedback = 'bad' THEN 1 ELSE 0 END) AS bad_count,
          s.video_id,
          s.cue_index,
          s.start_seconds,
          s.end_seconds,
          s.text,
          v.title AS video_title,
          v.video_path,
          (
            SELECT sp.start_seconds
            FROM subtitles sp
            WHERE sp.video_id = s.video_id
              AND sp.cue_index = s.cue_index - 1
          ) AS prev_start_seconds,
          (
            SELECT sp.text
            FROM subtitles sp
            WHERE sp.video_id = s.video_id
              AND sp.cue_index = s.cue_index - 1
          ) AS prev_text,
          (
            SELECT sn.text
            FROM subtitles sn
            WHERE sn.video_id = s.video_id
              AND sn.cue_index = s.cue_index + 1
          ) AS next_text
        FROM search_feedback sf
        JOIN subtitles s ON s.id = sf.subtitle_id
        JOIN videos v ON v.id = s.video_id
        WHERE sf.normalized_query IN ({placeholders})
          AND sf.category = ?
          AND sf.search_mode = ?
          AND v.has_video = 1
          AND v.has_subtitle = 1
          AND v.folder_path LIKE ?
          AND s.text NOT LIKE ?
        GROUP BY sf.normalized_query, sf.subtitle_id
        """,
        (*query_weights.keys(), category, search_mode, CATEGORY_PREFIXES[category], f"%{normalized_query}%"),
    ).fetchall()

    scored_candidates: dict[int, dict] = {}
    for row in rows:
        feedback_query = str(row["normalized_query"] or "").strip()
        if not feedback_query or feedback_query == normalized_query:
            continue
        useful_count = int(row["useful_count"] or 0)
        bad_count = int(row["bad_count"] or 0)
        if useful_count <= bad_count:
            continue

        query_weight = float(query_weights.get(feedback_query, 0.0))
        seed_score = 0.42 + query_weight * 0.12 + min(0.28, math.log1p(useful_count) * 0.12)
        subtitle_id = int(row["subtitle_id"])
        existing = scored_candidates.get(subtitle_id)
        if existing is not None and float(existing["score"]) >= seed_score:
            continue

        scored_candidates[subtitle_id] = {
            "id": str(row["subtitle_id"]),
            "videoId": int(row["video_id"]),
            "videoTitle": row["video_title"],
            "videoPath": row["video_path"],
            "startSeconds": row["start_seconds"],
            "endSeconds": row["end_seconds"],
            "text": row["text"],
            "prevStartSeconds": row["prev_start_seconds"],
            "prevText": row["prev_text"] or "",
            "nextText": row["next_text"] or "",
            "score": round(seed_score, 6),
            "matchSource": "feedback-seed",
            "matchedTerm": feedback_query,
        }

    return sorted(
        scored_candidates.values(),
        key=lambda item: (float(item.get("score") or 0.0), int(item["videoId"]), float(item["startSeconds"])),
        reverse=True,
    )[:limit]


def compute_feedback_boost(*, useful_count: int, bad_count: int) -> float:
    """Turn direct feedback counts into a ranking boost."""
    positive = math.log1p(max(0, useful_count))
    negative = math.log1p(max(0, bad_count))
    return max(-1.0, min(1.2, positive * 0.25 - negative * 0.18))


def compute_related_feedback_boost(*, useful_count: float, bad_count: float) -> float:
    """Turn related-query feedback counts into a smaller ranking boost."""
    positive = math.log1p(max(0.0, useful_count))
    negative = math.log1p(max(0.0, bad_count))
    return max(-0.45, min(0.55, positive * 0.16 - negative * 0.12))


class SemanticSearchWorker:
    """Manage a long-lived local semantic-search subprocess."""
    def __init__(self) -> None:
        self.process = None
        self.lock = threading.Lock()

    def _start(self) -> None:
        self.process = subprocess.Popen(
            [
                str(SEMANTIC_PYTHON),
                str(SEMANTIC_SCRIPT),
                "--db",
                str(DB_PATH),
                "--model",
                LOCAL_EMBED_MODEL,
                "--min-score",
                str(SEMANTIC_MIN_SCORE),
                "--serve-stdio",
            ],
            cwd=str(ROOT_DIR),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )

    def search(self, *, query: str, category: str) -> dict:
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                self._start()

            assert self.process is not None
            assert self.process.stdin is not None
            assert self.process.stdout is not None

            request = {"query": query, "category": category}
            try:
                self.process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
                self.process.stdin.flush()
                response_line = self.process.stdout.readline()
            except (BrokenPipeError, OSError):
                self._start()
                assert self.process is not None
                assert self.process.stdin is not None
                assert self.process.stdout is not None
                self.process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
                self.process.stdin.flush()
                response_line = self.process.stdout.readline()

            if not response_line:
                raise RuntimeError("semantic worker exited unexpectedly")

            return json.loads(response_line)


SEMANTIC_WORKER = SemanticSearchWorker()


class RangeRequestHandler(SimpleHTTPRequestHandler):
    """HTTP handler for static assets plus JSON APIs and clip endpoints."""
    range_header_pattern = re.compile(r"bytes=(\d*)-(\d*)$")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT_DIR), **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/search":
            self.handle_search(parsed.query)
            return

        if parsed.path == "/api/meta":
            self.handle_meta()
            return

        if parsed.path == "/api/semantic-search":
            self.handle_semantic_search(parsed.query)
            return

        if parsed.path == "/api/search-feedback":
            self.send_error(HTTPStatus.METHOD_NOT_ALLOWED, "Use POST for search feedback")
            return

        if parsed.path == "/api/clip":
            self.handle_clip(parsed.query)
            return
        if parsed.path == "/api/clip-range":
            self.handle_clip_range(parsed.query)
            return
        if parsed.path == "/api/thumbnail":
            self.handle_thumbnail(parsed.query)
            return

        if parsed.path == "/api/subtitle-at":
            self.handle_subtitle_at(parsed.query)
            return

        if parsed.path == "/api/admin/subtitle":
            self.handle_admin_subtitle(parsed.query)
            return

        if parsed.path == "/api/admin/video":
            self.handle_admin_video(parsed.query)
            return

        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/admin/subtitle":
            self.handle_admin_save_subtitle()
            return

        if parsed.path == "/api/admin/video":
            self.handle_admin_create_video()
            return

        if parsed.path == "/api/admin/generate":
            self.handle_admin_generate()
            return

        if parsed.path == "/api/clip-lab/export":
            self.handle_clip_lab_export()
            return

        if parsed.path == "/api/search-feedback":
            self.handle_search_feedback()
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def handle_meta(self):
        connection = sqlite3.connect(DB_PATH)
        try:
            metadata = dict(connection.execute("SELECT key, value FROM metadata").fetchall())
            payload = {"ok": True, "metadata": metadata}
        finally:
            connection.close()

        self.send_json(payload)

    def handle_search(self, query_string: str):
        params = parse_qs(query_string)
        query = (params.get("q", [""])[0] or "").strip()
        whole_word = params.get("whole_word", ["0"])[0] == "1"
        category = (params.get("category", [""])[0] or "").strip()

        if not query:
            self.send_json({"ok": True, "query": "", "wholeWord": whole_word, "category": category, "records": []})
            return

        folder_like = CATEGORY_PREFIXES.get(category)
        if folder_like is None:
            self.send_json({"ok": False, "error": "invalid category"}, status=HTTPStatus.BAD_REQUEST)
            return

        connection = sqlite3.connect(DB_PATH)
        connection.row_factory = sqlite3.Row

        try:
            rows = connection.execute(
                """
                SELECT
                  s.id,
                  s.video_id,
                  s.cue_index,
                  v.title AS video_title,
                  v.video_path,
                  s.start_seconds,
                  s.end_seconds,
                  s.text,
                  (
                    SELECT sp.start_seconds
                    FROM subtitles sp
                    WHERE sp.video_id = s.video_id
                      AND sp.cue_index = s.cue_index - 1
                  ) AS prev_start_seconds,
                  (
                    SELECT sp.text
                    FROM subtitles sp
                    WHERE sp.video_id = s.video_id
                      AND sp.cue_index = s.cue_index - 1
                  ) AS prev_text,
                  (
                    SELECT sn.text
                    FROM subtitles sn
                    WHERE sn.video_id = s.video_id
                      AND sn.cue_index = s.cue_index + 1
                  ) AS next_text
                FROM subtitles s
                JOIN videos v ON v.id = s.video_id
                WHERE v.has_video = 1
                  AND v.has_subtitle = 1
                  AND v.folder_path LIKE ?
                  AND s.text LIKE ?
                ORDER BY v.title COLLATE NOCASE, s.start_seconds
                """,
                (folder_like, f"%{query}%"),
            ).fetchall()
        finally:
            connection.close()

        pattern = None
        if whole_word:
            escaped = re.escape(query)
            pattern = re.compile(rf"(^|[^\w])({escaped})(?=[^\w]|$)", re.IGNORECASE)

        records = []
        for row in rows:
            text = row["text"]
            if pattern and not pattern.search(text):
                continue

            records.append(
                {
                    "id": str(row["id"]),
                    "videoId": int(row["video_id"]),
                    "cueIndex": int(row["cue_index"]),
                    "videoTitle": row["video_title"],
                    "videoPath": row["video_path"],
                    "startSeconds": row["start_seconds"],
                    "endSeconds": row["end_seconds"],
                    "text": text,
                    "prevStartSeconds": row["prev_start_seconds"],
                    "prevText": row["prev_text"] or "",
                    "nextText": row["next_text"] or "",
                }
            )

        self.send_json(
            {
                "ok": True,
                "query": query,
                "wholeWord": whole_word,
                "category": category,
                "records": records,
            }
        )

    def handle_semantic_search(self, query_string: str):
        params = parse_qs(query_string)
        query = (params.get("q", [""])[0] or "").strip()
        category = (params.get("category", [""])[0] or "").strip()

        if not query:
            self.send_json({"ok": True, "query": "", "category": category, "records": []})
            return

        if category not in CATEGORY_PREFIXES:
            self.send_json({"ok": False, "error": "invalid category"}, status=HTTPStatus.BAD_REQUEST)
            return

        if not SEMANTIC_PYTHON.exists():
            self.send_json(
                {"ok": False, "error": "local semantic search env not found", "details": "Missing .venv/bin/python"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return

        try:
            payload = SEMANTIC_WORKER.search(query=query, category=category)
        except Exception as error:
            self.send_json(
                {
                    "ok": False,
                    "error": "semantic search failed",
                    "details": str(error),
                },
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return

        connection = sqlite3.connect(DB_PATH)
        connection.row_factory = sqlite3.Row
        try:
            normalized_query = normalize_query(query)
            related_terms = load_related_query_terms(
                connection,
                normalized_query=normalized_query,
            )
            related_term_candidates = load_related_term_candidates(
                connection,
                related_terms=related_terms,
                category=category,
                exclude_query=normalized_query,
            )
            feedback_seed_candidates = load_feedback_seed_candidates(
                connection,
                normalized_query=normalized_query,
                related_terms=related_terms,
                category=category,
                search_mode="semantic",
            )
            feedback_by_subtitle = load_feedback_counts(
                connection,
                normalized_query=normalized_query,
                category=category,
                search_mode="semantic",
            )
            related_feedback_by_subtitle = load_related_feedback_counts(
                connection,
                related_terms=related_terms,
                category=category,
                search_mode="semantic",
            )
        finally:
            connection.close()

        combined_records: dict[int, dict] = {}
        for record in payload.get("records", []):
            combined_records[int(record["id"])] = dict(record)
        for record in related_term_candidates:
            subtitle_id = int(record["id"])
            existing = combined_records.get(subtitle_id)
            if existing is None or float(record.get("score") or 0.0) > float(existing.get("score") or 0.0):
                combined_records[subtitle_id] = dict(record)
        for record in feedback_seed_candidates:
            subtitle_id = int(record["id"])
            existing = combined_records.get(subtitle_id)
            if existing is None or float(record.get("score") or 0.0) > float(existing.get("score") or 0.0):
                combined_records[subtitle_id] = dict(record)

        reranked_records = []
        for record in combined_records.values():
            subtitle_id = int(record["id"])
            feedback = feedback_by_subtitle.get(subtitle_id, {"usefulCount": 0, "badCount": 0})
            related_feedback = related_feedback_by_subtitle.get(
                subtitle_id,
                {"usefulCount": 0.0, "badCount": 0.0},
            )
            feedback_boost = compute_feedback_boost(
                useful_count=feedback["usefulCount"],
                bad_count=feedback["badCount"],
            )
            related_feedback_boost = compute_related_feedback_boost(
                useful_count=related_feedback["usefulCount"],
                bad_count=related_feedback["badCount"],
            )
            reranked_record = dict(record)
            reranked_record["usefulCount"] = feedback["usefulCount"]
            reranked_record["badCount"] = feedback["badCount"]
            reranked_record["relatedUsefulCount"] = related_feedback["usefulCount"]
            reranked_record["relatedBadCount"] = related_feedback["badCount"]
            reranked_record["feedbackBoost"] = feedback_boost
            reranked_record["relatedFeedbackBoost"] = related_feedback_boost
            reranked_record["rankScore"] = (
                float(record.get("score") or 0.0) + feedback_boost + related_feedback_boost
            )
            reranked_records.append(reranked_record)

        reranked_records.sort(
            key=lambda item: (float(item.get("rankScore") or 0.0), float(item.get("score") or 0.0)),
            reverse=True,
        )
        payload["relatedTerms"] = sorted(related_terms.keys())
        payload["records"] = reranked_records[:60]
        self.send_json(payload)

    def handle_search_feedback(self):
        payload = self.read_json_body()
        if payload is None:
            return

        query = str(payload.get("query") or "").strip()
        category = str(payload.get("category") or "").strip()
        search_mode = str(payload.get("searchMode") or "").strip()
        feedback = str(payload.get("feedback") or "").strip()

        try:
            subtitle_id = int(payload["subtitleId"])
            video_id = int(payload["videoId"])
        except (KeyError, TypeError, ValueError):
            self.send_json({"ok": False, "error": "subtitleId and videoId are required"}, status=HTTPStatus.BAD_REQUEST)
            return

        rank_index_raw = payload.get("rankIndex")
        score_raw = payload.get("score")
        try:
            rank_index = int(rank_index_raw) if rank_index_raw not in (None, "") else None
            score = float(score_raw) if score_raw not in (None, "") else None
        except (TypeError, ValueError):
            self.send_json({"ok": False, "error": "invalid rankIndex or score"}, status=HTTPStatus.BAD_REQUEST)
            return

        if not query:
            self.send_json({"ok": False, "error": "query is required"}, status=HTTPStatus.BAD_REQUEST)
            return
        if category not in CATEGORY_PREFIXES:
            self.send_json({"ok": False, "error": "invalid category"}, status=HTTPStatus.BAD_REQUEST)
            return
        if search_mode != "semantic":
            self.send_json({"ok": False, "error": "only semantic feedback is supported for now"}, status=HTTPStatus.BAD_REQUEST)
            return
        if feedback not in {"useful", "bad"}:
            self.send_json({"ok": False, "error": "feedback must be useful or bad"}, status=HTTPStatus.BAD_REQUEST)
            return

        connection = sqlite3.connect(DB_PATH)
        connection.row_factory = sqlite3.Row
        try:
            ensure_search_feedback_schema(connection)
            subtitle_row = connection.execute(
                """
                SELECT s.id, s.video_id
                FROM subtitles s
                JOIN videos v ON v.id = s.video_id
                WHERE s.id = ?
                  AND s.video_id = ?
                  AND v.folder_path LIKE ?
                """,
                (subtitle_id, video_id, CATEGORY_PREFIXES[category]),
            ).fetchone()

            if subtitle_row is None:
                self.send_json({"ok": False, "error": "subtitle not found for feedback"}, status=HTTPStatus.NOT_FOUND)
                return

            connection.execute(
                """
                INSERT INTO search_feedback (
                  query,
                  normalized_query,
                  category,
                  search_mode,
                  subtitle_id,
                  video_id,
                  rank_index,
                  score,
                  feedback,
                  created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(normalized_query, category, search_mode, subtitle_id, rank_index)
                DO UPDATE SET
                  query = excluded.query,
                  video_id = excluded.video_id,
                  score = excluded.score,
                  feedback = excluded.feedback,
                  created_at = excluded.created_at
                """,
                (
                    query,
                    normalize_query(query),
                    category,
                    search_mode,
                    subtitle_id,
                    video_id,
                    rank_index,
                    score,
                    feedback,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            connection.commit()

            counts_row = connection.execute(
                """
                SELECT
                  SUM(CASE WHEN feedback = 'useful' THEN 1 ELSE 0 END) AS useful_count,
                  SUM(CASE WHEN feedback = 'bad' THEN 1 ELSE 0 END) AS bad_count
                FROM search_feedback
                WHERE normalized_query = ?
                  AND category = ?
                  AND search_mode = ?
                  AND subtitle_id = ?
                """,
                (normalize_query(query), category, search_mode, subtitle_id),
            ).fetchone()
        finally:
            connection.close()

        useful_count = int((counts_row["useful_count"] if counts_row else 0) or 0)
        bad_count = int((counts_row["bad_count"] if counts_row else 0) or 0)
        self.send_json(
            {
                "ok": True,
                "query": query,
                "subtitleId": subtitle_id,
                "feedback": feedback,
                "usefulCount": useful_count,
                "badCount": bad_count,
            }
        )

    def handle_clip(self, query_string: str):
        params = parse_qs(query_string)
        subtitle_id = (params.get("subtitle_id", [""])[0] or "").strip()

        if not subtitle_id.isdigit():
            self.send_json({"ok": False, "error": "subtitle_id is required"}, status=HTTPStatus.BAD_REQUEST)
            return

        try:
            output_path = ensure_clip_file(int(subtitle_id))
        except FileNotFoundError as error:
            self.send_json({"ok": False, "error": str(error)}, status=HTTPStatus.NOT_FOUND)
            return
        except ValueError as error:
            self.send_json({"ok": False, "error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            return
        except RuntimeError as error:
            self.send_json({"ok": False, "error": str(error)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        location = "/" + quote(output_path.relative_to(ROOT_DIR).as_posix())
        self.path = location
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.end_headers()

    def handle_clip_range(self, query_string: str):
        params = parse_qs(query_string)
        video_id = (params.get("video_id", [""])[0] or "").strip()
        start = (params.get("start", [""])[0] or "").strip()
        end = (params.get("end", [""])[0] or "").strip()

        if not video_id.isdigit():
            self.send_json({"ok": False, "error": "video_id is required"}, status=HTTPStatus.BAD_REQUEST)
            return

        try:
            start_seconds = float(start)
            end_seconds = float(end)
        except ValueError:
            self.send_json({"ok": False, "error": "start and end are required"}, status=HTTPStatus.BAD_REQUEST)
            return

        try:
            output_path = ensure_clip_range_file(
                int(video_id),
                start_seconds=start_seconds,
                end_seconds=end_seconds,
            )
        except FileNotFoundError as error:
            self.send_json({"ok": False, "error": str(error)}, status=HTTPStatus.NOT_FOUND)
            return
        except ValueError as error:
            self.send_json({"ok": False, "error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            return
        except RuntimeError as error:
            self.send_json({"ok": False, "error": str(error)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        location = "/" + quote(output_path.relative_to(ROOT_DIR).as_posix())
        self.path = location
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.end_headers()

    def handle_thumbnail(self, query_string: str):
        params = parse_qs(query_string)
        subtitle_id = (params.get("subtitle_id", [""])[0] or "").strip()

        if not subtitle_id.isdigit():
            self.send_json({"ok": False, "error": "subtitle_id is required"}, status=HTTPStatus.BAD_REQUEST)
            return

        try:
            output_path = ensure_thumbnail_file(int(subtitle_id))
        except FileNotFoundError as error:
            self.send_json({"ok": False, "error": str(error)}, status=HTTPStatus.NOT_FOUND)
            return
        except ValueError as error:
            self.send_json({"ok": False, "error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            return
        except RuntimeError as error:
            self.send_json({"ok": False, "error": str(error)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        location = "/" + quote(output_path.relative_to(ROOT_DIR).as_posix())
        self.path = location
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.end_headers()

    def handle_subtitle_at(self, query_string: str):
        params = parse_qs(query_string)
        video_id = (params.get("video_id", [""])[0] or "").strip()
        current_time = (params.get("time", [""])[0] or "").strip()

        if not video_id.isdigit():
            self.send_json({"ok": False, "error": "video_id is required"}, status=HTTPStatus.BAD_REQUEST)
            return

        try:
            time_value = float(current_time)
        except ValueError:
            self.send_json({"ok": False, "error": "time is required"}, status=HTTPStatus.BAD_REQUEST)
            return

        connection = sqlite3.connect(DB_PATH)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                """
                SELECT
                  id,
                  video_id,
                  cue_index,
                  start_seconds,
                  end_seconds,
                  clip_start_seconds,
                  clip_end_seconds,
                  text
                FROM subtitles
                WHERE video_id = ?
                ORDER BY
                  CASE
                    WHEN ? BETWEEN start_seconds AND end_seconds THEN 0
                    ELSE 1
                  END,
                  CASE
                    WHEN ? < start_seconds THEN start_seconds - ?
                    WHEN ? > end_seconds THEN ? - end_seconds
                    ELSE 0
                  END,
                  cue_index
                LIMIT 1
                """,
                (int(video_id), time_value, time_value, time_value, time_value, time_value),
            ).fetchone()
        finally:
            connection.close()

        if row is None:
            self.send_json({"ok": False, "error": "subtitle not found for video"}, status=HTTPStatus.NOT_FOUND)
            return

        clip_start = max(0.0, float(row["clip_start_seconds"]) - CLIP_REQUEST_PAD_SECONDS)
        lab_time = max(0.0, time_value - clip_start)
        self.send_json(
            {
                "ok": True,
                "record": {
                    "subtitleId": int(row["id"]),
                    "videoId": int(row["video_id"]),
                    "cueIndex": int(row["cue_index"]),
                    "startSeconds": float(row["start_seconds"]),
                    "endSeconds": float(row["end_seconds"]),
                    "clipStartSeconds": float(row["clip_start_seconds"]),
                    "clipEndSeconds": float(row["clip_end_seconds"]),
                    "labTime": lab_time,
                    "text": row["text"],
                },
            }
        )

    def handle_admin_subtitle(self, query_string: str):
        params = parse_qs(query_string)
        subtitle_id = (params.get("subtitle_id", [""])[0] or "").strip()

        if not subtitle_id.isdigit():
            self.send_json({"ok": False, "error": "subtitle_id is required"}, status=HTTPStatus.BAD_REQUEST)
            return

        connection = sqlite3.connect(DB_PATH)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                """
                SELECT
                  s.id,
                  s.video_id,
                  s.cue_index,
                  s.start_seconds,
                  s.end_seconds,
                  s.clip_start_seconds,
                  s.clip_end_seconds,
                  s.clip_mode,
                  s.text,
                  v.title AS video_title,
                  v.folder_path,
                  v.srt_path
                FROM subtitles s
                JOIN videos v ON v.id = s.video_id
                WHERE s.id = ?
                """,
                (int(subtitle_id),),
            ).fetchone()
        finally:
            connection.close()

        if row is None:
            self.send_json({"ok": False, "error": "subtitle not found"}, status=HTTPStatus.NOT_FOUND)
            return

        self.send_json(
            {
                "ok": True,
                "record": {
                    "id": int(row["id"]),
                    "videoId": int(row["video_id"]),
                    "videoTitle": row["video_title"],
                    "folderPath": row["folder_path"],
                    "srtPath": row["srt_path"],
                    "cueIndex": int(row["cue_index"]),
                    "startSeconds": float(row["start_seconds"]),
                    "endSeconds": float(row["end_seconds"]),
                    "clipStartSeconds": float(row["clip_start_seconds"]),
                    "clipEndSeconds": float(row["clip_end_seconds"]),
                    "clipMode": row["clip_mode"],
                    "text": row["text"],
                },
            }
        )

    def handle_admin_video(self, query_string: str):
        params = parse_qs(query_string)
        video_id = (params.get("video_id", [""])[0] or "").strip()

        if not video_id.isdigit():
            self.send_json({"ok": False, "error": "video_id is required"}, status=HTTPStatus.BAD_REQUEST)
            return

        connection = sqlite3.connect(DB_PATH)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                """
                SELECT id, title, folder_path, video_path, srt_path, has_video, has_subtitle, source_url
                FROM videos
                WHERE id = ?
                """,
                (int(video_id),),
            ).fetchone()
        finally:
            connection.close()

        if row is None:
            self.send_json({"ok": False, "error": "video not found"}, status=HTTPStatus.NOT_FOUND)
            return

        self.send_json(
            {
                "ok": True,
                "record": {
                    "id": int(row["id"]),
                    "title": row["title"],
                    "folderPath": row["folder_path"],
                    "videoPath": row["video_path"],
                    "srtPath": row["srt_path"],
                    "hasVideo": int(row["has_video"]),
                    "hasSubtitle": int(row["has_subtitle"]),
                    "sourceUrl": row["source_url"] or "",
                },
            }
        )

    def handle_admin_create_video(self):
        payload = self.read_json_body()
        if payload is None:
            return

        category = str(payload.get("category") or "").strip()
        title = str(payload.get("title") or "").strip()
        source_url = str(payload.get("sourceUrl") or "").strip()
        category_dir_map = {
            "humor": "幽默",
            "writing": "文笔",
        }

        category_dir_name = category_dir_map.get(category)
        if category_dir_name is None:
            self.send_json({"ok": False, "error": "invalid category"}, status=HTTPStatus.BAD_REQUEST)
            return
        if not title:
            self.send_json({"ok": False, "error": "title is required"}, status=HTTPStatus.BAD_REQUEST)
            return

        folder_name = sanitize_file_name(title)
        folder_path = ROOT_DIR / "data" / category_dir_name / folder_name
        folder_path.mkdir(parents=True, exist_ok=True)

        source_payload = {"title": title}
        if source_url:
            source_payload["sourceUrl"] = source_url
        (folder_path / "source.json").write_text(f"{json.dumps(source_payload, ensure_ascii=False, indent=2)}\n", encoding="utf-8")

        relative_folder = folder_path.relative_to(ROOT_DIR).as_posix()
        command = [
            "python3",
            str(BUILD_SQLITE_SCRIPT),
            "--folder",
            relative_folder,
            "--commit-every",
            "1",
        ]
        result = subprocess.run(command, cwd=str(ROOT_DIR), capture_output=True, text=True)
        if result.returncode != 0:
            self.send_json(
                {
                    "ok": False,
                    "error": "video sync failed",
                    "details": (result.stderr or result.stdout)[-1600:],
                },
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return

        connection = sqlite3.connect(DB_PATH)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                """
                SELECT id, title, folder_path, video_sha256, has_video, has_subtitle
                FROM videos
                WHERE folder_path = ?
                """,
                (relative_folder,),
            ).fetchone()
            duplicate_rows = []
            if row is not None and row["video_sha256"]:
                duplicate_rows = connection.execute(
                    """
                    SELECT id, title, folder_path
                    FROM videos
                    WHERE video_sha256 = ?
                      AND id != ?
                    ORDER BY id
                    """,
                    (row["video_sha256"], int(row["id"])),
                ).fetchall()
        finally:
            connection.close()

        if row is None:
            self.send_json(
                {"ok": False, "error": "video created but not found after sync"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return

        self.send_json(
            {
                "ok": True,
                "record": {
                    "id": int(row["id"]),
                    "title": row["title"],
                    "folderPath": row["folder_path"],
                    "hasVideo": int(row["has_video"]),
                    "hasSubtitle": int(row["has_subtitle"]),
                },
                "duplicateVideos": [
                    {
                        "id": int(duplicate_row["id"]),
                        "title": duplicate_row["title"],
                        "folderPath": duplicate_row["folder_path"],
                    }
                    for duplicate_row in duplicate_rows
                ],
                "duplicateWarning": bool(duplicate_rows),
                "output": (result.stdout or "").strip()[-1600:],
            }
        )

    def handle_admin_save_subtitle(self):
        payload = self.read_json_body()
        if payload is None:
            return

        try:
            subtitle_id = int(payload["subtitleId"]) if payload.get("subtitleId") not in (None, "") else None
            video_id = int(payload["videoId"])
            cue_index = int(payload["cueIndex"])
            start_seconds = float(payload["startSeconds"])
            end_seconds = float(payload["endSeconds"])
            text = str(payload["text"]).strip()
        except (KeyError, TypeError, ValueError):
            self.send_json({"ok": False, "error": "invalid payload"}, status=HTTPStatus.BAD_REQUEST)
            return

        if not text:
            self.send_json({"ok": False, "error": "text is required"}, status=HTTPStatus.BAD_REQUEST)
            return

        clip_start, clip_end = build_fallback_clip(start_seconds, end_seconds)
        connection = sqlite3.connect(DB_PATH)
        connection.row_factory = sqlite3.Row

        try:
            video_row = connection.execute(
                "SELECT id, title, folder_path, srt_path FROM videos WHERE id = ?",
                (video_id,),
            ).fetchone()
            if video_row is None:
                self.send_json({"ok": False, "error": "video not found"}, status=HTTPStatus.BAD_REQUEST)
                return

            existing = None
            if subtitle_id is not None:
                existing = connection.execute(
                    "SELECT id, text FROM subtitles WHERE id = ?",
                    (subtitle_id,),
                ).fetchone()

            if existing is not None:
                connection.execute(
                    "INSERT INTO subtitle_fts(subtitle_fts, rowid, text) VALUES('delete', ?, ?)",
                    (subtitle_id, existing["text"]),
                )
                connection.execute(
                    """
                    UPDATE subtitles
                    SET video_id = ?,
                        cue_index = ?,
                        start_seconds = ?,
                        end_seconds = ?,
                        clip_start_seconds = ?,
                        clip_end_seconds = ?,
                        clip_mode = ?,
                        text = ?
                    WHERE id = ?
                    """,
                    (
                        video_id,
                        cue_index,
                        start_seconds,
                        end_seconds,
                        clip_start,
                        clip_end,
                        "fallback",
                        text,
                        subtitle_id,
                    ),
                )
                saved_id = subtitle_id
            else:
                cursor = connection.execute(
                    """
                    INSERT INTO subtitles (
                      video_id,
                      cue_index,
                      start_seconds,
                      end_seconds,
                      clip_start_seconds,
                      clip_end_seconds,
                      clip_mode,
                      text
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        video_id,
                        cue_index,
                        start_seconds,
                        end_seconds,
                        clip_start,
                        clip_end,
                        "fallback",
                        text,
                    ),
                )
                saved_id = int(cursor.lastrowid)

            delete_vec_rows(connection, [saved_id])
            connection.execute("INSERT INTO subtitle_fts(rowid, text) VALUES (?, ?)", (saved_id, text))
            connection.execute(
                """
                UPDATE videos
                SET subtitle_cue_count = (
                  SELECT COUNT(*) FROM subtitles WHERE video_id = ?
                )
                WHERE id = ?
                """,
                (video_id, video_id),
            )
            subtitle_rows = connection.execute(
                """
                SELECT cue_index, start_seconds, end_seconds, text
                FROM subtitles
                WHERE video_id = ?
                ORDER BY cue_index, id
                """,
                (video_id,),
            ).fetchall()

            srt_relative_path = video_row["srt_path"]
            if srt_relative_path:
                srt_path = ROOT_DIR / str(srt_relative_path)
            else:
                folder_path = ROOT_DIR / str(video_row["folder_path"])
                srt_path = folder_path / f"{video_row['title']}.srt"
                connection.execute(
                    """
                    UPDATE videos
                    SET srt_path = ?, has_subtitle = 1
                    WHERE id = ?
                    """,
                    (srt_path.relative_to(ROOT_DIR).as_posix(), video_id),
                )

            srt_path.parent.mkdir(parents=True, exist_ok=True)
            srt_path.write_text(render_srt(subtitle_rows), encoding="utf-8")
            connection.commit()
        except sqlite3.IntegrityError as error:
            connection.rollback()
            self.send_json({"ok": False, "error": f"save failed: {error}"}, status=HTTPStatus.BAD_REQUEST)
            return
        except OSError as error:
            connection.rollback()
            self.send_json({"ok": False, "error": f"srt write failed: {error}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        finally:
            connection.close()

        self.send_json(
            {
                "ok": True,
                "subtitleId": saved_id,
                "videoId": video_id,
                "message": "saved",
            }
        )

    def handle_admin_generate(self):
        payload = self.read_json_body()
        if payload is None:
            return

        action = str(payload.get("action") or "").strip()
        try:
            video_id = int(payload["videoId"])
        except (KeyError, TypeError, ValueError):
            self.send_json({"ok": False, "error": "videoId is required"}, status=HTTPStatus.BAD_REQUEST)
            return

        if action == "sync":
            command = [
                "python3",
                str(BUILD_SQLITE_SCRIPT),
                "--video-id",
                str(video_id),
                "--commit-every",
                "1",
            ]
        elif action == "clip":
            command = [
                "python3",
                str(BUILD_SQLITE_SCRIPT),
                "--rebuild-clips",
                "--video-id",
                str(video_id),
                "--commit-every",
                "1",
            ]
        elif action == "embedding":
            if not SEMANTIC_PYTHON.exists():
                self.send_json(
                    {"ok": False, "error": "local embedding env not found", "details": "Missing .venv/bin/python"},
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return
            command = [
                str(SEMANTIC_PYTHON),
                str(EMBED_SQLITE_SCRIPT),
                "--provider",
                "local",
                "--model",
                LOCAL_EMBED_MODEL,
                "--video-id",
                str(video_id),
            ]
        else:
            self.send_json({"ok": False, "error": "invalid action"}, status=HTTPStatus.BAD_REQUEST)
            return

        result = subprocess.run(command, cwd=str(ROOT_DIR), capture_output=True, text=True)
        if result.returncode != 0:
            self.send_json(
                {
                    "ok": False,
                    "error": f"{action} generation failed",
                    "details": (result.stderr or result.stdout)[-1600:],
                },
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return

        self.send_json(
            {
                "ok": True,
                "action": action,
                "videoId": video_id,
                "output": (result.stdout or "").strip()[-1600:],
            }
        )

    def handle_clip_lab_export(self):
        payload = self.read_json_body()
        if payload is None:
            return

        src = str(payload.get("src") or "").strip()
        try:
            start = max(0.0, float(payload.get("start") or 0.0))
            end = max(0.0, float(payload.get("end") or 0.0))
            x = max(0, int(payload.get("x") or 0))
            y = max(0, int(payload.get("y") or 0))
            width = max(1, int(payload.get("w") or 1))
            height = max(1, int(payload.get("h") or 1))
        except (TypeError, ValueError):
            self.send_json({"ok": False, "error": "invalid export parameters"}, status=HTTPStatus.BAD_REQUEST)
            return

        if not src:
            self.send_json({"ok": False, "error": "src is required"}, status=HTTPStatus.BAD_REQUEST)
            return
        if end <= start:
            self.send_json({"ok": False, "error": "end must be greater than start"}, status=HTTPStatus.BAD_REQUEST)
            return

        source_path = resolve_local_media_path(src)
        if source_path is None or not source_path.exists():
            self.send_json({"ok": False, "error": "source clip not found"}, status=HTTPStatus.BAD_REQUEST)
            return

        CLIP_LAB_EXPORTS_DIR.mkdir(exist_ok=True)
        duration = max(0.1, end - start)
        crop_expr = f"crop={width}:{height}:{x}:{y},fps=12,scale=480:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse"
        source_base = sanitize_file_name(source_path.stem)
        export_key = hashlib.sha1(
            f"{source_path}:{start:.3f}:{end:.3f}:{x}:{y}:{width}:{height}".encode("utf-8")
        ).hexdigest()[:12]
        output_name = f"{source_base}__crop__{export_key}.gif"
        output_path = CLIP_LAB_EXPORTS_DIR / output_name

        if not output_path.exists():
            command = [
                "ffmpeg",
                "-y",
                "-ss",
                f"{start:.3f}",
                "-t",
                f"{duration:.3f}",
                "-i",
                str(source_path),
                "-vf",
                crop_expr,
                "-an",
                str(output_path),
            ]
            result = subprocess.run(command, cwd=str(ROOT_DIR), capture_output=True, text=True)
            if result.returncode != 0:
                self.send_json(
                    {
                        "ok": False,
                        "error": "ffmpeg failed to export gif",
                        "details": (result.stderr or result.stdout)[-1600:],
                    },
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return

        self.send_json(
            {
                "ok": True,
                "url": f"/clip_lab_exports/{quote(output_name)}",
                "path": str(output_path),
                "fileName": output_name,
            }
        )

    def send_json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self):
        content_length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(content_length) if content_length > 0 else b""
        try:
            return json.loads(body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self.send_json({"ok": False, "error": "invalid json body"}, status=HTTPStatus.BAD_REQUEST)
            return None

    def send_head(self):
        path = self.translate_path(self.path)

        if os.path.isdir(path):
            return super().send_head()

        ctype = self.guess_type(path)

        try:
            file = open(path, "rb")
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return None

        file_size = os.fstat(file.fileno()).st_size
        range_header = self.headers.get("Range")

        if not range_header:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-type", ctype)
            self.send_header("Content-Length", str(file_size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            return file

        match = self.range_header_pattern.match(range_header.strip())

        if not match:
            file.close()
            self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, "Invalid Range header")
            return None

        start_text, end_text = match.groups()

        if start_text == "" and end_text == "":
            file.close()
            self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, "Invalid Range header")
            return None

        if start_text == "":
            suffix_length = int(end_text)
            start = max(0, file_size - suffix_length)
            end = file_size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else file_size - 1

        if start > end or start >= file_size:
            file.close()
            self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, "Range out of bounds")
            return None

        end = min(end, file_size - 1)
        content_length = end - start + 1

        self.send_response(HTTPStatus.PARTIAL_CONTENT)
        self.send_header("Content-type", ctype)
        self.send_header("Content-Length", str(content_length))
        self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

        file.seek(start)
        self.range = (start, end)
        return file

    def copyfile(self, source, outputfile):
        byte_range = getattr(self, "range", None)

        if not byte_range:
            return super().copyfile(source, outputfile)

        start, end = byte_range
        remaining = end - start + 1
        chunk_size = 64 * 1024

        while remaining > 0:
            chunk = source.read(min(chunk_size, remaining))

            if not chunk:
                break

            outputfile.write(chunk)
            remaining -= len(chunk)

        self.range = None


def sanitize_file_name(value: str) -> str:
    """Convert arbitrary text into a filesystem-safe folder name."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", value).strip()
    return cleaned or "clip"


def ensure_clip_file(subtitle_id: int) -> Path:
    """Create or reuse the clip file for one subtitle."""
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        subtitle = connection.execute(
            """
            SELECT
              s.id,
              s.clip_start_seconds,
              s.clip_end_seconds,
              s.clip_mode,
              v.title AS video_title,
              v.video_path
            FROM subtitles s
            JOIN videos v ON v.id = s.video_id
            WHERE s.id = ?
            """,
            (subtitle_id,),
        ).fetchone()
    finally:
        connection.close()

    if subtitle is None:
        raise FileNotFoundError("subtitle not found")

    clip_start = max(0.0, float(subtitle["clip_start_seconds"]) - CLIP_REQUEST_PAD_SECONDS)
    clip_end = float(subtitle["clip_end_seconds"])
    clip_mode = subtitle["clip_mode"]
    video_rel_path = subtitle["video_path"]
    if not video_rel_path:
        raise ValueError("video path missing")

    source_video = ROOT_DIR / str(video_rel_path)
    if not source_video.exists():
        raise FileNotFoundError("video file not found")

    CLIPS_DIR.mkdir(exist_ok=True)
    safe_title = sanitize_file_name(subtitle["video_title"])
    clip_name = (
        f"{safe_title}"
        f"__s{int(clip_start * 1000)}"
        f"__e{int(clip_end * 1000)}"
        f"__{clip_mode}.mp4"
    )
    output_path = CLIPS_DIR / clip_name

    if output_path.exists():
        return output_path

    duration = max(0.1, clip_end - clip_start)
    command = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{clip_start:.3f}",
        "-i",
        str(source_video),
        "-t",
        f"{duration:.3f}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    result = subprocess.run(command, cwd=str(ROOT_DIR), capture_output=True, text=True)
    if result.returncode != 0 or not output_path.exists():
        raise RuntimeError(f"ffmpeg failed to generate clip: {(result.stderr or result.stdout)[-1200:]}")
    return output_path


def ensure_thumbnail_file(subtitle_id: int) -> Path:
    """Create or reuse a cached thumbnail for one subtitle."""
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """
            SELECT
              s.id,
              s.start_seconds,
              s.end_seconds,
              s.clip_start_seconds,
              s.clip_end_seconds,
              v.id AS video_id,
              v.video_path,
              v.video_mtime_ns,
              v.srt_mtime_ns
            FROM subtitles s
            JOIN videos v ON v.id = s.video_id
            WHERE s.id = ?
            """,
            (subtitle_id,),
        ).fetchone()
    finally:
        connection.close()

    if row is None:
        raise FileNotFoundError("subtitle not found")

    video_rel_path = row["video_path"]
    if not video_rel_path:
        raise ValueError("video path missing")

    source_video = ROOT_DIR / str(video_rel_path)
    if not source_video.exists():
        raise FileNotFoundError("video file not found")

    clip_start = max(0.0, float(row["clip_start_seconds"] or row["start_seconds"] or 0.0))
    clip_end = max(clip_start + 0.1, float(row["clip_end_seconds"] or row["end_seconds"] or clip_start + 0.1))
    clip_duration = max(0.1, clip_end - clip_start)
    capture_time = clip_start + max(0.15, min(1.0, clip_duration / 2))
    capture_time = min(max(clip_start, capture_time), clip_end - 0.05)

    THUMBNAILS_DIR.mkdir(exist_ok=True)
    output_dir = THUMBNAILS_DIR / str(int(row["video_id"]))
    output_dir.mkdir(parents=True, exist_ok=True)
    output_name = (
        f"{int(row['id'])}"
        f"__v{int(row['video_mtime_ns'] or 0)}"
        f"__s{int(row['srt_mtime_ns'] or 0)}.webp"
    )
    output_path = output_dir / output_name

    if output_path.exists():
        return output_path

    command = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{capture_time:.3f}",
        "-i",
        str(source_video),
        "-frames:v",
        "1",
        "-vf",
        "scale=480:-1:flags=lanczos",
        "-c:v",
        "libwebp",
        "-q:v",
        "82",
        "-compression_level",
        "6",
        str(output_path),
    ]
    result = subprocess.run(command, cwd=str(ROOT_DIR), capture_output=True, text=True)
    if result.returncode != 0 or not output_path.exists():
        raise RuntimeError(f"ffmpeg failed to generate thumbnail: {(result.stderr or result.stdout)[-1200:]}")
    return output_path


def ensure_clip_range_file(video_id: int, *, start_seconds: float, end_seconds: float) -> Path:
    """Create or reuse a clip file for an arbitrary time range."""
    if end_seconds <= start_seconds:
        raise ValueError("end must be greater than start")

    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        video = connection.execute(
            """
            SELECT id, title, video_path
            FROM videos
            WHERE id = ?
            """,
            (video_id,),
        ).fetchone()
    finally:
        connection.close()

    if video is None:
        raise FileNotFoundError("video not found")

    video_rel_path = video["video_path"]
    if not video_rel_path:
        raise ValueError("video path missing")

    source_video = ROOT_DIR / str(video_rel_path)
    if not source_video.exists():
        raise FileNotFoundError("video file not found")

    clip_start = max(0.0, start_seconds)
    clip_end = max(clip_start + 0.1, end_seconds)

    CLIPS_DIR.mkdir(exist_ok=True)
    safe_title = sanitize_file_name(video["title"])
    clip_name = (
        f"{safe_title}"
        f"__s{int(clip_start * 1000)}"
        f"__e{int(clip_end * 1000)}"
        f"__merged.mp4"
    )
    output_path = CLIPS_DIR / clip_name

    if output_path.exists():
        return output_path

    duration = max(0.1, clip_end - clip_start)
    command = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{clip_start:.3f}",
        "-i",
        str(source_video),
        "-t",
        f"{duration:.3f}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    result = subprocess.run(command, cwd=str(ROOT_DIR), capture_output=True, text=True)
    if result.returncode != 0 or not output_path.exists():
        raise RuntimeError(f"ffmpeg failed to generate clip: {(result.stderr or result.stdout)[-1200:]}")
    return output_path


def resolve_local_media_path(src: str) -> Path | None:
    """Resolve a stored media path to an existing local file."""
    parsed = urlparse(src)
    if parsed.path == "/api/clip":
        subtitle_id = (parse_qs(parsed.query).get("subtitle_id", [""])[0] or "").strip()
        if not subtitle_id.isdigit():
            return None
        try:
            return ensure_clip_file(int(subtitle_id))
        except (FileNotFoundError, ValueError, RuntimeError):
            return None
    if parsed.path == "/api/clip-range":
        params = parse_qs(parsed.query)
        video_id = (params.get("video_id", [""])[0] or "").strip()
        start = (params.get("start", [""])[0] or "").strip()
        end = (params.get("end", [""])[0] or "").strip()
        if not video_id.isdigit():
            return None
        try:
            return ensure_clip_range_file(
                int(video_id),
                start_seconds=float(start),
                end_seconds=float(end),
            )
        except (FileNotFoundError, ValueError, RuntimeError):
            return None

    path_value = parsed.path if parsed.scheme or parsed.netloc else src
    if not path_value:
        return None
    path_value = unquote(path_value)
    if path_value.startswith("/"):
        candidate = (ROOT_DIR / path_value.lstrip("/")).resolve()
    else:
        candidate = (ROOT_DIR / path_value).resolve()
    try:
        candidate.relative_to(ROOT_DIR.resolve())
    except ValueError:
        return None
    return candidate


def format_srt_timestamp(seconds: float) -> str:
    """Format seconds as an SRT timestamp string."""
    total_milliseconds = max(0, int(round(seconds * 1000)))
    hours = total_milliseconds // 3_600_000
    remainder = total_milliseconds % 3_600_000
    minutes = remainder // 60_000
    remainder %= 60_000
    secs = remainder // 1000
    milliseconds = remainder % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


def render_srt(rows) -> str:
    """Render subtitle rows back into SRT text."""
    blocks = []
    for row_number, row in enumerate(rows, start=1):
        text = str(row["text"] or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        blocks.append(
            "\n".join(
                [
                    str(row_number),
                    f"{format_srt_timestamp(float(row['start_seconds']))} --> {format_srt_timestamp(float(row['end_seconds']))}",
                    text,
                ]
            )
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def clamp_clip_window(clip_start: float, clip_end: float, cue_start: float, cue_end: float) -> tuple[float, float]:
    """Clamp a clip window so it always covers the cue and stays sane."""
    clip_start = max(0.0, min(clip_start, cue_start))
    clip_end = max(cue_end, clip_end)

    if clip_end - clip_start <= MAX_SMART_SEGMENT_SECONDS:
        return clip_start, clip_end

    cue_duration = max(0.0, cue_end - cue_start)
    if cue_duration >= MAX_SMART_SEGMENT_SECONDS:
        return cue_start, cue_end

    extra_budget = MAX_SMART_SEGMENT_SECONDS - cue_duration
    desired_start = max(0.0, cue_start - extra_budget / 2)
    desired_end = max(cue_end, cue_end + extra_budget / 2)
    return desired_start, min(desired_end, desired_start + MAX_SMART_SEGMENT_SECONDS)


def build_fallback_clip(cue_start: float, cue_end: float) -> tuple[float, float]:
    """Build a conservative fallback clip around one subtitle."""
    return clamp_clip_window(
        cue_start - FALLBACK_PAD_SECONDS,
        cue_end + FALLBACK_PAD_SECONDS,
        cue_start,
        cue_end,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("port", nargs="?", type=int, default=int(os.environ.get("PORT", "4173")))
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    args = parser.parse_args()
    port = args.port
    host = args.host
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        ensure_search_feedback_schema(connection)
        ensure_query_group_schema(connection)
        connection.commit()
    finally:
        connection.close()
    server = ThreadingHTTPServer((host, port), RangeRequestHandler)
    print(f"Serving on http://{host}:{port}")
    server.serve_forever()
