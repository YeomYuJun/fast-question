# 원격 모드 서버 이미지. 빌드 문맥은 저장소 루트다(deploy/compose.yaml 의 build.context, 루트 .dockerignore).
# 설정은 이미지 밖 /etc/fq/config.json(읽기 전용 마운트), 데이터는 볼륨 /data 다.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt requirements-server.txt ./
RUN pip install -r requirements.txt -r requirements-server.txt

COPY kb ./kb
COPY web ./web
COPY mcp_server ./mcp_server
COPY run_web.py ./
COPY scripts/global_words.py ./scripts/

# 볼륨을 처음 붙일 때 Docker 가 이미지의 /data 소유자를 그대로 가져가므로 VOLUME 앞에서 만든다
RUN useradd --system --uid 10001 --no-create-home --home-dir /app --shell /usr/sbin/nologin fq \
    && mkdir -p /data /etc/fq \
    && chown fq:fq /data
USER fq
VOLUME ["/data"]
EXPOSE 8765

# /healthz 는 Host 검사를 하지 않아 컨테이너 안에서 127.0.0.1 로 부를 수 있다. 503 이면 urlopen 이 예외를 던진다
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=4)"]

CMD ["python", "run_web.py", "--config", "/etc/fq/config.json"]
