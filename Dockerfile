FROM python:3.12-slim

# ffmpeg for audio extraction / subtitle demuxing, plus runtime probe tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
  && rm -rf /var/lib/apt/lists/*

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
