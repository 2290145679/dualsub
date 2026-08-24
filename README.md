# 🎬 DualSub Web - 影视库双语字幕合并工具

把 MKV/MP4 影片里**内嵌的中文 + 英文字幕轨**自动合并为**上下双行双语字幕**。

## 功能

- 📂 Web 界面浏览影视库目录（支持 Emby 观影库等挂载目录）
- 🎞️ 自动识别每个影片的字幕轨语言（chi/zho = 中文，eng/en = 英文）
- ✅ 只列出"中英双轨齐全"的可处理文件，一键批量处理
- 🔀 同一时间点上下双行混排：英文在上、中文在下
- 📄 **生成 `<文件名>.dual.srt` 到影片同目录，绝不修改原文件**
- ⏳ 实时进度条 + 成功/失败/跳过统计

## 部署（在 NAS SSH 终端执行）

```bash
# 一键部署（构建镜像 + 启动容器，端口 6543）
bash /vol1/1000/Docker/dualsub-web/deploy.sh
```

> 前提：当前账号有 docker 权限（通常 admin 用户直接可用）。

## 使用

1. 浏览器打开 `http://<NAS_IP>:6543`
2. 点进子目录（如 `Emby观影库/欧美剧/...`）
3. 勾选要处理的影片（只有"可处理"的才会显示 ✓）
4. 点「🚀 开始处理选中项」
5. 完成后，每个影片旁生成 `.dual.srt`，用播放器挂载即可显示双语

## 配置

编辑 `deploy.sh` 里的 `MOUNTS` 数组可添加更多影视目录：

```bash
MOUNTS=(
    "/vol2/1000/Emby观影库:/media/Emby观影库"
    "/vol1/xxx/电影:/media/电影"        # 示例：再加一个目录
)
```

修改后重新执行 `bash deploy.sh` 即可（会自动重建容器）。

## 常见命令

| 操作 | 命令 |
|---|---|
| 查看日志 | `docker logs -f dualsub-web` |
| 停止 | `docker stop dualsub-web` |
| 启动 | `docker start dualsub-web` |
| 卸载 | `docker rm -f dualsub-web` |

## 目录结构

```
/vol1/1000/Docker/dualsub-web/
├── Dockerfile        # 镜像定义 (python:3.11-alpine + ffmpeg)
├── app.py            # Flask Web 服务
├── merge_dual.py     # 双语字幕合并核心
├── requirements.txt  # Python 依赖 (flask)
├── deploy.sh         # 一键部署脚本
└── templates/
    └── index.html    # Web 界面
```
