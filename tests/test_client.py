"""Tool-level tests using MCPServer directly (no HTTP transport)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from src.db import DatabaseManager
from src.tools import register_tools

SOURCE_DATA = Path(__file__).resolve().parents[1] / "data" / "dbip"
VERSIONED = {
    "dbip-country-lite.mmdb": "dbip-country-lite-2026-09.mmdb",
    "dbip-city-lite.mmdb": "dbip-city-lite-2026-09.mmdb",
    "dbip-asn-lite.mmdb": "dbip-asn-lite-2026-09.mmdb",
}


def local_data_available() -> bool:
    return all((SOURCE_DATA / source_name).exists() for source_name in VERSIONED.values())


def make_data_dir() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="ip-info-test-"))
    for fixed, source_name in VERSIONED.items():
        (tmp / fixed).symlink_to(SOURCE_DATA / source_name)
    return tmp


class FakeUpdater:
    def __init__(self) -> None:
        self.last_trigger_result = {"status": "up_to_date", "message": "no change"}
        self.last_status = {
            "status": "ready",
            "database_version": "2026-09",
            "ready": True,
            "action": None,
            "last_update": None,
        }

    def status(self):
        return dict(self.last_status)

    def trigger_update(self):
        return dict(self.last_trigger_result)


class ToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        if not local_data_available():
            self.skipTest("本地私有 MMDB 测试数据未包含在公开仓库中")
        self.data_dir = make_data_dir()
        self.db = DatabaseManager(self.data_dir)
        self.db.load_all()
        self.updater = FakeUpdater()
        self.server = MCPServer(
            name="ip-info-test", version="0.0.0", instructions="test tools"
        )
        register_tools(self.server, self.db, self.updater)

    async def test_tools_are_registered(self):
        tools = await self.server.list_tools()
        names = {tool.name for tool in tools}
        self.assertEqual(
            names, {"query_ip", "batch_query_ips", "update_databases", "get_update_status"}
        )

    async def test_query_single(self):
        result = await self.server.call_tool("query_ip", {"ip": "8.8.8.8"})
        text = self._text(result)
        self.assertIn("Google LLC", text)
        self.assertIn("United States", text)

    async def test_query_invalid_ip(self):
        result = await self.server.call_tool("query_ip", {"ip": "not-an-ip"})
        self.assertIn("无效或查询失败", self._text(result))

    async def test_batch_query(self):
        result = await self.server.call_tool(
            "batch_query_ips",
            {"ips": ["8.8.8.8", "1.1.1.1"]},
        )
        text = self._text(result)
        self.assertIn("Google LLC", text)
        self.assertIn("Cloudflare", text)

    async def test_update_tools_return_updater_data(self):
        result = await self.server.call_tool("update_databases", {})
        self.assertIn("no change", self._text(result))
        status = await self.server.call_tool("get_update_status", {})
        self.assertIn("2026-09", self._text(status))

    async def test_query_blocked_while_updating(self):
        self.assertTrue(self.db.begin_update())
        try:
            result = await self.server.call_tool("query_ip", {"ip": "8.8.8.8"})
            self.assertIn("正在更新", self._text(result))
        finally:
            self.db.end_update()
        result = await self.server.call_tool("query_ip", {"ip": "8.8.8.8"})
        self.assertIn("Google LLC", self._text(result))

    @staticmethod
    def _text(result) -> str:
        if not result.content:
            return ""
        return "\n".join(
            block.text if hasattr(block, "text") else str(block) for block in result.content
        )


if __name__ == "__main__":
    unittest.main()
