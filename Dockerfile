# syntax=docker/dockerfile:1
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
# The runtime installs exactly the versions in uv.lock (with hashes), then the wheel without resolving anything.
RUN mkdir -p /dist \
 && uv export --frozen --no-dev --no-emit-project --format requirements-txt -o /dist/requirements.txt \
 && uv build --wheel --out-dir /dist

FROM python:3.12-slim
# io.modelcontextprotocol.server.name proves to the official MCP Registry that this image belongs to server.json
# (its "name" must match exactly, case included). Change both together.
LABEL org.opencontainers.image.source="https://github.com/AutomaticAI-io/earshot-relay" \
      org.opencontainers.image.description="Send speech to your phone through your own S3 bucket and the Earshot Android app: HTTP API, MCP server and CLI." \
      org.opencontainers.image.licenses="MIT" \
      io.modelcontextprotocol.server.name="io.github.AutomaticAI-io/earshot-relay"
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /dist/ /tmp/dist/
RUN pip install --no-cache-dir --require-hashes -r /tmp/dist/requirements.txt \
 && pip install --no-cache-dir --no-deps /tmp/dist/*.whl \
 && rm -rf /tmp/dist
RUN useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin earshot \
 && mkdir /data && chown earshot:earshot /data
ENV EARSHOT_RELAY_HOST=0.0.0.0 \
    EARSHOT_RELAY_PORT=8080 \
    EARSHOT_RELAY_DATA_DIR=/data \
    PYTHONUNBUFFERED=1
USER 10001
VOLUME /data
# 8081 is used only with EARSHOT_RELAY_BEACON_PORT=8081 (publish it too; see docs/tunnels.md); 8082 only with
# EARSHOT_RELAY_COMPAT_PORT=8082 (see docs/compat.md).
EXPOSE 8080 8081 8082
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,sys,urllib.request as u; sys.exit(0 if u.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('EARSHOT_RELAY_PORT','8080'), timeout=4).status == 200 else 1)"
ENTRYPOINT ["earshot-relay"]
CMD ["serve"]
