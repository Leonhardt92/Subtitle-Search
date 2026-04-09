# JavaScript

这个目录存放前端页面脚本。

当前文件：

- [app.js](/Users/zhanzz/Subtitle-Search/assets/js/app.js)
  首页搜索、播放、跳转 Clip / Lab
- [admin.js](/Users/zhanzz/Subtitle-Search/assets/js/admin.js)
  数据录入页逻辑
- [clip-lab.js](/Users/zhanzz/Subtitle-Search/assets/js/clip-lab.js)
  Clip Lab 裁剪、黑边检测、GIF 导出

对应关系：

- [index.html](/Users/zhanzz/Subtitle-Search/index.html) -> [app.js](/Users/zhanzz/Subtitle-Search/assets/js/app.js)
- [admin.html](/Users/zhanzz/Subtitle-Search/admin.html) -> [admin.js](/Users/zhanzz/Subtitle-Search/assets/js/admin.js)
- [clip-lab.html](/Users/zhanzz/Subtitle-Search/clip-lab.html) -> [clip-lab.js](/Users/zhanzz/Subtitle-Search/assets/js/clip-lab.js)

开发建议：

- 页面专属逻辑放对应文件，不混写
- 如果以后出现跨页面共用逻辑，再单独拆模块
