#!/usr/bin/env python3

from __future__ import annotations

import argparse
import importlib
import json
import os
import sqlite3
import struct
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from sqlite_vec_utils import ensure_vec_table, load_sqlite_vec_extension, quote_identifier, vec_table_exists, vec_table_name


ROOT_DIR = Path.cwd()
DEFAULT_DB = ROOT_DIR / "subtitles.db"
DEFAULT_OPENAI_MODEL = "text-embedding-3-small"
DEFAULT_LOCAL_MODEL = "BAAI/bge-small-zh-v1.5"
OPENAI_EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build subtitle embeddings into SQLite.")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="Path to subtitles.db")
    parser.add_argument(
        "--provider",
        choices=["openai", "local"],
        default="local",
        help="Embedding provider. Use 'local' for offline Hugging Face models.",
    )
    parser.add_argument("--model", default=None, help="Embedding model name")
    parser.add_argument("--batch-size", type=int, default=50, help="Batch size for embedding requests")
    parser.add_argument("--limit", type=int, default=None, help="Only embed the first N pending subtitles")
    parser.add_argument("--force", action="store_true", help="Recompute embeddings even if they already exist")
    parser.add_argument("--video-id", type=int, default=None, help="Only embed subtitles belonging to this video id")
    return parser.parse_args()


def fetch_embeddings(api_key: str, model: str, inputs: list[str]) -> list[list[float]]:
    payload = json.dumps({"model": model, "input": inputs}).encode("utf-8")
    request = urllib.request.Request(
        OPENAI_EMBEDDINGS_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI embeddings request failed: {error.code} {detail}") from error

    data = json.loads(body)
    return [item["embedding"] for item in data["data"]]


def pack_embedding(values: list[float]) -> bytes:
    return struct.pack(f"<{len(values)}f", *values)


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


def normalize_rows(vectors, torch_module):
    return torch_module.nn.functional.normalize(vectors, p=2, dim=1)


def fetch_local_embeddings(tokenizer, model, torch_module, device: str, inputs: list[str]) -> list[list[float]]:
    with torch_module.no_grad():
        encoded = tokenizer(
            inputs,
            padding=True,
            truncation=True,
            max_length=512,
            return_tensors="pt",
        )
        encoded = {key: value.to(device) for key, value in encoded.items()}
        output = model(**encoded)
        pooled = mean_pool(output.last_hidden_state, encoded["attention_mask"], torch_module)
        normalized = normalize_rows(pooled, torch_module).cpu()
    return normalized.tolist()


def main() -> None:
    args = parse_args()
    model_name = args.model or (DEFAULT_LOCAL_MODEL if args.provider == "local" else DEFAULT_OPENAI_MODEL)
    api_key = None
    local_encoder = None

    if args.provider == "openai":
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise SystemExit("OPENAI_API_KEY is required when --provider openai is used.")
    else:
        try:
            local_encoder = load_local_encoder(model_name)
        except ModuleNotFoundError as error:
            raise SystemExit(
                "Local embedding dependencies are missing. "
                "Create a venv and install: pip install torch transformers"
            ) from error

    connection = sqlite3.connect(Path(args.db))
    connection.row_factory = sqlite3.Row
    try:
        load_sqlite_vec_extension(connection)
    except Exception as error:
        raise SystemExit(f"sqlite-vec is required for embedding writes: {error}") from error

    vec_table = vec_table_name(model_name)
    if vec_table_exists(connection, vec_table):
        quoted_vec_table = quote_identifier(vec_table)
        query = f"""
          SELECT s.id, s.text
          FROM subtitles s
          LEFT JOIN {quoted_vec_table} vec
            ON vec.rowid = s.id
          WHERE (? IS NULL OR s.video_id = ?)
            AND (? = 1 OR vec.rowid IS NULL)
          ORDER BY s.id
        """
        rows = connection.execute(
            query,
            (
                args.video_id,
                args.video_id,
                1 if args.force else 0,
            ),
        ).fetchall()
    else:
        rows = connection.execute(
            """
            SELECT s.id, s.text
            FROM subtitles s
            WHERE (? IS NULL OR s.video_id = ?)
            ORDER BY s.id
            """,
            (args.video_id, args.video_id),
        ).fetchall()
    pending = [(row["id"], row["text"]) for row in rows]

    if args.limit is not None:
        pending = pending[: args.limit]

    if not pending:
        print("No subtitles need embeddings.")
        return

    updated = 0
    for start in range(0, len(pending), args.batch_size):
        batch = pending[start : start + args.batch_size]
        texts = [item[1] for item in batch]
        if args.provider == "openai":
            embeddings = fetch_embeddings(api_key, model_name, texts)
        else:
            tokenizer, model, torch_module, device = local_encoder
            embeddings = fetch_local_embeddings(tokenizer, model, torch_module, device, texts)
        now = datetime.now(timezone.utc).isoformat()

        for (subtitle_id, _text), vector in zip(batch, embeddings):
            actual_vec_table = ensure_vec_table(
                connection,
                model_name=model_name,
                dimensions=len(vector),
            )
            quoted_vec_table = quote_identifier(actual_vec_table)
            connection.execute(f"DELETE FROM {quoted_vec_table} WHERE rowid = ?", (subtitle_id,))
            connection.execute(
                f"INSERT INTO {quoted_vec_table}(rowid, embedding) VALUES (?, ?)",
                (subtitle_id, pack_embedding(vector)),
            )
            updated += 1

        connection.commit()
        print(f"Embedded {updated}/{len(pending)} subtitles")

    connection.execute(
        """
        INSERT INTO metadata (key, value)
        VALUES ('embedding_count', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (str(connection.execute(f"SELECT COUNT(*) FROM {quote_identifier(vec_table_name(model_name))}").fetchone()[0]),),
    )
    connection.commit()
    connection.close()

    print(f"Finished embedding {updated} subtitles with {model_name} via {args.provider}")


if __name__ == "__main__":
    main()
