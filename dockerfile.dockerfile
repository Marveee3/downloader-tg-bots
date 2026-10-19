FROM python:3.11-slim

# 安装 FFmpeg（yt-dlp 合并/压缩必需）
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 每次构建时更新 yt-dlp 到最新版（重要！旧版容易失效）
RUN pip install --no-cache-dir -U yt-dlp

COPY . .

CMD ["python", "bot.py"]