#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


ROOT_DIR = Path.cwd()
DATA_DIR = ROOT_DIR / "data"
OUTPUT_DB = ROOT_DIR / "subtitles.db"
SCENE_SCORE_THRESHOLD = 0.18
MAX_SMART_SEGMENT_SECONDS = 120.0
FALLBACK_PAD_SECONDS = 8.0
SCENE_JOIN_EPSILON_SECONDS = 0.05


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build or incrementally sync subtitles.db from data/.")
    parser.add_argument(
        "--full-rebuild",
        action="store_true",
        help="Drop and rebuild every table from scratch instead of incrementally syncing changed folders.",
    )
    parser.add_argument(
        "--folder",
        action="append",
        default=[],
        help="Only sync the specified folder under the repo root. Can be passed multiple times.",
    )
    parser.add_argument(
        "--video-id",
        action="append",
        type=int,
        default=[],
        help="Only sync the specified video id from the videos table. Can be passed multiple times.",
    )
    parser.add_argument(
        "--rebuild-clips",
        action="store_true",
        help="Recompute subtitle clip ranges for matching folders even when source/video/subtitle mtimes are unchanged.",
    )
    parser.add_argument(
        "--only-unprocessed-clips",
        action="store_true",
        help="When rebuilding clips, only process videos whose clip_processed is still 0.",
    )
    parser.add_argument(
        "--commit-every",
        type=int,
        default=0,
        help="Commit after every N changed folders. Defaults to 1 for clip refresh, otherwise commit at the end.",
    )
    parser.add_argument(
        "--max-folders",
        type=int,
        default=0,
        help="Only process up to N target folders after filtering. 0 means no limit.",
    )
    return parser.parse_args()


def parse_timestamp(value: str) -> float:
    parts = value.split(":")
    if len(parts) != 3:
        return 0.0

    try:
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds, milliseconds = parts[2].split(",")
        return hours * 3600 + minutes * 60 + int(seconds) + int(milliseconds) / 1000
    except (TypeError, ValueError):
        return 0.0


def parse_srt(content: str) -> list[dict[str, float | str]]:
    normalized = content.replace("\r", "").strip()
    if not normalized:
        return []

    cues: list[dict[str, float | str]] = []
    for block in normalized.split("\n\n"):
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if len(lines) < 3:
            continue

        time_line = lines[1]
        parts = time_line.split(" --> ")
        if len(parts) != 2:
            continue

        text = " ".join(lines[2:]).strip()
        if not text:
            continue

        cues.append(
            {
                "start_seconds": parse_timestamp(parts[0]),
                "end_seconds": parse_timestamp(parts[1]),
                "text": text,
            }
        )

    return cues


def is_video_folder(folder: Path) -> bool:
    try:
        entries = list(folder.iterdir())
    except OSError:
        return False

    for entry in entries:
        if not entry.is_file():
            continue
        if entry.suffix.lower() in {".mp4", ".webm", ".mov", ".m4v", ".srt"}:
            return True
        if entry.name == "source.json":
            return True

    return False


def collect_video_folders(current_dir: Path) -> list[Path]:
    folders: set[Path] = set()

    try:
        entries = list(current_dir.iterdir())
    except OSError:
        return []

    for entry in entries:
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        folders.update(collect_video_folders(entry))

    if is_video_folder(current_dir):
        folders.add(current_dir)

    return sorted(folders)


def pick_video(entries: Iterable[Path]) -> Path | None:
    candidates = [entry for entry in entries if entry.is_file() and entry.suffix.lower() in {".mp4", ".webm", ".mov", ".m4v"}]
    preferred_suffixes = [".faststart.mp4", ".web.mp4", ".mp4", ".webm", ".mov", ".m4v"]

    for suffix in preferred_suffixes:
        for candidate in candidates:
            if candidate.name.lower().endswith(suffix):
                return candidate
    return None


def relative_posix(value: Path) -> str:
    return value.relative_to(ROOT_DIR).as_posix()


def probe_duration(video_path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ],
        cwd=str(ROOT_DIR),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return 0.0

    try:
        return max(0.0, float(result.stdout.strip()))
    except ValueError:
        return 0.0


def calculate_file_sha256(path: Path | None) -> str | None:
    if path is None:
        return None

    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def detect_scene_boundaries(video_path: Path) -> list[float]:
    filter_expr = f"select='gt(scene,{SCENE_SCORE_THRESHOLD})',showinfo"
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(video_path),
            "-filter:v",
            filter_expr,
            "-an",
            "-f",
            "null",
            "-",
        ],
        cwd=str(ROOT_DIR),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []

    boundaries: list[float] = [0.0]
    for match in re.finditer(r"pts_time:([0-9]+(?:\.[0-9]+)?)", result.stderr):
        try:
            timestamp = float(match.group(1))
        except ValueError:
            continue
        if timestamp > boundaries[-1]:
            boundaries.append(timestamp)
    return boundaries


def clamp_clip_window(
    clip_start: float,
    clip_end: float,
    cue_start: float,
    cue_end: float,
    video_duration: float,
) -> tuple[float, float]:
    clip_start = max(0.0, min(clip_start, cue_start))
    clip_end = max(cue_end, clip_end)

    if video_duration > 0:
        clip_end = min(video_duration, clip_end)

    if clip_end - clip_start <= MAX_SMART_SEGMENT_SECONDS:
        return clip_start, clip_end

    cue_duration = max(0.0, cue_end - cue_start)
    if cue_duration >= MAX_SMART_SEGMENT_SECONDS:
        return cue_start, cue_end

    extra_budget = MAX_SMART_SEGMENT_SECONDS - cue_duration
    desired_start = cue_start - extra_budget / 2
    desired_end = cue_end + extra_budget / 2

    if desired_start < 0.0:
        desired_end += -desired_start
        desired_start = 0.0

    if video_duration > 0 and desired_end > video_duration:
        desired_start -= desired_end - video_duration
        desired_end = video_duration

    desired_start = max(0.0, min(desired_start, cue_start))
    desired_end = max(cue_end, desired_end)

    if desired_end - desired_start > MAX_SMART_SEGMENT_SECONDS:
        desired_end = desired_start + MAX_SMART_SEGMENT_SECONDS
        if desired_end < cue_end:
            desired_end = cue_end
            desired_start = cue_end - MAX_SMART_SEGMENT_SECONDS

    return max(0.0, desired_start), desired_end


def build_fallback_clip(cue_start: float, cue_end: float, video_duration: float) -> tuple[float, float, str]:
    clip_start, clip_end = clamp_clip_window(
        cue_start - FALLBACK_PAD_SECONDS,
        cue_end + FALLBACK_PAD_SECONDS,
        cue_start,
        cue_end,
        video_duration,
    )
    return clip_start, clip_end, "fallback"


def expand_scene_window(
    boundaries: list[float],
    interval_index: int,
    cue_start: float,
    cue_end: float,
    video_duration: float,
) -> tuple[float, float] | None:
    left_index = interval_index
    right_index = interval_index + 1
    clip_start = boundaries[left_index]
    clip_end = boundaries[right_index]
    clip_start, clip_end = clamp_clip_window(clip_start, clip_end, cue_start, cue_end, video_duration)
    if clip_end - clip_start > MAX_SMART_SEGMENT_SECONDS:
        return None
    return clip_start, clip_end


def build_clip_ranges(cues: list[dict[str, float | str]], video_path: Path | None) -> list[tuple[float, float, str]]:
    if not cues:
        return []

    if video_path is None:
        return [
            build_fallback_clip(
                float(cue["start_seconds"]),
                float(cue["end_seconds"]),
                0.0,
            )
            for cue in cues
        ]

    video_duration = probe_duration(video_path)
    scene_boundaries = detect_scene_boundaries(video_path)
    if not scene_boundaries:
        return [
            build_fallback_clip(
                float(cue["start_seconds"]),
                float(cue["end_seconds"]),
                video_duration,
            )
            for cue in cues
        ]

    if video_duration > scene_boundaries[-1]:
        scene_boundaries.append(video_duration)

    ranges: list[tuple[float, float, str]] = []

    right_index = 1
    for cue in cues:
        cue_start = float(cue["start_seconds"])
        cue_end = float(cue["end_seconds"])
        cue_mid = (cue_start + cue_end) / 2

        while right_index < len(scene_boundaries) and cue_mid >= scene_boundaries[right_index]:
            right_index += 1

        interval_index = max(0, min(right_index - 1, len(scene_boundaries) - 2))
        expanded_window = expand_scene_window(
            scene_boundaries,
            interval_index,
            cue_start,
            cue_end,
            video_duration,
        )

        if expanded_window is None:
            ranges.append(build_fallback_clip(cue_start, cue_end, video_duration))
            continue

        clip_start, clip_end = expanded_window
        ranges.append((clip_start, clip_end, "scene"))

    for index in range(len(ranges) - 1):
        clip_start, clip_end, clip_mode = ranges[index]
        next_clip_start, next_clip_end, next_clip_mode = ranges[index + 1]
        current_cue_end = float(cues[index]["end_seconds"])
        next_cue_start = float(cues[index + 1]["start_seconds"])

        # If a scene clip ends exactly at the next subtitle boundary, but the next
        # subtitle already expands into a longer scene window, keep the current clip
        # alive through that longer right edge to avoid cutting the picture too early.
        if clip_mode != "scene" or next_clip_mode != "scene":
            continue
        if abs(clip_end - next_cue_start) > SCENE_JOIN_EPSILON_SECONDS:
            continue
        if clip_end > current_cue_end + SCENE_JOIN_EPSILON_SECONDS:
            continue
        if next_clip_start >= clip_end - SCENE_JOIN_EPSILON_SECONDS:
            continue
        if next_clip_end <= clip_end + SCENE_JOIN_EPSILON_SECONDS:
            continue

        merged_start, merged_end = clamp_clip_window(
            clip_start,
            next_clip_end,
            float(cues[index]["start_seconds"]),
            current_cue_end,
            video_duration,
        )
        ranges[index] = (merged_start, merged_end, clip_mode)

    return ranges


def init_db(connection: sqlite3.Connection, *, drop_existing: bool) -> None:
    drop_sql = ""
    if drop_existing:
        drop_sql = """
        DROP TABLE IF EXISTS subtitle_fts;
        DROP TABLE IF EXISTS subtitle_embeddings;
        DROP TABLE IF EXISTS subtitles;
        DROP TABLE IF EXISTS videos;
        DROP TABLE IF EXISTS metadata;
        """

    connection.executescript(
        f"""
        PRAGMA journal_mode = WAL;
        PRAGMA foreign_keys = ON;

        {drop_sql}

        CREATE TABLE IF NOT EXISTS metadata (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS videos (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          title TEXT NOT NULL UNIQUE,
          folder_path TEXT NOT NULL,
          video_path TEXT,
          video_sha256 TEXT,
          srt_path TEXT,
          has_video INTEGER NOT NULL DEFAULT 0,
          has_subtitle INTEGER NOT NULL DEFAULT 0,
          source_url TEXT,
          subtitle_cue_count INTEGER NOT NULL DEFAULT 0,
          source_mtime_ns INTEGER NOT NULL DEFAULT 0,
          video_mtime_ns INTEGER NOT NULL DEFAULT 0,
          srt_mtime_ns INTEGER NOT NULL DEFAULT 0,
          clip_processed INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS subtitles (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
          cue_index INTEGER NOT NULL,
          start_seconds REAL NOT NULL,
          end_seconds REAL NOT NULL,
          clip_start_seconds REAL NOT NULL,
          clip_end_seconds REAL NOT NULL,
          clip_mode TEXT NOT NULL,
          text TEXT NOT NULL,
          UNIQUE(video_id, cue_index)
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS subtitle_fts USING fts5(
          text,
          content='subtitles',
          content_rowid='id',
          tokenize='unicode61'
        );

        CREATE TABLE IF NOT EXISTS subtitle_embeddings (
          subtitle_id INTEGER PRIMARY KEY REFERENCES subtitles(id) ON DELETE CASCADE,
          model TEXT NOT NULL,
          dimensions INTEGER NOT NULL,
          embedding_json TEXT NOT NULL,
          embedding_blob BLOB NOT NULL,
          embedding_norm REAL NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_videos_folder_path ON videos (folder_path);
        CREATE INDEX IF NOT EXISTS idx_videos_source_url ON videos (source_url);
        CREATE INDEX IF NOT EXISTS idx_subtitles_video_start ON subtitles (video_id, start_seconds);
        CREATE INDEX IF NOT EXISTS idx_embeddings_model ON subtitle_embeddings (model);
        """
    )

    video_columns = {row[1] for row in connection.execute("PRAGMA table_info(videos)")}
    for column_name in ["video_sha256", "source_mtime_ns", "video_mtime_ns", "srt_mtime_ns", "clip_processed"]:
        if column_name not in video_columns:
            if column_name == "video_sha256":
                connection.execute("ALTER TABLE videos ADD COLUMN video_sha256 TEXT")
            else:
                connection.execute(
                    f"ALTER TABLE videos ADD COLUMN {column_name} INTEGER NOT NULL DEFAULT 0"
                )

    connection.execute("CREATE INDEX IF NOT EXISTS idx_videos_video_sha256 ON videos (video_sha256)")


def resolve_target_folders(requested_folders: list[str]) -> list[Path]:
    if not requested_folders:
        return collect_video_folders(DATA_DIR)

    resolved: list[Path] = []
    for raw_value in requested_folders:
        candidate = (ROOT_DIR / raw_value).resolve()
        if not candidate.exists():
            raise SystemExit(f"Folder does not exist: {raw_value}")
        if not candidate.is_dir():
            raise SystemExit(f"Folder is not a directory: {raw_value}")
        resolved.append(candidate)
    return sorted(set(resolved))


def resolve_video_id_folders(connection: sqlite3.Connection, requested_video_ids: list[int]) -> list[Path]:
    if not requested_video_ids:
        return []

    resolved: list[Path] = []
    seen_ids: set[int] = set()
    for video_id in requested_video_ids:
        if video_id in seen_ids:
            continue
        seen_ids.add(video_id)
        row = connection.execute(
            "SELECT folder_path FROM videos WHERE id = ?",
            (video_id,),
        ).fetchone()
        if row is None:
            raise SystemExit(f"Video id does not exist: {video_id}")
        resolved.append((ROOT_DIR / str(row["folder_path"])).resolve())
    return resolved


def filter_unprocessed_clip_folders(
    connection: sqlite3.Connection,
    folders: list[Path],
) -> list[Path]:
    pending_folder_rows = connection.execute(
        """
        SELECT folder_path
        FROM videos
        WHERE clip_processed = 0
        ORDER BY id
        """
    ).fetchall()
    pending_paths = {ROOT_DIR / str(row["folder_path"]) for row in pending_folder_rows}
    return [folder for folder in folders if folder in pending_paths]


def count_unprocessed_clip_folders(
    connection: sqlite3.Connection,
    allowed_folders: list[Path] | None = None,
) -> int:
    if allowed_folders:
        relative_folders = [relative_posix(folder) for folder in allowed_folders]
        placeholders = ",".join("?" for _ in relative_folders)
        row = connection.execute(
            f"""
            SELECT COUNT(*)
            FROM videos
            WHERE clip_processed = 0
              AND folder_path IN ({placeholders})
            """,
            relative_folders,
        ).fetchone()
        return int(row[0] if row else 0)

    row = connection.execute(
        """
        SELECT COUNT(*)
        FROM videos
        WHERE clip_processed = 0
        """
    ).fetchone()
    return int(row[0] if row else 0)


def fetch_unprocessed_clip_folder_batch(
    connection: sqlite3.Connection,
    *,
    allowed_folders: list[Path] | None = None,
    limit: int = 20,
) -> list[Path]:
    if allowed_folders:
        relative_folders = [relative_posix(folder) for folder in allowed_folders]
        placeholders = ",".join("?" for _ in relative_folders)
        rows = connection.execute(
            f"""
            SELECT folder_path
            FROM videos
            WHERE clip_processed = 0
              AND folder_path IN ({placeholders})
            ORDER BY id
            LIMIT ?
            """,
            [*relative_folders, limit],
        ).fetchall()
    else:
        rows = connection.execute(
            """
            SELECT folder_path
            FROM videos
            WHERE clip_processed = 0
            ORDER BY id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    return [(ROOT_DIR / str(row["folder_path"])).resolve() for row in rows]


def file_mtime_ns(path: Path | None) -> int:
    if path is None:
        return 0

    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def delete_video_subtitles(connection: sqlite3.Connection, video_id: int) -> None:
    existing_rows = connection.execute(
        "SELECT id, text FROM subtitles WHERE video_id = ? ORDER BY id",
        (video_id,),
    ).fetchall()

    for subtitle_id, text in existing_rows:
        connection.execute(
            "INSERT INTO subtitle_fts(subtitle_fts, rowid, text) VALUES('delete', ?, ?)",
            (subtitle_id, text),
        )

    connection.execute("DELETE FROM subtitles WHERE video_id = ?", (video_id,))


def sync_video_folder(
    connection: sqlite3.Connection,
    folder: Path,
    *,
    force_clip_refresh: bool = False,
    only_unprocessed_clips: bool = False,
) -> tuple[bool, int]:
    entries = list(folder.iterdir())
    srt_entry = next((entry for entry in entries if entry.is_file() and entry.suffix.lower() == ".srt"), None)
    video_entry = pick_video(entries)

    video_title = folder.name
    source_path = folder / "source.json"
    source_url = None

    if source_path.exists():
        try:
            source_json = json.loads(source_path.read_text("utf-8"))
            source_url = source_json.get("videoUrl") or source_json.get("sourceUrl") or source_json.get("url")
        except json.JSONDecodeError:
            pass

    source_mtime_ns = file_mtime_ns(source_path if source_path.exists() else None)
    video_mtime_ns = file_mtime_ns(video_entry)
    srt_mtime_ns = file_mtime_ns(srt_entry)

    existing_row = connection.execute(
        """
        SELECT id, title, source_mtime_ns, video_mtime_ns, srt_mtime_ns, clip_processed, video_sha256
        FROM videos
        WHERE folder_path = ?
        """,
        (relative_posix(folder),),
    ).fetchone()

    if (
        force_clip_refresh
        and only_unprocessed_clips
        and existing_row
        and int(existing_row["clip_processed"] or 0) == 1
    ):
        existing_cue_count = connection.execute(
            "SELECT COUNT(*) FROM subtitles WHERE video_id = ?",
            (existing_row["id"],),
        ).fetchone()[0]
        return False, int(existing_cue_count)

    if (
        not force_clip_refresh
        and
        existing_row
        and existing_row["title"] == video_title
        and int(existing_row["source_mtime_ns"] or 0) == source_mtime_ns
        and int(existing_row["video_mtime_ns"] or 0) == video_mtime_ns
        and int(existing_row["srt_mtime_ns"] or 0) == srt_mtime_ns
        and int(existing_row["clip_processed"] or 0) == 1
        and (not video_entry or bool(existing_row["video_sha256"]))
    ):
        existing_cue_count = connection.execute(
            "SELECT COUNT(*) FROM subtitles WHERE video_id = ?",
            (existing_row["id"],),
        ).fetchone()[0]
        return False, int(existing_cue_count)

    cues = []
    if srt_entry:
        content = srt_entry.read_text("utf-8")
        cues = parse_srt(content)
    clip_ranges = build_clip_ranges(cues, video_entry)
    video_sha256 = calculate_file_sha256(video_entry)

    clip_processed = 1 if video_entry and srt_entry else 0

    if existing_row:
        video_id = int(existing_row["id"])
        connection.execute(
            """
            UPDATE videos
            SET title = ?,
                folder_path = ?,
                video_path = ?,
                video_sha256 = ?,
                srt_path = ?,
                has_video = ?,
                has_subtitle = ?,
                source_url = ?,
                subtitle_cue_count = ?,
                source_mtime_ns = ?,
                video_mtime_ns = ?,
                srt_mtime_ns = ?,
                clip_processed = ?
            WHERE id = ?
            """,
            (
                video_title,
                relative_posix(folder),
                relative_posix(video_entry) if video_entry else None,
                video_sha256,
                relative_posix(srt_entry) if srt_entry else None,
                1 if video_entry else 0,
                1 if srt_entry else 0,
                source_url,
                len(cues),
                source_mtime_ns,
                video_mtime_ns,
                srt_mtime_ns,
                clip_processed,
                video_id,
            ),
        )
    else:
        cursor = connection.execute(
            """
            INSERT INTO videos (
              title,
              folder_path,
              video_path,
              video_sha256,
              srt_path,
              has_video,
              has_subtitle,
              source_url,
              subtitle_cue_count,
              source_mtime_ns,
              video_mtime_ns,
              srt_mtime_ns,
              clip_processed
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                video_title,
                relative_posix(folder),
                relative_posix(video_entry) if video_entry else None,
                video_sha256,
                relative_posix(srt_entry) if srt_entry else None,
                1 if video_entry else 0,
                1 if srt_entry else 0,
                source_url,
                len(cues),
                source_mtime_ns,
                video_mtime_ns,
                srt_mtime_ns,
                clip_processed,
            ),
        )
        video_id = int(cursor.lastrowid)

    delete_video_subtitles(connection, video_id)

    if not video_entry or not srt_entry:
        return True, 0

    for cue_index, cue in enumerate(cues):
        clip_start, clip_end, clip_mode = clip_ranges[cue_index]
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
                cue["start_seconds"],
                cue["end_seconds"],
                clip_start,
                clip_end,
                clip_mode,
                cue["text"],
            ),
        )
        subtitle_id = cursor.lastrowid
        connection.execute(
            "INSERT INTO subtitle_fts (rowid, text) VALUES (?, ?)",
            (subtitle_id, cue["text"]),
        )

    return True, len(cues)


def update_metadata(connection: sqlite3.Connection) -> tuple[int, int, int]:
    video_count = int(connection.execute("SELECT COUNT(*) FROM videos").fetchone()[0])
    subtitle_count = int(connection.execute("SELECT COUNT(*) FROM subtitles").fetchone()[0])
    embedding_count = int(connection.execute("SELECT COUNT(*) FROM subtitle_embeddings").fetchone()[0])

    metadata = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "video_count": str(video_count),
        "subtitle_count": str(subtitle_count),
        "embedding_count": str(embedding_count),
    }

    connection.executemany(
        """
        INSERT INTO metadata (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        list(metadata.items()),
    )
    return video_count, subtitle_count, embedding_count


def main() -> None:
    args = parse_args()
    folders = resolve_target_folders(args.folder)

    if args.full_rebuild:
        for target in [OUTPUT_DB, OUTPUT_DB.with_name(f"{OUTPUT_DB.name}-wal"), OUTPUT_DB.with_name(f"{OUTPUT_DB.name}-shm")]:
            if target.exists():
                target.unlink()

    connection = sqlite3.connect(OUTPUT_DB)
    connection.row_factory = sqlite3.Row

    init_db(connection, drop_existing=args.full_rebuild)

    if args.video_id:
        folders = resolve_video_id_folders(connection, args.video_id)

    changed_folders = 0
    skipped_folders = 0
    changed_cues = 0
    processed_folders = 0
    pending_changed_folders = 0
    commit_every = args.commit_every or (1 if args.rebuild_clips else 0)

    if args.rebuild_clips and args.only_unprocessed_clips:
        allowed_folders = folders if args.folder or args.video_id else None
        total_folders = count_unprocessed_clip_folders(connection, allowed_folders)
        if args.max_folders > 0:
            total_folders = min(total_folders, args.max_folders)

        batch_limit = 20
        while processed_folders < total_folders:
            remaining = total_folders - processed_folders
            folders_batch = fetch_unprocessed_clip_folder_batch(
                connection,
                allowed_folders=allowed_folders,
                limit=min(batch_limit, remaining),
            )
            if not folders_batch:
                break

            for folder in folders_batch:
                changed, cue_count = sync_video_folder(
                    connection,
                    folder,
                    force_clip_refresh=args.rebuild_clips,
                    only_unprocessed_clips=args.only_unprocessed_clips,
                )
                processed_folders += 1
                if changed:
                    changed_folders += 1
                    changed_cues += cue_count
                    pending_changed_folders += 1
                else:
                    skipped_folders += 1

                if commit_every > 0 and pending_changed_folders >= commit_every:
                    connection.commit()
                    print(
                        f"Committed progress: {processed_folders}/{total_folders} folders processed, "
                        f"{changed_folders} changed, {skipped_folders} skipped"
                    )
                    pending_changed_folders = 0
    else:
        if args.max_folders > 0:
            folders = folders[: args.max_folders]

        total_folders = len(folders)

        for folder in folders:
            changed, cue_count = sync_video_folder(
                connection,
                folder,
                force_clip_refresh=args.rebuild_clips,
                only_unprocessed_clips=args.only_unprocessed_clips,
            )
            processed_folders += 1
            if changed:
                changed_folders += 1
                changed_cues += cue_count
                pending_changed_folders += 1
            else:
                skipped_folders += 1

            if commit_every > 0 and pending_changed_folders >= commit_every:
                connection.commit()
                print(
                    f"Committed progress: {processed_folders}/{total_folders} folders processed, "
                    f"{changed_folders} changed, {skipped_folders} skipped"
                )
                pending_changed_folders = 0

    video_count, subtitle_count, _embedding_count = update_metadata(connection)
    connection.commit()
    connection.close()

    if args.full_rebuild:
        mode = "full rebuild"
    elif args.rebuild_clips:
        mode = "clip refresh"
    else:
        mode = "incremental sync"
    print(f"SQLite update complete ({mode}): {OUTPUT_DB.name}")
    print(f"Changed folders: {changed_folders}")
    print(f"Skipped folders: {skipped_folders}")
    print(f"Changed subtitles: {changed_cues}")
    print(f"Videos: {video_count}")
    print(f"Subtitles: {subtitle_count}")


if __name__ == "__main__":
    main()
