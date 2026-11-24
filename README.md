# DeepRead

英语精读学习平台：上传视频 / 音频，自动生成精读课程。

## 工作原理

1. **上传**任意视频 / 音频（mp4 / mkv / mp3 / m4a …）
2. **双通道文本获取**：
   - 调用你配置的 OpenAI 兼容中转 API（`/v1/audio/transcriptions`）分段转写
   - 自动提取视频内嵌字幕轨（mov_text / srt / ass / webvtt）
3. **对齐融合**：字幕轨与转写按时间轴对齐、相似度校对，不一致处标记「不确定」，显著提升原文准确率
4. **精讲生成**（chat completions）：逐句翻译（中英互译）+ 词汇精讲 + 语法注释 + 长难句拆解 + 口语表达 + 行业词汇 + 文化背景 + 跟读提示
5. 在学习页逐句精读：播放同步高亮、点击跳转、生词卡自测、A-B 循环跟读、倍速播放

学习进度保存在服务器本地，换浏览器不丢失。

## 部署（Docker Compose）

```bash
mkdir deepread && cd deepread
# 创建 docker-compose.yml（见下），然后：
docker compose up -d
```

```yaml
services:
  deepread:
    image: ghcr.io/hyperion858/deepread:latest
    container_name: deepread
    ports:
      - "8000:8000"
    volumes:
      - ./data:/app/data
    restart: unless-stopped
```

支持 `linux/amd64` 与 `linux/arm64`。打开 `http://<主机IP>:8000`，先到 **设置** 页配置中转 API（Base URL、Key、转写模型、Chat 模型），然后上传素材即可。

## 数据

所有数据都在 `./data` 目录（volume 挂载）：

- `config.json` — API 配置（含 key，注意备份）
- `media/` — 上传的原始素材
- `courses/<id>/` — 转写、精讲 JSON 与学习进度

## 本地开发

```bash
docker compose up --build   # 本地构建镜像
```

或直接跑（需 Python 3.10+ 与 ffmpeg）：

```bash
pip install -r requirements.txt
uvicorn server:app --host 0.0.0.0 --port 8000
```

## CI

推送到 `main` 或打 `v*` tag 时，GitHub Actions 用 buildx 构建多架构镜像并推送到 `ghcr.io/hyperion858/deepread`。
