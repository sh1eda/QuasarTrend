# TradingView golden dataset

Golden tests discover every CSV in this directory automatically. The current
exports are `tradingview_15m.csv` and `tradingview_4h.csv`.

Generate it with `references/pinescript/parity_export.pine` on
`BINANCE:BTCUSDT.P`. Export 15m and 4H separately when both timeframes are being
validated. Include a long warm-up before the comparison window.

Required columns are the chart OHLC input columns plus the export harness plots.
The first exported row containing a complete recursive indicator checkpoint can
seed diagnostic recurrence comparison; comparison begins on the following
candle. A separate cold-start convergence diagnostic verifies whether the raw
exported history is itself long enough.
