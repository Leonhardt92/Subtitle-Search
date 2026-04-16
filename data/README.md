# Data

这个目录是真实源数据目录，数据库只是从这里同步出来的索引。

结构：

```text
data/
├── 幽默/
│   └── 视频标题/
│       ├── 视频标题.mp4
│       ├── 视频标题.srt
│       └── source.json
└── 文笔/
    └── 视频标题/
        ├── 视频标题.mp4
        ├── 视频标题.srt
        └── source.json
```

约定：

- 一个视频目录只放一个主视频文件
- 如果有字幕，统一命名成和视频同名的 `.srt`
- `source.json` 用来保存来源地址，例如 B 站 URL

新增视频推荐流程：

1. 在 `data/幽默/` 或 `data/文笔/` 下创建视频目录
2. 放入 `mp4`
3. 如果已有字幕，放入同名 `.srt`
4. 写入 `source.json`
5. 运行：

```bash
python3 ./scripts/build-sqlite.py --folder "data/分类/视频目录" --commit-every 1
```

获取字幕的两种常见方式：

- 直接复制现成 `.srt`
- 用 `VideoCaptioner` 转写后再放回该目录
