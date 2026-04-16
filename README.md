# Subtitle Search

本项目用于本地管理视频、字幕和搜索结果，当前包含：

- 前端字幕搜索页面
- 支持视频按时间跳转的本地静态服务
- `subtitles.db` SQLite 数据库
- `sqlite-vec` 向量索引
- `thumbnails/` 字幕缩略图缓存

## 目录结构

```text
.
├── assets/
│   ├── css/
│   └── js/
├── data/
├── clips/
├── thumbnails/
├── clip_lab_exports/
├── index.html
├── package.json
├── scripts/
└── subtitles.db
```

分目录说明：

- [assets/README.md](/Users/zhanzz/Subtitle-Search/assets/README.md)
- [assets/css/README.md](/Users/zhanzz/Subtitle-Search/assets/css/README.md)
- [assets/js/README.md](/Users/zhanzz/Subtitle-Search/assets/js/README.md)
- [scripts/README.md](/Users/zhanzz/Subtitle-Search/scripts/README.md)
- [data/README.md](/Users/zhanzz/Subtitle-Search/data/README.md)
- [docs/database.md](/Users/zhanzz/Subtitle-Search/docs/database.md)
- [docs/admin.md](/Users/zhanzz/Subtitle-Search/docs/admin.md)

## 启动网页

```bash
npm run serve
```

默认地址：

`http://127.0.0.1:4173`

这个服务使用 [scripts/serve.py](/Users/zhanzz/Subtitle-Search/scripts/serve.py)，支持 `Range` 请求，适合浏览器视频拖动和按字幕时间跳转。

页面入口：

- 首页：`/`
- 数据录入页：`/admin.html`

## 获取字幕文件

这一块已经单独整理到：

- [data/README.md](/Users/zhanzz/Subtitle-Search/data/README.md)

你当前实际在用的流程就是：

1. 把 `mp4 / srt / source.json` 放进 `data/分类/视频目录/`
2. 如果没有中文字幕，用 `VideoCaptioner` 转写
3. 再用 [build-sqlite.py](/Users/zhanzz/Subtitle-Search/scripts/build-sqlite.py) 增量同步入库

## 生成 SQLite 数据库

```bash
npm run build:sqlite
```

会重新生成：

- [subtitles.db](/Users/zhanzz/Subtitle-Search/subtitles.db)

数据库、clip、embedding、常用 SQL 已经单独整理到：

- [docs/database.md](/Users/zhanzz/Subtitle-Search/docs/database.md)

## 数据录入

数据录入页说明已整理到：

- [docs/admin.md](/Users/zhanzz/Subtitle-Search/docs/admin.md)

数据库、常用 SQL、clip 字段说明、embedding 说明都已经整理到：

- [docs/database.md](/Users/zhanzz/Subtitle-Search/docs/database.md)

全文搜索：

```sql
SELECT s.id, v.title, s.start_seconds, s.text
FROM subtitle_fts f
JOIN subtitles s ON s.id = f.rowid
JOIN videos v ON v.id = s.video_id
WHERE subtitle_fts MATCH '瑞幸'
LIMIT 20;
```

看 embedding 数量：

```sql
SELECT COUNT(*) FROM subtitle_vec_baai_bge_small_zh_v1_5_ad29b19a;
```

## package.json 命令

当前可用脚本：

```bash
npm run build:sqlite
npm run embed:sqlite
npm run embed:sqlite:local
npm run serve
```

如果你用本地离线 embedding，推荐直接配合 `.venv`：

```bash
.venv/bin/python ./scripts/embed-sqlite.py --provider local --model BAAI/bge-small-zh-v1.5
```

## 现状说明

- 前端搜索现在直接走 Python + SQLite
- SQLite 现在已经接入 `sqlite-vec`，语义搜索优先走向量索引
- `subtitles.db-wal` 和 `subtitles.db-shm` 是 SQLite 的正常伴生文件
- 智能切分已经支持按 `video_id` 单条补跑，也支持后台批量回填未处理记录
- 数据录入页已经支持新增视频自动分配 `video_id`
- 保存字幕现在会同步回写 `.srt`，数据库和源字幕文件会保持一致
- 新增视频的重复提醒当前基于 `folder_path + video_sha256`
- `subtitle_fts_*` 和 `subtitle_vec_*` 这类表是 FTS / sqlite-vec 的内部辅助表，不要手动删除
- embedding 现在直接写入 `sqlite-vec`，不再依赖旧的 `subtitle_embeddings`
