"""
JARVIS GRID SEARCH — перебор SL/TP/трейлинг на исторических данных.

Данные скачиваются ОДИН РАЗ (это самая долгая часть), затем на них прогоняются
десятки комбинаций SL/TP, чтобы найти, есть ли вообще разумное сочетание
параметров, которое даёт положительный edge на этом рынке — вместо того чтобы
гадать вручную.

Итог первого бэктеста (SL 0.5%/TP 2.5%) показал winrate 3.4% — это в разы хуже
даже случайного блуждания цены, что указывает: узкий фиксированный SL 0.5%
выбивается рыночным шумом внутри 15-минутной свечи, ещё до того как сделка
успевает развиться в любую сторону. Здесь проверяем более широкий диапазон.

Запуск: python backtest_gridsearch.py
"""

import os, time, json, tempfile
from datetime import datetime, timezone, timedelta

from backtest import (
    get_top_pairs, fetch_history, score_bar, calc_fee,
    PAIRS_COUNT, MONTHS_BACK, LEVERAGE, log, MAX_HOLD_BARS,
)

# === Grid of parameters to test ===
SL_GRID   = [0.5, 0.8, 1.2, 1.5, 2.0, 3.0]      # % stop loss
TP_GRID   = [1.5, 2.5, 3.5, 5.0, 7.0, 10.0]     # % take profit
# trailing distance and partial-close level scale with SL/TP for each combo
SIZE = 2000.0


def simulate_trade_param(c15, entry_idx, sl_pct, tp_pct):
    """Same engine as backtest.py's simulate_trade, but SL/TP/trailing/partial are parameterized."""
    trail_pct = sl_pct            # trailing distance = SL distance (reasonable heuristic)
    partial_pct = round(tp_pct * 0.5, 3)   # partial close halfway to TP
    partial_ratio = 0.5

    entry = c15[entry_idx]["o"]
    sl = entry * (1 - sl_pct / 100)
    tp = entry * (1 + tp_pct / 100)
    peak = entry
    size = SIZE
    partial_closed = False
    entry_fee = calc_fee(size * LEVERAGE, maker=True)
    realized = -entry_fee

    for j in range(entry_idx, min(entry_idx + MAX_HOLD_BARS, len(c15))):
        bar = c15[j]
        if bar["h"] > peak:
            peak = bar["h"]
            new_sl = peak * (1 - trail_pct / 100)
            if new_sl > sl:
                sl = new_sl
        pnl_pct = (bar["c"] - entry) / entry * LEVERAGE

        if not partial_closed and pnl_pct * 100 >= partial_pct:
            partial_size = size * partial_ratio
            partial_pnl = partial_size * pnl_pct
            fee = calc_fee(partial_size * LEVERAGE, maker=True)
            realized += partial_pnl - fee
            size = size * (1 - partial_ratio)
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
            result = "WIN" if hit_tp else ("BREAKEVEN" if partial_closed and realized >= 0 else "LOSS")
            return {"result": result, "pnl_usd": round(realized, 2), "closed_idx": j}
    return None


def run_grid_search():
    pairs = get_top_pairs(PAIRS_COUNT)
    since_ms = int((datetime.now(timezone.utc) - timedelta(days=MONTHS_BACK * 30)).timestamp() * 1000)

    # === Download once, find signal entry indices once (independent of SL/TP) ===
    pair_data = {}  # symbol -> {"c15":..., "entry_indices":[...]}
    for n, symbol in enumerate(pairs, 1):
        try:
            log(f"[{n}/{len(pairs)}] {symbol}: downloading history...")
            c15 = fetch_history(symbol, "15m", since_ms)
            c1h = fetch_history(symbol, "1H", since_ms)
            if len(c15) < 120 or len(c1h) < 30:
                log(f"  skip {symbol}: insufficient history")
                continue

            entry_indices = []
            in_position_until = -1
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
                entry_indices.append(idx + 1)
                # NOTE: for signal detection only (not trade simulation), assume a fixed
                # cool-down of 20 bars (~5h) after a signal to avoid re-signaling every bar
                in_position_until = idx + 20

            pair_data[symbol] = {"c15": c15, "entry_indices": entry_indices}
            log(f"  {symbol}: {len(entry_indices)} signal entry points found")
            time.sleep(0.2)
        except Exception as e:
            log(f"  ERROR {symbol}: {e}")
            continue

    total_signals = sum(len(d["entry_indices"]) for d in pair_data.values())
    log(f"Data download complete. {total_signals} total signal entry points across {len(pair_data)} pairs.")
    log(f"Starting grid search: {len(SL_GRID)} SL x {len(TP_GRID)} TP = {len(SL_GRID)*len(TP_GRID)} combinations")

    grid_results = []
    for sl_pct in SL_GRID:
        for tp_pct in TP_GRID:
            if tp_pct <= sl_pct:
                continue  # skip nonsensical combos where TP <= SL
            trades = []
            for symbol, d in pair_data.items():
                c15 = d["c15"]
                for entry_idx in d["entry_indices"]:
                    trade = simulate_trade_param(c15, entry_idx, sl_pct, tp_pct)
                    if trade:
                        trades.append(trade)
            if not trades:
                continue
            wins = [t for t in trades if t["result"] == "WIN"]
            total_pnl = sum(t["pnl_usd"] for t in trades)
            winrate = len(wins) / len(trades) * 100
            rr = tp_pct / sl_pct
            breakeven_wr = 100 / (1 + rr)  # theoretical breakeven winrate for this RR (fee-free approx)
            combo_result = {
                "sl_pct": sl_pct, "tp_pct": tp_pct, "rr": round(rr, 2),
                "trades": len(trades), "winrate_pct": round(winrate, 2),
                "breakeven_winrate_pct": round(breakeven_wr, 2),
                "total_pnl_usd": round(total_pnl, 2),
                "avg_pnl_per_trade": round(total_pnl / len(trades), 3),
                "edge_vs_breakeven": round(winrate - breakeven_wr, 2),
            }
            grid_results.append(combo_result)
            log(f"  SL={sl_pct}% TP={tp_pct}% (RR 1:{rr:.1f}): {len(trades)} trades, "
                f"winrate={winrate:.1f}% (breakeven={breakeven_wr:.1f}%), "
                f"total_pnl=${total_pnl:,.2f}")

    grid_results.sort(key=lambda x: x["total_pnl_usd"], reverse=True)

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pairs_tested": list(pair_data.keys()),
        "months_back": MONTHS_BACK,
        "total_signal_points": total_signals,
        "grid_results_sorted_by_total_pnl": grid_results,
        "best_combo": grid_results[0] if grid_results else None,
        "note": ("Each combo reuses the SAME signal entry points (score>=89 mechanical filter), "
                 "only SL/TP/trailing/partial-close distances differ. trailing_pct = sl_pct, "
                 "partial_close_pct = tp_pct*0.5 for every combo. breakeven_winrate_pct is the "
                 "theoretical minimum winrate needed to break even at that RR (fee-free "
                 "approximation) — compare it to actual winrate_pct to see if the combo has edge."),
    }

    result_path_tmp = os.path.join(tempfile.gettempdir(), "gridsearch_result.json")
    result_path_local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gridsearch_result.json")
    with open(result_path_tmp, "w") as f:
        json.dump(result, f, indent=2)
    with open(result_path_local, "w") as f:
        json.dump(result, f, indent=2)

    log("=" * 60)
    log("TOP 5 COMBOS BY TOTAL PNL:")
    for r in grid_results[:5]:
        log(f"  SL={r['sl_pct']}% TP={r['tp_pct']}% RR=1:{r['rr']} -> "
            f"winrate={r['winrate_pct']}% (breakeven={r['breakeven_winrate_pct']}%, "
            f"edge={r['edge_vs_breakeven']:+.1f}pts), total_pnl=${r['total_pnl_usd']:,.2f}, "
            f"trades={r['trades']}")
    log("=" * 60)
    log(f"DONE: grid search complete, {len(grid_results)} combos tested")
    log(f"Result saved to: {result_path_local}")

    push_to_github_custom(result)


def push_to_github_custom(result):
    import requests, base64
    GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
    GITHUB_REPO = os.environ.get("GITHUB_REPO", "konstantin514113-dotcom/jarvis-analyst")
    RESULTS_BRANCH = "state-storage"
    RESULTS_PATH = "gridsearch_result.json"
    if not GITHUB_TOKEN:
        log("No GITHUB_TOKEN set, result saved only locally")
        return
    content_b64 = base64.b64encode(json.dumps(result, indent=2).encode()).decode()
    r = requests.get(
        f"https://api.github.com/repos/{GITHUB_REPO}/contents/{RESULTS_PATH}?ref={RESULTS_BRANCH}",
        headers={"Authorization": f"token {GITHUB_TOKEN}"}, timeout=10
    )
    sha = r.json().get("sha") if r.status_code == 200 else None
    payload = {"message": "Grid search result", "content": content_b64, "branch": RESULTS_BRANCH}
    if sha:
        payload["sha"] = sha
    resp = requests.put(
        f"https://api.github.com/repos/{GITHUB_REPO}/contents/{RESULTS_PATH}",
        headers={"Authorization": f"token {GITHUB_TOKEN}"}, json=payload, timeout=10
    )
    log(f"Pushed to GitHub ({RESULTS_BRANCH}/{RESULTS_PATH}): status {resp.status_code}")


if __name__ == "__main__":
    run_grid_search()
