"""Strict, typed, deterministic JSON codec for :class:`ReplayState`."""

from __future__ import annotations

import json
import math
from typing import Any

from quasartrend.indicators import HemaTrend, KalmanStep
from quasartrend.indicators.pine import dumps_checkpoint
from quasartrend.replay import ReplayConfig, ReplayState
from quasartrend.strategy import (
    Direction,
    OpenTrade,
    ReadinessState,
    StrategyState,
    StrategyStatus,
)

from .models import CHECKPOINT_VERSION, CheckpointVersionError, CodecError


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CodecError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise CodecError(f"non-finite JSON constant: {value}")


def _loads(payload: str) -> Any:
    if not isinstance(payload, str):
        raise CodecError("checkpoint payload must be a string")
    try:
        return json.loads(
            payload, object_pairs_hook=_reject_duplicate_pairs, parse_constant=_reject_constant
        )
    except (json.JSONDecodeError, TypeError) as exc:
        raise CodecError("malformed checkpoint JSON") from exc


def _object(value: object, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CodecError(f"{label} must be an object")
    actual = set(value)
    if actual != fields:
        missing = sorted(fields - actual)
        unknown = sorted(actual - fields)
        raise CodecError(f"{label} fields mismatch; missing={missing}, unknown={unknown}")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CodecError(f"{label} must be an integer")
    return value


def _optional_integer(value: object, label: str) -> int | None:
    return None if value is None else _integer(value, label)


def _finite_float(value: object, label: str, *, nullable: bool = False) -> float | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CodecError(f"{label} must be a finite float")
    result = float(value)
    if not math.isfinite(result):
        raise CodecError(f"{label} must be finite")
    return result


def _string(value: object, label: str, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise CodecError(f"{label} must be {'a non-empty ' if nonempty else 'a '}string")
    return value


def _enum(value: object, cls: type[Any], label: str, *, nullable: bool = False) -> Any:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise CodecError(f"{label} must be an enum string")
    try:
        return cls(value)
    except ValueError as exc:
        raise CodecError(f"invalid {label}: {value}") from exc


def _validate_indicator(value: object, kind: str) -> None:
    """Validate every recursive current indicator field before reconstruction."""

    def number(v: object, label: str, *, nullable: bool = True) -> None:
        _finite_float(v, label, nullable=nullable)

    def integer(v: object, label: str) -> None:
        if _integer(v, label) <= 0:
            raise CodecError(f"{label} must be positive")

    def ema(item: object, label: str) -> None:
        row = _object(item, {"type", "version", "length", "value", "seed_values", "observations"}, label)
        if row["type"] not in ("PineEMA", "PineRMA") or _integer(row["version"], f"{label}.version") != 1:
            raise CodecError(f"invalid {label} checkpoint identity")
        integer(row["length"], f"{label}.length")
        number(row["value"], f"{label}.value")
        if not isinstance(row["seed_values"], list):
            raise CodecError(f"{label}.seed_values must be a list")
        for index, seed in enumerate(row["seed_values"]):
            number(seed, f"{label}.seed_values[{index}]", nullable=False)
        if _integer(row["observations"], f"{label}.observations") < 0:
            raise CodecError(f"{label}.observations must be non-negative")

    def hema(item: object, label: str) -> None:
        row = _object(item, {"type", "version", "length", "half_ema", "full_ema", "final_ema"}, label)
        if row["type"] != "HEMA" or _integer(row["version"], f"{label}.version") != 1:
            raise CodecError(f"invalid {label} checkpoint identity")
        integer(row["length"], f"{label}.length")
        ema(row["half_ema"], f"{label}.half_ema")
        ema(row["full_ema"], f"{label}.full_ema")
        ema(row["final_ema"], f"{label}.final_ema")

    if kind == "hema":
        row = _object(value, {"type", "version", "fast_length", "slow_length", "fast", "slow", "previous_fast", "previous_slow"}, "HemaTrend")
        if row["type"] != "HemaTrend" or _integer(row["version"], "HemaTrend.version") != 1:
            raise CodecError("invalid HemaTrend checkpoint identity")
        integer(row["fast_length"], "HemaTrend.fast_length")
        integer(row["slow_length"], "HemaTrend.slow_length")
        hema(row["fast"], "HemaTrend.fast")
        hema(row["slow"], "HemaTrend.slow")
        number(row["previous_fast"], "HemaTrend.previous_fast")
        number(row["previous_slow"], "HemaTrend.previous_slow")
        return
    if kind != "kalman":
        raise AssertionError(kind)
    row = _object(value, {"type", "version", "kalman_period", "kalman_alpha", "kalman_beta", "factor", "atr_period", "kalman", "atr", "previous_k", "previous_atr", "previous_lower_band", "previous_upper_band", "previous_supertrend", "previous_direction"}, "KalmanStep")
    if row["type"] != "KalmanStep" or _integer(row["version"], "KalmanStep.version") != 1:
        raise CodecError("invalid KalmanStep checkpoint identity")
    integer(row["kalman_period"], "KalmanStep.kalman_period")
    number(row["kalman_alpha"], "KalmanStep.kalman_alpha", nullable=False)
    number(row["kalman_beta"], "KalmanStep.kalman_beta", nullable=False)
    number(row["factor"], "KalmanStep.factor", nullable=False)
    integer(row["atr_period"], "KalmanStep.atr_period")
    kalman = _object(row["kalman"], {"type", "version", "period", "alpha", "beta", "v1", "v2", "v3", "v4", "v5", "previous_close"}, "KalmanStep.kalman")
    if kalman["type"] != "KalmanFilter" or _integer(kalman["version"], "KalmanFilter.version") != 1:
        raise CodecError("invalid KalmanFilter checkpoint identity")
    integer(kalman["period"], "KalmanFilter.period")
    for field in ("alpha", "beta", "v2", "v3", "v4"):
        number(kalman[field], f"KalmanFilter.{field}", nullable=False)
    for field in ("v1", "v5", "previous_close"):
        number(kalman[field], f"KalmanFilter.{field}")
    atr = _object(row["atr"], {"type", "version", "length", "previous_close", "rma"}, "KalmanStep.atr")
    if atr["type"] != "PineATR" or _integer(atr["version"], "PineATR.version") != 1:
        raise CodecError("invalid PineATR checkpoint identity")
    integer(atr["length"], "PineATR.length")
    number(atr["previous_close"], "PineATR.previous_close")
    ema(atr["rma"], "PineATR.rma")
    if atr["rma"]["type"] != "PineRMA":
        raise CodecError("PineATR.rma must be PineRMA")
    for field in ("previous_k", "previous_atr", "previous_lower_band", "previous_upper_band", "previous_supertrend", "previous_direction"):
        number(row[field], f"KalmanStep.{field}")


def _indicator_checkpoint(payload: object, kind: str, config: ReplayConfig | None, label: str) -> str:
    raw = _string(payload, label)
    value = _loads(raw)
    _validate_indicator(value, kind)
    canonical = dumps_checkpoint(value)
    if raw != canonical:
        raise CodecError(f"{label} must be canonical strict JSON")
    try:
        if kind == "hema":
            if config is None:
                HemaTrend.from_checkpoint(value)
            elif label == "ltf_hema_checkpoint":
                HemaTrend.from_checkpoint(value, expected_fast_length=config.ltf_hema_fast_length, expected_slow_length=config.ltf_hema_slow_length)
            else:
                HemaTrend.from_checkpoint(value, expected_fast_length=config.htf_hema_fast_length, expected_slow_length=config.htf_hema_slow_length)
        elif config is None:
            KalmanStep.from_checkpoint(value)
        else:
            KalmanStep.from_checkpoint(value, expected_kalman_period=config.kalman_period, expected_kalman_alpha=config.kalman_alpha, expected_kalman_beta=config.kalman_beta, expected_factor=config.kalman_factor, expected_atr_period=config.kalman_atr_period)
    except (KeyError, TypeError, ValueError) as exc:
        raise CodecError(f"invalid {label}") from exc
    return raw


def _strategy_state(value: object) -> StrategyState:
    fields = {"symbol", "status", "current_bias", "previous_bias", "bias_epoch", "bias_activation_timestamp", "current_hema", "previous_hema", "current_kalman", "previous_kalman", "pending_direction", "pending_flip_timestamp", "pending_bias_epoch", "trade", "next_trade_sequence", "last_timestamp", "readiness"}
    row = _object(value, fields, "strategy_state")
    trade: OpenTrade | None
    if row["trade"] is None:
        trade = None
    else:
        trade_row = _object(row["trade"], {"trade_id", "side", "entry_price", "entry_timestamp", "atr_at_entry", "stop_price", "bias_epoch", "setup_origin_timestamp"}, "trade")
        try:
            trade = OpenTrade(
                trade_id=_string(trade_row["trade_id"], "trade.trade_id", nonempty=True),
                side=_enum(trade_row["side"], Direction, "trade.side"),
                entry_price=_finite_float(trade_row["entry_price"], "trade.entry_price", nullable=False),
                entry_timestamp=_integer(trade_row["entry_timestamp"], "trade.entry_timestamp"),
                atr_at_entry=_finite_float(trade_row["atr_at_entry"], "trade.atr_at_entry", nullable=False),
                stop_price=_finite_float(trade_row["stop_price"], "trade.stop_price", nullable=False),
                bias_epoch=_integer(trade_row["bias_epoch"], "trade.bias_epoch"),
                setup_origin_timestamp=_integer(trade_row["setup_origin_timestamp"], "trade.setup_origin_timestamp"),
            )
        except (TypeError, ValueError) as exc:
            raise CodecError("invalid trade") from exc
    try:
        return StrategyState(
            symbol=_string(row["symbol"], "strategy_state.symbol", nonempty=True),
            status=_enum(row["status"], StrategyStatus, "strategy_state.status"),
            current_bias=_enum(row["current_bias"], Direction, "strategy_state.current_bias", nullable=True),
            previous_bias=_enum(row["previous_bias"], Direction, "strategy_state.previous_bias", nullable=True),
            bias_epoch=_integer(row["bias_epoch"], "strategy_state.bias_epoch"),
            bias_activation_timestamp=_optional_integer(row["bias_activation_timestamp"], "strategy_state.bias_activation_timestamp"),
            current_hema=_enum(row["current_hema"], Direction, "strategy_state.current_hema", nullable=True),
            previous_hema=_enum(row["previous_hema"], Direction, "strategy_state.previous_hema", nullable=True),
            current_kalman=_enum(row["current_kalman"], Direction, "strategy_state.current_kalman", nullable=True),
            previous_kalman=_enum(row["previous_kalman"], Direction, "strategy_state.previous_kalman", nullable=True),
            pending_direction=_enum(row["pending_direction"], Direction, "strategy_state.pending_direction", nullable=True),
            pending_flip_timestamp=_optional_integer(row["pending_flip_timestamp"], "strategy_state.pending_flip_timestamp"),
            pending_bias_epoch=_optional_integer(row["pending_bias_epoch"], "strategy_state.pending_bias_epoch"),
            trade=trade,
            next_trade_sequence=_integer(row["next_trade_sequence"], "strategy_state.next_trade_sequence"),
            last_timestamp=_optional_integer(row["last_timestamp"], "strategy_state.last_timestamp"),
            readiness=_enum(row["readiness"], ReadinessState, "strategy_state.readiness"),
        )
    except (TypeError, ValueError) as exc:
        raise CodecError("invalid strategy_state") from exc


def _state_dict(state: ReplayState, config: ReplayConfig | None) -> dict[str, Any]:
    # Round-trip through the same strict decoder so direct calls reject malformed
    # hand-built ReplayState checkpoints before they are persisted.
    encoded = {
        "symbol": state.symbol,
        "strategy_state": {
            "symbol": state.strategy_state.symbol,
            "status": state.strategy_state.status.value,
            "current_bias": None if state.strategy_state.current_bias is None else state.strategy_state.current_bias.value,
            "previous_bias": None if state.strategy_state.previous_bias is None else state.strategy_state.previous_bias.value,
            "bias_epoch": state.strategy_state.bias_epoch,
            "bias_activation_timestamp": state.strategy_state.bias_activation_timestamp,
            "current_hema": None if state.strategy_state.current_hema is None else state.strategy_state.current_hema.value,
            "previous_hema": None if state.strategy_state.previous_hema is None else state.strategy_state.previous_hema.value,
            "current_kalman": None if state.strategy_state.current_kalman is None else state.strategy_state.current_kalman.value,
            "previous_kalman": None if state.strategy_state.previous_kalman is None else state.strategy_state.previous_kalman.value,
            "pending_direction": None if state.strategy_state.pending_direction is None else state.strategy_state.pending_direction.value,
            "pending_flip_timestamp": state.strategy_state.pending_flip_timestamp,
            "pending_bias_epoch": state.strategy_state.pending_bias_epoch,
            "trade": None if state.strategy_state.trade is None else {
                "trade_id": state.strategy_state.trade.trade_id, "side": state.strategy_state.trade.side.value,
                "entry_price": float(state.strategy_state.trade.entry_price), "entry_timestamp": state.strategy_state.trade.entry_timestamp,
                "atr_at_entry": float(state.strategy_state.trade.atr_at_entry), "stop_price": float(state.strategy_state.trade.stop_price),
                "bias_epoch": state.strategy_state.trade.bias_epoch, "setup_origin_timestamp": state.strategy_state.trade.setup_origin_timestamp,
            },
            "next_trade_sequence": state.strategy_state.next_trade_sequence,
            "last_timestamp": state.strategy_state.last_timestamp,
            "readiness": state.strategy_state.readiness.value,
        },
        "ltf_hema_checkpoint": state.ltf_hema_checkpoint,
        "ltf_kalman_checkpoint": state.ltf_kalman_checkpoint,
        "htf_hema_checkpoint": state.htf_hema_checkpoint,
        "latest_htf_bias": None if state.latest_htf_bias is None else state.latest_htf_bias.value,
        "chronology_cursor": None if state.chronology_cursor is None else list(state.chronology_cursor),
    }
    _decode_state(encoded, config)
    return encoded


def _decode_state(value: object, config: ReplayConfig | None) -> ReplayState:
    row = _object(value, {"symbol", "strategy_state", "ltf_hema_checkpoint", "ltf_kalman_checkpoint", "htf_hema_checkpoint", "latest_htf_bias", "chronology_cursor"}, "replay_state")
    symbol = _string(row["symbol"], "replay_state.symbol", nonempty=True)
    strategy_state = _strategy_state(row["strategy_state"])
    cursor: tuple[int, int] | None
    if row["chronology_cursor"] is None:
        cursor = None
    else:
        if not isinstance(row["chronology_cursor"], list) or len(row["chronology_cursor"]) != 2:
            raise CodecError("chronology_cursor must be a two-item array or null")
        cursor = (_integer(row["chronology_cursor"][0], "chronology_cursor[0]"), _integer(row["chronology_cursor"][1], "chronology_cursor[1]"))
        if cursor[1] not in (0, 1):
            raise CodecError("chronology_cursor priority must be 0 or 1")
    try:
        return ReplayState(
            symbol=symbol,
            strategy_state=strategy_state,
            ltf_hema_checkpoint=_indicator_checkpoint(row["ltf_hema_checkpoint"], "hema", config, "ltf_hema_checkpoint"),
            ltf_kalman_checkpoint=_indicator_checkpoint(row["ltf_kalman_checkpoint"], "kalman", config, "ltf_kalman_checkpoint"),
            htf_hema_checkpoint=_indicator_checkpoint(row["htf_hema_checkpoint"], "hema", config, "htf_hema_checkpoint"),
            latest_htf_bias=_enum(row["latest_htf_bias"], Direction, "latest_htf_bias", nullable=True),
            chronology_cursor=cursor,
        )
    except (TypeError, ValueError) as exc:
        raise CodecError("invalid replay_state") from exc


def encode_replay_state(state: ReplayState, *, expected_config: ReplayConfig | None = None) -> str:
    if not isinstance(state, ReplayState):
        raise CodecError("state must be ReplayState")
    envelope = {"checkpoint_version": CHECKPOINT_VERSION, "state": _state_dict(state, expected_config)}
    return json.dumps(envelope, allow_nan=False, sort_keys=True, separators=(",", ":"))


def decode_replay_state(payload: str, *, expected_config: ReplayConfig | None = None) -> ReplayState:
    envelope = _object(_loads(payload), {"checkpoint_version", "state"}, "checkpoint envelope")
    version = _integer(envelope["checkpoint_version"], "checkpoint_version")
    if version != CHECKPOINT_VERSION:
        raise CheckpointVersionError(f"unsupported checkpoint version: {version}")
    state = _decode_state(envelope["state"], expected_config)
    if encode_replay_state(state, expected_config=expected_config) != payload:
        raise CodecError("checkpoint payload must be canonical JSON")
    return state
