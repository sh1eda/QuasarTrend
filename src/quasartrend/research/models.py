"""Immutable, provenance-bound observational Phase 7 records."""
from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import date
from enum import Enum

from quasartrend.backtest import BacktestConfig
from quasartrend.replay import ReplayConfig
from quasartrend.strategy import Direction, StrategyConfig

RESEARCH_SCHEMA_VERSION = "phase7-research/v2"
FEATURE_DEFINITION_VERSION = "phase7-entry-context/v2"
MAE_MFE_CONVENTION_VERSION = "subsequent_15m_through_exit_bar_ohlc/v1"
PHASE6_SHA = "086be5546075ff49f980ce1a73ed44c10b3ae3a9"

@dataclass(frozen=True, slots=True)
class ResearchConfig:
    adr_definition: str = "ADR_14_UTC_PREVIOUS_COMPLETE_DATES"
    utc_boundary: str = "00:00 UTC"
    source_open_feature_convention: str = "source_open_time"
    decision_split_convention: str = "finalized_at"
    mae_mfe_convention: str = MAE_MFE_CONVENTION_VERSION
    phase6_sha: str = PHASE6_SHA
    final_oos_closed_trade_floor: int = 50
    setup_retention_floor: float = .25
    production_lineage_enabled: bool = False

    def __post_init__(self) -> None:
        expected = {
            "adr_definition": "ADR_14_UTC_PREVIOUS_COMPLETE_DATES",
            "utc_boundary": "00:00 UTC",
            "source_open_feature_convention": "source_open_time",
            "decision_split_convention": "finalized_at",
            "mae_mfe_convention": MAE_MFE_CONVENTION_VERSION,
            "phase6_sha": PHASE6_SHA,
        }
        for name, value in expected.items():
            if getattr(self, name) != value:
                raise ValueError(f"Phase 7 {name} is fixed at {value!r}")
        if (
            isinstance(self.final_oos_closed_trade_floor, bool)
            or not isinstance(self.final_oos_closed_trade_floor, int)
            or self.final_oos_closed_trade_floor <= 0
        ):
            raise ValueError("final OOS closed-trade floor must be a positive integer")
        if not 0.0 < self.setup_retention_floor <= 1.0:
            raise ValueError("setup retention floor must be in (0, 1]")
        if self.production_lineage_enabled:
            raise ValueError("production lineage remains disabled in this Phase 7 scope")

@dataclass(frozen=True, slots=True)
class SplitConfig:
    development_start: str = "2026-05-15"; development_end: str = "2026-07-09"
    validation_start: str = "2026-07-10"; validation_end: str = "2026-07-28"
    final_oos_start: str = "2026-07-29"; final_oos_end: str = "2026-08-16"

    def __post_init__(self) -> None:
        windows = (
            (date.fromisoformat(self.development_start), date.fromisoformat(self.development_end)),
            (date.fromisoformat(self.validation_start), date.fromisoformat(self.validation_end)),
            (date.fromisoformat(self.final_oos_start), date.fromisoformat(self.final_oos_end)),
        )
        for start, end in windows:
            if start > end:
                raise ValueError("split start must not be after split end")
        for (_, prior_end), (next_start, _) in zip(windows, windows[1:]):
            if next_start <= prior_end:
                raise ValueError("Phase 7 splits must be chronological and non-overlapping")

class FieldClass(str, Enum):
    ENTRY_TIME_FEATURE = "entry_time_feature"
    POST_ENTRY_OUTCOME = "post_entry_outcome"
    IDENTITY_METADATA = "identity_metadata"

class AdrStatus(str, Enum):
    AVAILABLE = "available"; WARMUP = "warmup"; INCOMPLETE_PRIOR_SESSION = "incomplete_prior_session"; MALFORMED_INPUT = "malformed_input"

class SessionStatus(str, Enum):
    COMPLETE_PREFIX = "complete_prefix"; INCOMPLETE_PREFIX = "incomplete_prefix"

class SetupStatus(str, Enum):
    REJECTED = "rejected"; ARMED = "armed"; CANCELLED = "cancelled"; OPENED = "opened"

@dataclass(frozen=True, slots=True)
class AdrContext:
    session_date: str; adr: float | None; status: AdrStatus; complete_prior_sessions: int

@dataclass(frozen=True, slots=True)
class SetupRow:
    schema_version: str; setup_id: str; symbol: str; direction: Direction
    source_open_timestamp: int; finalized_timestamp: int; decision_timestamp: int; source_processing_key: tuple[int, int]
    setup_origin_timestamp: int; strategy_fingerprint: str
    htf_bias: Direction | None; bias_epoch: int | None; htf_bias_age_ms: int | None
    kalman_transition_age_ms: int | None; kalman_persistence_bars: int | None
    setup_reference_price: float; atr_at_setup: float | None
    utc_hour: int; utc_weekday: int; utc_six_hour_bucket: int; ms_since_utc_open: int
    session_status: SessionStatus; session_observed_bar_count: int
    session_open_available: float | None; session_high_available: float | None; session_low_available: float | None
    adr: float | None; adr_status: AdrStatus; adr_extension: float | None; atr_extension: float | None
    eligible_baseline_setup: bool; was_armed: bool; setup_status: SetupStatus; linked_trade_id: str | None; resolution_reasons: tuple[str, ...]
    event_id: str

@dataclass(frozen=True, slots=True)
class TradeRow:
    schema_version: str; trade_id: str; setup_id: str; entry_event_id: str; exit_event_id: str | None
    symbol: str; direction: Direction; source_open_timestamp: int; finalized_timestamp: int; decision_timestamp: int; source_processing_key: tuple[int, int]
    setup_origin_timestamp: int; bias_epoch: int; strategy_fingerprint: str
    htf_bias: Direction | None; htf_bias_age_ms: int | None; setup_age_ms: int
    kalman_transition_age_ms: int | None; kalman_persistence_bars: int | None
    atr_at_entry: float; canonical_entry_price: float; canonical_stop_price: float; canonical_risk_per_unit: float
    quantity: float | None; execution_entry_price: float | None
    utc_hour: int; utc_weekday: int; utc_six_hour_bucket: int; ms_since_utc_open: int
    session_status: SessionStatus; session_observed_bar_count: int
    session_open_available: float | None; session_high_available: float | None; session_low_available: float | None
    adr: float | None; adr_status: AdrStatus; adr_extension: float | None; atr_extension: float | None
    abs_stop_distance: float; stop_atr_ratio: float; stop_adr_ratio: float | None
    outcome_state: str; exit_timestamp: int | None; exit_source_open_timestamp: int | None; exit_finalized_timestamp: int | None
    canonical_exit_price: float | None; execution_exit_price: float | None; exit_primary_reason: str | None; exit_all_reasons: tuple[str, ...]
    stop_hit: bool | None; strategy_exit: bool | None; gross_pnl: float | None; net_pnl: float | None; entry_fee: float | None; exit_fee: float | None; total_fees: float | None
    realized_r: float | None; mae: float | None; mfe: float | None; mae_r: float | None; mfe_r: float | None
    observed_duration_bars: int | None; expected_duration_bars: int | None; elapsed_duration_ms: int | None; mae_mfe_convention_version: str
    data_quality_flags: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class SourceArtifactManifest:
    declared_symbol: str; timeframe: str; raw_input_sha256: str; normalized_content_sha256: str
    row_count: int; date_range: tuple[str | None, str | None]; parser_id: str
    identity_status: str = "declared_unverified"

@dataclass(frozen=True, slots=True)
class ProvenanceManifest:
    schema_version: str; source_artifacts: tuple[SourceArtifactManifest, ...]
    phase6_sha: str; source_description: str; source_reference: str | None
    strategy_fingerprint: str; replay_fingerprint: str; backtest_fingerprint: str; research_fingerprint: str; split_fingerprint: str
    feature_definition_version: str = FEATURE_DEFINITION_VERSION

@dataclass(frozen=True, slots=True)
class ResearchBuildContext:
    manifest: ProvenanceManifest; raw_inputs: tuple[tuple[str, bytes], ...]
    replay_config: ReplayConfig; strategy_config: StrategyConfig; backtest_config: BacktestConfig
    research_config: object; split_config: object

@dataclass(frozen=True, slots=True)
class ResearchDataset:
    schema_version: str; manifest: ProvenanceManifest; manifest_id: str
    replay_fingerprint: str; strategy_fingerprint: str; backtest_fingerprint: str; research_fingerprint: str; split_fingerprint: str
    setup_rows: tuple[SetupRow, ...]; trade_rows: tuple[TradeRow, ...]

@dataclass(frozen=True, slots=True)
class ResearchMetrics:
    eligible_setups: int; opened_trades: int; closed_trades: int; retained_setups: int | None; retained_trades: int | None
    setup_retention: float | None; trade_retention: float | None; total_r: float; expectancy_r: float | None; r_per_setup: float | None
    profit_factor: float | None; stop_rate: float | None; win_rate: float | None; mean_r: float | None; median_r: float | None
    mae_observation_count: int; mfe_observation_count: int
    mean_mae_r: float | None; mean_mfe_r: float | None; mean_duration_ms: float | None
    mean_observed_duration_bars: float | None = None
    mean_expected_duration_bars: float | None = None

@dataclass(frozen=True, slots=True)
class CandidateComparison:
    window_role: str; window_start_ms: int; window_end_ms: int; baseline: ResearchMetrics; candidate: ResearchMetrics
    expectancy_delta: float | None; total_r_delta: float; r_per_setup_delta: float | None; profit_factor_delta: float | None; stop_rate_delta: float | None
    retained_setup_delta: int | None; retained_trade_delta: int | None; economically_improved: bool

@dataclass(frozen=True, slots=True)
class CandidateEvidence:
    production_eligible: bool; reasons: tuple[str, ...]; final_oos_closed_trades: int; setup_retention: float | None
    oos_windows: int; improved_windows: int; aggregate_total_r_ratio: float | None

def schema_map() -> dict[str, dict[str, FieldClass]]:
    entry = {"direction", "htf_bias", "bias_epoch", "htf_bias_age_ms", "setup_reference_price", "atr_at_setup", "atr_at_entry", "kalman_transition_age_ms", "kalman_persistence_bars", "utc_hour", "utc_weekday", "utc_six_hour_bucket", "ms_since_utc_open", "session_status", "session_observed_bar_count", "session_open_available", "session_high_available", "session_low_available", "adr", "adr_status", "adr_extension", "atr_extension", "canonical_entry_price", "canonical_stop_price", "canonical_risk_per_unit", "abs_stop_distance", "stop_atr_ratio", "stop_adr_ratio", "setup_age_ms"}
    outcome = {"eligible_baseline_setup", "was_armed", "setup_status", "linked_trade_id", "resolution_reasons", "outcome_state", "exit_timestamp", "exit_source_open_timestamp", "exit_finalized_timestamp", "canonical_exit_price", "execution_exit_price", "exit_primary_reason", "exit_all_reasons", "stop_hit", "strategy_exit", "gross_pnl", "net_pnl", "entry_fee", "exit_fee", "total_fees", "realized_r", "mae", "mfe", "mae_r", "mfe_r", "observed_duration_bars", "expected_duration_bars", "elapsed_duration_ms", "data_quality_flags"}
    return {table: {item.name: FieldClass.ENTRY_TIME_FEATURE if item.name in entry else FieldClass.POST_ENTRY_OUTCOME if item.name in outcome else FieldClass.IDENTITY_METADATA for item in fields(row)} for table, row in (("setup", SetupRow), ("trade", TradeRow))}

def validate_entry_feature_selectors(table: str, selectors: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    mapping = schema_map()
    if table not in mapping: raise ValueError("table must be 'setup' or 'trade'")
    selected = tuple(selectors)
    if len(selected) != len(set(selected)): raise ValueError("feature selectors must be unique")
    bad = [x for x in selected if mapping[table].get(x) is not FieldClass.ENTRY_TIME_FEATURE]
    if bad: raise ValueError(f"non-entry-time feature selector(s): {', '.join(bad)}")
    return selected
