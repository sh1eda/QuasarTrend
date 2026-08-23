"""Public immutable records and errors for Phase 4 checkpoint persistence."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import hashlib
import json
import math

from quasartrend.replay import ReplayConfig, ReplayState, Timeframe
from quasartrend.strategy import StrategyConfig


SCHEMA_VERSION = 1
CHECKPOINT_VERSION = 1


class PersistenceError(RuntimeError):
    """Base class for a persistence operation that cannot safely proceed."""


class SchemaVersionError(PersistenceError):
    pass


class CheckpointVersionError(PersistenceError):
    pass


class CheckpointCorruptionError(PersistenceError):
    pass


class CodecError(CheckpointCorruptionError):
    pass


class IdentityError(PersistenceError):
    pass


class ConfigMismatchError(IdentityError):
    pass


class SymbolMismatchError(IdentityError):
    pass


class ChronologyRegressionError(PersistenceError):
    pass


class PersistenceWriteError(PersistenceError):
    pass


def _json_value(value: object) -> object:
    """Turn dataclass/enum configuration values into finite JSON primitives."""

    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise IdentityError("configuration contains a non-finite float")
    return value


def canonical_identity_json(
    symbol: str,
    replay_config: ReplayConfig,
    strategy_config: StrategyConfig,
    execution_timeframe: Timeframe = Timeframe.MINUTES_15,
    htf_timeframe: Timeframe = Timeframe.HOURS_4,
) -> str:
    """Return the canonical, complete behavior identity used for fingerprinting."""

    if not isinstance(symbol, str) or not symbol:
        raise IdentityError("symbol must be a non-empty string")
    if execution_timeframe is not Timeframe.MINUTES_15 or htf_timeframe is not Timeframe.HOURS_4:
        raise IdentityError("Phase 4 supports only the fixed 15m/4h replay topology")
    topology = {
        "execution_timeframe": {
            "duration_ms": execution_timeframe.duration_ms,
            "priority": execution_timeframe.priority,
            "value": execution_timeframe.value,
        },
        "htf_timeframe": {
            "duration_ms": htf_timeframe.duration_ms,
            "priority": htf_timeframe.priority,
            "value": htf_timeframe.value,
        },
    }
    payload = {
        "checkpoint_version": CHECKPOINT_VERSION,
        "identity_format_version": 1,
        "replay_config": _json_value(asdict(replay_config)),
        "schema_version": SCHEMA_VERSION,
        "strategy_config": _json_value(asdict(strategy_config)),
        "symbol": symbol,
        "topology": topology,
    }
    return json.dumps(payload, allow_nan=False, sort_keys=True, separators=(",", ":"))


def fingerprint_identity(
    symbol: str,
    replay_config: ReplayConfig,
    strategy_config: StrategyConfig,
    execution_timeframe: Timeframe = Timeframe.MINUTES_15,
    htf_timeframe: Timeframe = Timeframe.HOURS_4,
) -> str:
    return hashlib.sha256(
        canonical_identity_json(
            symbol, replay_config, strategy_config, execution_timeframe, htf_timeframe
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class PersistenceIdentity:
    """The sole durable identity for one symbol and fixed replay topology."""

    symbol: str
    replay_config: ReplayConfig = field(default_factory=ReplayConfig)
    strategy_config: StrategyConfig = field(default_factory=StrategyConfig)
    execution_timeframe: Timeframe = Timeframe.MINUTES_15
    htf_timeframe: Timeframe = Timeframe.HOURS_4
    config_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "config_fingerprint",
            fingerprint_identity(
                self.symbol,
                self.replay_config,
                self.strategy_config,
                self.execution_timeframe,
                self.htf_timeframe,
            ),
        )

    @property
    def canonical_json(self) -> str:
        return canonical_identity_json(
            self.symbol,
            self.replay_config,
            self.strategy_config,
            self.execution_timeframe,
            self.htf_timeframe,
        )


CheckpointIdentity = PersistenceIdentity


@dataclass(frozen=True, slots=True)
class StoredCheckpoint:
    identity: PersistenceIdentity
    state: ReplayState
    saved_at_ms: int
    last_finalized_at: int | None
    last_priority: int | None
    checkpoint_version: int = CHECKPOINT_VERSION
