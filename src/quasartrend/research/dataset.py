"""Strict, full-history Phase 7 dataset construction."""
from __future__ import annotations
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
import math
from typing import Any

from quasartrend.backtest import BacktestEngine, BacktestResult, ClosedTrade
from quasartrend.replay import HistoricalBar, ReplayEngine, ReplayResult, ReplayTrace, Timeframe
from quasartrend.strategy import Direction, EventType
from .adr import BAR_MS, adr_contexts, utc_date, validate_canonical_15m_bars
from .models import (AdrStatus, MAE_MFE_CONVENTION_VERSION, PHASE6_SHA, RESEARCH_SCHEMA_VERSION, ResearchBuildContext, ResearchDataset, SessionStatus, SetupRow, SetupStatus, TradeRow)
from .provenance import event_identity, fingerprint, setup_identity, source_fingerprint
from .source import parse_tradingview_export, validate_canonical_source_bars

def _parts(ts: int) -> tuple[int, int, int, int]:
    d = datetime.fromtimestamp(ts / 1000, UTC); z = d.replace(hour=0, minute=0, second=0, microsecond=0)
    return d.hour, d.weekday(), d.hour // 6, int((d-z).total_seconds()*1000)


def source_open_utc_features(source_open_timestamp: int) -> tuple[int, int, int, int]:
    """Return source-open hour, weekday, six-hour bucket, and UTC-day offset."""
    return _parts(source_open_timestamp)


def calculate_excursions(
    *, direction: Direction, entry_price: float, stop_price: float,
    subsequent_bars: tuple[Any, ...] | list[Any],
) -> tuple[float, float, float, float]:
    """MAE/MFE over bars after entry through the full exit-bar OHLC inclusive."""
    if not isinstance(direction, Direction):
        raise TypeError("direction must be a Direction")
    if not all(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and math.isfinite(value)
        for value in (entry_price, stop_price)
    ):
        raise ValueError("entry and stop must be finite numeric values")
    bars = tuple(subsequent_bars)
    prior: int | None = None
    symbol: str | None = None
    for bar in bars:
        if not isinstance(bar, HistoricalBar) or bar.timeframe is not Timeframe.MINUTES_15:
            raise TypeError("excursion bars must be canonical 15m HistoricalBar values")
        if symbol is None:
            symbol = bar.symbol
        elif bar.symbol != symbol:
            raise ValueError("excursion bars must have one symbol")
        if prior is not None and bar.open_time <= prior:
            raise ValueError("excursion bars must be strictly chronological")
        prior = bar.open_time
        if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close):
            raise ValueError("excursion bar OHLC envelope is malformed")
    risk = abs(entry_price - stop_price)
    if not math.isfinite(risk) or risk <= 0:
        raise ValueError("entry/stop risk must be finite and positive")
    low = min((bar.low for bar in bars), default=entry_price)
    high = max((bar.high for bar in bars), default=entry_price)
    mae = max(0.0, entry_price - low) if direction is Direction.LONG else max(0.0, high - entry_price)
    mfe = max(0.0, high - entry_price) if direction is Direction.LONG else max(0.0, entry_price - low)
    return mae, mfe, mae / risk, mfe / risk

def _meta(event: Any, key: str) -> Any | None: return dict(event.metadata).get(key)

def _validate_build(replay: ReplayResult, backtest: BacktestResult, context: ResearchBuildContext) -> tuple[str, str, str, str, str]:
    if not isinstance(context, ResearchBuildContext): raise TypeError("explicit provenance-bound ResearchBuildContext is required")
    m = context.manifest
    if m.phase6_sha != PHASE6_SHA: raise ValueError("research requires the exact frozen Phase 6 SHA")
    if m.schema_version != RESEARCH_SCHEMA_VERSION or m.feature_definition_version != "phase7-entry-context/v2": raise ValueError("manifest schema/feature-definition version mismatch")
    if not m.source_description: raise ValueError("manifest source description must be non-empty")
    rf, sf, bf, qf, xf = map(fingerprint, (context.replay_config, context.strategy_config, context.backtest_config, context.research_config, context.split_config))
    if (m.replay_fingerprint, m.strategy_fingerprint, m.backtest_fingerprint, m.research_fingerprint, m.split_fingerprint) != (rf, sf, bf, qf, xf): raise ValueError("manifest configuration fingerprint mismatch")
    traces = replay.traces
    prior = None; seen_event = set(); opened = set(); closed = set()
    for trace in traces:
        bar = trace.source_bar; key = bar.processing_key
        if prior is not None and key <= prior: raise ValueError("replay traces must be strict global processing order")
        prior = key
        if trace.post_state.symbol != bar.symbol: raise ValueError("trace state symbol mismatch")
        if trace.strategy_bar is not None:
            sb = trace.strategy_bar
            if (sb.symbol, sb.timestamp, sb.open, sb.high, sb.low, sb.close) != (bar.symbol, bar.finalized_at, bar.open, bar.high, bar.low, bar.close): raise ValueError("strategy bar/source bar mismatch")
        for ordinal, event in enumerate(trace.events):
            ek = (key, ordinal)
            if ek in seen_event or event.timestamp != bar.finalized_at or event.symbol != bar.symbol: raise ValueError("duplicate or inconsistent replay event")
            seen_event.add(ek)
            if event.type is EventType.TRADE_OPENED:
                if event.trade_id is None or event.trade_id in opened: raise ValueError("duplicate/missing opened trade id")
                opened.add(event.trade_id)
            if event.type is EventType.TRADE_CLOSED:
                if event.trade_id is None or event.trade_id in closed or event.trade_id not in opened: raise ValueError("duplicate/unknown closed trade id")
                closed.add(event.trade_id)
    ltf = tuple(t.source_bar for t in traces if t.source_bar.timeframe is Timeframe.MINUTES_15)
    htf = tuple(t.source_bar for t in traces if t.source_bar.timeframe is Timeframe.HOURS_4)
    validate_canonical_15m_bars(ltf)
    if not ltf: raise ValueError("research requires canonical 15m source bars")
    if not htf: raise ValueError("research requires canonical 4H source bars")
    inputs = dict(context.raw_inputs)
    if len(context.raw_inputs) != 2 or len(inputs) != 2 or set(inputs) != {Timeframe.MINUTES_15.value, Timeframe.HOURS_4.value}: raise ValueError("context must provide exactly one raw payload per source timeframe")
    artifacts = {artifact.timeframe: artifact for artifact in m.source_artifacts}
    if len(m.source_artifacts) != 2 or len(artifacts) != 2 or set(artifacts) != {Timeframe.MINUTES_15.value, Timeframe.HOURS_4.value}: raise ValueError("manifest must contain exactly one 15m and one 4H artifact")
    for timeframe, stream in ((Timeframe.MINUTES_15, ltf), (Timeframe.HOURS_4, htf)):
        artifact = artifacts[timeframe.value]
        if artifact.identity_status != "declared_unverified":
            raise ValueError("source identity must remain declared_unverified")
        validate_canonical_source_bars(stream, timeframe)
        parsed = parse_tradingview_export(
            inputs[timeframe.value], declared_symbol=artifact.declared_symbol,
            timeframe=timeframe, parser_id=artifact.parser_id,
        )
        dates = (utc_date(stream[0].open_time), utc_date(stream[-1].open_time))
        if parsed != stream or (artifact.declared_symbol, artifact.raw_input_sha256, artifact.normalized_content_sha256, artifact.row_count, artifact.date_range) != (stream[0].symbol, sha256(inputs[timeframe.value]).hexdigest(), source_fingerprint(stream), len(stream), dates):
            raise ValueError("source artifact does not match replay source stream")
    rebuilt = ReplayEngine(context.replay_config, context.strategy_config).run(tuple(t.source_bar for t in traces))
    if rebuilt != replay: raise ValueError("supplied replay is not the exact replay of its ordered source bars/config")
    rebuilt_backtest = BacktestEngine(context.backtest_config).run(replay)
    if rebuilt_backtest != backtest: raise ValueError("supplied backtest is not the exact accounting result of replay/config")
    if {t.trade_id for t in backtest.closed_trades} != closed: raise ValueError("backtest/replay closed trade IDs disagree")
    return rf, sf, bf, qf, xf

def _session(prefix: list[Any], bar: Any) -> tuple[SessionStatus, int, float | None, float | None, float | None]:
    start = bar.open_time - (bar.open_time % 86_400_000); expected = (bar.open_time-start)//BAR_MS + 1
    if len(prefix) != expected or any(x.open_time != start+i*BAR_MS for i,x in enumerate(prefix)):
        return SessionStatus.INCOMPLETE_PREFIX, len(prefix), None, None, None
    return SessionStatus.COMPLETE_PREFIX, len(prefix), prefix[0].open, max(x.high for x in prefix), min(x.low for x in prefix)

def build_research_dataset(replay: ReplayResult, backtest: BacktestResult, *, context: ResearchBuildContext) -> ResearchDataset:
    """Build only after exact replay/backtest/provenance reconstruction succeeds."""
    rf, sf, bf, qf, xf = _validate_build(replay, backtest, context)
    traces = tuple(t for t in replay.traces if t.source_bar.timeframe is Timeframe.MINUTES_15); bars = tuple(t.source_bar for t in traces)
    adr = adr_contexts(bars); prefixes: dict[str,list[Any]] = {}; setup_rows: list[SetupRow] = []; by_origin: dict[int,int] = {}; arms: dict[Direction,int] = {}; entries: dict[str,tuple[int,Any]] = {}; kalman: dict[Direction,tuple[int,int]] = {}
    for ix, trace in enumerate(traces):
        bar = trace.source_bar; date = utc_date(bar.open_time); p = prefixes.setdefault(date, []); p.append(bar); ss,n,so,sh,sl = _session(p,bar); ac=adr[date]; sb=trace.strategy_bar
        if sb and sb.kalman_transition: kalman[sb.kalman_transition]=(bar.finalized_at,1)
        elif sb and sb.kalman_direction: kalman[sb.kalman_direction]=(kalman.get(sb.kalman_direction,(0,0))[0],kalman.get(sb.kalman_direction,(0,0))[1]+1)
        hage = None if trace.post_state.bias_activation_timestamp is None or sb is None or sb.htf_bias is None else bar.finalized_at-trace.post_state.bias_activation_timestamp
        for ord,event in enumerate(trace.events):
            if event.type is EventType.HEMA_FLIP_DETECTED and event.side is not None:
                kt,kp=kalman.get(event.side,(None,None)); ae = None if ss is not SessionStatus.COMPLETE_PREFIX or ac.adr is None else ((bar.close-sl)/ac.adr if event.side is Direction.LONG else (sh-bar.close)/ac.adr)
                at = None if ss is not SessionStatus.COMPLETE_PREFIX or not sb or not sb.atr else ((bar.close-sl)/sb.atr if event.side is Direction.LONG else (sh-bar.close)/sb.atr)
                sid=setup_identity(symbol=bar.symbol,bias_epoch=trace.post_state.bias_epoch or None,direction=event.side.value,setup_origin_timestamp=bar.finalized_at,source_processing_key=bar.processing_key,strategy_fingerprint=sf)
                row=SetupRow(RESEARCH_SCHEMA_VERSION,sid,bar.symbol,event.side,bar.open_time,bar.finalized_at,bar.finalized_at,bar.processing_key,bar.finalized_at,sf,sb.htf_bias if sb else None,trace.post_state.bias_epoch or None,hage,None if kt is None else bar.finalized_at-kt,kp,bar.close,sb.atr if sb else None,*_parts(bar.open_time),ss,n,so,sh,sl,ac.adr,ac.status,ae,at,False,False,SetupStatus.REJECTED,None,(),event_identity(symbol=bar.symbol,source_processing_key=bar.processing_key,ordinal=ord,event_type=event.type.value,trade_id=None,strategy_fingerprint=sf))
                if sid in {r.setup_id for r in setup_rows}: raise ValueError("duplicate setup identity")
                by_origin[bar.finalized_at]=len(setup_rows); setup_rows.append(row)
            elif event.type is EventType.SETUP_ARMED and event.side is not None:
                origin=trace.post_state.pending_flip_timestamp or bar.finalized_at; i=by_origin.get(origin)
                if i is None: raise ValueError("armed setup lacks originating fresh flip")
                setup_rows[i]=replace(setup_rows[i],eligible_baseline_setup=True,was_armed=True,setup_status=SetupStatus.ARMED,resolution_reasons=(event.reason.value,)); arms[event.side]=i
            elif event.type is EventType.SETUP_CANCELLED and event.side is not None:
                i=arms.pop(event.side,None)
                if i is None: raise ValueError("cancelled setup lacks armed identity")
                setup_rows[i]=replace(setup_rows[i],setup_status=SetupStatus.CANCELLED,resolution_reasons=(event.reason.value,))
            elif event.type is EventType.DECISION_REJECTED:
                i=by_origin.get(bar.finalized_at)
                if i is not None and (event.side is None or setup_rows[i].direction is event.side) and not setup_rows[i].eligible_baseline_setup:
                    setup_rows[i]=replace(setup_rows[i],resolution_reasons=(event.reason.value,))
            elif event.type is EventType.TRADE_OPENED:
                trade=trace.post_state.trade
                if trade is None or trade.trade_id != event.trade_id or trade.trade_id in entries: raise ValueError("inconsistent/duplicate opened trade")
                i=by_origin.get(trade.setup_origin_timestamp)
                if i is None:
                    # Explicit resume fallback identity; full-history builds normally never take this path.
                    sid=setup_identity(symbol=bar.symbol,bias_epoch=trade.bias_epoch,direction=trade.side.value,setup_origin_timestamp=trade.setup_origin_timestamp,source_processing_key=(trade.setup_origin_timestamp,Timeframe.MINUTES_15.priority),strategy_fingerprint=sf)
                else:
                    sid=setup_rows[i].setup_id; setup_rows[i]=replace(setup_rows[i],eligible_baseline_setup=True,setup_status=SetupStatus.OPENED,linked_trade_id=trade.trade_id,resolution_reasons=(event.reason.value,))
                entries[trade.trade_id]=(ix,(trade,event,ord,sid,ss,n,so,sh,sl,ac,hage,kalman.get(trade.side,(None,None))))
    closes: dict[str,tuple[int,Any,int]]={}
    for ix,t in enumerate(traces):
        for ord,e in enumerate(t.events):
            if e.type is EventType.TRADE_CLOSED:
                if e.trade_id in closes: raise ValueError("duplicate close identity")
                closes[e.trade_id]=(ix,e,ord)
    closed={x.trade_id:x for x in backtest.closed_trades}; rows=[]
    for tid,(start,info) in entries.items():
        trade,event,ord,sid,ss,n,so,sh,sl,ac,hage,kstate=info; c=closed.get(tid); close=closes.get(tid)
        if (c is None) != (close is None): raise ValueError("unknown/missing accounting outcome")
        risk=abs(trade.entry_price-trade.stop_price); hour,wd,bucket,since=_parts(traces[start].source_bar.open_time); ext=None if ss is not SessionStatus.COMPLETE_PREFIX or ac.adr is None else ((trade.entry_price-sl)/ac.adr if trade.side is Direction.LONG else (sh-trade.entry_price)/ac.adr); aext=None if ss is not SessionStatus.COMPLETE_PREFIX else ((trade.entry_price-sl)/trade.atr_at_entry if trade.side is Direction.LONG else (sh-trade.entry_price)/trade.atr_at_entry)
        if c is None:
            vals=("censored",)+(None,)*6+((),)+(None,)*16+((),)
        else:
            end,ce,cord=close
            if (c.trade_id,c.symbol,c.side,c.entry_timestamp,c.canonical_entry_price,c.exit_timestamp,c.canonical_exit_price)!=(tid,trade_id_symbol(tid),trade.side,trade.entry_timestamp,trade.entry_price,ce.timestamp,ce.price):
                raise ValueError("closed accounting trade does not match replay entry/exit")
            seq=[t.source_bar for t in traces[start+1:end+1]]; expected=(traces[end].source_bar.open_time-traces[start].source_bar.open_time)//BAR_MS; contiguous=len(seq)==expected and all(bar.open_time==traces[start].source_bar.open_time+(i+1)*BAR_MS for i,bar in enumerate(seq)); mae,mfe,maer,mfer=calculate_excursions(direction=trade.side,entry_price=trade.entry_price,stop_price=trade.stop_price,subsequent_bars=seq); reasons=tuple(x.value for x in ce.reasons); stop=any(x=="exit_stop" for x in reasons); vals=("closed",c.exit_timestamp,traces[end].source_bar.open_time,traces[end].source_bar.finalized_at,c.canonical_exit_price,c.execution_exit_price,c.exit_reason,reasons,stop,any(x != "exit_stop" for x in reasons),c.gross_pnl,c.net_pnl,c.entry_fee,c.exit_fee,c.total_fees,c.net_pnl/(risk*c.quantity),mae if contiguous else None,mfe if contiguous else None,maer if contiguous else None,mfer if contiguous else None,len(seq),expected,c.exit_timestamp-trade.entry_timestamp,event_identity(symbol=trade_id_symbol(tid),source_processing_key=traces[end].source_bar.processing_key,ordinal=cord,event_type=EventType.TRADE_CLOSED.value,trade_id=tid,strategy_fingerprint=sf),(() if contiguous else ("post_entry_15m_gap",)))
        (state,exitts,exopen,exfin,canexit,execxit,reason,reasons,stophit,stratex,gross,net,efee,xfee,fees,rr,mae,mfe,maer,mfer,barsdur,expectedbars,elapsed,exitid,flags)=vals
        flags = flags + (("incomplete_utc_session_prefix",) if ss is SessionStatus.INCOMPLETE_PREFIX else ()) + (("adr_unavailable",) if ac.adr is None else ())
        rows.append(TradeRow(RESEARCH_SCHEMA_VERSION,tid,sid,event_identity(symbol=traces[start].source_bar.symbol,source_processing_key=traces[start].source_bar.processing_key,ordinal=ord,event_type=EventType.TRADE_OPENED.value,trade_id=tid,strategy_fingerprint=sf),exitid,traces[start].source_bar.symbol,trade.side,traces[start].source_bar.open_time,traces[start].source_bar.finalized_at,traces[start].source_bar.finalized_at,traces[start].source_bar.processing_key,trade.setup_origin_timestamp,trade.bias_epoch,sf,traces[start].strategy_bar.htf_bias if traces[start].strategy_bar else None,hage,traces[start].source_bar.finalized_at-trade.setup_origin_timestamp,None if kstate[0] is None else traces[start].source_bar.finalized_at-kstate[0],kstate[1],trade.atr_at_entry,trade.entry_price,trade.stop_price,risk,c.quantity if c else None,c.execution_entry_price if c else None,hour,wd,bucket,since,ss,n,so,sh,sl,ac.adr,ac.status,ext,aext,risk,risk/trade.atr_at_entry,None if not ac.adr else risk/ac.adr,state,exitts,exopen,exfin,canexit,execxit,reason,reasons,stophit,stratex,gross,net,efee,xfee,fees,rr,mae,mfe,maer,mfer,barsdur,expectedbars,elapsed,MAE_MFE_CONVENTION_VERSION,flags))
    setup_rows.sort(key=lambda r:(r.decision_timestamp,r.source_processing_key,r.setup_id)); rows.sort(key=lambda r:(r.decision_timestamp,r.source_processing_key,r.trade_id))
    return ResearchDataset(RESEARCH_SCHEMA_VERSION,context.manifest,fingerprint(context.manifest),rf,sf,bf,qf,xf,tuple(setup_rows),tuple(rows))

def trade_id_symbol(trade_id: str) -> str:
    symbol,sep,seq=trade_id.rpartition(":")
    if not sep or not symbol or not seq.isdigit(): raise ValueError("malformed strategy trade id")
    return symbol
