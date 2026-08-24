#!/usr/bin/env bash
# =============================================================================
#  DualSub Web - 一键部署脚本
#  在 NAS 的 SSH 终端 (有 docker 权限的账号, 通常是 admin) 运行:
#      bash /vol1/1000/Docker/dualsub-web/deploy.sh
#
#  它会:
#    1. 构建 Docker 镜像 dualsub-web
#    2. 运行容器, 映射端口 6543
#    3. 把影视库目录挂载到容器 /media (可按需修改下方 MOUNT)
#
#  访问: http://<NAS_IP>:6543
# =============================================================================

# ---------- 可配置项 ----------
# 容器名
CONTAINER_NAME="dualsub-web"
# 端口映射: 宿主机端口:容器端口
HOST_PORT="6543"
# 要挂载的影视库根目录 (把 NAS 上的影视目录挂进容器 /media)
# 示例: "/vol2/1000/Emby观影库:/media"  -> 容器内就能浏览整个 Emby 观影库
#    可挂多个: "/vol2/1000/Emby观影库:/media/Emby观影库 /vol1/xxx:/media/xxx"
MOUNTS=(
    "/vol2/1000/Emby观影库:/media/Emby观影库"
)
# 是否每次重启容器时重新构建 (建议首次 true)
BUILD=true

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}" || exit 1

echo "=== DualSub Web 部署 ==="

# 1. 构建镜像
if [ "${BUILD}" = "true" ]; then
    echo "[1/3] 构建镜像 dualsub-web ..."
    docker build -t dualsub-web .
    [ $? -ne 0 ] && echo "构建失败" && exit 1
else
    echo "[1/3] 跳过构建 (使用现有镜像)"
fi

# 2. 停止并删除旧容器 (如有)
echo "[2/3] 清理旧容器 ..."
docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true

# 3. 运行新容器
echo "[3/3] 启动容器 ..."
MOUNT_ARGS=()
for m in "${MOUNTS[@]}"; do
    MOUNT_ARGS+=("-v" "$m")
done

docker run -d \
    --name "${CONTAINER_NAME}" \
    --restart unless-stopped \
    -p "${HOST_PORT}:6543" \
    -e MEDIA_ROOT=/media \
    -e TZ=Asia/Shanghai \
    "${MOUNT_ARGS[@]}" \
    dualsub-web

if [ $? -eq 0 ]; then
    echo ""
    echo "=============================================="
    echo "✅ 部署完成!"
    echo "   访问: http://<NAS_IP>:${HOST_PORT}"
    echo ""
    echo "   查看日志: docker logs -f dualsub-web"
    echo "   停止:     docker stop dualsub-web"
    echo "   卸载:     docker rm -f dualsub-web"
    echo "=============================================="
else
    echo "启动失败, 请检查 docker 权限或日志"
    exit 1
fi