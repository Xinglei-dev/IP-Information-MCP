"""Database manager for the three db-ip Lite MMDB databases.

The manager keeps one current generation of MaxMind readers and exposes
thread-safe queries.  A newer generation is installed by first writing new
files beside the active ones (atomically via os.replace), then swapping in
fresh readers through :meth:`DatabaseManager.load_all`.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import maxminddb

logger = logging.getLogger("ipinfo.db")

# Fixed file names used inside DATA_DIR.  Keeping the names stable lets an
# updater download to a temporary name and os.replace() over these entries.
COUNTRY_FILE = "dbip-country-lite.mmdb"
CITY_FILE = "dbip-city-lite.mmdb"
ASN_FILE = "dbip-asn-lite.mmdb"
DB_FILES = {
    "country": COUNTRY_FILE,
    "city": CITY_FILE,
    "asn": ASN_FILE,
}

EXPECTED_TYPES = {
    "country": "DBIP-Country-Lite",
    "city": "DBIP-City-Lite",
    "asn": "DBIP-ASN-Lite",
}

QUERY_UPDATING_MSG = "数据库正在更新，请稍后再试"
QUERY_NOT_READY_MSG = "数据库尚未初始化，请稍后再试"


class DatabaseManager:
    """Owns the active readers and the database update state."""

    def __init__(self, data_dir: str | Path = "/data/dbip") -> None:
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._ready = False
        self._version = "unknown"
        self._updating = False

        self._readers: dict[str, maxminddb.Reader] = {}

    # ------------------------------------------------------------------
    # Loading / reloading
    # ------------------------------------------------------------------

    def load_all(self) -> None:
        """Load the fixed-name files from DATA_DIR, replacing active readers."""
        paths = {
            kind: self._data_dir / DB_FILES[kind]
            for kind in DB_FILES
        }
        missing = [str(p) for p in paths.values() if not p.is_file()]
        if missing:
            raise FileNotFoundError("数据库文件缺失: " + ", ".join(missing))

        new_readers: dict[str, maxminddb.Reader] = {}
        try:
            for kind, path in paths.items():
                reader = maxminddb.open_database(str(path))
                meta = reader.metadata()
                if EXPECTED_TYPES[kind] not in str(meta.database_type):
                    reader.close()
                    raise ValueError(
                        f"{path.name} 类型不匹配: {meta.database_type!r}"
                    )
                new_readers[kind] = reader
        except Exception:
            for reader in new_readers.values():
                try:
                    reader.close()
                except Exception:
                    pass
            raise

        with self._lock:
            old_readers = self._readers
            self._readers = new_readers
            self._ready = True
            self._version = self._version_from_meta(new_readers["country"].metadata())

        # Close old readers only after the swap so in-flight queries using a
        # previously captured reference remain valid until they finish.
        for reader in old_readers.values():
            try:
                reader.close()
            except Exception:
                pass

        logger.info("databases loaded: version=%s files=%s", self._version, ", ".join(DB_FILES))

    def files(self) -> dict[str, Path]:
        """Current expected paths, primarily for startup validation."""
        return {kind: self._data_dir / name for kind, name in DB_FILES.items()}

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def query(self, ip: str) -> tuple[dict[str, Any] | None, str | None]:
        """Return ``(record, None)`` or ``(None, user_message)`` if queries
        are temporarily unavailable."""
        # The lock also serializes queries against load_all's reader swap, so
        # a reader is never closed while another thread is inside .get().
        try:
            with self._lock:
                if not self._ready:
                    return None, QUERY_NOT_READY_MSG
                if self._updating:
                    return None, QUERY_UPDATING_MSG
                country = self._readers["country"].get(ip) if self._readers.get("country") else None
                city = self._readers["city"].get(ip) if self._readers.get("city") else None
                asn = self._readers["asn"].get(ip) if self._readers.get("asn") else None
        except Exception as exc:
            return None, f"IP 无效或查询失败: {exc}"
        return self._format(ip, country, city, asn), None

    def batch_query(self, ips: list[str]) -> tuple[list[dict[str, Any]] | None, str | None]:
        results: list[dict[str, Any]] = []
        with self._lock:
            if not self._ready:
                return None, QUERY_NOT_READY_MSG
            if self._updating:
                return None, QUERY_UPDATING_MSG
        for ip in ips:
            # Re-check state for each item because an update could start
            # between tool calls, but this is naturally quick.
            result, err = self.query(ip)
            if err:
                return None, err
            results.append(result)
        return results, None

    @staticmethod
    def _safe_get(obj: Any, *keys: Any, default: Any = None) -> Any:
        if obj is None:
            return default
        for key in keys:
            if isinstance(obj, dict):
                obj = obj.get(key)
            elif isinstance(obj, list) and isinstance(key, int):
                try:
                    obj = obj[key]
                except (IndexError, TypeError):
                    return default
            else:
                return default
            if obj is None:
                return default
        return obj

    def _format(
        self,
        ip: str,
        country: dict[str, Any] | None,
        city: dict[str, Any] | None,
        asn: dict[str, Any] | None,
    ) -> dict[str, Any]:
        country_names = self._safe_get(country, "country", "names", default={})
        city_names = self._safe_get(city, "city", "names", default={})

        region = ""
        subs = self._safe_get(city, "subdivisions", default=[])
        if isinstance(subs, list) and subs:
            region = self._safe_get(subs[0], "names", "en", default="")

        return {
            "ip": ip,
            "country": country_names.get("en", "") if isinstance(country_names, dict) else "",
            "country_code": self._safe_get(country, "country", "iso_code", default=""),
            "region": region,
            "city": city_names.get("en", "") if isinstance(city_names, dict) else "",
            "latitude": self._safe_get(city, "location", "latitude", default=0.0),
            "longitude": self._safe_get(city, "location", "longitude", default=0.0),
            "asn": self._safe_get(asn, "autonomous_system_number", default=0),
            "as_organization": self._safe_get(
                asn, "autonomous_system_organization", default=""
            ),
            "is_eu": self._safe_get(
                country, "country", "is_in_european_union", default=False
            ),
        }

    # ------------------------------------------------------------------
    # Update state
    # ------------------------------------------------------------------

    @property
    def ready(self) -> bool:
        with self._lock:
            return self._ready

    @property
    def version(self) -> str:
        with self._lock:
            return self._version

    @property
    def updating(self) -> bool:
        with self._lock:
            return self._updating

    def begin_update(self) -> bool:
        """Mark an update as running.  Returns False if one is already active."""
        with self._lock:
            if self._updating:
                return False
            self._updating = True
            return True

    def end_update(self, *, load: bool = False) -> None:
        """Clear the updating flag.  When load is True a caller has already
        swapped readers through load_all()."""
        with self._lock:
            self._updating = False
        if load:
            # load_all itself sets _ready/_version; called before clearing the
            # flag so queries can proceed immediately.
            pass

    @staticmethod
    def _version_from_meta(meta: Any) -> str:
        epoch = getattr(meta, "build_epoch", None)
        if epoch:
            return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m")
        return "unknown"

    def close(self) -> None:
        with self._lock:
            old = self._readers
            self._readers = {}
            self._ready = False
        for reader in old.values():
            try:
                reader.close()
            except Exception:
                pass
