"""
JARVIS BACKTEST — механический скринер на исторических данных OKX.

Тестирует ТОЛЬКО механический фильтр (RSI/MACD/MA20/score >= 89), без финального
отбора Claude (топ-5) и без фильтра спреда/стакана (эти данные OKX исторически не хранит).
Параметры сделки — согласованная целевая конфигурация: плечо x2, SL 0.5%, TP 2.5%,
трейлинг 0.8%, частичное закрытие 50% при +1.5%, комиссии OKX maker/taker.

Результат пушится в GitHub на ветку state-storage (уже существует, используется demo_state.json),
файл backtest_result.json — можно скачать/просмотреть оттуда.

Запуск: python backtest.py
Переменные окружения: GITHUB_TOKEN, GITHUB_REPO (уже настроены в Railway для основного бота),
опционально BT_PAIRS (default 50), BT_MONTHS (default 12).
"""

import os, time, json, base64, requests
from datetime import datetime, timezone, timedelta

OKX_BASE = "https://www.okx.com"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "konstantin514113-dotcom/jarvis-analyst")
RESULTS_BRANCH = "state-storage"
RESULTS_PATH = "backtest_result.json"

PAIRS_COUNT = int(os.environ.get("BT_PAIRS", "50"))
MONTHS_BACK = int(os.environ.get("BT_MONTHS", "12"))

# === Trading params — согласованная целевая конфигурация ===
LEVERAGE       = 2.0
SL_PCT         = 0.5
TP_PCT         = 2.5
TRAIL_PCT      = 0.8
PARTIAL_PCT    = 1.5
PARTIAL_RATIO  = 0.5
SIZE           = 2000.0   # условный размер позиции для расчёта $ P&L (не влияет на winrate/RR)
MAKER_FEE      = 0.08 / 100
TAKER_FEE      = 0.10 / 100
SCORE_MIN      = 89
MAX_HOLD_BARS  = 400       # ~4 суток на 15m барах — если сделка не закрылась, отбрасываем (редкость)

STABLES = {"USDC", "BUSD", "DAI", "USDD", "TUSD"}


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def get_top_pairs(n):
    r = requests.get(f"{OKX_BASE}/api/v5/market/tickers?instType=SPOT", timeout=15)
    data = r.json().get("data", [])
    usdt = [d for d in data if d["instId"].endswith("-USDT")
            and d["instId"].split("-")[0] not in STABLES]
    usdt.sort(key=lambda d: float(d.get("volCcy24h", 0)), reverse=True)
    pairs = [d["instId"] for d in usdt[:n]]
    log(f"Top-{n} pairs by 24h volume selected: {pairs[:10]}...")
    return pairs


def fetch_history(symbol, bar, since_ms):
    """Paginate backwards through /market/history-candles until since_ms is reached."""
    all_candles = []
    after = None
    base_url = f"{OKX_BASE}/api/v5/market/history-candles?instId={symbol}&bar={bar}&limit=100"
    tries = 0
    while True:
        url = base_url + (f"&after={after}" if after else "")
        try:
            r = requests.get(url, timeout=10)
            data = r.json().get("data", [])
        except Exception:
            tries += 1
            if tries > 3:
                break
            time.sleep(1)
            continue
        if not data:
            break
        all_candles.extend(data)
        oldest_ts = int(data[-1][0])
        after = oldest_ts
        if oldest_ts <= since_ms:
            break
        time.sleep(0.12)
    parsed = []
    for c in all_candles:
        ts = int(c[0])
        if ts < since_ms:
            continue
        parsed.append({"ts": ts, "o": float(c[1]), "h": float(c[2]),
                        "l": float(c[3]), "c": float(c[4]), "vol": float(c[5])})
    parsed.sort(key=lambda x: x["ts"])
    return parsed


def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0:
        return 100
    return 100 - (100 / (1 + ag / al))


def _ema(data, n):
    k = 2 / (n + 1)
    e = data[0]
    for d in data[1:]:
        e = d * k + e * (1 - k)
    return e


def calc_macd_state(closes):
    if len(closes) < 27:
        return "unknown"
    macd = _ema(closes[-12:], 12) - _ema(closes[-26:], 26)
    prev = _ema(closes[-13:-1], 12) - _ema(closes[-27:-1], 26)
    if macd > 0 and macd > prev:
        return "bullish"
    if macd < 0 and macd < prev:
        return "bearish"
    if macd > prev:
        return "crossing_up"
    return "crossing_down"


def calc_ma(closes, period=20):
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def score_bar(window15, window1h):
    """Replicates bot.py analyze_pair() screener logic on a historical window."""
    closes15 = [c["c"] for c in window15]
    closes1h = [c["c"] for c in window1h]
    price = closes15[-1]
    rsi15 = calc_rsi(closes15); macd15 = calc_macd_state(closes15)
    rsi1h = calc_rsi(closes1h); macd1h = calc_macd_state(closes1h)
    ma20 = calc_ma(closes15)
    above_ma = price > ma20 if ma20 else False
    htf = (40 <= rsi1h <= 70) and ("bullish" in macd1h or "crossing_up" in macd1h)
    if not htf:
        return None
    day_window = window15[-96:] if len(window15) >= 96 else window15
    change24h = (price - day_window[0]["c"]) / day_window[0]["c"] * 100
    high24 = max(c["h"] for c in day_window)
    dist = (high24 - price) / high24 * 100 if high24 > 0 else 0
    vol24_proxy = sum(c["vol"] * c["c"] for c in day_window)
    score = 0
    score += min(change24h * 3, 30)
    score += min(vol24_proxy / 50000, 20)
    score += 15 if 45 <= rsi15 <= 65 else 0
    score += 15 if "bullish" in macd15 or "crossing_up" in macd15 else 0
    score += 10 if above_ma else 0
    score += 10 if dist > 1 else 0
    score += 20
    if score < SCORE_MIN:
        return None
    return round(score, 1)


def calc_fee(notional, maker=False):
    rate = MAKER_FEE if maker else TAKER_FEE
    return notional * rate


def simulate_trade(c15, entry_idx):
    """Enter LONG at open of entry_idx, walk forward bar-by-bar applying trailing/partial/SL/TP."""
    entry = c15[entry_idx]["o"]
    sl = entry * (1 - SL_PCT / 100)
    tp = entry * (1 + TP_PCT / 100)
    peak = entry
    size = SIZE
    partial_closed = False
    entry_fee = calc_fee(size * LEVERAGE, maker=True)
    realized = -entry_fee
    fees_total = entry_fee

    for j in range(entry_idx, min(entry_idx + MAX_HOLD_BARS, len(c15))):
        bar = c15[j]
        if bar["h"] > peak:
            peak = bar["h"]
            new_sl = peak * (1 - TRAIL_PCT / 100)
            if new_sl > sl:
                sl = new_sl
        pnl_pct = (bar["c"] - entry) / entry * LEVERAGE

        if not partial_closed and pnl_pct * 100 >= PARTIAL_PCT:
            partial_size = size * PARTIAL_RATIO
            partial_pnl = partial_size * pnl_pct
            fee = calc_fee(partial_size * LEVERAGE, maker=True)
            realized += partial_pnl - fee
            fees_total += fee
            size = size * (1 - PARTIAL_RATIO)
            partial_closed = True
            sl = entry

        hit_tp = bar["h"] >= tp
        hit_sl = bar["l"] <= sl
        if hit_tp or hit_sl:
            exit_price = tp if hit_tp else sl
            pnl_pct_final = (exit_price - entry) / entry * LEVERAGE
            pnl_usd = size * pnl_pct_final
            fee = calc_fee(size * LEVERAGE, maker=hit_tp)
            realized += pnl_usd - fee
            fees_total += fee
            result = "WIN" if hit_tp else ("BREAKEVEN" if partial_closed and realized >= 0 else "LOSS")
            return {"result": result, "pnl_usd": round(realized, 2),
                    "fees_total": round(fees_total, 2), "closed_idx": j}
    return None  # never hit SL/TP within horizon — dropped from stats


def run_backtest():
    pairs = get_top_pairs(PAIRS_COUNT)
    since_ms = int((datetime.now(timezone.utc) - timedelta(days=MONTHS_BACK * 30)).timestamp() * 1000)
    all_trades = []

    for n, symbol in enumerate(pairs, 1):
        try:
            log(f"[{n}/{len(pairs)}] {symbol}: downloading history...")
            c15 = fetch_history(symbol, "15m", since_ms)
            c1h = fetch_history(symbol, "1H", since_ms)
            if len(c15) < 120 or len(c1h) < 30:
                log(f"  skip {symbol}: insufficient history ({len(c15)} x15m, {len(c1h)} x1H)")
                continue

            in_position_until = -1
            trades_for_pair = 0
            for idx in range(96, len(c15) - 1):
                if idx <= in_position_until:
                    continue
                ts = c15[idx]["ts"]
                window15 = c15[max(0, idx - 96):idx + 1]
                h1window = [h for h in c1h if h["ts"] <= ts][-30:]
                if len(h1window) < 27:
                    continue
                sc = score_bar(window15, h1window)
                if sc is None:
                    continue
                trade = simulate_trade(c15, idx + 1)
                if trade is None:
                    continue
                trade["symbol"] = symbol
                trade["entry_ts"] = c15[idx + 1]["ts"]
                trade["score"] = sc
                all_trades.append(trade)
                in_position_until = trade["closed_idx"]
                trades_for_pair += 1
            log(f"  {symbol}: {trades_for_pair} trades found")
            time.sleep(0.2)
        except Exception as e:
            log(f"  ERROR {symbol}: {e}")
            continue

    wins = [t for t in all_trades if t["result"] == "WIN"]
    losses = [t for t in all_trades if t["result"] == "LOSS"]
    breakeven = [t for t in all_trades if t["result"] == "BREAKEVEN"]
    total_pnl = sum(t["pnl_usd"] for t in all_trades)
    total_fees = sum(t["fees_total"] for t in all_trades)
    winrate = (len(wins) / len(all_trades) * 100) if all_trades else 0

    quarters = {}
    for t in all_trades:
        dt = datetime.fromtimestamp(t["entry_ts"] / 1000, tz=timezone.utc)
        q = f"{dt.year}-Q{(dt.month - 1)//3 + 1}"
        quarters.setdefault(q, {"trades": 0, "wins": 0, "pnl": 0.0})
        quarters[q]["trades"] += 1
        quarters[q]["wins"] += 1 if t["result"] == "WIN" else 0
        quarters[q]["pnl"] += t["pnl_usd"]
    for q in quarters:
        quarters[q]["winrate_pct"] = round(quarters[q]["wins"] / quarters[q]["trades"] * 100, 1)
        quarters[q]["pnl"] = round(quarters[q]["pnl"], 2)

    by_symbol = {}
    for t in all_trades:
        s = t["symbol"]
        by_symbol.setdefault(s, {"trades": 0, "wins": 0, "pnl": 0.0})
        by_symbol[s]["trades"] += 1
        by_symbol[s]["wins"] += 1 if t["result"] == "WIN" else 0
        by_symbol[s]["pnl"] += t["pnl_usd"]

    # running balance / max drawdown, starting from $10,000, sized as 20% of balance per trade at entry
    balance = 10000.0
    peak_balance = balance
    max_dd_pct = 0.0
    for t in sorted(all_trades, key=lambda x: x["entry_ts"]):
        scale = (balance * 0.20) / SIZE
        balance += t["pnl_usd"] * scale
        if balance > peak_balance:
            peak_balance = balance
        dd = (peak_balance - balance) / peak_balance * 100 if peak_balance > 0 else 0
        if dd > max_dd_pct:
            max_dd_pct = dd

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pairs_tested": pairs,
        "months_back": MONTHS_BACK,
        "config": {"leverage": LEVERAGE, "sl_pct": SL_PCT, "tp_pct": TP_PCT,
                   "trailing_pct": TRAIL_PCT, "partial_close_pct": PARTIAL_PCT,
                   "score_min": SCORE_MIN, "maker_fee_pct": MAKER_FEE*100, "taker_fee_pct": TAKER_FEE*100},
        "total_trades": len(all_trades),
        "wins": len(wins), "losses": len(losses), "breakeven": len(breakeven),
        "winrate_pct": round(winrate, 2),
        "total_pnl_usd_fixed_size": round(total_pnl, 2),
        "total_fees_usd_fixed_size": round(total_fees, 2),
        "avg_pnl_per_trade": round(total_pnl / len(all_trades), 2) if all_trades else 0,
        "final_balance_compounded_from_10k": round(balance, 2),
        "max_drawdown_pct_compounded": round(max_dd_pct, 2),
        "by_quarter": quarters,
        "by_symbol": by_symbol,
        "note": ("Mechanical LONG-only screener (RSI/MACD/MA20/score>=89), NO Claude top-5 ranking, "
                 "NO spread/orderbook liquidity filter (unavailable historically — OKX doesn't store "
                 "historical order book depth). SHORT side of the hedge model is not backtested "
                 "separately: in the live code it is just labeling the 2 lowest-scored members of the "
                 "already-bullish-filtered top-5 as SHORT, not an independently bearish-filtered signal."),
    }

    with open("/tmp/backtest_result.json", "w") as f:
        json.dump(result, f, indent=2)
    log(f"DONE: {len(all_trades)} trades, winrate {winrate:.1f}%, "
        f"final balance ${balance:,.2f}, max DD {max_dd_pct:.1f}%")
    push_to_github(result)


def push_to_github(result):
    if not GITHUB_TOKEN:
        log("No GITHUB_TOKEN set, result saved only to /tmp/backtest_result.json")
        return
    content_b64 = base64.b64encode(json.dumps(result, indent=2).encode()).decode()
    r = requests.get(
        f"https://api.github.com/repos/{GITHUB_REPO}/contents/{RESULTS_PATH}?ref={RESULTS_BRANCH}",
        headers={"Authorization": f"token {GITHUB_TOKEN}"}, timeout=10
    )
    sha = r.json().get("sha") if r.status_code == 200 else None
    payload = {"message": "Backtest result", "content": content_b64, "branch": RESULTS_BRANCH}
    if sha:
        payload["sha"] = sha
    resp = requests.put(
        f"https://api.github.com/repos/{GITHUB_REPO}/contents/{RESULTS_PATH}",
        headers={"Authorization": f"token {GITHUB_TOKEN}"}, json=payload, timeout=10
    )
    log(f"Pushed to GitHub ({RESULTS_BRANCH}/{RESULTS_PATH}): status {resp.status_code}")


if __name__ == "__main__":
    run_backtest()
