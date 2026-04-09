# Database

这个文档专门说明本项目的数据库、增量同步、clip 和 embedding。

主库文件：

- [subtitles.db](/Users/zhanzz/Subtitle-Search/subtitles.db)

相关脚本：

- [build-sqlite.py](/Users/zhanzz/Subtitle-Search/scripts/build-sqlite.py)
- [embed-sqlite.py](/Users/zhanzz/Subtitle-Search/scripts/embed-sqlite.py)

## 表结构

当前数据库包含这些表：

- `videos`
- `subtitles`
- `subtitle_fts`
- `subtitle_embeddings`
- `metadata`

说明：

- `subtitle_fts` 使用 SQLite FTS5 做全文检索
- `subtitle_embeddings` 用来存每条字幕的 embedding
- embedding 当前以 `JSON + BLOB + norm` 的形式存储
- `videos` 会递归收录 `data/` 下所有视频目录，不再只收“已经有字幕”的目录
- `subtitles` 会预先保存每条字幕对应的 `clip_start_seconds / clip_end_seconds / clip_mode`
- `clip_mode` 当前优先按画面切换分段，超过 2 分钟时退回到字幕前后各 1 分钟
- 只有同时存在视频和 `.srt` 时，才会真正生成智能切分结果
- `videos.clip_processed = 1` 表示该视频的字幕切分已经计算完成，`0` 表示还没处理完或当前缺少视频/字幕

## 增量同步

常用命令：

```bash
python3 ./scripts/build-sqlite.py --full-rebuild
python3 ./scripts/build-sqlite.py --folder "data/幽默/某个视频目录"
python3 ./scripts/build-sqlite.py --video-id 41
python3 ./scripts/build-sqlite.py --rebuild-clips --video-id 41
python3 ./scripts/build-sqlite.py --rebuild-clips --only-unprocessed-clips
python3 ./scripts/build-sqlite.py --rebuild-clips --only-unprocessed-clips --max-folders 10
```

参数补充：

- `--full-rebuild`：删库后全量重建
- `--folder`：只处理指定目录，可重复传入
- `--video-id`：只处理指定 `videos.id`，可重复传入
- `--rebuild-clips`：强制重算字幕 clip 区间
- `--only-unprocessed-clips`：配合 `--rebuild-clips` 使用，只处理 `clip_processed=0` 的视频
- `--max-folders`：限制本次最多处理多少个视频目录，适合分批补跑
- `--commit-every`：每处理多少个目录提交一次，长任务建议设成 `1`

## 智能切分回填

如果只是给旧数据补 clip，不需要全量重建整库，可以直接重算字幕切分：

```bash
python3 ./scripts/build-sqlite.py --rebuild-clips --video-id 41 --commit-every 1
```

只补还没处理过的记录：

```bash
python3 ./scripts/build-sqlite.py --rebuild-clips --only-unprocessed-clips --commit-every 1
```

后台跑整批补齐：

```bash
nohup python3 -u ./scripts/build-sqlite.py --rebuild-clips --only-unprocessed-clips --commit-every 1 > /tmp/subtitle-search-clip-backfill.log 2>&1 &
```

说明：

- 智能切分依赖 `ffprobe` 和 `ffmpeg`
- 这一步会按画面切换点重算每条字幕的 clip，耗时明显高于普通建库
- 单条视频处理完后会立即提交，并把 `videos.clip_processed` 更新为 `1`

## Embeddings

OpenAI 模式：

```bash
OPENAI_API_KEY=你的key npm run embed:sqlite
```

本地离线模式：

```bash
python3 ./scripts/embed-sqlite.py --provider local --model BAAI/bge-small-zh-v1.5
python3 ./scripts/embed-sqlite.py --provider local --model BAAI/bge-small-zh-v1.5 --video-id 2
```

其他示例：

```bash
python3 ./scripts/embed-sqlite.py --limit 100
python3 ./scripts/embed-sqlite.py --model text-embedding-3-small
python3 ./scripts/embed-sqlite.py --force
```

注意：

- OpenAI 模式需要可用的 API key
- `--provider local` 可以切到本地离线模型，不走 OpenAI API
- 本地离线方案当前优先推荐 `BAAI/bge-small-zh-v1.5`
- 本地模式首次运行会下载 Hugging Face 模型，需要先安装 `torch` 和 `transformers`
- 本地模式建议使用 `Python 3.11`
- 当前验证通过的一组版本是：`torch 2.2.2`、`transformers 4.41.2`、`numpy 1.26.4`
- 这些版本已经整理在 [requirements-local.txt](/Users/zhanzz/Subtitle-Search/requirements-local.txt)
- 如果 `transformers` 版本过高，可能会出现和 `torch` 不兼容的问题
- 如果 `numpy` 是 `2.x`，当前这组 `torch` 可能会报兼容性警告或初始化失败
- 首次下载 `BAAI/bge-small-zh-v1.5` 时会拉取大约 `96MB` 的模型文件
- `scripts/serve.py` 的语义搜索接口默认会优先使用 `.venv/bin/python`

本地离线 embedding 推荐安装方式：

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements-local.txt
```

## 常用 SQL

查看元信息：

```sql
SELECT * FROM metadata;
```

查看最近视频：

```sql
SELECT id, title, source_url, video_path
FROM videos
ORDER BY id DESC
LIMIT 20;
```

查哪些视频还没有字幕：

```sql
SELECT id, title, folder_path, video_path
FROM videos
WHERE has_video = 1 AND has_subtitle = 0
ORDER BY title;
```

查哪些视频还没做完 clip：

```sql
SELECT id, title, has_video, has_subtitle, clip_processed
FROM videos
WHERE clip_processed = 0
ORDER BY id;
```

查看字幕对应的预计算截取范围：

```sql
SELECT
  s.id,
  s.video_id,
  s.cue_index,
  s.text,
  s.start_seconds,
  s.end_seconds,
  s.clip_start_seconds,
  s.clip_end_seconds,
  s.clip_mode
FROM subtitles s
ORDER BY s.id DESC
LIMIT 20;
```
