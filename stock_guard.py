#!/usr/bin/env python3
"""US stock SEC filing alerts and conservative paper-trading signals."""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import ssl
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "stock_guard.db"


def load_env() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def load_config() -> dict[str, Any]:
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS seen_filings (accession TEXT PRIMARY KEY, seen_at TEXT NOT NULL)")
    con.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    return con


def notify(title: str, message: str) -> None:
    print(f"\n[{title}] {message}")
    if sys.platform == "darwin":
        safe_title = title.replace('"', "'")
        safe_message = message.replace('"', "'")
        subprocess.run(
            ["osascript", "-e", f'display notification "{safe_message}" with title "{safe_title}"'],
            check=False, capture_output=True,
        )


def state_get(con: sqlite3.Connection, key: str) -> str | None:
    row = con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def state_set(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("INSERT INTO state(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    con.commit()


def sma(values: list[float], n: int) -> float | None:
    return sum(values[-n:]) / n if len(values) >= n else None


def atr(rows: list[dict[str, Any]], n: int = 14) -> float | None:
    if len(rows) < n + 1:
        return None
    ranges = []
    for prev, cur in zip(rows[-n - 1:-1], rows[-n:]):
        ranges.append(max(cur["high"] - cur["low"], abs(cur["high"] - prev["close"]), abs(cur["low"] - prev["close"])))
    return sum(ranges) / n


def _number(value: Any) -> float:
    return float(value.iloc[0] if hasattr(value, "iloc") else value)


def fetch_prices(ticker: str, period: str = "5y") -> list[dict[str, Any]]:
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("Run `python -m pip install -r requirements.txt` first.") from exc
    frame = yf.download(ticker, period=period, auto_adjust=True, progress=False, threads=False)
    if frame.empty:
        raise RuntimeError(f"{ticker} price data is unavailable.")
    rows = []
    for idx, row in frame.iterrows():
        parsed = {
            "date": idx.date(), "open": _number(row["Open"]), "high": _number(row["High"]),
            "low": _number(row["Low"]), "close": _number(row["Close"]),
        }
        if all(math.isfinite(parsed[k]) for k in ("open", "high", "low", "close")):
            rows.append(parsed)
    return rows


def extended_quote(ticker: str) -> dict[str, Any]:
    """Show extended-hours quotes when available, but do not use them for trading signals."""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).fast_info
        return {
            "last": float(info["last_price"]),
            "previous_close": float(info["previous_close"]),
            "market_state": str(info.get("market_state", "UNKNOWN")),
        }
    except Exception:
        return {}


def signal(rows: list[dict[str, Any]], market_rows: list[dict[str, Any]], cfg: dict[str, Any]) -> dict[str, Any]:
    if len(rows) < 201 or len(market_rows) < 201:
        return {"action": "WAIT", "reason": "Fewer than 200 trading days of data"}
    closes = [r["close"] for r in rows]
    market = [r["close"] for r in market_rows]
    current = rows[-1]
    s20, s60, s120 = sma(closes, 20), sma(closes, 60), sma(closes, 120)
    market200 = sma(market, 200)
    recent_high = max(r["high"] for r in rows[-20:])
    prior_high = max(r["high"] for r in rows[-21:-1])
    pullback = 1 - current["close"] / recent_high
    volatility = atr(rows) or current["close"] * cfg["hard_stop_pct"] / 2
    stop_distance = max(current["close"] * cfg["hard_stop_pct"], 2 * volatility)
    in_pullback_zone = cfg["pullback_min_pct"] <= pullback <= cfg["pullback_max_pct"]
    reversal = in_pullback_zone and current["close"] > rows[-2]["high"]
    conditions = {
        "S&P 500 above its 200-day average": market[-1] > market200,
        "Close above the 120-day average": current["close"] > s120,
        "20-day average above the 60-day average": s20 > s60,
        "20-day breakout or pullback reversal": current["close"] > prior_high or reversal,
    }
    trend_ready = all(list(conditions.values())[:3])
    action = "BUY_CANDIDATE" if all(conditions.values()) else "WATCH_PULLBACK" if trend_ready and in_pullback_zone else "WAIT"
    return {
        "action": action, "date": str(current["date"]), "close": round(current["close"], 2),
        "stop_price": round(current["close"] - stop_distance, 2), "conditions": conditions,
        "pullback_pct": round(pullback * 100, 2),
        "reason": "All entry conditions passed" if action == "BUY_CANDIDATE" else "Pullback within an uptrend; awaiting reversal confirmation" if action == "WATCH_PULLBACK" else "Some entry conditions were not met",
    }


def sec_filings(cik: str, days: int = 4) -> list[dict[str, str]]:
    user_agent = os.environ.get("SEC_USER_AGENT", "US Stock Guard contact@example.com")
    request = urllib.request.Request(
        f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json",
        headers={"User-Agent": user_agent, "Accept": "application/json"},
    )
    try:
        import certifi
        ssl_context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ssl_context = ssl.create_default_context()
    with urllib.request.urlopen(request, timeout=20, context=ssl_context) as response:
        payload = json.load(response)
    recent = payload["filings"]["recent"]
    cutoff = date.today() - timedelta(days=days)
    filings = []
    for i, filed in enumerate(recent["filingDate"]):
        if date.fromisoformat(filed) < cutoff:
            continue
        accession = recent["accessionNumber"][i]
        accession_plain = accession.replace("-", "")
        primary = recent["primaryDocument"][i]
        cik_plain = str(int(cik))
        filings.append({
            "accession": accession, "date": filed, "form": recent["form"][i],
            "description": recent.get("primaryDocDescription", [""] * len(recent["form"]))[i],
            "link": f"https://www.sec.gov/Archives/edgar/data/{cik_plain}/{accession_plain}/{primary}",
        })
    return filings


POSITIVE = ("earnings", "results", "repurchase", "dividend", "contract")
NEGATIVE = ("restatement", "investigation", "impairment", "termination", "bankruptcy")


def fallback_classify(text: str) -> dict[str, str]:
    lowered = text.lower()
    pos = any(word in lowered for word in POSITIVE)
    neg = any(word in lowered for word in NEGATIVE)
    label = "Potentially positive" if pos and not neg else "Potentially negative" if neg and not pos else "Neutral; review source filing"
    return {"label": label, "summary": "Provisional classification based on the form and title.", "risk": "Do not use this as a trading basis before reviewing the full SEC filing."}


def ai_classify(company: str, filing: dict[str, str]) -> dict[str, str]:
    text = f"{filing['form']} {filing['description']}"
    if not os.environ.get("OPENAI_API_KEY"):
        return fallback_classify(text)
    try:
        from openai import OpenAI
        response = OpenAI().responses.create(
            model=os.environ.get("OPENAI_MODEL", "gpt-5.4-mini"),
            instructions="You are a conservative US equity SEC filing analyst. Do not draw conclusions from the title and form alone, and do not give buy or sell instructions.",
            input=f"Company: {company}\nFiling: {text}\nSource: {filing['link']}",
            text={"format": {"type": "json_schema", "name": "filing_analysis", "strict": True, "schema": {
                "type": "object", "additionalProperties": False,
                "properties": {"label": {"type": "string"}, "summary": {"type": "string"}, "risk": {"type": "string"}},
                "required": ["label", "summary", "risk"]}}}, store=False,
        )
        return json.loads(response.output_text)
    except Exception as exc:
        result = fallback_classify(text)
        result["risk"] = f"AI summary failed; using provisional classification: {exc}"
        return result


def check_filings(config: dict[str, Any]) -> None:
    con = db()
    for _, meta in config["symbols"].items():
        if not meta.get("cik"):
            continue
        for filing in sec_filings(meta["cik"]):
            if con.execute("SELECT 1 FROM seen_filings WHERE accession=?", (filing["accession"],)).fetchone():
                continue
            analysis = ai_classify(meta["name"], filing)
            notify(
                f"{meta['name']} New SEC filing",
                f"{filing['form']} · {filing['date']} · {analysis['label']}\n{analysis['summary']}\nCaution: {analysis['risk']}\n{filing['link']}",
            )
            con.execute("INSERT INTO seen_filings VALUES (?, ?)", (filing["accession"], datetime.now().isoformat()))
            con.commit()


def check_signals(config: dict[str, Any], push_status: bool = False) -> None:
    con = db()
    market = fetch_prices(config["market_ticker"], "2y")
    status_lines = []
    for ticker, meta in config["symbols"].items():
        result = signal(fetch_prices(ticker, "2y"), market, config["strategy"])
        if meta.get("tier") == "TRACK_ONLY":
            result["action"] = "TRACK_ONLY"
            result["reason"] = "Track only material news and abnormal moves"
        passed = ", ".join(k for k, v in result.get("conditions", {}).items() if v)
        failed = ", ".join(k for k, v in result.get("conditions", {}).items() if not v)
        quote = extended_quote(ticker)
        live = f" · latest ${quote['last']:.2f} ({quote['market_state']})" if quote else ""
        print(f"\n[{meta.get('tier', '-')}/{meta.get('sector', '-')}] {meta['name']} ({ticker}) {result['action']} · regular-session close ${result.get('close', '-')}{live}")
        print(f"  Pullback from 20-day high: -{result.get('pullback_pct', '-')}% · Candidate stop: ${result.get('stop_price', '-')}")
        print(f"  Passed: {passed or '-'}")
        print(f"  Failed: {failed or '-'}")
        status_lines.append(f"{meta['name']}: {result['action']} · ${result.get('close', '-')}\nFailed: {failed or '-'}")
        if result["action"] == "BUY_CANDIDATE" and meta.get("tier") != "TRACK_ONLY":
            key = f"signal:{ticker}:{result.get('date')}:{result['action']}"
            if state_get(con, key) is None:
                notify(f"{meta['name']} Buy candidate", f"Price conditions passed · Planned stop ${result['stop_price']}. Human review is required.")
                state_set(con, key, datetime.now().isoformat())
    if push_status:
        notify("US Stock Guard status", "\n\n".join(status_lines) + "\n\nNo live orders were placed.")


def portfolio_status(config: dict[str, Any], push_status: bool = False) -> None:
    holdings = config.get("portfolio", {})
    if not holdings:
        print("The portfolio section in config.json is empty.")
        return
    total_cost = total_value = 0.0
    lines = []
    print("Portfolio status · regular-session close · excludes FX and taxes")
    for ticker, holding in holdings.items():
        rows = fetch_prices(ticker, "1mo")
        price = rows[-1]["close"]
        quantity, average = float(holding["quantity"]), float(holding["average_price"])
        cost, value = quantity * average, quantity * price
        pnl, pnl_pct = value - cost, (price / average - 1) * 100
        total_cost += cost; total_value += value
        hard_stop = average * (1 - config["strategy"]["hard_stop_pct"])
        line = f"{ticker}: {quantity:g} shares · ${price:.2f} · {pnl_pct:+.1f}% (${pnl:+,.0f}) · -10% line from average cost ${hard_stop:.2f}"
        lines.append(line); print(line)
    total_pct = (total_value / total_cost - 1) * 100
    summary = f"Total cost ${total_cost:,.0f} · Market value ${total_value:,.0f} · {total_pct:+.1f}% (${total_value-total_cost:+,.0f})"
    print(summary)
    if push_status:
        notify("US stock portfolio", "\n".join(lines + [summary]) + "\nNo live orders were placed.")


@dataclass
class Position:
    cash: float
    qty: float = 0
    entry: float = 0
    peak: float = 0
    tp1: bool = False
    tp2: bool = False


def backtest_one(rows: list[dict[str, Any]], market: list[dict[str, Any]], config: dict[str, Any], max_weight: float) -> dict[str, float]:
    initial, cost, risk = float(config["initial_cash"]), float(config["transaction_cost_rate"]), float(config["risk_per_trade"])
    strategy, position = config["strategy"], Position(initial)
    equity_peak, max_dd, trades, wins = initial, 0.0, 0, 0
    market_by_date = {r["date"]: r for r in market}
    history, aligned_market, cooldown_until = [], [], date.min
    for row in rows:
        if row["date"] not in market_by_date:
            continue
        history.append(row); aligned_market.append(market_by_date[row["date"]])
        price = row["close"]
        if position.qty:
            position.peak = max(position.peak, row["high"])
            exit_all = price <= position.entry * (1 - strategy["hard_stop_pct"]) or price <= position.peak * (1 - strategy["trailing_stop_pct"])
            if len(history) >= 62:
                prior60 = sma([x["close"] for x in history[:-1]], 60)
                priorprior60 = sma([x["close"] for x in history[:-2]], 60)
                exit_all |= history[-1]["close"] < prior60 and history[-2]["close"] < priorprior60
            fraction = 1.0 if exit_all else 0.0
            if not fraction and not position.tp2 and price >= position.entry * (1 + strategy["take_profit_2_pct"]):
                fraction, position.tp2 = 0.3 / max(0.7 if position.tp1 else 1, 0.01), True
            elif not fraction and not position.tp1 and price >= position.entry * (1 + strategy["take_profit_1_pct"]):
                fraction, position.tp1 = 0.3, True
            if fraction:
                sold = min(position.qty, position.qty * fraction)
                position.cash += sold * price * (1 - cost); position.qty -= sold
                if position.qty < 1e-9:
                    wins += int(price > position.entry); trades += 1; position.qty = 0
                    cooldown_until = row["date"] + timedelta(days=strategy["cooldown_days"])
        equity = position.cash + position.qty * price
        equity_peak = max(equity_peak, equity)
        drawdown = 1 - equity / equity_peak
        max_dd = max(max_dd, drawdown)
        if not position.qty and row["date"] >= cooldown_until and drawdown < config["max_drawdown_halt"]:
            sig = signal(history, aligned_market, strategy)
            if sig["action"] == "BUY_CANDIDATE":
                risk_budget = equity * risk * (0.5 if drawdown >= config["max_drawdown_reduce"] else 1)
                qty_risk = risk_budget / max(price - sig["stop_price"], 0.01)
                position.qty = min(qty_risk, equity * max_weight / price, position.cash / price)
                position.cash -= position.qty * price; position.entry = position.peak = price
                position.tp1 = position.tp2 = False
    final = position.cash + position.qty * rows[-1]["close"]
    years = max((rows[-1]["date"] - rows[0]["date"]).days / 365.25, 0.1)
    return {"return": (final / initial - 1) * 100, "cagr": ((final / initial) ** (1 / years) - 1) * 100,
            "max_dd": max_dd * 100, "trades": trades, "win_rate": wins / trades * 100 if trades else 0}


def run_backtest(config: dict[str, Any]) -> None:
    market = fetch_prices(config["market_ticker"], "10y")
    print("Backtest results do not guarantee future performance and exclude FX, taxes, and slippage.")
    for ticker, meta in config["symbols"].items():
        result = backtest_one(fetch_prices(ticker, "10y"), market, config, meta["max_weight"])
        print(f"{meta['name']}: Return {result['return']:.1f}% · CAGR {result['cagr']:.1f}% · Max drawdown {result['max_dd']:.1f}% · Closed trades {int(result['trades'])} · win rate {result['win_rate']:.1f}%")


def main() -> None:
    load_env(); config = load_config()
    parser = argparse.ArgumentParser(description="US stock SEC filing alerts and paper-trading signals")
    parser.add_argument("command", choices=["check", "filings", "signals", "portfolio", "status", "test-notification", "backtest"])
    args = parser.parse_args()
    if args.command in ("check", "filings"): check_filings(config)
    if args.command in ("check", "signals"): check_signals(config)
    if args.command == "portfolio": portfolio_status(config)
    if args.command == "status":
        check_signals(config, push_status=True)
        portfolio_status(config, push_status=True)
    if args.command == "test-notification": notify("US Stock Guard", "Mac notification test. No live orders were placed.")
    if args.command == "backtest": run_backtest(config)


if __name__ == "__main__":
    main()
