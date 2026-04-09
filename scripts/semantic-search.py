#!/usr/bin/env python3

from __future__ import annotations

import argparse
import importlib
import json
import math
import sqlite3
import struct
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT_DIR / "subtitles.db"
DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"
DEFAULT_MIN_SCORE = 0.6
CATEGORY_PREFIXES = {
    "humor": "data/幽默/%",
    "writing": "data/文笔/%",
}


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


def unpack_embedding(blob: bytes) -> list[float]:
    if not blob:
        return []
    size = len(blob) // 4
    return list(struct.unpack(f"<{size}f", blob))


def cosine_similarity(query_vector: list[float], row_vector: list[float], row_norm: float) -> float:
    if not row_vector or row_norm <= 0:
        return 0.0
    dot = sum(left * right for left, right in zip(query_vector, row_vector))
    return dot / row_norm


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
              ) AS next_text,
              e.embedding_blob,
              e.embedding_norm
            FROM subtitle_embeddings e
            JOIN subtitles s ON s.id = e.subtitle_id
            JOIN videos v ON v.id = s.video_id
            WHERE e.model = ?
              AND v.has_video = 1
              AND v.has_subtitle = 1
              AND v.folder_path LIKE ?
            """,
            (model_name, CATEGORY_PREFIXES[category]),
        ).fetchall()
    finally:
        connection.close()

    scored_records = []
    for row in rows:
        similarity = cosine_similarity(
            query_vector,
            unpack_embedding(row["embedding_blob"]),
            float(row["embedding_norm"] or 0.0),
        )
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
    return {
        "ok": True,
        "query": query,
        "category": category,
        "model": model_name,
        "minScore": min_score,
        "records": scored_records[:limit],
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
