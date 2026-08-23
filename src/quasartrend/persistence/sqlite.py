"""Small crash-safe SQLite persistence adapter for immutable replay state."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time
from urllib.parse import quote

from quasartrend.replay import ReplayState

from .codec import decode_replay_state, encode_replay_state
from .models import (
    CHECKPOINT_VERSION,
    SCHEMA_VERSION,
    CheckpointCorruptionError,
    ChronologyRegressionError,
    ConfigMismatchError,
    PersistenceError,
    PersistenceIdentity,
    PersistenceWriteError,
    SchemaVersionError,
    StoredCheckpoint,
    SymbolMismatchError,
)


_CREATE_TABLE = """
CREATE TABLE checkpoints (
    symbol TEXT NOT NULL,
    execution_timeframe TEXT NOT NULL,
    htf_timeframe TEXT NOT NULL,
    config_fingerprint TEXT NOT NULL,
    checkpoint_version INTEGER NOT NULL,
    saved_at_ms INTEGER NOT NULL,
    last_finalized_at INTEGER NULL,
    last_priority INTEGER NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY(symbol, execution_timeframe, htf_timeframe),
    CHECK ((last_finalized_at IS NULL) = (last_priority IS NULL))
)
"""


class SQLiteCheckpointStore:
    """A side-effect-free handle with short-lived SQLite connections."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def save_checkpoint(self, identity: PersistenceIdentity, state: ReplayState) -> StoredCheckpoint:
        self._validate_identity_state(identity, state)
        state_payload = encode_replay_state(state, expected_config=identity.replay_config)
        cursor = state.chronology_cursor
        saved_at_ms = time.time_ns() // 1_000_000
        payload = json.dumps(
            {
                "checkpoint_version": CHECKPOINT_VERSION,
                "config_fingerprint": identity.config_fingerprint,
                "last_finalized_at": None if cursor is None else cursor[0],
                "last_priority": None if cursor is None else cursor[1],
                "state": json.loads(state_payload)["state"],
                "symbol": state.symbol,
            },
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        # Save is the only operation permitted to create directories/files.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.path, isolation_level=None)
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            self._initialize_or_validate(connection)
            prior = connection.execute(
                "SELECT config_fingerprint, last_finalized_at, last_priority FROM checkpoints WHERE symbol=? AND execution_timeframe=? AND htf_timeframe=?",
                self._slot(identity),
            ).fetchone()
            if prior is not None:
                if not isinstance(prior[0], str) or prior[0] != identity.config_fingerprint:
                    raise ConfigMismatchError("checkpoint slot has a different configuration fingerprint")
                prior_cursor = self._cursor_from_columns(prior[1], prior[2])
                if prior_cursor is not None and (cursor is None or cursor < prior_cursor):
                    raise ChronologyRegressionError("checkpoint cursor regresses the stored slot")
            connection.execute(
                """INSERT INTO checkpoints (
                    symbol, execution_timeframe, htf_timeframe, config_fingerprint,
                    checkpoint_version, saved_at_ms, last_finalized_at, last_priority, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, execution_timeframe, htf_timeframe) DO UPDATE SET
                    config_fingerprint=excluded.config_fingerprint,
                    checkpoint_version=excluded.checkpoint_version,
                    saved_at_ms=excluded.saved_at_ms,
                    last_finalized_at=excluded.last_finalized_at,
                    last_priority=excluded.last_priority,
                    payload=excluded.payload""",
                (*self._slot(identity), identity.config_fingerprint, CHECKPOINT_VERSION, saved_at_ms,
                 None if cursor is None else cursor[0], None if cursor is None else cursor[1], payload),
            )
            connection.execute("COMMIT")
        except PersistenceError:
            if connection is not None:
                self._rollback(connection)
            raise
        except sqlite3.Error as exc:
            if connection is not None:
                self._rollback(connection)
            raise PersistenceWriteError("SQLite checkpoint save failed") from exc
        finally:
            if connection is not None:
                connection.close()
        return StoredCheckpoint(identity, state, saved_at_ms, None if cursor is None else cursor[0], None if cursor is None else cursor[1])

    def load_checkpoint(self, identity: PersistenceIdentity) -> StoredCheckpoint | None:
        if not self.path.exists():
            return None
        connection = self._open_existing()
        try:
            if not self._validate_existing(connection):
                return None
            row = connection.execute(
                """SELECT symbol, execution_timeframe, htf_timeframe, config_fingerprint, checkpoint_version, saved_at_ms,
                          last_finalized_at, last_priority, payload
                   FROM checkpoints WHERE symbol=? AND execution_timeframe=? AND htf_timeframe=?""",
                self._slot(identity),
            ).fetchone()
            if row is None:
                return None
            symbol, execution_timeframe, htf_timeframe, fingerprint, version, saved_at, last_at, last_priority, payload = row
            if not all(isinstance(value, str) for value in (symbol, execution_timeframe, htf_timeframe, fingerprint, payload)):
                raise CheckpointCorruptionError("checkpoint row text metadata is invalid")
            if (symbol, execution_timeframe, htf_timeframe) != self._slot(identity):
                raise CheckpointCorruptionError("checkpoint row slot metadata is invalid")
            if isinstance(version, bool) or not isinstance(version, int):
                from .models import CheckpointVersionError
                raise CheckpointVersionError("checkpoint row version is not an integer")
            if isinstance(saved_at, bool) or not isinstance(saved_at, int):
                raise CheckpointCorruptionError("checkpoint saved_at_ms is not an integer")
            if fingerprint != identity.config_fingerprint:
                raise ConfigMismatchError("checkpoint slot has a different configuration fingerprint")
            if version != CHECKPOINT_VERSION:
                from .models import CheckpointVersionError
                raise CheckpointVersionError(f"unsupported row checkpoint version: {version}")
            state = self._decode_stored_payload(payload, identity, last_at, last_priority)
            return StoredCheckpoint(identity, state, saved_at, last_at, last_priority, version)
        except sqlite3.Error as exc:
            raise CheckpointCorruptionError("unable to read checkpoint database") from exc
        finally:
            connection.close()

    def delete_checkpoint(self, identity: PersistenceIdentity) -> bool:
        if not self.path.exists():
            return False
        connection = self._open_existing()
        try:
            if not self._validate_existing(connection):
                return False
            row = connection.execute(
                "SELECT config_fingerprint FROM checkpoints WHERE symbol=? AND execution_timeframe=? AND htf_timeframe=?",
                self._slot(identity),
            ).fetchone()
            if row is None:
                return False
            if not isinstance(row[0], str) or row[0] != identity.config_fingerprint:
                raise ConfigMismatchError("checkpoint slot has a different configuration fingerprint")
            connection.execute("BEGIN IMMEDIATE")
            deleted = connection.execute(
                "DELETE FROM checkpoints WHERE symbol=? AND execution_timeframe=? AND htf_timeframe=?",
                self._slot(identity),
            ).rowcount
            connection.execute("COMMIT")
            return deleted == 1
        except sqlite3.Error as exc:
            self._rollback(connection)
            raise PersistenceWriteError("SQLite checkpoint deletion failed") from exc
        finally:
            connection.close()

    def _open_existing(self) -> sqlite3.Connection:
        uri = f"file:{quote(str(self.path.resolve()))}?mode=rw"
        try:
            return sqlite3.connect(uri, uri=True, isolation_level=None)
        except sqlite3.Error as exc:
            raise CheckpointCorruptionError("unable to open existing checkpoint database") from exc

    @staticmethod
    def _slot(identity: PersistenceIdentity) -> tuple[str, str, str]:
        return (identity.symbol, identity.execution_timeframe.value, identity.htf_timeframe.value)

    @staticmethod
    def _validate_identity_state(identity: PersistenceIdentity, state: ReplayState) -> None:
        if not isinstance(identity, PersistenceIdentity):
            raise TypeError("identity must be PersistenceIdentity")
        if state.symbol != identity.symbol:
            raise SymbolMismatchError("state symbol must match persistence identity")

    @staticmethod
    def _cursor_from_columns(last_at: object, priority: object) -> tuple[int, int] | None:
        if last_at is None and priority is None:
            return None
        if isinstance(last_at, bool) or isinstance(priority, bool) or not isinstance(last_at, int) or not isinstance(priority, int):
            raise CheckpointCorruptionError("invalid checkpoint cursor columns")
        if priority not in (0, 1):
            raise CheckpointCorruptionError("invalid checkpoint cursor priority")
        return (last_at, priority)

    def _initialize_or_validate(self, connection: sqlite3.Connection) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if version == 0 and not tables:
            connection.execute(_CREATE_TABLE)
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            return
        if version != SCHEMA_VERSION:
            raise SchemaVersionError(f"unsupported database schema version: {version}")
        self._assert_schema(connection)

    def _validate_existing(self, connection: sqlite3.Connection) -> bool:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if version == 0 and not tables:
            # An existing zero-byte/empty SQLite file is explicit absence on
            # read paths.  Only save is authorized to initialize it.
            return False
        if version != SCHEMA_VERSION:
            raise SchemaVersionError(f"unsupported database schema version: {version}")
        self._assert_schema(connection)
        return True

    @staticmethod
    def _assert_schema(connection: sqlite3.Connection) -> None:
        columns = connection.execute("PRAGMA table_info(checkpoints)").fetchall()
        expected = (
            ("symbol", "TEXT", 1, 1), ("execution_timeframe", "TEXT", 1, 2),
            ("htf_timeframe", "TEXT", 1, 3), ("config_fingerprint", "TEXT", 1, 0),
            ("checkpoint_version", "INTEGER", 1, 0), ("saved_at_ms", "INTEGER", 1, 0),
            ("last_finalized_at", "INTEGER", 0, 0), ("last_priority", "INTEGER", 0, 0),
            ("payload", "TEXT", 1, 0),
        )
        actual = tuple((row[1], row[2].upper(), row[3], row[5]) for row in columns)
        if actual != expected:
            raise SchemaVersionError("checkpoint table schema is incompatible")
        schema_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='checkpoints'"
        ).fetchone()
        if schema_row is None or not isinstance(schema_row[0], str):
            raise SchemaVersionError("checkpoint table definition is unavailable")
        normalized = "".join(schema_row[0].upper().split())
        if (
            "PRIMARYKEY(SYMBOL,EXECUTION_TIMEFRAME,HTF_TIMEFRAME)" not in normalized
            or "CHECK((LAST_FINALIZED_ATISNULL)=(LAST_PRIORITYISNULL))" not in normalized
        ):
            raise SchemaVersionError("checkpoint table constraints are incompatible")

    def _decode_stored_payload(self, payload: object, identity: PersistenceIdentity, last_at: object, last_priority: object) -> ReplayState:
        if not isinstance(payload, str):
            raise CheckpointCorruptionError("checkpoint payload is not text")
        try:
            envelope = json.loads(payload, object_pairs_hook=self._no_duplicate, parse_constant=self._bad_constant)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise CheckpointCorruptionError("malformed stored checkpoint payload") from exc
        expected_fields = {"checkpoint_version", "config_fingerprint", "last_finalized_at", "last_priority", "state", "symbol"}
        if not isinstance(envelope, dict) or set(envelope) != expected_fields:
            raise CheckpointCorruptionError("stored checkpoint payload fields are invalid")
        if json.dumps(envelope, allow_nan=False, sort_keys=True, separators=(",", ":")) != payload:
            raise CheckpointCorruptionError("stored checkpoint payload must be canonical JSON")
        if isinstance(envelope["checkpoint_version"], bool) or not isinstance(envelope["checkpoint_version"], int) or envelope["checkpoint_version"] != CHECKPOINT_VERSION:
            from .models import CheckpointVersionError
            raise CheckpointVersionError("stored payload checkpoint version is unsupported")
        if not isinstance(envelope["config_fingerprint"], str):
            raise CheckpointCorruptionError("stored payload configuration fingerprint is invalid")
        if envelope["config_fingerprint"] != identity.config_fingerprint:
            raise ConfigMismatchError("stored payload configuration fingerprint mismatches")
        cursor = self._cursor_from_columns(last_at, last_priority)
        for name in ("last_finalized_at", "last_priority"):
            value = envelope[name]
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise CheckpointCorruptionError(f"stored payload {name} is invalid")
        if envelope["last_finalized_at"] != (None if cursor is None else cursor[0]) or envelope["last_priority"] != (None if cursor is None else cursor[1]):
            raise CheckpointCorruptionError("stored payload cursor differs from metadata")
        state_payload = json.dumps({"checkpoint_version": CHECKPOINT_VERSION, "state": envelope["state"]}, allow_nan=False, sort_keys=True, separators=(",", ":"))
        state = decode_replay_state(state_payload, expected_config=identity.replay_config)
        if not isinstance(envelope["symbol"], str) or envelope["symbol"] != state.symbol or state.symbol != identity.symbol:
            raise SymbolMismatchError("stored checkpoint symbol mismatches identity")
        if state.chronology_cursor != cursor:
            raise CheckpointCorruptionError("stored checkpoint cursor differs from replay state")
        return state

    @staticmethod
    def _no_duplicate(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    @staticmethod
    def _bad_constant(value: str) -> None:
        raise ValueError(f"non-finite constant: {value}")

    @staticmethod
    def _rollback(connection: sqlite3.Connection) -> None:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass
