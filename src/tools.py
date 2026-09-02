"""MCP tool registrations.

The tool functions are thin adapters over DatabaseManager and UpdateService,
so the server can keep its transport and lifecycle concerns separate.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from .db import DatabaseManager
from .updater import UpdateService

MAX_BATCH = 100


def register_tools(server: Any, db: DatabaseManager, updater: UpdateService) -> None:
    """Register all tools on an MCPServer instance."""

    @server.tool(
        name="query_ip",
        title="Query IP",
        description=(
            "Query one IPv4 or IPv6 address against local db-ip Lite "
            "databases. Returns country, country code, region, city, "
            "latitude/longitude, ASN and AS organization."
        ),
    )
    def query_ip(
        ip: Annotated[str, Field(description="IPv4 or IPv6 address to look up.")],
    ) -> dict[str, Any]:
        if not ip:
            return {"ok": False, "error": "ip 参数不能为空"}
        result, err = db.query(ip)
        if err is not None:
            return {"ok": False, "error": err, "ip": ip}
        return {"ok": True, "data": result}

    @server.tool(
        name="batch_query_ips",
        title="Batch Query IPs",
        description=(
            "Query up to 100 IPv4/IPv6 addresses in one call. Returns the "
            "same per-IP fields as query_ip, in input order. While the "
            "database is updating this tool returns a temporary notice."
        ),
    )
    def batch_query_ips(
        ips: Annotated[
            list[str],
            Field(
                description=(
                    "IPv4/IPv6 addresses to look up, at most "
                    f"{MAX_BATCH} per call."
                )
            ),
        ],
    ) -> dict[str, Any]:
        if not ips:
            return {"ok": False, "error": "ips 不能为空"}
        if len(ips) > MAX_BATCH:
            return {
                "ok": False,
                "error": f"单次最多查询 {MAX_BATCH} 个 IP，收到 {len(ips)} 个",
            }
        results, err = db.batch_query(ips)
        if err is not None:
            return {"ok": False, "error": err}
        return {"ok": True, "count": len(results), "data": results}

    @server.tool(
        name="update_databases",
        title="Update DB-IP Databases",
        description=(
            "Check db-ip.com for a newer monthly Lite release. If available, "
            "starts downloading and atomically swapping Country, City and ASN "
            "databases in the background and returns immediately. During the "
            "update, query_ip and batch_query_ips report 'database updating'. "
            "Use get_update_status to track progress."
        ),
    )
    def update_databases() -> dict[str, Any]:
        return updater.trigger_update()

    @server.tool(
        name="get_update_status",
        title="Get Database Update Status",
        description=(
            "Return database readiness, active version and the result of the "
            "last update attempt. status is 'updating' while a background "
            "update is in progress."
        ),
    )
    def get_update_status() -> dict[str, Any]:
        return updater.status()
