"""
JARVIS BACKTEST v3 — MEAN REVERSION: Bollinger Band bounce + RSI oversold + VWAP discount.

Both previous approaches (momentum RSI/MACD, and trend-following EMA cross) FAILED —
both are variants of "chase the move", and both lost systematically on 15m bars for
these 5 pairs. This tests the opposite family of strategy: mean reversion — betting
that price snaps back toward its recent average after an over-extension, which is
one of the most commonly documented approaches for liquid pairs (BTC/ETH/SOL/XRP)
specifically because they are heavily traded around VWAP by market makers/algos.

LONG signal fires on a 15m bar when ALL of:
1. Bar's low touches or pierces the lower Bollinger Band (20-period, 2 std dev) —
   price is statistically over-extended to the downside
2. Bar closes back ABOVE the lower band — a rejection/bounce candle, not a breakdown
3. RSI(14) < 35 — confirms oversold condition (confluence, not a standalone trigger)
4. Price is below VWAP — trading at a "discount" to volume-weighted fair value

Entry at the open of the NEXT bar (no lookahead). Same SL/TP grid search as before.

Запуск: python backtest_v3.py
"""

import os, time, json, tempfile, requests
from datetime import datetime, timezone, timedelta

OKX_BASE = "https://www.okx.com"
PAIRS = ["BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "LTC-USDT"]
MONTHS_BACK = 12
LEVERAGE = 2.0
SIZE = 2000.0
MAKER_FEE = 0.08 / 100
TAKER_FEE = 0.10 / 100
MAX_HOLD_BARS = 400

BB_PERIOD = 20
BB_MULT = 2.0
RSI_PERIOD = 14
RSI_OVERSOLD = 35
VWAP_WINDOW = 40

SL_GRID = [0.5, 0.8, 1.2, 1.5, 2.0, 3.0]
TP_GRID = [1.0, 1.5, 2.0, 2.5, 3.5, 5.0]  # mean-reversion targets are typically smaller than trend targets


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def fetch_history(symbol, bar, since_ms):
    all_candles, after, tries = [], None, 0
    base_url = f"{OKX_BASE}/api/v5/market/history-candles?instId={symbol}&bar={bar}&limit=100"
    while True:
        url = base_url + (f"&after={after}" if after else "")
        try:
            r = requests.get(url, timeout=10)
            data = r.json().get("data", [])
        except Exception:
            tries += 1
            if tries > 3: break
            time.sleep(1); continue
        if not data: break
        all_candles.extend(data)
        oldest_ts = int(data[-1][0]); after = oldest_ts
        if oldest_ts <= since_ms: break
        time.sleep(0.12)
    parsed = []
    for c in all_candles:
        ts = int(c[0])
        if ts < since_ms: continue
        parsed.append({"ts": ts, "o": float(c[1]), "h": float(c[2]),
                        "l": float(c[3]), "c": float(c[4]), "v": float(c[5])})
    parsed.sort(key=lambda x: x["ts"])
    return parsed


def calc_bollinger(closes, period=BB_PERIOD, mult=BB_MULT):
    if len(closes) < period: return None
    window = closes[-period:]
    ma = sum(window) / period
    var = sum((x-ma)**2 for x in window) / period
    std = var ** 0.5
    return {"ma": ma, "upper": ma + mult*std, "lower": ma - mult*std}


def calc_rsi(closes, period=RSI_PERIOD):
    if len(closes) < period+1: return 50
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i]-closes[i-1]
        gains.append(max(d,0)); losses.append(max(-d,0))
    ag = sum(gains[-period:])/period; al = sum(losses[-period:])/period
    if al == 0: return 100
    return 100 - (100/(1+ag/al))


def calc_vwap(window):
    num = sum(((c["h"]+c["l"]+c["c"])/3) * c["v"] for c in window)
    den = sum(c["v"] for c in window)
    return num/den if den > 0 else None


def find_signals(c15):
    signals = []
    closes = [c["c"] for c in c15]
    for idx in range(max(BB_PERIOD, RSI_PERIOD, VWAP_WINDOW)+1, len(c15)-1):
        bar = c15[idx]
        window_closes = closes[max(0, idx-BB_PERIOD+1):idx+1]
        bb = calc_bollinger(window_closes)
        if bb is None: continue
        touched_lower = bar["l"] <= bb["lower"]
        closed_above_lower = bar["c"] > bb["lower"]
        if not (touched_lower and closed_above_lower): continue
        rsi_window = closes[max(0, idx-RSI_PERIOD):idx+1]
        rsi = calc_rsi(rsi_window)
        if rsi >= RSI_OVERSOLD: continue
        vwap_window = c15[max(0, idx-VWAP_WINDOW+1):idx+1]
        vwap = calc_vwap(vwap_window)
        if vwap is None or bar["c"] >= vwap: continue  # must be below VWAP (discount)
        signals.append(idx)
    return signals


def calc_fee(notional, maker=False):
    rate = MAKER_FEE if maker else TAKER_FEE
    return notional * rate


def simulate_trade_param(c15, entry_idx, sl_pct, tp_pct):
    trail_pct = sl_pct
    partial_pct = round(tp_pct * 0.5, 3)
    partial_ratio = 0.5
    entry = c15[entry_idx]["o"]
    sl = entry * (1 - sl_pct/100)
    tp = entry * (1 + tp_pct/100)
    peak = entry
    size = SIZE
    partial_closed = False
    entry_fee = calc_fee(size*LEVERAGE, maker=True)
    realized = -entry_fee
    for j in range(entry_idx, min(entry_idx+MAX_HOLD_BARS, len(c15))):
        bar = c15[j]
        if bar["h"] > peak:
            peak = bar["h"]
            new_sl = peak*(1-trail_pct/100)
            if new_sl > sl: sl = new_sl
        pnl_pct = (bar["c"]-entry)/entry*LEVERAGE
        if not partial_closed and pnl_pct*100 >= partial_pct:
            partial_size = size*partial_ratio
            partial_pnl = partial_size*pnl_pct
            fee = calc_fee(partial_size*LEVERAGE, maker=True)
            realized += partial_pnl - fee
            size = size*(1-partial_ratio)
            partial_closed = True
            sl = entry
        hit_tp = bar["h"] >= tp
        hit_sl = bar["l"] <= sl
        if hit_tp or hit_sl:
            exit_price = tp if hit_tp else sl
            pnl_pct_final = (exit_price-entry)/entry*LEVERAGE
            pnl_usd = size*pnl_pct_final
            fee = calc_fee(size*LEVERAGE, maker=hit_tp)
            realized += pnl_usd - fee
            result = "WIN" if hit_tp else ("BREAKEVEN" if partial_closed and realized>=0 else "LOSS")
            return {"result": result, "pnl_usd": round(realized,2), "closed_idx": j}
    return None


def run():
    since_ms = int((datetime.now(timezone.utc) - timedelta(days=MONTHS_BACK*30)).timestamp()*1000)
    pair_data = {}
    total_signals = 0
    for n, symbol in enumerate(PAIRS, 1):
        log(f"[{n}/{len(PAIRS)}] {symbol}: downloading 15m candles...")
        c15 = fetch_history(symbol, "15m", since_ms)
        log(f"  {symbol}: {len(c15)} candles")
        if len(c15) < 200:
            log(f"  skip {symbol}: insufficient history")
            continue
        signal_indices = find_signals(c15)
        log(f"  {symbol}: {len(signal_indices)} signals found")
        pair_data[symbol] = {"c15": c15, "signals": signal_indices}
        total_signals += len(signal_indices)
        time.sleep(0.3)

    log(f"Total signals across {len(pair_data)} pairs: {total_signals}")
    log(f"Starting SL/TP grid search: {len(SL_GRID)}x{len(TP_GRID)} combos")

    grid_results = []
    for sl_pct in SL_GRID:
        for tp_pct in TP_GRID:
            if tp_pct <= sl_pct: continue
            trades = []
            for symbol, d in pair_data.items():
                c15 = d["c15"]
                in_position_until = -1
                for idx in d["signals"]:
                    if idx <= in_position_until: continue
                    trade = simulate_trade_param(c15, idx+1, sl_pct, tp_pct)
                    if trade:
                        trades.append(trade)
                        in_position_until = trade["closed_idx"]
            if not trades: continue
            wins = [t for t in trades if t["result"]=="WIN"]
            total_pnl = sum(t["pnl_usd"] for t in trades)
            winrate = len(wins)/len(trades)*100
            rr = tp_pct/sl_pct
            breakeven_wr = 100/(1+rr)
            combo = {"sl_pct": sl_pct, "tp_pct": tp_pct, "rr": round(rr,2),
                     "trades": len(trades), "winrate_pct": round(winrate,2),
                     "breakeven_winrate_pct": round(breakeven_wr,2),
                     "total_pnl_usd": round(total_pnl,2),
                     "edge_vs_breakeven": round(winrate-breakeven_wr,2)}
            grid_results.append(combo)
            log(f"  SL={sl_pct}% TP={tp_pct}% (RR 1:{rr:.1f}): {len(trades)} trades, "
                f"winrate={winrate:.1f}% (breakeven={breakeven_wr:.1f}%), pnl=${total_pnl:,.2f}")

    grid_results.sort(key=lambda x: x["total_pnl_usd"], reverse=True)

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pairs_tested": PAIRS,
        "months_back": MONTHS_BACK,
        "total_signals": total_signals,
        "signal_definition": "Bollinger(20,2) lower band touch + close back above + RSI14<35 + price<VWAP(40)",
        "grid_results_sorted_by_pnl": grid_results,
        "best_combo": grid_results[0] if grid_results else None,
    }

    for path in [os.path.join(tempfile.gettempdir(), "backtest_v3_result.json"),
                 os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_v3_result.json")]:
        with open(path, "w") as f:
            json.dump(result, f, indent=2)

    log("="*60)
    log("TOP 5 COMBOS:")
    for r in grid_results[:5]:
        log(f"  SL={r['sl_pct']}% TP={r['tp_pct']}% -> winrate={r['winrate_pct']}% "
            f"(breakeven={r['breakeven_winrate_pct']}%, edge={r['edge_vs_breakeven']:+.1f}pts), "
            f"pnl=${r['total_pnl_usd']:,.2f}, trades={r['trades']}")
    log("="*60)
    log(f"DONE: {len(grid_results)} combos tested, {total_signals} total signals")

    push_to_github(result)


def push_to_github(result):
    import base64
    GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
    GITHUB_REPO = os.environ.get("GITHUB_REPO", "konstantin514113-dotcom/jarvis-analyst")
    if not GITHUB_TOKEN:
        log("No GITHUB_TOKEN set, result saved only locally")
        return
    content_b64 = base64.b64encode(json.dumps(result, indent=2).encode()).decode()
    r = requests.get(
        f"https://api.github.com/repos/{GITHUB_REPO}/contents/backtest_v3_result.json?ref=state-storage",
        headers={"Authorization": f"token {GITHUB_TOKEN}"}, timeout=10
    )
    sha = r.json().get("sha") if r.status_code == 200 else None
    payload = {"message": "Backtest v3 result (mean reversion)", "content": content_b64, "branch": "state-storage"}
    if sha: payload["sha"] = sha
    resp = requests.put(
        f"https://api.github.com/repos/{GITHUB_REPO}/contents/backtest_v3_result.json",
        headers={"Authorization": f"token {GITHUB_TOKEN}"}, json=payload, timeout=10
    )
    log(f"Pushed to GitHub: status {resp.status_code}")


if __name__ == "__main__":
    run()
