# syntax=docker/dockerfile:1
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv build --wheel --out-dir /dist

FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm -f /tmp/*.whl
RUN useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin earshot \
 && mkdir /data && chown earshot:earshot /data
ENV EARSHOT_RELAY_HOST=0.0.0.0 \
    EARSHOT_RELAY_PORT=8080 \
    EARSHOT_RELAY_DATA_DIR=/data \
    PYTHONUNBUFFERED=1
USER 10001
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,sys,urllib.request as u; sys.exit(0 if u.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('EARSHOT_RELAY_PORT','8080'), timeout=4).status == 200 else 1)"
ENTRYPOINT ["earshot-relay"]
CMD ["serve"]
