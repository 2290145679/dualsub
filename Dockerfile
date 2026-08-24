# DualSub Web - 影视库双语字幕合并工具
# 基于 python:3.11-alpine + 系统 ffmpeg
FROM python:3.11-alpine

# 安装 ffmpeg (字幕轨道提取依赖)
RUN apk add --no-cache ffmpeg

WORKDIR /app

# Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 应用代码
COPY app.py merge_dual.py ./
COPY templates ./templates
COPY static ./static

# 媒体挂载点: 把影视库目录挂载到这里 (如 /vol2/1000/Emby观影库)
RUN mkdir -p /media && chmod 777 /media

ENV MEDIA_ROOT=/media
ENV PORT=6543

EXPOSE 6543

CMD ["python3", "app.py"]
