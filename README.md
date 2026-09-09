# US Stock Guard

A conservative research and paper-trading signal tool for US equities. It tracks SEC filings, price trends, pullbacks, risk limits, and portfolio status across configurable watchlists.

> This project never sends live orders. Its signals are not investment advice. Market data may be delayed or incomplete, and every strategy can lose money. Validate any approach with official brokerage data, backtesting, and paper trading before considering real capital.

## Highlights

- SEC filing monitoring through official EDGAR submissions data
- Optional AI-assisted filing classification with structured output
- Trend, breakout, and pullback-reversal signals
- ATR-aware stops, position caps, staged profit-taking, and drawdown limits
- Portfolio P&L and risk-line reporting
- Ten-year historical backtesting
- Optional macOS notifications
- SQLite deduplication for filings and alerts

## Quick start

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

Update `SEC_USER_AGENT` in `.env` with a contactable identifier. The OpenAI API key is optional; without it, the program uses a conservative title-based fallback.

```bash
.venv/bin/python stock_guard.py signals
.venv/bin/python stock_guard.py portfolio
.venv/bin/python stock_guard.py check
.venv/bin/python stock_guard.py status
.venv/bin/python stock_guard.py backtest
.venv/bin/python stock_guard.py test-notification
```

## Signal model

An entry candidate requires:

- The S&P 500 above its 200-day moving average
- The security above its 120-day moving average
- The 20-day moving average above the 60-day moving average
- A 20-day breakout or a confirmed reversal after a 3–10% pullback

`WATCH_PULLBACK` identifies an intact uptrend that has entered the configured pullback zone but has not yet confirmed a reversal. `BUY_CANDIDATE` means all configured price conditions passed; it is still not an order or recommendation.

## Risk controls

- 10% hard stop from entry
- 15% trailing stop from peak
- 30% staged exit at +20% and another 30% at +40%
- 0.5% account risk budget per trade
- Per-symbol maximum portfolio weights
- Reduced risk after an 8% portfolio drawdown
- New entries halted after a 12% drawdown

## Configuration

Edit `config.json` to change the watchlist, classifications, maximum weights, and strategy thresholds. The public example intentionally contains no real holdings. Add personal positions locally only; do not commit them.

Pre-market and after-hours quotes are displayed only as context. Completed regular-session bars drive the daily signals. FX, taxes, and real-world execution differences are not modeled.

## SEC filing analysis

The tool checks recent 8-K, 10-Q, 10-K, and other filings through the SEC submissions endpoint. New accessions are stored in SQLite to prevent duplicate notifications. Always open and read the linked filing before making a decision.

## Project status

This is a research and risk-monitoring prototype, not an automated trading system. The design intentionally keeps a human in the loop.
