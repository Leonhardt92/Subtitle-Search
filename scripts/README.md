# Scripts

这个目录存放后端服务和离线脚本。

主要文件：

- [serve.py](/Users/zhanzz/Subtitle-Search/scripts/serve.py)
  本地网页服务、API、Clip 导出、GIF 导出
- [build-sqlite.py](/Users/zhanzz/Subtitle-Search/scripts/build-sqlite.py)
  扫描 `data/`，增量同步 `videos / subtitles / subtitle_fts`，并计算 clip
- [embed-sqlite.py](/Users/zhanzz/Subtitle-Search/scripts/embed-sqlite.py)
  给字幕生成 embedding
- [semantic-search.py](/Users/zhanzz/Subtitle-Search/scripts/semantic-search.py)
  本地语义搜索模型进程

常用命令：

```bash
python3 ./scripts/serve.py
python3 ./scripts/build-sqlite.py --video-id 557 --commit-every 1
python3 ./scripts/build-sqlite.py --folder "data/幽默/某个视频目录" --commit-every 1
.venv/bin/python ./scripts/embed-sqlite.py --provider local --model BAAI/bge-small-zh-v1.5 --video-id 557
```

补充：

- `build-sqlite.py` 只在“同时有视频和 `.srt`”时才会真正生成 clip
- `embed-sqlite.py` 的本地模型模式建议使用仓库根目录 `.venv`
