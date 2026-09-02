FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data/dbip \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8010 \
    MCP_PATH=/mcp

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir \
        "mcp>=2.1" \
        "maxminddb>=3.1" \
        "uvicorn>=0.30"

COPY src ./src

RUN groupadd --gid 10001 app && \
    useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app && \
    mkdir -p /data/dbip && \
    chown -R app:app /data/dbip && \
    chown -R app:app /app

USER app

VOLUME ["/data/dbip"]
EXPOSE 8010

CMD ["python", "-m", "src.server"]
