# 🎬 双语字幕合并 (DualSub) - MoviePilot V2 插件

把影片**内嵌的中文 + 英文字幕轨**自动合并为**上下双行双语字幕**。

由 [DualSub Web](https://gitee.com/wuzhennana/dualsub-web) 改造为 MoviePilot V2 插件。

## 功能

- 🎞️ 自动识别影片内嵌字幕轨语言（中文 chi/zho、英文 eng/en）
- 🔀 同一时间点上下双行混排（英文在上 / 中文在上 可选）
- 📄 生成 `<文件名>.dual.srt` 到影片同目录，**不修改原文件**
- 📦 可选：用 ffmpeg 把双语字幕封回 MKV（视频/音频流 copy，不重编码）
- ⏱️ 监听媒体**整理完成（入库）**事件，自动处理新入库影片
- 🛠️ 支持手动批量处理指定目录
- 📊 插件详情页显示处理历史（成功/失败/跳过/忽略）

## 工作原理

1. 监听 MoviePilot 的 `TransferComplete`（整理完成/入库）事件
2. 从 `transferinfo.file_list_new` 取出新增的视频文件
3. 用 `ffprobe` 探测字幕轨，找到中文轨和英文轨
4. 用 `ffmpeg` 分别提取中/英 SRT
5. 按时间轴重叠配对，合并为上下双行双语 SRT
6. （可选）把双语 SRT 封回 MKV

## 依赖

- 容器内已安装 `ffmpeg` / `ffprobe`（MoviePilot V2 官方镜像自带）
- 无需额外 pip 依赖（纯标准库实现）

## 安装

### 方式一：本地插件仓库（推荐）

1. 在 MoviePilot 配置文件 `/config/app.env` 中添加本地插件仓库路径：

   ```
   PLUGIN_LOCAL_REPO_PATHS=/config/plugins
   ```

2. 把本插件目录 `dualsub` 放到宿主机 `/vol1/1000/Docker/MoviePilot/config/plugins/` 下

   最终结构：
   ```
   /vol1/1000/Docker/MoviePilot/config/plugins/dualsub/
   ├── __init__.py
   ├── merge_dual.py
   └── README.md
   ```

3. 重启 MoviePilot 容器（或在设置里重载插件）

4. 进入 MoviePilot → 设置 → 插件 → 本地插件，找到「双语字幕合并」并安装

### 方式二：直接放入插件目录

1. 把 `dualsub` 目录拷贝到容器内 `/app/app/plugins/`：

   ```bash
   docker cp dualsub moviepilot-v2:/app/app/plugins/dualsub
   ```

2. 在 MoviePilot 数据库的「已安装插件列表」中加入 `DualSub`
   （或通过 Web 界面安装本地插件触发注册）

3. 重启容器使插件生效

## 配置说明

| 配置项 | 说明 |
|---|---|
| 启用插件 | 总开关 |
| 入库自动执行 | 监听整理完成事件，自动处理新入库影片 |
| 发送通知 | 处理完成后发送 MoviePilot 通知 |
| 手动执行一次 | 勾选并保存，即扫描下方路径 |
| 递归子目录 | 手动执行时是否扫描子目录 |
| 清理历史记录 | 勾选并保存，清空任务历史 |
| 媒体路径 | 手动执行时扫描的路径（容器内绝对路径，每行一个） |
| 处理模式 | `仅生成.srt` / `仅封回MKV` / `生成srt+封回MKV` |
| 双语顺序 | 英文在上 或 中文在上 |
| 最小文件大小(MB) | 小于此值的视频跳过，0=不限 |
| 覆盖原文件 | 封回模式：覆盖原文件 或 生成 .dual.mkv |
| 覆盖时备份 | 覆盖模式下，原文件备份为 .bak |
| 已有双语字幕则跳过 | 已存在 .dual.srt/.dual.mkv 时跳过 |

## 路径说明

**所有路径均为容器内绝对路径**。MoviePilot V2 容器挂载示例：

| 容器内路径 | 说明 |
|---|---|
| `/vol2/1000/Emby入库` | 媒体库目录（推荐填这个） |
| `/vol2/1000/Qbdownload4T` | 下载目录 |

可通过 `docker inspect moviepilot-v2` 查看 `Mounts` 确认实际挂载路径。

## 输出文件

- `<影片名>.dual.srt` — 双语字幕文件（默认生成，最安全，不修改原视频）
- `<影片名>.dual.mkv` — 封回双语字幕的 MKV（仅 mux/both 模式，非覆盖时生成）
- `<影片名>.bak` — 原视频备份（仅覆盖+备份模式）

## 常见问题

**Q: 为什么入库后没有自动处理？**
- 检查「入库自动执行」是否开启
- 确认影片确实含中文和英文两条内嵌字幕轨（缺一则跳过）
- 查看插件详情页的任务状态和说明列

**Q: 封回 MKV 失败？**
- 部分非 MKV 容器（如 MP4）封回 SRT 可能失败，建议用「仅生成 .srt」模式
- 查看 MoviePilot 日志中的 ffmpeg 错误信息

**Q: 路径填了但扫描不到文件？**
- 确认填的是**容器内**路径，不是宿主机路径
- 确认路径已挂载到 MoviePilot 容器
