"""Deterministic capture state machine over durable MT5 observations.

Absence is deliberately NOT a session calendar. Only exact, independently
certified no-bar intervals may be skipped; every other missing slot after a
pinned initialization origin stalls the entire strategy batch. Ticks/M1 can
prove activity, never inactivity.
"""
from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json
from typing import Any, Mapping

from quasartrend.persistence import encode_replay_state
from quasartrend.replay import HistoricalBar, ReplayEngine, Timeframe
from quasartrend.strategy import EventType

from .durable import canonical


DURATIONS = {"m1": 60_000, "m15": 900_000, "h4": 14_400_000}
TIMEFRAMES = {"m15": Timeframe.MINUTES_15, "h4": Timeframe.HOURS_4}
WARMUP_FINALIZED_BARS = 600

# Exact half-open UTC intervals established by broker-native XMGlobal-MT5 9 / GOLD
# history. These are evidence-specific exceptions, not a recurring session rule.
# M1 absence remains diagnostic-only and is deliberately not suppressed here.
CERTIFIED_NO_BAR_INTERVALS = (
    {"certificate_id": "xm9-gold-2026-09-04-daily-closure",
     "source_server": "XMGlobal-MT5 9", "symbol": "GOLD", "timeframes": frozenset(("m15",)),
     "start_ms": 1_788_480_000_000, "end_ms": 1_788_483_600_000},
    {"certificate_id": "xm9-gold-2026-09-04-07-weekend-closure",
     "source_server": "XMGlobal-MT5 9", "symbol": "GOLD", "timeframes": frozenset(("m15", "h4")),
     "start_ms": 1_788_566_400_000, "end_ms": 1_788_742_800_000},
    {"certificate_id": "xm9-gold-2026-09-07-early-closure",
     "source_server": "XMGlobal-MT5 9", "symbol": "GOLD", "timeframes": frozenset(("m15",)),
     "start_ms": 1_788_816_600_000, "end_ms": 1_788_829_200_000},
)


class CaptureBlocked(RuntimeError):
    """A recoverable data blocker; further observations may resolve it."""


def historical(row: Mapping[str, Any]) -> HistoricalBar:
    return HistoricalBar("GOLD", TIMEFRAMES[row["timeframe"]], row["open_time"],
                         row["open"], row["high"], row["low"], row["close"], row["tick_volume"])


def shadow_row(signal: Mapping[str, Any]) -> dict[str, Any]:
    direction = signal["direction"]
    return {"shadow_id": sha256((signal["signal_id"] + ":family-1-long-only").encode()).hexdigest(),
            "decision_timestamp": signal["signal_timestamp"], "source_signal_id": signal["signal_id"],
            "candidate": "Family-1 long_only", "direction": direction,
            "decision": "admit" if direction == "long" else "reject",
            "reason": "long_only" if direction == "short" else "v1_long_opportunity", "context": dict(signal)}


class CaptureMachine:
    """Pure input-to-state/output transformation; it never consults wall time."""
    def __init__(self, *, activation_ms: int, max_signal_lag_ms: int,
                 source_server: str, symbol: str) -> None:
        self.activation_ms, self.max_signal_lag_ms = activation_ms, max_signal_lag_ms
        self.source_server, self.symbol = source_server, symbol
        self.engine = ReplayEngine()
        self.state = self.engine.initial_state("GOLD")
        self.known: dict[str, dict[int, dict[str, Any]]] = {name: {} for name in DURATIONS}
        self.tick_ids: dict[str, dict[str, Any]] = {}
        self.last_tick: int | None = None
        self.latest: dict[str, dict[str, Any] | None] = {"m15": None, "h4": None}
        self.pending_gaps: list[dict[str, Any]] = []
        self.m1_gaps: list[dict[str, Any]] = []
        self.missed_stale_signals = 0
        self.initialized = False
        self.last_observed_ms: int | None = None
        self.observations = 0

    def _certified_no_bar(self, timeframe: str, start_ms: int, end_ms: int) -> bool:
        return any(
            certificate["source_server"] == self.source_server
            and certificate["symbol"] == self.symbol
            and timeframe in certificate["timeframes"]
            and certificate["start_ms"] <= start_ms
            and end_ms <= certificate["end_ms"]
            for certificate in CERTIFIED_NO_BAR_INTERVALS
        )

    def _committed_certified_gap(self, timestamp_ms: int) -> tuple[str, int] | None:
        cursor = self.state.chronology_cursor
        if cursor is None:
            return None
        for name, timeframe in TIMEFRAMES.items():
            rows, duration = self.known[name], DURATIONS[name]
            if not rows or timestamp_ms < min(rows):
                continue
            origin = min(rows)
            stamp = origin + (timestamp_ms - origin) // duration * duration
            if (stamp not in rows
                    and self._certified_no_bar(name, stamp, stamp + duration)
                    and (stamp + duration, timeframe.priority) <= cursor):
                return name, stamp
        return None

    def snapshot(self) -> dict[str, Any]:
        return {"replay": json.loads(encode_replay_state(self.state, expected_config=self.engine.config)),
                "pending_gaps": self.pending_gaps, "initialized": self.initialized,
                "m1_gaps": self.m1_gaps,
                "missed_stale_signals": self.missed_stale_signals,
                "last_observed_ms": self.last_observed_ms, "observations": self.observations}

    def _gaps(self, now_ms: int) -> list[dict[str, Any]]:
        result = []
        evidence_times = [row["finalized_at"] for rows in self.known.values() for row in rows.values()]
        horizon = min(now_ms, max([self.last_tick or 0, *evidence_times], default=0))
        for name in DURATIONS:
            rows = self.known[name]
            warmup_count = sum(row["finalized_at"] <= self.activation_ms for row in rows.values())
            if name in TIMEFRAMES and not self.initialized and warmup_count < WARMUP_FINALIZED_BARS:
                result.append({"timeframe": name, "reason": "incomplete_finalized_warmup",
                               "required": WARMUP_FINALIZED_BARS, "available": warmup_count})
            if not rows:
                continue
            duration, origin = DURATIONS[name], min(rows)
            if any((stamp - origin) % duration for stamp in rows):
                raise ValueError(f"{name} native grid changed; timeframe semantics unresolved")
            # Origin comes from native candles, not Unix UTC-floor assumptions.
            for stamp in range(origin, horizon - duration + 1, duration):
                if stamp in rows:
                    continue
                active = any(stamp <= tick["time_msc"] < stamp + duration for tick in self.tick_ids.values())
                active |= any(stamp <= row["open_time"] < stamp + duration for row in self.known["m1"].values())
                if name == "h4":
                    active |= any(stamp <= row["open_time"] < stamp + duration for row in self.known["m15"].values())
                if not active and self._certified_no_bar(name, stamp, stamp + duration):
                    continue
                result.append({"timeframe": name, "open_time": stamp, "finalized_at": stamp + duration,
                               "reason": "activity_proven_missing_candle" if active else "unresolved_candle_or_session_closure"})
        return result

    def observe(self, observation: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
        now_ms = observation["observed_at"]
        cutoff_ms = observation.get("cutoff_ms", now_ms)
        if type(now_ms) is not int or now_ms < self.activation_ms:
            raise ValueError("observation precedes activation")
        if type(cutoff_ms) is not int or not self.activation_ms <= cutoff_ms <= now_ms:
            raise ValueError("invalid acquisition cutoff")
        if observation.get("activation_ms", self.activation_ms) != self.activation_ms:
            raise ValueError("activation identity changed")
        if self.last_observed_ms is not None and now_ms < self.last_observed_ms:
            raise ValueError("observation clock regressed")
        if observation["observation_id"] != str(self.observations + 1):
            raise ValueError("observation sequence mismatch")
        for timestamp_ms in (
            *(tick["time_msc"] for tick in observation["ticks"]),
            *(row["open_time"] for row in observation["rates"]["m1"]),
        ):
            contradiction = self._committed_certified_gap(timestamp_ms)
            if contradiction is not None:
                name, stamp = contradiction
                raise ValueError(
                    f"activity contradicts committed no-bar certificate: {name}:{stamp}"
                )
        out: dict[str, list[dict[str, Any]]] = {name: [] for name in ("ticks", "m1", "bars", "signals", "shadow", "gaps")}
        for tick in sorted(observation["ticks"], key=lambda row: (row["time_msc"], row["tick_id"])):
            identity = tick["tick_id"]
            if identity in self.tick_ids:
                if self.tick_ids[identity] != tick:
                    raise ValueError("conflicting duplicate tick")
                continue
            if self.last_tick is not None and tick["time_msc"] < self.last_tick:
                raise ValueError("late tick behind committed evidence")
            if tick["time_msc"] > cutoff_ms:
                raise ValueError("future tick")
            if self.last_tick is not None and tick["time_msc"] - self.last_tick > 60_000:
                out["gaps"].append({"gap_id": f"ticks:{self.last_tick}:{tick['time_msc']}", "observed_at": now_ms,
                                    "timeframe": "ticks", "after_open": self.last_tick, "before_open": tick["time_msc"],
                                    "reason": "observed_tick_interval", "gap_ms": tick["time_msc"] - self.last_tick})
            self.tick_ids[identity], self.last_tick = dict(tick), tick["time_msc"]
            out["ticks"].append(dict(tick))
        for name in DURATIONS:
            for row in observation["rates"][name]:
                stamp = row["open_time"]
                if row["timeframe"] != name or row["finalized_at"] != stamp + DURATIONS[name] or row["finalized_at"] > cutoff_ms:
                    raise ValueError("invalid finalized source bar")
                prior = self.known[name].get(stamp)
                if prior is not None:
                    if prior != row:
                        raise ValueError(f"conflicting duplicate finalized {name} bar")
                    continue
                if name in TIMEFRAMES and self.state.chronology_cursor is not None and historical(row).processing_key <= self.state.chronology_cursor:
                    raise ValueError("new historical candle behind committed strategy chronology")
                self.known[name][stamp] = dict(row)
                if name == "m1":
                    out["m1"].append({**row, "observed_at": now_ms})
        if not self.initialized:
            # Exactly the most recent 600 finalized candles at the original
            # activation cutoff, regardless of delayed bootstrap retrieval.
            # Earlier incomplete observations remain in the canonical WAL.
            for name in TIMEFRAMES:
                warmup = sorted(stamp for stamp, row in self.known[name].items() if row["finalized_at"] <= self.activation_ms)
                if len(warmup) >= WARMUP_FINALIZED_BARS:
                    origin = warmup[-WARMUP_FINALIZED_BARS]
                    self.known[name] = {stamp: row for stamp, row in self.known[name].items() if stamp >= origin}
        previous_gaps = self.pending_gaps
        gaps = self._gaps(cutoff_ms)
        self.pending_gaps = [gap for gap in gaps if gap["timeframe"] != "m1"]
        m1_gaps = [gap for gap in gaps if gap["timeframe"] == "m1"]
        if m1_gaps != self.m1_gaps:
            self.m1_gaps = m1_gaps
            out["gaps"].append({"gap_id": f"m1:{observation['observation_id']}", "observed_at": now_ms,
                                "timeframe": "m1", "status": "unresolved" if m1_gaps else "resolved", "pending": m1_gaps})
        if previous_gaps != self.pending_gaps:
            out["gaps"].append({"gap_id": f"strategy:{observation['observation_id']}", "observed_at": now_ms,
                                "timeframe": "strategy", "status": "blocked" if self.pending_gaps else "resolved",
                                "pending": self.pending_gaps})
        if not self.pending_gaps:
            self.initialized = True
            candidates = [row for name in TIMEFRAMES for row in self.known[name].values()
                          if self.state.chronology_cursor is None or historical(row).processing_key > self.state.chronology_cursor]
            for row in sorted(candidates, key=lambda row: historical(row).processing_key):
                bar = historical(row)
                stepped = self.engine.step(self.state, bar)
                self.state = stepped.state
                self.latest[row["timeframe"]] = dict(row)
                out["bars"].append(dict(row))
                for event in stepped.trace.events:
                    if event.type is not EventType.TRADE_OPENED or bar.finalized_at < self.activation_ms:
                        continue
                    if now_ms - bar.finalized_at > self.max_signal_lag_ms:
                        self.missed_stale_signals += 1
                        continue
                    trade = self.state.strategy_state.trade
                    assert trade is not None
                    h4 = self.latest["h4"]
                    signal = {"signal_id": sha256(f"v1:{bar.processing_key}:{trade.trade_id}".encode()).hexdigest(),
                              "signal_timestamp": now_ms, "decision_bar_timestamp": bar.open_time,
                              "strategy_version": "frozen-v1",
                              "config_hash": sha256(canonical({"replay": asdict(self.engine.config), "strategy": asdict(self.engine.strategy_engine.config)}).encode()).hexdigest(),
                              "direction": trade.side.value, "signal_type": "TradeOpened",
                              "armed_or_immediate": "immediate" if trade.setup_origin_timestamp == bar.finalized_at else "armed",
                              "setup_origin_timestamp": trade.setup_origin_timestamp,
                              "finalized_m15": {"open_time": bar.open_time, "finalized_at": bar.finalized_at, "ohlc": [bar.open, bar.high, bar.low, bar.close]},
                              "finalized_h4_bias": None if h4 is None else {**h4, "bias": None if self.state.latest_htf_bias is None else self.state.latest_htf_bias.value},
                              "theoretical_entry": trade.entry_price, "intended_initial_stop": trade.stop_price,
                              "intended_exit_rule": "frozen_v1_bias_reversal_or_hema_or_stop",
                              "state_hash": sha256(canonical(json.loads(encode_replay_state(self.state, expected_config=self.engine.config))).encode()).hexdigest()}
                    out["signals"].append(signal)
                    out["shadow"].append(shadow_row(signal))
        self.last_observed_ms = now_ms
        self.observations += 1
        return out
