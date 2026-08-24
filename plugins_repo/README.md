# DualSub - MoviePilot V2 插件仓库

把影片**内嵌的中文 + 英文字幕轨**自动合并为**上下双行双语字幕**，生成 `.dual.srt` 或封回 MKV（不重编码）。

- ⏱️ 监听媒体整理完成（入库）事件，自动处理新入库影片
- 🗂️ 插件详情页内置「媒体浏览」交互页：逐级浏览媒体库，实时探测字幕轨，单文件/批量处理
- 📄 默认只生成外挂 `.dual.srt`，**不修改原视频**
- 📦 可选 ffmpeg 重封装（`-c copy` 不重编码），覆盖前自动备份

## 安装

在 MoviePilot → 设置 → 插件 → 插件仓库管理，添加本仓库地址，然后在插件市场找到「双语字幕合并」安装。

## 依赖

容器内需有 `ffmpeg` / `ffprobe`（MoviePilot V2 官方镜像自带），无需额外 pip 依赖。

## 详细文档

见 [`plugins/dualsub/README.md`](plugins/dualsub/README.md)。
