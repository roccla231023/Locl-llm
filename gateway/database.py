"""SQLite persistence and in-place migrations for Local LLM Gateway."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 3


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:4]}{'•' * min(12, len(value) - 8)}{value[-4:]}"


class Database:
    """Small SQLite data layer.

    v0.2.x stored one upstream credential directly on ``channels``.  v3 keeps
    those legacy columns for compatibility, but moves routing to
    ``channel_credentials`` and gives each model route a ``credential_id``.
    Existing databases are migrated in place without exporting or replacing
    user data.
    """

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    base_url TEXT NOT NULL,
                    api_key TEXT NOT NULL DEFAULT '',
                    auth_type TEXT NOT NULL DEFAULT 'bearer',
                    extra_headers TEXT NOT NULL DEFAULT '{}',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS access_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    api_key TEXT NOT NULL UNIQUE,
                    channel_id INTEGER NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(channel_id) REFERENCES channels(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_access_keys_channel ON access_keys(channel_id);

                CREATE TABLE IF NOT EXISTS request_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    method TEXT NOT NULL,
                    path TEXT NOT NULL,
                    public_model TEXT NOT NULL DEFAULT '',
                    upstream_model TEXT NOT NULL DEFAULT '',
                    channel_name TEXT NOT NULL DEFAULT '',
                    credential_name TEXT NOT NULL DEFAULT '',
                    status INTEGER NOT NULL DEFAULT 0,
                    first_token_ms INTEGER,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_request_logs_created ON request_logs(created_at DESC);
                """
            )
            schema_row = db.execute(
                "SELECT value FROM settings WHERE key = 'schema_version' LIMIT 1"
            ).fetchone()
            try:
                previous_schema_version = int(schema_row["value"]) if schema_row else 0
            except (TypeError, ValueError):
                previous_schema_version = 0
            self._ensure_credential_table(db)
            # Only migrate legacy channel-level credentials while upgrading an
            # actual pre-v3 database. Running this on every v3 startup could
            # resurrect a deliberately deleted default group from the legacy
            # compatibility columns still stored on ``channels``.
            if previous_schema_version < SCHEMA_VERSION:
                self._migrate_legacy_credentials(db, include_empty=schema_row is None)
            self._ensure_models_table(db)
            self._ensure_log_columns(db)
            defaults = {
                "app_name": "本地 LLM 网关",
                "api_key": f"sk-local-{secrets.token_urlsafe(24)}",
                "log_limit": "500",
                "schema_version": str(SCHEMA_VERSION),
            }
            for key, value in defaults.items():
                db.execute(
                    "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
                    (key, value),
                )
            db.execute(
                "INSERT INTO settings(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @staticmethod
    def _ensure_credential_table(db: sqlite3.Connection) -> None:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS channel_credentials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                api_key TEXT NOT NULL DEFAULT '',
                auth_type TEXT NOT NULL DEFAULT 'bearer',
                extra_headers TEXT NOT NULL DEFAULT '{}',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(channel_id) REFERENCES channels(id) ON DELETE CASCADE,
                UNIQUE(channel_id, name)
            )
            """
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_credentials_channel "
            "ON channel_credentials(channel_id)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_credentials_enabled "
            "ON channel_credentials(channel_id, enabled)"
        )

    @staticmethod
    def _migrate_legacy_credentials(db: sqlite3.Connection, *, include_empty: bool = False) -> None:
        """Create one default credential for every old channel exactly once."""
        channels = db.execute(
            "SELECT id, name, api_key, auth_type, extra_headers, created_at, updated_at "
            "FROM channels ORDER BY id"
        ).fetchall()
        for channel in channels:
            exists = db.execute(
                "SELECT 1 FROM channel_credentials WHERE channel_id = ? LIMIT 1",
                (channel["id"],),
            ).fetchone()
            if exists:
                continue
            # Old v0.1.2/v0.2.0 channels with a real key (or explicit
            # no-auth) need a default credential. A newly-created v0.3
            # station intentionally has empty legacy columns until its first
            # group is added, so do not manufacture an invalid empty Bearer
            # credential on a later restart.
            has_legacy_config = include_empty or bool(channel["api_key"]) or channel["auth_type"] == "none"
            if not has_legacy_config:
                continue
            now = channel["updated_at"] or channel["created_at"] or utc_now()
            name = "默认凭证"
            db.execute(
                """
                INSERT INTO channel_credentials(
                    channel_id, name, api_key, auth_type, extra_headers,
                    enabled, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    channel["id"], name, channel["api_key"], channel["auth_type"],
                    channel["extra_headers"] or "{}", channel["created_at"] or now, now,
                ),
            )

    @staticmethod
    def _ensure_models_table(db: sqlite3.Connection) -> None:
        row = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='models'"
        ).fetchone()
        if not row:
            db.execute(
                """
                CREATE TABLE models (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id INTEGER NOT NULL,
                    credential_id INTEGER NOT NULL,
                    public_name TEXT NOT NULL,
                    upstream_name TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(channel_id) REFERENCES channels(id) ON DELETE CASCADE,
                    FOREIGN KEY(credential_id) REFERENCES channel_credentials(id) ON DELETE RESTRICT
                )
                """
            )
        else:
            columns = {
                item["name"] for item in db.execute("PRAGMA table_info(models)")
            }
            table_sql = (row["sql"] or "").upper()
            global_unique = "PUBLIC_NAME TEXT NOT NULL UNIQUE" in table_sql
            if "credential_id" not in columns or global_unique:
                # These names were used by v0.2.0. SQLite keeps indexes when
                # a table is renamed, so remove them before creating the new
                # table with the same index names.
                for index_name in (
                    "idx_models_channel", "idx_models_route", "idx_models_channel_public",
                    "idx_models_credential",
                ):
                    db.execute(f"DROP INDEX IF EXISTS {index_name}")
                db.execute("ALTER TABLE models RENAME TO models_legacy")
                db.execute(
                    """
                    CREATE TABLE models (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        channel_id INTEGER NOT NULL,
                        credential_id INTEGER NOT NULL,
                        public_name TEXT NOT NULL,
                        upstream_name TEXT NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY(channel_id) REFERENCES channels(id) ON DELETE CASCADE,
                        FOREIGN KEY(credential_id) REFERENCES channel_credentials(id) ON DELETE RESTRICT
                    )
                    """
                )
                db.execute(
                    """
                    INSERT INTO models(
                        id, channel_id, credential_id, public_name, upstream_name,
                        enabled, created_at, updated_at
                    )
                    SELECT m.id, m.channel_id, c.id, m.public_name, m.upstream_name,
                           m.enabled, m.created_at, m.updated_at
                    FROM models_legacy m
                    JOIN channel_credentials c
                      ON c.channel_id = m.channel_id
                     AND c.id = (
                         SELECT MIN(c2.id) FROM channel_credentials c2
                         WHERE c2.channel_id = m.channel_id
                     )
                    """
                )
                db.execute("DROP TABLE models_legacy")
        db.execute("CREATE INDEX IF NOT EXISTS idx_models_channel ON models(channel_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_models_credential ON models(credential_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_models_route ON models(public_name, enabled)")
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_models_channel_public "
            "ON models(channel_id, public_name)"
        )

    @staticmethod
    def _ensure_log_columns(db: sqlite3.Connection) -> None:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(request_logs)")}
        if "first_token_ms" not in columns:
            db.execute("ALTER TABLE request_logs ADD COLUMN first_token_ms INTEGER")
        if "credential_name" not in columns:
            db.execute("ALTER TABLE request_logs ADD COLUMN credential_name TEXT NOT NULL DEFAULT ''")

    def get_setting(self, key: str, default: str = "") -> str:
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def settings(self) -> dict[str, str]:
        with self.connect() as db:
            return {row["key"]: row["value"] for row in db.execute("SELECT key, value FROM settings")}

    def list_channels(self, reveal: bool = False) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT c.*, COUNT(DISTINCT m.id) AS model_count,
                       (SELECT COUNT(*) FROM channel_credentials cc WHERE cc.channel_id = c.id) AS credential_count,
                       (SELECT COUNT(*) FROM channel_credentials cc WHERE cc.channel_id = c.id AND cc.enabled = 1) AS enabled_credential_count
                FROM channels c
                LEFT JOIN models m ON m.channel_id = c.id
                GROUP BY c.id
                ORDER BY c.id DESC
                """
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["enabled"] = bool(item["enabled"])
            item["extra_headers"] = json.loads(item["extra_headers"] or "{}")
            if not reveal:
                item["api_key"] = mask_secret(item["api_key"])
            result.append(item)
        return result

    def get_channel(self, channel_id: int, reveal: bool = True) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT c.*,
                       (SELECT COUNT(*) FROM channel_credentials cc WHERE cc.channel_id = c.id) AS credential_count,
                       (SELECT COUNT(*) FROM channel_credentials cc WHERE cc.channel_id = c.id AND cc.enabled = 1) AS enabled_credential_count
                FROM channels c WHERE c.id = ?
                """,
                (channel_id,),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        item["extra_headers"] = json.loads(item["extra_headers"] or "{}")
        if not reveal:
            item["api_key"] = mask_secret(item["api_key"])
        return item

    def list_credentials(self, channel_id: int, reveal: bool = False) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT cc.*, c.name AS channel_name, c.base_url, c.enabled AS channel_enabled,
                       COUNT(m.id) AS model_count
                FROM channel_credentials cc
                JOIN channels c ON c.id = cc.channel_id
                LEFT JOIN models m ON m.credential_id = cc.id
                WHERE cc.channel_id = ?
                GROUP BY cc.id
                ORDER BY cc.id
                """,
                (channel_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["enabled"] = bool(item["enabled"])
            item["channel_enabled"] = bool(item["channel_enabled"])
            item["extra_headers"] = json.loads(item["extra_headers"] or "{}")
            if not reveal:
                item["api_key"] = mask_secret(item["api_key"])
            result.append(item)
        return result

    def get_credential(self, credential_id: int, reveal: bool = True) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT cc.*, c.name AS channel_name, c.base_url, c.enabled AS channel_enabled
                FROM channel_credentials cc JOIN channels c ON c.id = cc.channel_id
                WHERE cc.id = ?
                """,
                (credential_id,),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        item["channel_enabled"] = bool(item["channel_enabled"])
        item["extra_headers"] = json.loads(item["extra_headers"] or "{}")
        if not reveal:
            item["api_key"] = mask_secret(item["api_key"])
        return item

    def add_credential(self, data: dict[str, Any]) -> int:
        now = utc_now()
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO channel_credentials(
                    channel_id, name, api_key, auth_type, extra_headers,
                    enabled, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(data["channel_id"]), data["name"], data["api_key"],
                    data.get("auth_type", "bearer"),
                    json.dumps(data.get("extra_headers", {}), ensure_ascii=False),
                    int(data.get("enabled", True)), now, now,
                ),
            )
            return int(cursor.lastrowid)

    def update_credential(self, credential_id: int, data: dict[str, Any]) -> bool:
        with self.connect() as db:
            cursor = db.execute(
                """
                UPDATE channel_credentials
                SET channel_id = ?, name = ?, api_key = ?, auth_type = ?,
                    extra_headers = ?, enabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    int(data["channel_id"]), data["name"], data["api_key"],
                    data.get("auth_type", "bearer"),
                    json.dumps(data.get("extra_headers", {}), ensure_ascii=False),
                    int(data.get("enabled", True)), utc_now(), credential_id,
                ),
            )
            return cursor.rowcount > 0

    def delete_credential(self, credential_id: int) -> bool:
        with self.connect() as db:
            cursor = db.execute("DELETE FROM channel_credentials WHERE id = ?", (credential_id,))
            return cursor.rowcount > 0

    def default_credential(self, channel_id: int) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT cc.*, c.name AS channel_name, c.base_url,
                       c.enabled AS channel_enabled
                FROM channel_credentials cc JOIN channels c ON c.id = cc.channel_id
                WHERE cc.channel_id = ? ORDER BY cc.id LIMIT 1
                """,
                (channel_id,),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["extra_headers"] = json.loads(item["extra_headers"] or "{}")
        item["enabled"] = bool(item["enabled"])
        item["channel_enabled"] = bool(item["channel_enabled"])
        return item

    def credential_exists(self, credential_id: int, channel_id: int | None = None) -> bool:
        with self.connect() as db:
            query = "SELECT 1 FROM channel_credentials WHERE id = ?"
            params: list[Any] = [credential_id]
            if channel_id is not None:
                query += " AND channel_id = ?"
                params.append(channel_id)
            return db.execute(query, params).fetchone() is not None

    def get_enabled_credentials(self, channel_id: int) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT cc.*, c.name AS channel_name, c.base_url,
                       c.enabled AS channel_enabled
                FROM channel_credentials cc JOIN channels c ON c.id = cc.channel_id
                WHERE cc.channel_id = ? AND cc.enabled = 1 AND c.enabled = 1
                ORDER BY cc.id
                """,
                (channel_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["extra_headers"] = json.loads(item["extra_headers"] or "{}")
            item["enabled"] = bool(item["enabled"])
            item["channel_enabled"] = bool(item["channel_enabled"])
            result.append(item)
        return result

    def find_access_key(self, api_key: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT k.*, k.api_key AS local_api_key, c.name AS channel_name,
                       c.base_url, c.enabled AS channel_enabled
                FROM access_keys k JOIN channels c ON c.id = k.channel_id
                WHERE k.api_key = ?
                """,
                (api_key,),
            ).fetchone()
        if not row or not row["enabled"] or not row["channel_enabled"]:
            return None
        item = dict(row)
        item["local_key"] = item.pop("local_api_key")
        item["kind"] = "channel"
        return item

    def list_access_keys(self, reveal: bool = False) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT k.*, c.name AS channel_name, c.enabled AS channel_enabled
                FROM access_keys k JOIN channels c ON c.id = k.channel_id
                ORDER BY k.id DESC
                """
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["enabled"] = bool(item["enabled"])
            item["channel_enabled"] = bool(item["channel_enabled"])
            if not reveal:
                item["api_key"] = mask_secret(item["api_key"])
            result.append(item)
        return result

    def get_access_key(self, key_id: int, reveal: bool = True) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT k.*, c.name AS channel_name, c.enabled AS channel_enabled
                FROM access_keys k JOIN channels c ON c.id = k.channel_id
                WHERE k.id = ?
                """,
                (key_id,),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        item["channel_enabled"] = bool(item["channel_enabled"])
        if not reveal:
            item["api_key"] = mask_secret(item["api_key"])
        return item

    def add_access_key(self, data: dict[str, Any]) -> int:
        now = utc_now()
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO access_keys(name, api_key, channel_id, enabled, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (data["name"], data["api_key"], int(data["channel_id"]),
                 int(data.get("enabled", True)), now, now),
            )
            return int(cursor.lastrowid)

    def update_access_key(self, key_id: int, data: dict[str, Any]) -> bool:
        with self.connect() as db:
            cursor = db.execute(
                """
                UPDATE access_keys SET name = ?, api_key = ?, channel_id = ?,
                    enabled = ?, updated_at = ? WHERE id = ?
                """,
                (data["name"], data["api_key"], int(data["channel_id"]),
                 int(data.get("enabled", True)), utc_now(), key_id),
            )
            return cursor.rowcount > 0

    def delete_access_key(self, key_id: int) -> bool:
        with self.connect() as db:
            cursor = db.execute("DELETE FROM access_keys WHERE id = ?", (key_id,))
            return cursor.rowcount > 0

    def access_key_exists(self, api_key: str, exclude_id: int | None = None) -> bool:
        with self.connect() as db:
            query = "SELECT 1 FROM access_keys WHERE api_key = ?"
            params: list[Any] = [api_key]
            if exclude_id is not None:
                query += " AND id != ?"
                params.append(exclude_id)
            return db.execute(query, params).fetchone() is not None

    def add_channel(self, data: dict[str, Any]) -> int:
        now = utc_now()
        with self.connect() as db:
            cursor = db.execute(
                """
                INSERT INTO channels(name, base_url, api_key, auth_type, extra_headers,
                                     enabled, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (data["name"], data["base_url"], data.get("api_key", ""),
                 data.get("auth_type", "bearer"),
                 json.dumps(data.get("extra_headers", {}), ensure_ascii=False),
                 int(data.get("enabled", True)), now, now),
            )
            channel_id = int(cursor.lastrowid)
            if data.get("api_key") or data.get("auth_type") == "none":
                db.execute(
                    """
                    INSERT INTO channel_credentials(
                        channel_id, name, api_key, auth_type, extra_headers,
                        enabled, created_at, updated_at
                    ) VALUES(?, '默认凭证', ?, ?, ?, ?, ?, ?)
                    """,
                    (channel_id, data.get("api_key", ""), data.get("auth_type", "bearer"),
                     json.dumps(data.get("extra_headers", {}), ensure_ascii=False),
                     int(data.get("enabled", True)), now, now),
                )
            return channel_id

    def update_channel(self, channel_id: int, data: dict[str, Any]) -> bool:
        with self.connect() as db:
            current = db.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
            if not current:
                return False
            api_key = data.get("api_key", "")
            auth_type = data.get("auth_type", current["auth_type"])
            extra_headers = data.get("extra_headers", json.loads(current["extra_headers"] or "{}"))
            # Empty api_key means the new station form intentionally did not
            # edit legacy credentials.  Non-empty values retain old API use.
            update_legacy = bool(api_key) or auth_type == "none"
            if update_legacy:
                db.execute(
                    """
                    UPDATE channels SET name = ?, base_url = ?, api_key = ?, auth_type = ?,
                        extra_headers = ?, enabled = ?, updated_at = ? WHERE id = ?
                    """,
                    (data["name"], data["base_url"], api_key, auth_type,
                     json.dumps(extra_headers, ensure_ascii=False), int(data.get("enabled", True)),
                     utc_now(), channel_id),
                )
                default = db.execute(
                    "SELECT id FROM channel_credentials WHERE channel_id = ? ORDER BY id LIMIT 1",
                    (channel_id,),
                ).fetchone()
                values = (api_key, auth_type, json.dumps(extra_headers, ensure_ascii=False),
                          int(data.get("enabled", True)), utc_now())
                if default:
                    db.execute(
                        """UPDATE channel_credentials SET api_key = ?, auth_type = ?,
                           extra_headers = ?, enabled = ?, updated_at = ? WHERE id = ?""",
                        (*values, default["id"]),
                    )
                else:
                    db.execute(
                        """INSERT INTO channel_credentials(
                           channel_id, name, api_key, auth_type, extra_headers, enabled,
                           created_at, updated_at) VALUES(?, '默认凭证', ?, ?, ?, ?, ?, ?)""",
                        (channel_id, api_key, auth_type, json.dumps(extra_headers, ensure_ascii=False),
                         int(data.get("enabled", True)), utc_now(), utc_now()),
                    )
            else:
                db.execute(
                    """UPDATE channels SET name = ?, base_url = ?, enabled = ?, updated_at = ?
                       WHERE id = ?""",
                    (data["name"], data["base_url"], int(data.get("enabled", True)), utc_now(), channel_id),
                )
            return True

    def delete_channel(self, channel_id: int) -> bool:
        with self.connect() as db:
            cursor = db.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
            return cursor.rowcount > 0

    def list_models(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT m.*, c.name AS channel_name, c.enabled AS channel_enabled,
                       cc.name AS credential_name, cc.enabled AS credential_enabled
                FROM models m
                JOIN channels c ON c.id = m.channel_id
                JOIN channel_credentials cc ON cc.id = m.credential_id
                ORDER BY m.public_name COLLATE NOCASE
                """
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["enabled"] = bool(item["enabled"])
            item["channel_enabled"] = bool(item["channel_enabled"])
            item["credential_enabled"] = bool(item["credential_enabled"])
            result.append(item)
        return result

    def get_model(self, model_id: int) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM models WHERE id = ?", (model_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        return item

    def compare_credential_models(self, credential_id: int, upstream_names: list[str]) -> dict[str, Any]:
        """Compare one group's live upstream model list with its local routes.

        This is deliberately a read-only comparison.  A missing upstream name
        may mean a rename, removal, permission change, or a temporarily
        incomplete ``/v1/models`` response, so callers must ask the user before
        importing, replacing, or disabling any route.
        """
        names = sorted(set(upstream_names), key=str.lower)
        upstream_set = set(names)
        with self.connect() as db:
            credential = db.execute(
                """
                SELECT cc.id, cc.name, cc.channel_id, cc.enabled,
                       c.name AS channel_name, c.enabled AS channel_enabled
                FROM channel_credentials cc
                JOIN channels c ON c.id = cc.channel_id
                WHERE cc.id = ?
                """,
                (credential_id,),
            ).fetchone()
            if not credential:
                raise ValueError("分组凭证不存在")
            routes = [dict(row) for row in db.execute(
                """
                SELECT m.id, m.channel_id, m.credential_id, m.public_name,
                       m.upstream_name, m.enabled, m.created_at, m.updated_at
                FROM models m
                WHERE m.credential_id = ?
                ORDER BY m.public_name COLLATE NOCASE
                """,
                (credential_id,),
            ).fetchall()]
            channel_routes = [dict(row) for row in db.execute(
                """
                SELECT m.id, m.public_name, m.upstream_name,
                       m.credential_id, cc.name AS credential_name
                FROM models m
                JOIN channel_credentials cc ON cc.id = m.credential_id
                WHERE m.channel_id = ?
                """,
                (credential["channel_id"],),
            ).fetchall()]

        for route in routes:
            route["enabled"] = bool(route["enabled"])
        available_routes = [route for route in routes if route["upstream_name"] in upstream_set]
        missing_routes = [
            route for route in routes
            if route["enabled"] and route["upstream_name"] not in upstream_set
        ]
        disabled_missing_routes = [
            route for route in routes
            if not route["enabled"] and route["upstream_name"] not in upstream_set
        ]
        routed_upstream = {route["upstream_name"] for route in routes}
        public_name_routes = {route["public_name"]: route for route in channel_routes}
        new_models = []
        for name in names:
            if name in routed_upstream:
                continue
            conflict = public_name_routes.get(name)
            new_models.append({
                "name": name,
                "importable": conflict is None,
                "conflict": None if conflict is None else {
                    "id": conflict["id"],
                    "public_name": conflict["public_name"],
                    "upstream_name": conflict["upstream_name"],
                    "credential_id": conflict["credential_id"],
                    "credential_name": conflict["credential_name"],
                },
            })

        credential_data = dict(credential)
        credential_data["enabled"] = bool(credential_data["enabled"])
        credential_data["channel_enabled"] = bool(credential_data["channel_enabled"])
        return {
            "checked_at": utc_now(),
            "credential": credential_data,
            "upstream_models": names,
            "new_models": new_models,
            "available_routes": available_routes,
            "missing_routes": missing_routes,
            "disabled_missing_routes": disabled_missing_routes,
            "summary": {
                "upstream_models": len(names),
                "routes": len(routes),
                "new_models": len(new_models),
                "importable_models": sum(item["importable"] for item in new_models),
                "available_routes": len(available_routes),
                "missing_routes": len(missing_routes),
                "disabled_missing_routes": len(disabled_missing_routes),
            },
        }

    def add_model(self, data: dict[str, Any]) -> int:
        now = utc_now()
        credential_id = data.get("credential_id")
        with self.connect() as db:
            if credential_id in (None, "", 0, "0"):
                row = db.execute(
                    "SELECT id FROM channel_credentials WHERE channel_id = ? ORDER BY id LIMIT 1",
                    (int(data["channel_id"]),),
                ).fetchone()
                if not row:
                    raise ValueError("该中转站没有分组凭证")
                credential_id = row["id"]
            cursor = db.execute(
                """
                INSERT INTO models(channel_id, credential_id, public_name, upstream_name,
                                   enabled, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (int(data["channel_id"]), int(credential_id), data["public_name"],
                 data["upstream_name"], int(data.get("enabled", True)), now, now),
            )
            return int(cursor.lastrowid)

    def update_model(self, model_id: int, data: dict[str, Any]) -> bool:
        with self.connect() as db:
            cursor = db.execute(
                """
                UPDATE models SET channel_id = ?, credential_id = ?, public_name = ?,
                    upstream_name = ?, enabled = ?, updated_at = ? WHERE id = ?
                """,
                (int(data["channel_id"]), int(data["credential_id"]), data["public_name"],
                 data["upstream_name"], int(data.get("enabled", True)), utc_now(), model_id),
            )
            return cursor.rowcount > 0

    def delete_model(self, model_id: int) -> bool:
        with self.connect() as db:
            cursor = db.execute("DELETE FROM models WHERE id = ?", (model_id,))
            return cursor.rowcount > 0

    def import_models(self, channel_id: int, credential_id: int, model_names: list[str], prefix_on_conflict: str) -> dict[str, Any]:
        imported: list[dict[str, str]] = []
        skipped: list[str] = []
        conflicts: list[str] = []
        now = utc_now()
        with self.connect() as db:
            existing = {
                row["public_name"] for row in db.execute(
                    "SELECT public_name FROM models WHERE channel_id = ?", (channel_id,)
                )
            }
            for upstream_name in model_names:
                public_name = upstream_name
                if public_name in existing:
                    # Same station/model names must never silently move to a
                    # different group.  The UI can edit or resolve explicitly.
                    conflicts.append(upstream_name)
                    continue
                db.execute(
                    """
                    INSERT INTO models(channel_id, credential_id, public_name, upstream_name,
                                       enabled, created_at, updated_at)
                    VALUES(?, ?, ?, ?, 1, ?, ?)
                    """,
                    (channel_id, credential_id, public_name, upstream_name, now, now),
                )
                existing.add(public_name)
                imported.append({"public_name": public_name, "upstream_name": upstream_name})
        return {"imported": imported, "skipped": skipped, "conflicts": conflicts}

    def model_exists(self, public_name: str, channel_id: int) -> bool:
        with self.connect() as db:
            return db.execute(
                "SELECT 1 FROM models WHERE public_name = ? AND channel_id = ?",
                (public_name, channel_id),
            ).fetchone() is not None

    def resolve_route(self, public_name: str, channel_id: int | None = None) -> dict[str, Any] | None:
        with self.connect() as db:
            query = """
                SELECT m.public_name, m.upstream_name, m.enabled AS route_enabled,
                       c.id AS channel_id, c.name, c.base_url, c.enabled AS channel_enabled,
                       cc.id AS credential_id, cc.name AS credential_name,
                       cc.api_key, cc.auth_type, cc.extra_headers,
                       cc.enabled AS credential_enabled
                FROM models m
                JOIN channels c ON c.id = m.channel_id
                JOIN channel_credentials cc ON cc.id = m.credential_id
                WHERE m.public_name = ? AND m.enabled = 1
                  AND c.enabled = 1 AND cc.enabled = 1
            """
            parameters: list[Any] = [public_name]
            if channel_id is not None:
                query += " AND c.id = ?"
                parameters.append(channel_id)
            rows = db.execute(query, parameters).fetchall()
        if len(rows) != 1:
            return None
        item = dict(rows[0])
        item["extra_headers"] = json.loads(item["extra_headers"] or "{}")
        return item

    def route_binding_state(self, public_name: str, channel_id: int) -> str:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT m.enabled AS route_enabled, cc.enabled AS credential_enabled,
                       c.enabled AS channel_enabled
                FROM models m JOIN channels c ON c.id = m.channel_id
                JOIN channel_credentials cc ON cc.id = m.credential_id
                WHERE m.public_name = ? AND c.id = ? LIMIT 1
                """,
                (public_name, channel_id),
            ).fetchone()
        if not row:
            return "missing"
        if not row["route_enabled"]:
            return "route_disabled"
        if not row["credential_enabled"]:
            return "credential_disabled"
        if not row["channel_enabled"]:
            return "channel_disabled"
        return "enabled"

    def route_count(self, public_name: str) -> int:
        with self.connect() as db:
            return db.execute(
                """
                SELECT COUNT(*) AS n FROM models m JOIN channels c ON c.id = m.channel_id
                JOIN channel_credentials cc ON cc.id = m.credential_id
                WHERE m.public_name = ? AND m.enabled = 1 AND c.enabled = 1 AND cc.enabled = 1
                """,
                (public_name,),
            ).fetchone()["n"]

    def public_models(self, channel_id: int | None = None) -> list[dict[str, Any]]:
        with self.connect() as db:
            query = """
                SELECT m.public_name, m.created_at, c.name AS channel_name,
                       cc.name AS credential_name
                FROM models m JOIN channels c ON c.id = m.channel_id
                JOIN channel_credentials cc ON cc.id = m.credential_id
                WHERE m.enabled = 1 AND c.enabled = 1 AND cc.enabled = 1
            """
            params: list[Any] = []
            if channel_id is not None:
                query += " AND c.id = ?"
                params.append(channel_id)
            else:
                query += " GROUP BY m.public_name HAVING COUNT(*) = 1"
            query += " ORDER BY m.public_name COLLATE NOCASE"
            rows = db.execute(query, params).fetchall()
            return [dict(row) for row in rows]

    def add_log(self, data: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO request_logs(created_at, method, path, public_model, upstream_model,
                    channel_name, credential_name, status, first_token_ms, duration_ms, error)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (utc_now(), data.get("method", ""), data.get("path", ""),
                 data.get("public_model", ""), data.get("upstream_model", ""),
                 data.get("channel_name", ""), data.get("credential_name", ""),
                 int(data.get("status", 0)), optional_int(data.get("first_token_ms")),
                 int(data.get("duration_ms", 0)), str(data.get("error", ""))[:1000]),
            )
            try:
                limit = max(0, int(self.get_setting("log_limit", "500")))
            except ValueError:
                limit = 500
            if limit:
                db.execute(
                    "DELETE FROM request_logs WHERE id NOT IN "
                    "(SELECT id FROM request_logs ORDER BY id DESC LIMIT ?)",
                    (limit,),
                )

    def list_logs(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = min(max(limit, 1), 1000)
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM request_logs ORDER BY id DESC LIMIT ?", (limit,)
            )]

    def clear_logs(self) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM request_logs")

    def stats(self) -> dict[str, int]:
        with self.connect() as db:
            values = {
                "channels": db.execute("SELECT COUNT(*) AS n FROM channels").fetchone()["n"],
                "enabled_channels": db.execute("SELECT COUNT(*) AS n FROM channels WHERE enabled=1").fetchone()["n"],
                "credentials": db.execute("SELECT COUNT(*) AS n FROM channel_credentials").fetchone()["n"],
                "enabled_credentials": db.execute("SELECT COUNT(*) AS n FROM channel_credentials WHERE enabled=1").fetchone()["n"],
                "models": db.execute("SELECT COUNT(*) AS n FROM models").fetchone()["n"],
                "enabled_models": db.execute("SELECT COUNT(*) AS n FROM models WHERE enabled=1").fetchone()["n"],
                "requests": db.execute("SELECT COUNT(*) AS n FROM request_logs").fetchone()["n"],
                "access_keys": db.execute("SELECT COUNT(*) AS n FROM access_keys").fetchone()["n"],
                "enabled_access_keys": db.execute("SELECT COUNT(*) AS n FROM access_keys WHERE enabled=1").fetchone()["n"],
            }
        return values
