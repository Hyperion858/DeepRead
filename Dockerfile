FROM python:3.12-slim

# Beijing time: course timestamps + daily backup schedule follow CST (UTC+8)
ENV TZ=Asia/Shanghai
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
  && ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
  && echo "Asia/Shanghai" > /etc/timezone \
  && rm -rf /var/lib/apt/lists/*

# static ffmpeg/ffprobe single binaries (~45MB total) instead of the distro
# package and its ~400MB codec dependency tree; same ffmpeg, same filters
COPY --from=mwader/static-ffmpeg:9.1 /ffmpeg /usr/local/bin/ffmpeg
COPY --from=mwader/static-ffmpeg:9.1 /ffprobe /usr/local/bin/ffprobe

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .
COPY pipeline/ pipeline/
COPY app/ app/

# data dir is mounted as a volume by compose
RUN mkdir -p /app/data
VOLUME /app/data

EXPOSE 8000

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
