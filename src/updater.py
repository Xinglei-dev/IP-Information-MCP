"""db-ip Lite release discovery and online database updates.

The update path is shared by first-boot initialisation and runtime agent
triggered refreshes:

1. Fetch the official lite page and discover the current ``YYYY-MM`` release.
2. Download three ``.mmdb.gz`` files to a temporary directory on the same
   filesystem as DATA_DIR.
3. Gunzip, open with maxminddb and verify the expected database type.
4. ``os.replace`` the temporary files over the fixed file names.
5. Reload the :class:`DatabaseManager` readers so the next queries use the
   new data.

Runtime updates are asynchronous so ``update_databases`` can return
immediately; queries return a friendly "updating" message while the swap is
in progress.
"""

from __future__ import annotations

import gzip
import logging
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError

from .db import DB_FILES, EXPECTED_TYPES, DatabaseManager

logger = logging.getLogger("ipinfo.updater")

# The summary page does not expose download links in plain HTML; each
# database detail page does.
RELEASE_PAGE_URLS = [
    "https://db-ip.com/db/download/ip-to-country-lite",
    "https://db-ip.com/db/download/ip-to-city-lite",
    "https://db-ip.com/db/download/ip-to-asn-lite",
]
DOWNLOAD_BASE = "https://download.db-ip.com/free"
USER_AGENT = "ip-info-mcp/0.1"

# Releases are monthly; one URL pattern is enough once the page is valid.
_RELEASE_RE = re.compile(r"dbip-(?:country|city|asn)-lite-(\d{4})-(\d{2})\.mmdb\.gz")


@dataclass(frozen=True)
class Release:
    year: int
    month: int

    @property
    def label(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"

    def download_urls(self) -> dict[str, str]:
        return {
            kind: f"{DOWNLOAD_BASE}/dbip-{kind}-lite-{self.label}.mmdb.gz"
            for kind in DB_FILES
        }

    def __str__(self) -> str:
        return self.label


def parse_release(html: str) -> Release | None:
    matches = _RELEASE_RE.findall(html)
    if not matches:
        return None
    year, month = matches[0]
    return Release(int(year), int(month))


def fetch_latest_release() -> Release:
    errors: list[str] = []
    for page_url in RELEASE_PAGE_URLS:
        try:
            req = request.Request(page_url, headers={"User-Agent": USER_AGENT})
            with request.urlopen(req, timeout=45) as resp:
                html = resp.read().decode("utf-8", errors="replace")
            release = parse_release(html)
            if release is not None:
                logger.info("db-ip latest release: %s", release.label)
                return release
            errors.append(f"{page_url}: no MMDB link found")
        except Exception as exc:
            errors.append(f"{page_url}: {exc}")
    raise RuntimeError("无法解析 db-ip 最新版本: " + "; ".join(errors))


def _download(url: str, dest: Path) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        req = request.Request(url, headers={"User-Agent": USER_AGENT})
        with request.urlopen(req, timeout=180) as resp, tmp.open("wb") as out:
            shutil.copyfileobj(resp, out, length=1024 * 1024)
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)


class UpdateService:
    """Coordinates version discovery, downloads and reader reloads."""

    def __init__(self, db: DatabaseManager) -> None:
        self.db = db
        self._data_dir = Path(db.data_dir)
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_result: dict[str, Any] | None = None
        self._current_action: str | None = None

    # ------------------------------------------------------------------
    # Public state
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        updating = self.db.updating
        with self._lock:
            action = self._current_action
            last = dict(self._last_result) if self._last_result else None

        if updating:
            status = "updating"
        elif last is None:
            status = "ready"
        elif last.get("ok"):
            status = "success"
        else:
            status = "failed"

        message = ""
        if last and last.get("error"):
            message = str(last["error"])
        elif last and last.get("already_up_to_date"):
            message = "已是最新版本"
        elif last and last.get("version"):
            message = f"数据库已更新到 {last['version']}"

        return {
            "status": status,
            "message": message,
            "database_version": self.db.version,
            "ready": self.db.ready,
            "action": action,
            "started_at": last.get("started_at") if last else None,
            "finished_at": last.get("finished_at") if last else None,
            "last_update": last,
        }

    def check_for_update(self) -> tuple[bool, Release]:
        """Return ``(needs_update, release)``.  No state is changed."""
        release = fetch_latest_release()
        return release.label != self.db.version, release

    def trigger_update(self) -> dict[str, Any]:
        """Synchronous preflight plus background download.

        Called by the MCP tool.  If no new release exists the answer is
        returned immediately; otherwise a thread performs the update.
        """
        if self.db.updating:
            return {"status": "already_running", "message": "数据库更新已在运行中"}

        try:
            needs_update, release = self.check_for_update()
        except Exception as exc:
            result = {
                "ok": False,
                "status": "failed",
                "message": f"检查 db-ip 版本失败: {exc}",
                "error": f"检查 db-ip 版本失败: {exc}",
                "started_at": _utcnow(),
                "finished_at": _utcnow(),
            }
            with self._lock:
                self._last_result = result
            return result

        if not needs_update:
            return {
                "status": "up_to_date",
                "message": f"当前已是最新版本 {release.label}",
                "database_version": release.label,
            }

        if not self.db.begin_update():
            return {"status": "already_running", "message": "数据库更新已在运行中"}

        with self._lock:
            self._current_action = "downloading"
            self._last_result = None
        self._thread = threading.Thread(
            target=self._run_background,
            args=(release,),
            name="dbip-update",
            daemon=True,
        )
        self._thread.start()
        return {
            "status": "started",
            "message": f"开始更新到 {release.label}，可通过 get_update_status 查询进度",
            "database_version": self.db.version,
        }

    def initialize(self) -> dict[str, Any]:
        """Synchronous first-boot download used before serving HTTP."""
        self.db.begin_update()
        try:
            release = fetch_latest_release()
            result = self._download_and_reload(release)
        except Exception as exc:
            logger.exception("first-boot database initialization failed")
            result = {"ok": False, "error": f"数据库初始化失败: {exc}"}
        finally:
            self.db.end_update()
        if not result.get("ok"):
            raise RuntimeError(result.get("error", "数据库初始化失败"))
        return result

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run_background(self, release: Release) -> None:
        result: dict[str, Any] = {"started_at": _utcnow()}
        try:
            result.update(self._download_and_reload(release))
            result["finished_at"] = _utcnow()
        except Exception as exc:  # defensive; helpers already report errors
            logger.exception("database update failed")
            result["ok"] = False
            result["error"] = f"更新失败: {exc}"
        finally:
            self.db.end_update()
            with self._lock:
                self._current_action = None
                self._last_result = result
            logger.info("update finished: %s", result.get("ok"))

    def _download_and_reload(self, release: Release) -> dict[str, Any]:
        data_dir = self._data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        tmp_root = Path(tempfile.mkdtemp(prefix=".ipinfo-", dir=data_dir))
        try:
            prepared: dict[str, Path] = {}
            urls = release.download_urls()
            for kind, filename in DB_FILES.items():
                gz_path = tmp_root / f"{kind}.mmdb.gz"
                raw_path = tmp_root / f"{kind}.mmdb"
                logger.info("downloading %s", urls[kind])
                _download(urls[kind], gz_path)
                with gzip.open(gz_path, "rb") as fin, raw_path.open("wb") as fout:
                    shutil.copyfileobj(fin, fout, length=1024 * 1024)
                _verify_reader(raw_path, kind)
                prepared[kind] = raw_path

            # All three are valid.  Replace fixed names one by one.  Readers
            # are still the old generation until reload below.
            for kind, filename in DB_FILES.items():
                target = data_dir / filename
                prepared[kind].replace(target)

            self.db.load_all()
            return {"ok": True, "version": self.db.version}
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _verify_reader(path: Path, kind: str) -> None:
    import maxminddb

    try:
        with maxminddb.open_database(str(path)) as reader:
            meta = reader.metadata()
            expected = EXPECTED_TYPES[kind]
            if expected not in str(meta.database_type):
                raise ValueError(
                    f"{path.name} metadata 类型不匹配: 期望 {expected!r}, 实际 {meta.database_type!r}"
                )
    except Exception as exc:
        raise ValueError(f"{path.name} 不是有效的 MMDB: {exc}") from exc
