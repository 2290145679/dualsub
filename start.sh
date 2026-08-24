#!/usr/bin/env bash
# =============================================================================
#  DualSub Web - 一键启动脚本 (零依赖, 纯标准库)
#
#  用法:
#    bash start.sh            # 前台运行 (Ctrl+C 停止)
#    bash start.sh 6543       # 指定端口
#    bash start.sh 6543 &     # 后台运行
#
#  访问: http://<NAS_IP>:6543
#  依赖: python3 + ffmpeg (NAS 系统自带, 无需安装任何东西)
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${1:-6543}"

# 检查依赖
command -v python3 >/dev/null 2>&1 || { echo "❌ 缺少 python3"; exit 1; }
command -v ffmpeg >/dev/null 2>&1 || { echo "❌ 缺少 ffmpeg (字幕处理需要)"; exit 1; }
command -v ffprobe >/dev/null 2>&1 || { echo "❌ 缺少 ffprobe (字幕探测需要)"; exit 1; }

echo "🎬 DualSub Web"
echo "   目录: ${SCRIPT_DIR}"
echo "   端口: ${PORT}"
echo "   访问: http://<NAS_IP>:${PORT}"
echo "   (Ctrl+C 停止)"
echo ""

cd "${SCRIPT_DIR}"
exec python3 app.py "${PORT}"
