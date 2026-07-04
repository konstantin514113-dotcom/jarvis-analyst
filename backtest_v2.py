"""
JARVIS BACKTEST v2 — EMA9/21 + VWAP + объём + 1H тренд + funding rate.

Тестирует НОВЫЙ сигнал (заменивший нейтральный RSI-фильтр, который провалил
первый бэктест с winrate 3.4%) на 5 отобранных ликвидных парах:
BTC-USDT, ETH-USDT, SOL-USDT, XRP-USDT, LTC-USDT.

Сигнал LONG срабатывает когда ВСЕ условия выполнены на 15m баре:
1. EMA9 пересекает EMA21 снизу вверх (конкретное событие, не нейтральная зона)
2. Объём свечи пересечения выше среднего за 20 баров (подтверждение интереса)
3. Цена выше VWAP (institutional fair value bias)
4. Цена выше EMA50 на 1H (совпадение с более крупным трендом)
5. Funding rate перпетуального фьючерса той же монеты не экстремально высокий
   (избегаем входа в перегретый лонгами рынок)

Сразу же прогоняет grid search по SL/TP (как и в первом раунде — не гадаем
параметры, а перебираем и смотрим на факт).

Запуск: python backtest_v2.py
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

EMA_FAST, EMA_SLOW, EMA_TREND_1H = 9, 21, 50
VOL_LOOKBACK = 20
FUNDING_RATE_MAX_PCT = 0.05

SL_GRID = [0.5, 0.8, 1.2, 1.5, 2.0, 3.0]
TP_GRID = [1.5, 2.5, 3.5, 5.0, 7.0, 10.0]


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


def fetch_funding_history(symbol, since_ms):
    """Historical funding rate for the perpetual swap. Returns list of {ts, rate_pct} sorted ascending."""
    swap_id = symbol.replace("-USDT", "-USDT-SWAP")
    all_rates, after, tries = [], None, 0
    base_url = f"{OKX_BASE}/api/v5/public/funding-rate-history?instId={swap_id}&limit=100"
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
        all_rates.extend(data)
        oldest_ts = int(data[-1]["fundingTime"]); after = oldest_ts
        if oldest_ts <= since_ms: break
        time.sleep(0.12)
    parsed = []
    for r_ in all_rates:
        ts = int(r_["fundingTime"])
        if ts < since_ms: continue
        parsed.append({"ts": ts, "rate_pct": float(r_["fundingRate"]) * 100})
    parsed.sort(key=lambda x: x["ts"])
    return parsed


def _ema_series(closes, period):
    if len(closes) < period: return []
    k = 2/(period+1)
    out = [closes[0]]
    for c in closes[1:]:
        out.append(c*k + out[-1]*(1-k))
    return out


def calc_vwap(window):
    num = sum(((c["h"]+c["l"]+c["c"])/3) * c["v"] for c in window)
    den = sum(c["v"] for c in window)
    return num/den if den > 0 else None


def find_signals(c15, c1h, funding_hist):
    """Returns list of bar indices (15m) where the full EMA/VWAP/volume/trend/funding
    signal fires, using only data available up to that bar (no lookahead)."""
    signals = []
    closes15 = [c["c"] for c in c15]
    closes1h = [c["c"] for c in c1h]
    ema1h_full = _ema_series(closes1h, EMA_TREND_1H)
    # map each 1h index to its ts for alignment
    for idx in range(max(EMA_SLOW+2, 40), len(c15) - 1):
        window = c15[max(0, idx-40):idx+1]
        wcloses = [c["c"] for c in window]
        ema9 = _ema_series(wcloses, EMA_FAST)
        ema21 = _ema_series(wcloses, EMA_SLOW)
        if len(ema9) < 2 or len(ema21) < 2: continue
        cross_up = ema9[-2] <= ema21[-2] and ema9[-1] > ema21[-1]
        if not cross_up: continue
        vols = [c["v"] for c in window]
        if len(vols) < VOL_LOOKBACK+1: continue
        avg_vol = sum(vols[-VOL_LOOKBACK-1:-1]) / VOL_LOOKBACK
        if vols[-1] <= avg_vol: continue
        vwap = calc_vwap(window)
        price = c15[idx]["c"]
        if vwap is None or price <= vwap: continue
        ts = c15[idx]["ts"]
        h1_upto = [i for i, hc in enumerate(c1h) if hc["ts"] <= ts]
        if not h1_upto: continue
        h1_idx = h1_upto[-1]
        if h1_idx < EMA_TREND_1H: continue
        ema50_1h_val = _ema_series(closes1h[:h1_idx+1], EMA_TREND_1H)
        if not ema50_1h_val: continue
        if price <= ema50_1h_val[-1]: continue
        # funding filter: find most recent funding rate at/before ts
        recent_funding = [f for f in funding_hist if f["ts"] <= ts]
        if recent_funding and recent_funding[-1]["rate_pct"] > FUNDING_RATE_MAX_PCT:
            continue
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
        log(f"[{n}/{len(PAIRS)}] {symbol}: downloading 15m/1H candles + funding history...")
        c15 = fetch_history(symbol, "15m", since_ms)
        c1h = fetch_history(symbol, "1H", since_ms)
        funding_hist = fetch_funding_history(symbol, since_ms)
        log(f"  {symbol}: {len(c15)} x15m, {len(c1h)} x1H, {len(funding_hist)} funding points")
        if len(c15) < 200 or len(c1h) < 60:
            log(f"  skip {symbol}: insufficient history")
            continue
        signal_indices = find_signals(c15, c1h, funding_hist)
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
        "signal_definition": "EMA9x21 cross up + volume>avg20 + price>VWAP + price>1H_EMA50 + funding<=0.05%",
        "grid_results_sorted_by_pnl": grid_results,
        "best_combo": grid_results[0] if grid_results else None,
    }

    for path in [os.path.join(tempfile.gettempdir(), "backtest_v2_result.json"),
                 os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_v2_result.json")]:
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
        f"https://api.github.com/repos/{GITHUB_REPO}/contents/backtest_v2_result.json?ref=state-storage",
        headers={"Authorization": f"token {GITHUB_TOKEN}"}, timeout=10
    )
    sha = r.json().get("sha") if r.status_code == 200 else None
    payload = {"message": "Backtest v2 result (EMA/VWAP/funding)", "content": content_b64, "branch": "state-storage"}
    if sha: payload["sha"] = sha
    resp = requests.put(
        f"https://api.github.com/repos/{GITHUB_REPO}/contents/backtest_v2_result.json",
        headers={"Authorization": f"token {GITHUB_TOKEN}"}, json=payload, timeout=10
    )
    log(f"Pushed to GitHub: status {resp.status_code}")


if __name__ == "__main__":
    run()
