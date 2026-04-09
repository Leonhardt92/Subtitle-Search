# CSS

这个目录存放前端样式文件。

当前文件：

- [styles.css](/Users/zhanzz/Subtitle-Search/assets/css/styles.css)
  首页和数据录入页共用样式
- [clip-lab.css](/Users/zhanzz/Subtitle-Search/assets/css/clip-lab.css)
  `clip-lab.html` 独立样式

使用方式：

- 首页和录入页通过 `<link rel="stylesheet" href="./assets/css/styles.css" />` 引入
- Clip Lab 页面通过 `<link rel="stylesheet" href="./assets/css/clip-lab.css" />` 引入

约定：

- 共用样式优先放 `styles.css`
- 只有某个独立页面才会用到的样式，单独拆文件
