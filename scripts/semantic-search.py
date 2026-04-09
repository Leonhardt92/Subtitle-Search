#!/usr/bin/env python3

from __future__ import annotations

import argparse
import importlib
import json
import sqlite3
import struct
import sys
from pathlib import Path

from sqlite_vec_utils import load_sqlite_vec_extension, quote_identifier, vec_table_exists, vec_table_name


ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT_DIR / "subtitles.db"
DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"
DEFAULT_MIN_SCORE = 0.6
CATEGORY_PREFIXES = {
    "humor": "data/幽默/%",
    "writing": "data/文笔/%",
}


def normalize_query(value: str) -> str:
    return " ".join(str(value or "").strip().split()).lower()


def text_contains_query(text: str, query: str) -> bool:
    normalized_query = normalize_query(query)
    normalized_text = normalize_query(text)
    return bool(normalized_query) and normalized_query in normalized_text


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local semantic subtitle search.")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="Path to subtitles.db")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Embedding model name")
    parser.add_argument("--query", help="Semantic search query")
    parser.add_argument("--category", choices=sorted(CATEGORY_PREFIXES), help="Folder category")
    parser.add_argument("--limit", type=int, default=60, help="Maximum number of subtitle matches to return")
    parser.add_argument("--min-score", type=float, default=DEFAULT_MIN_SCORE, help="Minimum similarity score to keep")
    parser.add_argument("--serve-stdio", action="store_true", help="Keep model loaded and handle JSON requests over stdin/stdout")
    return parser.parse_args()


def load_local_encoder(model_name: str):
    transformers = importlib.import_module("transformers")
    torch = importlib.import_module("torch")

    tokenizer = transformers.AutoTokenizer.from_pretrained(model_name)
    model = transformers.AutoModel.from_pretrained(model_name)
    device = "mps" if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available() else "cpu"
    if device == "cpu" and torch.cuda.is_available():
        device = "cuda"

    model.to(device)
    model.eval()
    return tokenizer, model, torch, device


def mean_pool(last_hidden_state, attention_mask, torch_module):
    mask = attention_mask.unsqueeze(-1).expand(last_hidden_state.size()).float()
    summed = (last_hidden_state * mask).sum(dim=1)
    counts = mask.sum(dim=1).clamp(min=1e-9)
    return summed / counts


def encode_query(tokenizer, model, torch_module, device: str, text: str) -> list[float]:
    with torch_module.no_grad():
        encoded = tokenizer(
            [text],
            padding=True,
            truncation=True,
            max_length=512,
            return_tensors="pt",
        )
        encoded = {key: value.to(device) for key, value in encoded.items()}
        output = model(**encoded)
        pooled = mean_pool(output.last_hidden_state, encoded["attention_mask"], torch_module)
        normalized = torch_module.nn.functional.normalize(pooled, p=2, dim=1).cpu()
    return normalized[0].tolist()


def distance_to_similarity(distance: float) -> float:
    return 1.0 - (float(distance) ** 2) / 2.0


def run_vec_search(
    *,
    connection: sqlite3.Connection,
    model_name: str,
    query_blob: bytes,
    query_text: str,
    category: str,
    limit: int,
    min_score: float,
) -> list[dict]:
    table_name = vec_table_name(model_name)
    if not vec_table_exists(connection, table_name):
        return []

    quoted_vec_table = quote_identifier(table_name)
    candidate_limit = max(limit * 20, 1000)
    rows = connection.execute(
        f"""
        SELECT
          s.id,
          s.video_id,
          s.cue_index,
          s.start_seconds,
          s.end_seconds,
          s.text,
          videos.title AS video_title,
          videos.video_path,
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
          ) AS next_text,
          vec_idx.distance AS distance
        FROM {quoted_vec_table} AS vec_idx
        JOIN subtitles s ON s.id = vec_idx.rowid
        JOIN videos ON videos.id = s.video_id
        WHERE vec_idx.embedding MATCH ?
          AND k = ?
          AND videos.has_video = 1
          AND videos.has_subtitle = 1
          AND videos.folder_path LIKE ?
          AND s.text NOT LIKE ?
        """,
        (query_blob, candidate_limit, CATEGORY_PREFIXES[category], f"%{query_text.strip()}%"),
    ).fetchall()

    scored_records = []
    for row in rows:
        similarity = distance_to_similarity(float(row["distance"] or 0.0))
        if similarity <= min_score or similarity >= 1.0:
            continue
        scored_records.append(
            {
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
                "score": similarity,
            }
        )

    scored_records.sort(key=lambda item: item["score"], reverse=True)
    return scored_records[:limit]


def run_search(
    *,
    db_path: str,
    model_name: str,
    tokenizer,
    model,
    torch_module,
    device: str,
    query: str,
    category: str,
    limit: int,
    min_score: float,
) -> dict:
    connection = sqlite3.connect(Path(db_path))
    connection.row_factory = sqlite3.Row
    try:
        query_vector = encode_query(tokenizer, model, torch_module, device, query.strip())
        try:
            vec_loaded = load_sqlite_vec_extension(connection)
        except Exception:
            vec_loaded = False

        if vec_loaded and vec_table_exists(connection, vec_table_name(model_name)):
            vec_scored_records = run_vec_search(
                connection=connection,
                model_name=model_name,
                query_blob=struct.pack(f"<{len(query_vector)}f", *query_vector),
                query_text=query,
                category=category,
                limit=limit,
                min_score=min_score,
            )
            return {
                "ok": True,
                "query": query,
                "category": category,
                "model": model_name,
                "minScore": min_score,
                "records": vec_scored_records,
                "engine": "sqlite-vec",
            }
    finally:
        connection.close()
    return {
        "ok": False,
        "query": query,
        "category": category,
        "model": model_name,
        "minScore": min_score,
        "records": [],
        "error": "sqlite-vec index not available for this model",
        "engine": "sqlite-vec-missing",
    }


def serve_stdio(args: argparse.Namespace) -> None:
    tokenizer, model, torch_module, device = load_local_encoder(args.model)
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
            payload = run_search(
                db_path=str(request.get("db") or args.db),
                model_name=str(request.get("model") or args.model),
                tokenizer=tokenizer,
                model=model,
                torch_module=torch_module,
                device=device,
                query=str(request["query"]),
                category=str(request["category"]),
                limit=int(request.get("limit", args.limit)),
                min_score=float(request.get("minScore", args.min_score)),
            )
        except Exception as error:
            payload = {"ok": False, "error": f"semantic search failed: {error}"}

        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def main() -> None:
    args = parse_args()
    if args.serve_stdio:
        serve_stdio(args)
        return

    if not args.query or not args.category:
        raise SystemExit("--query and --category are required unless --serve-stdio is used.")

    tokenizer, model, torch_module, device = load_local_encoder(args.model)
    payload = run_search(
        db_path=args.db,
        model_name=args.model,
        tokenizer=tokenizer,
        model=model,
        torch_module=torch_module,
        device=device,
        query=args.query,
        category=args.category,
        limit=args.limit,
        min_score=args.min_score,
    )
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
