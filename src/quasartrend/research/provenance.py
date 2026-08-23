"""Canonical SHA256 identity and provenance helpers."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from hashlib import sha256
import json
from typing import Any

from .models import (
    FEATURE_DEFINITION_VERSION,
    ProvenanceManifest,
    RESEARCH_SCHEMA_VERSION,
    SourceArtifactManifest,
)
from .source import PARSER_ID, parse_tradingview_export
from quasartrend.replay import Timeframe
from .adr import utc_date


def canonical_json(value: Any) -> str:
    def default(item: Any) -> Any:
        if is_dataclass(item):
            return asdict(item)
        if hasattr(item, "value"):
            return item.value
        if isinstance(item, set):
            return sorted(item)
        raise TypeError(f"cannot canonicalize {type(item)!r}")
    return json.dumps(value, default=default, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value: Any) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def source_fingerprint(bars: tuple[Any, ...] | list[Any]) -> str:
    return fingerprint(tuple(
        (
            bar.symbol, bar.timeframe.value, bar.open_time, float(bar.open),
            float(bar.high), float(bar.low), float(bar.close),
            None if bar.volume is None else float(bar.volume),
        )
        for bar in bars
    ))


def setup_identity(*, symbol: str, bias_epoch: int | None, direction: str, setup_origin_timestamp: int, source_processing_key: tuple[int, int], strategy_fingerprint: str) -> str:
    return fingerprint({"schema_namespace": "quasartrend.research", "schema_version": RESEARCH_SCHEMA_VERSION, "identity": "setup", "symbol": symbol,
                        "bias_epoch": bias_epoch, "direction": direction,
                        "setup_origin_timestamp": setup_origin_timestamp,
                        "source_processing_key": source_processing_key,
                        "strategy_fingerprint": strategy_fingerprint})


def event_identity(*, symbol: str, source_processing_key: tuple[int, int], ordinal: int, event_type: str, trade_id: str | None, strategy_fingerprint: str) -> str:
    return fingerprint({"schema_namespace": "quasartrend.research", "schema_version": RESEARCH_SCHEMA_VERSION, "identity": "event", "symbol": symbol,
                        "source_processing_key": source_processing_key, "event_ordinal": ordinal,
                        "event_type": event_type, "trade_id": trade_id,
                        "strategy_fingerprint": strategy_fingerprint})


def make_source_artifact(
    *, declared_symbol: str, timeframe: str, raw_input: bytes,
    parser_id: str = PARSER_ID,
) -> SourceArtifactManifest:
    try:
        stream_timeframe = Timeframe(timeframe)
    except ValueError as error:
        raise ValueError("artifact timeframe must be a supported replay timeframe") from error
    bars = parse_tradingview_export(
        raw_input, declared_symbol=declared_symbol,
        timeframe=stream_timeframe, parser_id=parser_id,
    )
    return SourceArtifactManifest(
        declared_symbol, timeframe, sha256(raw_input).hexdigest(),
        source_fingerprint(bars), len(bars),
        (utc_date(bars[0].open_time), utc_date(bars[-1].open_time)), parser_id,
    )


def make_manifest(
    *, source_artifacts: tuple[SourceArtifactManifest, ...], phase6_sha: str,
    source_description: str, source_reference: str | None, strategy_config: Any,
    replay_config: Any, backtest_config: Any, research_config: Any,
    split_config: Any,
) -> ProvenanceManifest:
    return ProvenanceManifest(
        RESEARCH_SCHEMA_VERSION, source_artifacts, phase6_sha,
        source_description, source_reference, fingerprint(strategy_config),
        fingerprint(replay_config), fingerprint(backtest_config),
        fingerprint(research_config), fingerprint(split_config),
        FEATURE_DEFINITION_VERSION,
    )
