"""Frozen Phase 7.2 setup-origin trend-regime feature reconstruction.

This module is intentionally observational.  It consumes the canonical replay
trace and setup identities without changing replay, strategy, or accounting
semantics.  It produces all 523 source-history HEMA-flip setup snapshots for
causal warm-up only; it contains no outcome fields and MUST NOT be bucketed
directly.  Diagnosis must role-filter development before deriving edges or
joining outcomes.  No validation or final-OOS outcomes are accessed here.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path

from quasartrend.indicators import Candle, HemaRelation, HemaTrend, KalmanStep, TrendDirection
from quasartrend.replay import HistoricalBar, ReplayTrace, Timeframe
from quasartrend.strategy import Direction, EventType, ReasonCode, StrategyBar

from .adr import adr_contexts, utc_date
from .models import FieldClass
from .pipeline import CanonicalResearchBundle
from .provenance import fingerprint


REGIME_FEATURE_VERSION = "phase7.2-regime-setup-origin-features/v1"
REGIME_FEATURE_DEFINITION_SCHEMA_VERSION = "phase7.2-regime-setup-origin-feature-definitions/v1"
PHASE7_1_BASE_SHA = "a2df0b86d89319d08b8440d4ef2ba40bee42d56d"
_BAR_MS = 900_000
SETUP_ORIGIN_ANCHOR = "setup_origin"
_SETUP_ORIGIN_TIMESTAMP = "canonical SetupRow.decision_timestamp at the HEMA-flip origin"
_SOURCE_SCOPE = (
    "Full source-history SetupRow HEMA-flip snapshots for causal warm-up only; "
    "definitions and feature reconstruction access no TradeRow or outcome field. "
    "The full dataset fingerprint is an opaque immutable baseline-provenance binding."
)
_EXPECTED_BINDINGS = {
    "manifest_id": "812d4449ce31615324f021533ce8d4492f9f49ffc98bd1879e823add7991b39c",
    "dataset_fingerprint": "96fb832135bef85123bcca522e6a7836aed58531e73158870c68c030ec9e1982",
    "strategy_fingerprint": "bb8fdc3cda4c39b43a09d8fb6a95a05077d35a01e14e0718a6af75ef21f1f0e6",
    "replay_fingerprint": "54452d8b4a309209586684b6ed356d4b7344f560d2025f1f0c4ad2d001e80312",
    "backtest_fingerprint": "596a0f6010107a8a3d9f3a4cace7cbb828110af3b1d9d37c58558cc3bb2b40d3",
    "research_fingerprint": "54e5635d41f26fd935e3ebd1e93a865c014566c683bda0f9838809fe7c929f33",
    "split_fingerprint": "0d807d81cf3ef72d5fa84a40666c0f2f360da059dcb46188cdad33397aebef5a",
}
_EXPECTED_SOURCE_ARTIFACTS = (
    {
        "declared_symbol": "BTCUSDT", "timeframe": "15m",
        "raw_input_sha256": "f5d251aa0e04616b8b74e222b8cb99b7eb9f37c74125815c58453c963727a657",
        "normalized_content_sha256": "6d50aef93fc0e213f05ae1d141493c4802d0c8dd333ee423be80266a1f7b382f",
        "row_count": 10452, "date_range": ("2026-05-01", "2026-08-17"),
        "parser_id": "tradingview-dual-ohlc-csv/v1", "identity_status": "declared_unverified",
    },
    {
        "declared_symbol": "BTCUSDT", "timeframe": "4h",
        "raw_input_sha256": "e463dc26298df500b39ac618e58a59fe1644cb8515ffcbd39181477a2990082b",
        "normalized_content_sha256": "90726fa09988a4acec01ace0e592903ee93dbfd7540852331feeef95df28b46e",
        "row_count": 8480, "date_range": ("2022-10-04", "2026-08-17"),
        "parser_id": "tradingview-dual-ohlc-csv/v1", "identity_status": "declared_unverified",
    },
)
_EXPECTED_SOURCE_COUNTS = (("15m", 10452), ("4h", 8480))


@dataclass(frozen=True, slots=True)
class FeatureDefinition:
    """One immutable setup-origin definition.

    ``ENTRY_TIME_FEATURE`` here means legally available at the canonical
    HEMA-flip setup-origin decision, not a later trade-entry observation.
    """

    name: str
    mathematical_definition: str
    lookback: str
    normalization: str
    decision_timestamp: str
    missing_behavior: str
    warmup_behavior: str
    source_state: str
    field_class: FieldClass
    anchor: str = SETUP_ORIGIN_ANCHOR
    feature_version: str = REGIME_FEATURE_VERSION


FEATURE_DEFINITIONS = (
    FeatureDefinition(
        "hema_fast_slope_atr_8",
        "side_sign * (fast_hema[t] - fast_hema[t-8]) / (8 * ATR[t])",
        "8 finalized 15m bars from t-8 through t",
        "directional fast-HEMA change per bar divided by current ATR",
        _SETUP_ORIGIN_TIMESTAMP,
        "null when either HEMA value is nonfinite/unavailable, ATR is missing/nonfinite/nonpositive, or t-8..t has a 15m gap",
        "null until the current finalized 15m bar has an eight-bar predecessor with consecutive 15m spacing",
        "reconstructed 15m HemaTrend fast value; current canonical StrategyBar ATR; SetupRow direction",
        FieldClass.ENTRY_TIME_FEATURE,
    ),
    *(
        FeatureDefinition(
            f"directional_efficiency_{lookback}",
            "abs(close[t] - close[t-N]) / sum(abs(close[i] - close[i-1]), i=t-N+1..t)",
            f"{lookback} finalized 15m close-to-close changes",
            "unitless directional displacement divided by total path length",
            _SETUP_ORIGIN_TIMESTAMP,
            "null before N finalized close changes or when t-N..t has a 15m gap; exactly 0.0 when the denominator is exactly zero",
            f"null until {lookback} finalized close changes with consecutive 15m spacing are available",
            "canonical finalized 15m HistoricalBar closes",
            FieldClass.ENTRY_TIME_FEATURE,
        )
        for lookback in (8, 16, 32)
    ),
    FeatureDefinition(
        "hema_flip_count_16",
        "count(HEMA flip events over finalized 15m bars t-15..t, inclusive)",
        "exactly 16 finalized 15m bars, current included",
        "none; event count",
        _SETUP_ORIGIN_TIMESTAMP,
        "null before 16 finalized bars or when t-15..t has a 15m gap",
        "null until 16 finalized 15m bars with consecutive spacing are available",
        "current and historical canonical StrategyBar.hema_flip events",
        FieldClass.ENTRY_TIME_FEATURE,
    ),
    FeatureDefinition(
        "kalman_flip_count_16",
        "count(Kalman transition events over finalized 15m bars t-15..t, inclusive)",
        "exactly 16 finalized 15m bars, current included",
        "none; event count",
        _SETUP_ORIGIN_TIMESTAMP,
        "null before 16 finalized bars or when t-15..t has a 15m gap",
        "null until 16 finalized 15m bars with consecutive spacing are available",
        "current and historical canonical StrategyBar.kalman_transition events",
        FieldClass.ENTRY_TIME_FEATURE,
    ),
    FeatureDefinition(
        "combined_flip_count_16",
        "hema_flip_count_16 + kalman_flip_count_16",
        "exactly 16 finalized 15m bars, current included",
        "none; exact sum of the two component event counts",
        _SETUP_ORIGIN_TIMESTAMP,
        "null before either 16-bar component count is available, including a t-15..t 15m gap",
        "null until 16 finalized 15m bars with consecutive spacing are available",
        "derived only from the two frozen 16-bar flip-count features",
        FieldClass.ENTRY_TIME_FEATURE,
    ),
    FeatureDefinition(
        "kalman_persistence_bars",
        "SetupRow.kalman_persistence_bars without reconstruction or transformation",
        "as recorded at the setup decision timestamp",
        "none; exact canonical value reuse",
        _SETUP_ORIGIN_TIMESTAMP,
        "null remains null",
        "the canonical SetupRow warm-up/missing state is retained exactly",
        "canonical SetupRow.kalman_persistence_bars",
        FieldClass.ENTRY_TIME_FEATURE,
    ),
    FeatureDefinition(
        "hema_kalman_aligned",
        "StrategyBar.hema_direction == StrategyBar.kalman_direction",
        "current finalized 15m bar only",
        "categorical boolean agreement",
        _SETUP_ORIGIN_TIMESTAMP,
        "null when either current direction is unavailable",
        "null until both current finalized direction states are available",
        "current canonical StrategyBar HEMA and Kalman directions",
        FieldClass.ENTRY_TIME_FEATURE,
    ),
    FeatureDefinition(
        "htf_hema_aligned",
        "StrategyBar.htf_bias == StrategyBar.hema_direction",
        "legally latest finalized 4H bias and current finalized 15m HEMA state",
        "categorical boolean agreement",
        _SETUP_ORIGIN_TIMESTAMP,
        "null when either legally current direction is unavailable",
        "null until both current finalized direction states are available",
        "current canonical StrategyBar.htf_bias and StrategyBar.hema_direction",
        FieldClass.ENTRY_TIME_FEATURE,
    ),
    FeatureDefinition(
        "atr_adr_ratio",
        "ATR[t] / ADR_14(previous complete UTC dates only)",
        "current ATR and prior 14 complete UTC-date ADR",
        "current ATR divided by prior-only 14-date UTC ADR",
        _SETUP_ORIGIN_TIMESTAMP,
        "null when ATR or ADR is missing, nonfinite, or nonpositive",
        "null until a complete prior-14-UTC-date ADR is available",
        "current canonical StrategyBar ATR and adr_contexts(canonical finalized 15m bars)",
        FieldClass.ENTRY_TIME_FEATURE,
    ),
)

FEATURE_DEFINITION_FINGERPRINT = fingerprint(FEATURE_DEFINITIONS)


@dataclass(frozen=True, slots=True)
class SetupRegimeFeatureRow:
    """Setup-origin identity metadata and the eleven frozen entry-time values.

    These are HEMA-flip ``SetupRow`` snapshots, not later ``TradeRow`` entry
    observations.  Any future candidate must treat them as a setup-origin
    reject/accept gate and must not reinterpret them as trade-entry features.
    """

    setup_id: str
    symbol: str
    direction: Direction
    source_open_timestamp: int
    finalized_timestamp: int
    decision_timestamp: int
    source_processing_key: tuple[int, int]
    hema_fast_slope_atr_8: float | None
    directional_efficiency_8: float | None
    directional_efficiency_16: float | None
    directional_efficiency_32: float | None
    hema_flip_count_16: int | None
    kalman_flip_count_16: int | None
    combined_flip_count_16: int | None
    kalman_persistence_bars: int | None
    hema_kalman_aligned: bool | None
    htf_hema_aligned: bool | None
    atr_adr_ratio: float | None


@dataclass(frozen=True, slots=True)
class FeatureDefinitionArtifact:
    """Path-independent serialized binding for the frozen feature family."""

    schema_version: str
    feature_version: str
    anchor: str
    source_scope: str
    phase7_1_base_sha: str
    manifest_id: str
    dataset_fingerprint: str
    strategy_fingerprint: str
    replay_fingerprint: str
    backtest_fingerprint: str
    research_fingerprint: str
    split_fingerprint: str
    source_artifacts: tuple[object, ...]
    source_counts: tuple[tuple[str, int], ...]
    definition_fingerprint: str
    definitions: tuple[FeatureDefinition, ...]


def _hema_direction(relation: HemaRelation) -> Direction | None:
    if relation is HemaRelation.ABOVE:
        return Direction.LONG
    if relation is HemaRelation.BELOW:
        return Direction.SHORT
    return None


def _kalman_direction(direction: TrendDirection) -> Direction:
    return Direction.LONG if direction is TrendDirection.BULLISH else Direction.SHORT


def _validate_canonical_bundle_binding(bundle: CanonicalResearchBundle) -> None:
    """Refuse all non-frozen inputs before producing Phase 7.2 observations."""
    dataset = bundle.dataset
    observed = {
        "manifest_id": dataset.manifest_id,
        "dataset_fingerprint": fingerprint(dataset),
        "strategy_fingerprint": fingerprint(bundle.strategy_config),
        "replay_fingerprint": fingerprint(bundle.replay_config),
        "backtest_fingerprint": fingerprint(bundle.backtest_config),
        "research_fingerprint": fingerprint(bundle.research_config),
        "split_fingerprint": fingerprint(bundle.split_config),
    }
    for name, expected in _EXPECTED_BINDINGS.items():
        if observed[name] != expected:
            raise ValueError(f"canonical Phase 7.1 bundle binding mismatch: {name}")
    stored = {
        "strategy_fingerprint": dataset.strategy_fingerprint,
        "replay_fingerprint": dataset.replay_fingerprint,
        "backtest_fingerprint": dataset.backtest_fingerprint,
        "research_fingerprint": dataset.research_fingerprint,
        "split_fingerprint": dataset.split_fingerprint,
    }
    for name, value in stored.items():
        if value != observed[name] or getattr(dataset.manifest, name) != observed[name]:
            raise ValueError(f"canonical Phase 7.1 stored/live provenance mismatch: {name}")
    artifacts = tuple(asdict(artifact) for artifact in dataset.manifest.source_artifacts)
    if artifacts != _EXPECTED_SOURCE_ARTIFACTS:
        raise ValueError("canonical Phase 7.1 source artifact binding mismatch")
    if bundle.source_counts != _EXPECTED_SOURCE_COUNTS:
        raise ValueError("canonical Phase 7.1 source count binding mismatch")


def _finite_positive(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) and result > 0.0 else None


def directional_efficiency(
    closes: tuple[float, ...] | list[float], index: int, lookback: int,
    *, open_times: tuple[int, ...] | list[int],
) -> float | None:
    """Return the fixed causal directional-efficiency statistic at ``index``."""
    if lookback <= 0:
        raise ValueError("lookback must be positive")
    if index < 0 or index >= len(closes):
        raise IndexError("index must identify a close")
    if len(open_times) != len(closes):
        raise ValueError("open_times must align one-for-one with closes")
    if index < lookback:
        return None
    if open_times is not None and not _has_consecutive_15m(open_times, index, lookback):
        return None
    current = float(closes[index])
    prior = float(closes[index - lookback])
    values = tuple(float(closes[i]) for i in range(index - lookback, index + 1))
    if not all(math.isfinite(value) for value in values):
        return None
    denominator = sum(abs(values[offset] - values[offset - 1]) for offset in range(1, len(values)))
    if denominator == 0.0:
        return 0.0
    result = abs(current - prior) / denominator
    return result if math.isfinite(result) else None


def hema_fast_slope_atr(
    fast_values: tuple[float, ...] | list[float],
    index: int,
    direction: Direction,
    atr: float | None,
    *, open_times: tuple[int, ...] | list[int],
) -> float | None:
    """Return the frozen directional fast-HEMA slope/ATR statistic at ``index``."""
    if not isinstance(direction, Direction):
        raise TypeError("direction must be a Direction")
    if index < 0 or index >= len(fast_values):
        raise IndexError("index must identify a fast HEMA value")
    if len(open_times) != len(fast_values):
        raise ValueError("open_times must align one-for-one with fast_values")
    if index < 8:
        return None
    if not _has_consecutive_15m(open_times, index, 8):
        return None
    normalized_atr = _finite_positive(atr)
    current = float(fast_values[index])
    prior = float(fast_values[index - 8])
    if normalized_atr is None or not math.isfinite(current) or not math.isfinite(prior):
        return None
    sign = 1.0 if direction is Direction.LONG else -1.0
    result = sign * (current - prior) / (8.0 * normalized_atr)
    return result if math.isfinite(result) else None


def _has_consecutive_15m(
    open_times: tuple[int, ...] | list[int], index: int, lookback: int,
) -> bool:
    if lookback <= 0:
        raise ValueError("lookback must be positive")
    if index < lookback or index >= len(open_times):
        return False
    window = open_times[index - lookback : index + 1]
    return len(window) == lookback + 1 and all(
        isinstance(timestamp, int) and not isinstance(timestamp, bool)
        for timestamp in window
    ) and all(
        window[offset] - window[offset - 1] == _BAR_MS
        for offset in range(1, len(window))
    )


def _window_flip_counts(
    strategy_bars: tuple[StrategyBar, ...], index: int,
    *, open_times: tuple[int, ...] | list[int],
) -> tuple[int | None, int | None, int | None]:
    if index < 15 or not _has_consecutive_15m(open_times, index, 15):
        return None, None, None
    window = strategy_bars[index - 15 : index + 1]
    hema = sum(bar.hema_flip is not None for bar in window)
    kalman = sum(bar.kalman_transition is not None for bar in window)
    return hema, kalman, hema + kalman


def flip_counts_16(
    strategy_bars: tuple[StrategyBar, ...] | list[StrategyBar], index: int,
    *, open_times: tuple[int, ...] | list[int],
) -> tuple[int | None, int | None, int | None]:
    """Return causal 16-bar HEMA, Kalman, and exact combined event counts."""
    bars = tuple(strategy_bars)
    if index < 0 or index >= len(bars):
        raise IndexError("index must identify a StrategyBar")
    if len(open_times) != len(bars):
        raise ValueError("open_times must align one-for-one with strategy_bars")
    return _window_flip_counts(bars, index, open_times=open_times)


def _validate_trace(
    trace: ReplayTrace,
    reconstructed_hema_direction: Direction | None,
    reconstructed_hema_flip: Direction | None,
    reconstructed_kalman_direction: Direction,
    reconstructed_kalman_transition: Direction | None,
    reconstructed_atr: float | None,
) -> StrategyBar:
    bar = trace.source_bar
    strategy_bar = trace.strategy_bar
    if bar.timeframe is not Timeframe.MINUTES_15 or strategy_bar is None:
        raise ValueError("regime features require a canonical finalized 15m strategy trace")
    if strategy_bar.timestamp != bar.finalized_at:
        raise ValueError("15m StrategyBar timestamp does not match source finalization")
    if (
        strategy_bar.hema_direction is not reconstructed_hema_direction
        or strategy_bar.hema_flip is not reconstructed_hema_flip
    ):
        raise ValueError(
            "reconstructed 15m HEMA direction/flip disagrees with canonical StrategyBar"
        )
    if (
        strategy_bar.kalman_direction is not reconstructed_kalman_direction
        or strategy_bar.kalman_transition is not reconstructed_kalman_transition
        or not _same_optional_float(strategy_bar.atr, reconstructed_atr)
    ):
        raise ValueError(
            "reconstructed 15m Kalman direction/transition/ATR disagrees with canonical StrategyBar"
        )
    _validate_emitted_indicator_events(trace, strategy_bar)
    return strategy_bar


def _same_optional_float(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return left is right
    return math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-12)


def _validate_emitted_indicator_events(trace: ReplayTrace, strategy_bar: StrategyBar) -> None:
    expected_hema = () if strategy_bar.hema_flip is None else (strategy_bar.hema_flip,)
    expected_kalman = () if strategy_bar.kalman_transition is None else (strategy_bar.kalman_transition,)
    hema_events = tuple(event for event in trace.events if event.type is EventType.HEMA_FLIP_DETECTED)
    kalman_events = tuple(event for event in trace.events if event.type is EventType.KALMAN_TRANSITION_DETECTED)
    if not _valid_indicator_events(
        hema_events, expected_hema, trace, ReasonCode.HEMA_FLIP_DETECTED,
    ):
        raise ValueError("canonical HEMA StrategyBar/ReplayTrace event mismatch")
    if not _valid_indicator_events(
        kalman_events, expected_kalman, trace, ReasonCode.KALMAN_TRANSITION_DETECTED,
    ):
        raise ValueError("canonical Kalman StrategyBar/ReplayTrace event mismatch")


def _valid_indicator_events(
    events: tuple[object, ...], expected_sides: tuple[Direction, ...],
    trace: ReplayTrace, expected_reason: ReasonCode,
) -> bool:
    if len(events) != len(expected_sides):
        return False
    return all(
        event.symbol == trace.source_bar.symbol
        and event.timestamp == trace.source_bar.finalized_at
        and event.type in (EventType.HEMA_FLIP_DETECTED, EventType.KALMAN_TRANSITION_DETECTED)
        and event.side is expected_side
        and event.reason is expected_reason
        and event.trade_id is None
        and event.price is None
        and event.reasons == ()
        and event.metadata == ()
        for event, expected_side in zip(events, expected_sides)
    )


def _validate_htf_bias_reconstruction(bundle: CanonicalResearchBundle) -> None:
    """Reconstruct legal 4H bias in full replay order before any LTF feature use."""
    htf = HemaTrend(
        bundle.replay_config.htf_hema_fast_length,
        bundle.replay_config.htf_hema_slow_length,
    )
    latest_bias: Direction | None = None
    prior_key: tuple[int, int] | None = None
    for trace in bundle.replay.traces:
        source = trace.source_bar
        if prior_key is not None and source.processing_key <= prior_key:
            raise ValueError("canonical replay traces must be strict processing order")
        prior_key = source.processing_key
        if source.timeframe is Timeframe.HOURS_4:
            result = htf.update(Candle(
                source.open, source.high, source.low, source.close,
                float("nan") if source.volume is None else source.volume, source.open_time,
            ))
            latest_bias = _hema_direction(result.relation)
            if trace.strategy_bar is not None:
                raise ValueError("4H replay trace must not have a StrategyBar")
        elif source.timeframe is not Timeframe.MINUTES_15:
            raise ValueError("regime features require only canonical 15m/4H replay traces")
        if trace.htf_bias_after_update is not latest_bias:
            raise ValueError("reconstructed legal 4H bias disagrees with ReplayTrace")
        if source.timeframe is Timeframe.MINUTES_15:
            if trace.strategy_bar is None or trace.strategy_bar.htf_bias is not latest_bias:
                raise ValueError("reconstructed legal 4H bias disagrees with 15m StrategyBar")


def _canonical_15m(
    bundle: CanonicalResearchBundle,
) -> tuple[tuple[HistoricalBar, ...], tuple[StrategyBar, ...], tuple[float, ...]]:
    traces = tuple(
        trace for trace in bundle.replay.traces
        if trace.source_bar.timeframe is Timeframe.MINUTES_15
    )
    if not traces:
        raise ValueError("regime features require canonical finalized 15m traces")
    prior_open: int | None = None
    hema = HemaTrend(
        bundle.replay_config.ltf_hema_fast_length,
        bundle.replay_config.ltf_hema_slow_length,
    )
    kalman = KalmanStep(
        kalman_period=bundle.replay_config.kalman_period,
        kalman_alpha=bundle.replay_config.kalman_alpha,
        kalman_beta=bundle.replay_config.kalman_beta,
        factor=bundle.replay_config.kalman_factor,
        atr_period=bundle.replay_config.kalman_atr_period,
    )
    bars: list[HistoricalBar] = []
    strategy_bars: list[StrategyBar] = []
    fast_values: list[float] = []
    for trace in traces:
        source = trace.source_bar
        if prior_open is not None and source.open_time <= prior_open:
            raise ValueError("canonical 15m traces must be strictly chronological")
        prior_open = source.open_time
        candle = Candle(
            source.open, source.high, source.low, source.close,
            float("nan") if source.volume is None else source.volume, source.open_time,
        )
        result = hema.update(candle)
        kalman_result = kalman.update(candle)
        hema_direction = _hema_direction(result.relation)
        hema_flip = (
            Direction.LONG if result.bullish_cross else
            Direction.SHORT if result.bearish_cross else None
        )
        kalman_transition = (
            Direction.LONG if kalman_result.bullish_transition else
            Direction.SHORT if kalman_result.bearish_transition else None
        )
        reconstructed_atr = kalman_result.atr if math.isfinite(kalman_result.atr) else None
        strategy_bars.append(_validate_trace(
            trace, hema_direction, hema_flip,
            _kalman_direction(kalman_result.semantic_direction), kalman_transition,
            reconstructed_atr,
        ))
        bars.append(source)
        fast_values.append(result.fast.value)
    return tuple(bars), tuple(strategy_bars), tuple(fast_values)


def build_setup_regime_feature_rows(
    bundle: CanonicalResearchBundle,
) -> tuple[SetupRegimeFeatureRow, ...]:
    """Build setup-origin feature rows for canonical HEMA-flip decisions only."""
    _validate_canonical_bundle_binding(bundle)
    _validate_htf_bias_reconstruction(bundle)
    bars, strategy_bars, fast_values = _canonical_15m(bundle)
    setups = bundle.dataset.setup_rows
    expected_order = tuple(sorted(
        setups,
        key=lambda row: (row.decision_timestamp, row.source_processing_key, row.setup_id),
    ))
    if setups != expected_order or len({row.setup_id for row in setups}) != len(setups):
        raise ValueError("canonical setup identity/order is invalid")
    index_by_decision = {bar.finalized_at: index for index, bar in enumerate(bars)}
    if len(index_by_decision) != len(bars):
        raise ValueError("canonical 15m finalization timestamps must be unique")
    adr_by_date = adr_contexts(bars)
    closes = tuple(bar.close for bar in bars)
    open_times = tuple(bar.open_time for bar in bars)
    rows: list[SetupRegimeFeatureRow] = []
    for setup in setups:
        index = index_by_decision.get(setup.decision_timestamp)
        if index is None:
            raise ValueError("setup decision timestamp lacks a canonical 15m bar")
        source = bars[index]
        state = strategy_bars[index]
        if (
            setup.symbol != source.symbol
            or setup.source_open_timestamp != source.open_time
            or setup.finalized_timestamp != source.finalized_at
            or setup.source_processing_key != source.processing_key
            or setup.direction is not state.hema_flip
        ):
            raise ValueError("setup identity does not match its canonical HEMA-flip decision trace")

        atr = _finite_positive(state.atr)
        slope = hema_fast_slope_atr(
            fast_values, index, setup.direction, atr, open_times=open_times,
        )

        hema_count, kalman_count, combined_count = _window_flip_counts(
            strategy_bars, index, open_times=open_times,
        )
        adr = adr_by_date[utc_date(source.open_time)].adr
        adr_value = _finite_positive(adr)
        atr_adr = None if atr is None or adr_value is None else atr / adr_value
        if atr_adr is not None and not math.isfinite(atr_adr):
            atr_adr = None
        hema_kalman = (
            None if state.hema_direction is None or state.kalman_direction is None
            else state.hema_direction is state.kalman_direction
        )
        htf_hema = (
            None if state.htf_bias is None or state.hema_direction is None
            else state.htf_bias is state.hema_direction
        )
        rows.append(SetupRegimeFeatureRow(
            setup.setup_id, setup.symbol, setup.direction, setup.source_open_timestamp,
            setup.finalized_timestamp, setup.decision_timestamp,
            setup.source_processing_key, slope,
            directional_efficiency(closes, index, 8, open_times=open_times),
            directional_efficiency(closes, index, 16, open_times=open_times),
            directional_efficiency(closes, index, 32, open_times=open_times),
            hema_count, kalman_count, combined_count,
            setup.kalman_persistence_bars, hema_kalman, htf_hema, atr_adr,
        ))
    return tuple(rows)


def feature_definition_artifact(bundle: CanonicalResearchBundle) -> FeatureDefinitionArtifact:
    """Return the frozen-definition artifact bound to this canonical dataset."""
    _validate_canonical_bundle_binding(bundle)
    return FeatureDefinitionArtifact(
        REGIME_FEATURE_DEFINITION_SCHEMA_VERSION,
        REGIME_FEATURE_VERSION,
        SETUP_ORIGIN_ANCHOR,
        _SOURCE_SCOPE,
        PHASE7_1_BASE_SHA,
        bundle.dataset.manifest_id,
        fingerprint(bundle.dataset),
        bundle.dataset.strategy_fingerprint,
        bundle.dataset.replay_fingerprint,
        bundle.dataset.backtest_fingerprint,
        bundle.dataset.research_fingerprint,
        bundle.dataset.split_fingerprint,
        tuple(bundle.dataset.manifest.source_artifacts),
        bundle.source_counts,
        FEATURE_DEFINITION_FINGERPRINT,
        FEATURE_DEFINITIONS,
    )


def validate_regime_feature_artifact(artifact: FeatureDefinitionArtifact) -> FeatureDefinitionArtifact:
    """Reject stale, mutated, noncanonical, or non-setup-origin artifacts."""
    if not isinstance(artifact, FeatureDefinitionArtifact):
        raise TypeError("a FeatureDefinitionArtifact is required")
    if (
        artifact.schema_version != REGIME_FEATURE_DEFINITION_SCHEMA_VERSION
        or artifact.feature_version != REGIME_FEATURE_VERSION
        or artifact.anchor != SETUP_ORIGIN_ANCHOR
        or artifact.source_scope != _SOURCE_SCOPE
        or artifact.phase7_1_base_sha != PHASE7_1_BASE_SHA
        or artifact.definition_fingerprint != FEATURE_DEFINITION_FINGERPRINT
        or artifact.definitions != FEATURE_DEFINITIONS
    ):
        raise ValueError("regime feature artifact definition/version/anchor mismatch")
    if any(
        definition.anchor != SETUP_ORIGIN_ANCHOR
        or definition.feature_version != REGIME_FEATURE_VERSION
        or definition.field_class is not FieldClass.ENTRY_TIME_FEATURE
        for definition in artifact.definitions
    ):
        raise ValueError("regime feature artifact contains a non-setup-origin definition")
    observed = {
        "manifest_id": artifact.manifest_id,
        "dataset_fingerprint": artifact.dataset_fingerprint,
        "strategy_fingerprint": artifact.strategy_fingerprint,
        "replay_fingerprint": artifact.replay_fingerprint,
        "backtest_fingerprint": artifact.backtest_fingerprint,
        "research_fingerprint": artifact.research_fingerprint,
        "split_fingerprint": artifact.split_fingerprint,
    }
    if any(observed[name] != expected for name, expected in _EXPECTED_BINDINGS.items()):
        raise ValueError("regime feature artifact canonical binding mismatch")
    artifacts = tuple(asdict(source) for source in artifact.source_artifacts)
    if artifacts != _EXPECTED_SOURCE_ARTIFACTS or artifact.source_counts != _EXPECTED_SOURCE_COUNTS:
        raise ValueError("regime feature artifact source binding mismatch")
    return artifact


def validate_regime_feature_selectors(
    selectors: tuple[str, ...] | list[str], artifact: FeatureDefinitionArtifact,
) -> tuple[str, ...]:
    """Accept only the immutable, entry-time Phase 7.2 feature names."""
    validate_regime_feature_artifact(artifact)
    if fingerprint(FEATURE_DEFINITIONS) != FEATURE_DEFINITION_FINGERPRINT:
        raise ValueError("regime feature definition fingerprint mismatch")
    selected = tuple(selectors)
    if len(selected) != len(set(selected)):
        raise ValueError("regime feature selectors must be unique")
    known = {definition.name for definition in FEATURE_DEFINITIONS}
    unknown = tuple(name for name in selected if not isinstance(name, str) or name not in known)
    if unknown:
        raise ValueError(f"unknown regime feature selector(s): {', '.join(map(str, unknown))}")
    return selected


def feature_definition_json(value: FeatureDefinitionArtifact) -> bytes:
    """Strict, deterministic, newline-terminated JSON for the definition artifact."""
    return json.dumps(
        asdict(value), sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8") + b"\n"


def write_feature_definition_artifact(
    value: FeatureDefinitionArtifact | CanonicalResearchBundle,
    path: Path,
    *,
    overwrite: bool = False,
) -> None:
    """Write the artifact once; existing paths require explicit replacement."""
    artifact = (
        feature_definition_artifact(value)
        if isinstance(value, CanonicalResearchBundle) else value
    )
    if not isinstance(artifact, FeatureDefinitionArtifact):
        raise TypeError("value must be a FeatureDefinitionArtifact or CanonicalResearchBundle")
    validate_regime_feature_artifact(artifact)
    if path.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite existing artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(feature_definition_json(artifact))
