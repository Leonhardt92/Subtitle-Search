# Data Entry

这个文档说明 `admin.html` 数据录入页的使用方式。

页面地址：

`http://127.0.0.1:4173/admin.html`

相关文件：

- [admin.html](/Users/zhanzz/Subtitle-Search/admin.html)
- [assets/js/admin.js](/Users/zhanzz/Subtitle-Search/assets/js/admin.js)
- [scripts/serve.py](/Users/zhanzz/Subtitle-Search/scripts/serve.py)

## 当前支持

- 新增视频并同步
- 按 `subtitle_id` 读取字幕
- 保存字幕
- 增量同步本视频
- 生成本视频 clip
- 生成本视频 embedding

## 新增视频并同步

填写：

- 分类
- 标题
- 可选 `sourceUrl`

行为：

- 页面会在 `data/分类/标题/` 下创建目录和 `source.json`
- 然后只对这个目录增量调用 [build-sqlite.py](/Users/zhanzz/Subtitle-Search/scripts/build-sqlite.py)
- 同步完成后自动回填新生成的 `video_id`

## 新增判断与重复提醒

- 是否是新增视频，主判断依据是 `videos.folder_path`
- 目录没入过库，就当新增
- 目录已存在，就当已有视频增量同步
- 另外会对视频文件计算 `videos.video_sha256`
- 如果发现“目录是新的，但视频内容和库里已有视频相同”，录入页会返回疑似重复提醒

## 保存字幕

保存字幕时会：

- 更新 `subtitles`
- 更新 `subtitle_fts`
- 删除该字幕旧的 embedding，避免语义搜索继续使用旧文本
- 把该 `video_id` 下当前全部字幕重新写回对应 `.srt`

所以后续再跑 [build-sqlite.py](/Users/zhanzz/Subtitle-Search/scripts/build-sqlite.py) 时，不会把已修正的字幕又覆盖回旧内容。

## 单视频操作

页面里这几个按钮都只针对当前表单里的 `video_id`：

- `build-sqlite 同步本视频`
- `生成本视频 clip`
- `生成本视频 embedding`

也就是说：

- 不会扫全库
- 只处理当前视频
- 适合修完字幕后马上补同步、补 clip、补 embedding
