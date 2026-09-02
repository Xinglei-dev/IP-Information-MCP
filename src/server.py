"""IP-Info MCP server entry point.

Lifecycle contract:
- DATA_DIR must contain three valid MMDB files (country/city/asn).
- On first boot with an empty DATA_DIR, the process downloads and verifies
  all three releases before opening the HTTP port.  If that fails, the
  process exits non-zero.
- Every MCP endpoint requires the same static Bearer token configured with
  MCP_AUTH_TOKEN.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Callable

from .db import DatabaseManager
from .tools import register_tools
from .updater import UpdateService

logger = logging.getLogger("ipinfo.server")


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"环境变量 {name} 必须设置")
    return value


def ensure_databases(db: DatabaseManager, updater: UpdateService) -> None:
    """Load existing databases or perform blocking first-boot download."""
    try:
        db.load_all()
        logger.info("existing databases ready (version %s)", db.version)
        return
    except FileNotFoundError:
        logger.info("data directory is empty; initializing from db-ip.com")
    except Exception as exc:
        logger.warning("existing databases unusable (%s); reinitializing", exc)

    result = updater.initialize()
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "数据库初始化失败"))
    logger.info("databases initialized to version %s", db.version)


def bearer_auth_middleware(app: Callable, token: str) -> Callable:
    """Minimal ASGI middleware that requires ``Authorization: Bearer <token>``."""

    async def middleware(scope, receive, send):
        if scope["type"] != "http":
            await app(scope, receive, send)
            return

        auth_header = None
        for raw_name, raw_value in scope.get("headers", []):
            if raw_name.lower() == b"authorization":
                auth_header = raw_value.decode("latin-1")
                break

        expected = f"Bearer {token}"
        if auth_header != expected:
            body = json.dumps({"error": "unauthorized"}).encode()
            headers = [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", b"Bearer"),
            ]
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": headers,
            })
            await send({"type": "http.response.body", "body": body})
            return

        await app(scope, receive, send)

    return middleware


def build_app(data_dir: str = "/data/dbip", auth_token: str | None = None) -> Callable:
    """Create the MCPServer Starlette app, authenticated as configured."""
    from mcp.server.mcpserver import MCPServer

    db = DatabaseManager(data_dir)
    updater = UpdateService(db)
    ensure_databases(db, updater)

    server = MCPServer(
        name="ip-info",
        title="IP Information MCP Server",
        version="0.1.0",
        description="Offline db-ip Lite IP geolocation and ASN lookup",
        instructions=(
            "Query IP locations with query_ip or batch_query_ips. Use "
            "get_update_status to inspect database readiness. Use "
            "update_databases to fetch the newest monthly db-ip Lite release."
        ),
    )
    register_tools(server, db, updater)

    app = server.streamable_http_app(
        streamable_http_path=os.environ.get("MCP_PATH", "/mcp"),
        host=os.environ.get("MCP_HOST", "0.0.0.0"),
    )

    if auth_token:
        app = bearer_auth_middleware(app, auth_token)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        token = require_env("MCP_AUTH_TOKEN")
        data_dir = os.environ.get("DATA_DIR", "/data/dbip")
        port = int(os.environ.get("MCP_PORT", "8010"))
        host = os.environ.get("MCP_HOST", "0.0.0.0")
    except RuntimeError as exc:
        logger.error("%s", exc)
        raise SystemExit(1) from exc

    app = build_app(data_dir=data_dir, auth_token=token)

    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
