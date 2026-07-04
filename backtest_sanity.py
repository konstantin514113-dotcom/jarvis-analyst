"""
JARVIS SANITY CHECK — сигнальные сделки vs случайные сделки.

Проверяет: действительно ли механический фильтр (RSI/MACD/MA20/score>=89) даёт
преимущество, или те же результаты можно получить, просто входя в сделки наугад
с теми же параметрами (SL/TP/трейлинг/частичное закрытие/комиссии).

Логика: для каждой пары считаем сигнальные точки входа (как в backtest.py),
затем на ТЕХ ЖЕ данных случайно выбираем РОВНО СТОЛЬКО ЖЕ точек входа (без учёта
score), симулируем сделки одинаковым движком для обеих групп и сравниваем winrate.

Если winrate сигнальных сделок заметно выше случайных — фильтр даёт реальный edge.
Если они близки — весь профит (если есть) идёт не от фильтра, а от самой
конструкции SL/TP/трейлинга, и полагаться на фильтр отбора нельзя.

Запуск: python backtest_sanity.py
Переменные окружения: те же, что у backtest.py (GITHUB_TOKEN опционально, BT_PAIRS, BT_MONTHS)
"""

import os, time, json, random, tempfile
from datetime import datetime, timezone, timedelta

from backtest import (
    get_top_pairs, fetch_history, score_bar, simulate_trade,
    PAIRS_COUNT, MONTHS_BACK, log, push_to_github,
)

random.seed(42)  # reproducible random sample


def run_sanity_check():
    pairs = get_top_pairs(PAIRS_COUNT)
    since_ms = int((datetime.now(timezone.utc) - timedelta(days=MONTHS_BACK * 30)).timestamp() * 1000)

    signal_trades = []
    random_trades = []

    for n, symbol in enumerate(pairs, 1):
        try:
            log(f"[{n}/{len(pairs)}] {symbol}: downloading history...")
            c15 = fetch_history(symbol, "15m", since_ms)
            c1h = fetch_history(symbol, "1H", since_ms)
            if len(c15) < 120 or len(c1h) < 30:
                log(f"  skip {symbol}: insufficient history")
                continue

            # --- Pass 1: collect signal indices (same logic as backtest.py) ---
            signal_indices = []
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
                trade = simulate_trade(c15, idx + 1)
                if trade is None:
                    continue
                trade["symbol"] = symbol
                trade["entry_ts"] = c15[idx + 1]["ts"]
                signal_trades.append(trade)
                in_position_until = trade["closed_idx"]
                signal_indices.append(idx)

            n_signals = len(signal_indices)
            if n_signals == 0:
                log(f"  {symbol}: 0 signal trades, skipping random comparison")
                continue

            # --- Pass 2: same COUNT of random entries, no score filter ---
            valid_range = list(range(96, len(c15) - 1))
            random.shuffle(valid_range)
            picked = 0
            in_position_until = -1
            for idx in valid_range:
                if picked >= n_signals:
                    break
                if idx <= in_position_until:
                    continue
                trade = simulate_trade(c15, idx + 1)
                if trade is None:
                    continue
                trade["symbol"] = symbol
                trade["entry_ts"] = c15[idx + 1]["ts"]
                random_trades.append(trade)
                in_position_until = trade["closed_idx"]
                picked += 1

            log(f"  {symbol}: {n_signals} signal trades, {picked} random trades")
            time.sleep(0.2)
        except Exception as e:
            log(f"  ERROR {symbol}: {e}")
            continue

    def summarize(trades):
        if not trades:
            return {"trades": 0, "winrate_pct": 0, "total_pnl_usd": 0, "avg_pnl": 0}
        wins = [t for t in trades if t["result"] == "WIN"]
        total_pnl = sum(t["pnl_usd"] for t in trades)
        return {
            "trades": len(trades),
            "wins": len(wins),
            "winrate_pct": round(len(wins) / len(trades) * 100, 2),
            "total_pnl_usd": round(total_pnl, 2),
            "avg_pnl_per_trade": round(total_pnl / len(trades), 2),
        }

    signal_summary = summarize(signal_trades)
    random_summary = summarize(random_trades)

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pairs_tested": pairs,
        "months_back": MONTHS_BACK,
        "signal_strategy": signal_summary,
        "random_baseline": random_summary,
        "edge_winrate_pct_points": round(signal_summary["winrate_pct"] - random_summary["winrate_pct"], 2),
        "note": ("Same trade management (SL/TP/trailing/partial-close, x2 leverage, OKX fees) applied "
                 "to both groups. Only entry selection differs: signal_strategy uses the mechanical "
                 "RSI/MACD/MA20/score>=89 filter; random_baseline enters the SAME NUMBER of trades per "
                 "pair at randomly chosen bars with no filter. If signal_strategy's winrate is not "
                 "meaningfully higher than random_baseline's, the filter provides little or no edge — "
                 "any apparent profitability comes from the trade construction (SL/TP/trailing) itself, "
                 "not from signal selection."),
    }

    result_path_tmp = os.path.join(tempfile.gettempdir(), "sanity_check_result.json")
    result_path_local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sanity_check_result.json")
    with open(result_path_tmp, "w") as f:
        json.dump(result, f, indent=2)
    with open(result_path_local, "w") as f:
        json.dump(result, f, indent=2)

    log(f"DONE: signal winrate {signal_summary['winrate_pct']}% ({signal_summary['trades']} trades) "
        f"vs random winrate {random_summary['winrate_pct']}% ({random_summary['trades']} trades) — "
        f"edge = {result['edge_winrate_pct_points']:+.2f} pts")
    log(f"Result saved to: {result_path_local}")

    push_to_github_custom(result)


def push_to_github_custom(result):
    """Same as backtest.py's push_to_github but targets a different filename."""
    import requests, base64
    GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
    GITHUB_REPO = os.environ.get("GITHUB_REPO", "konstantin514113-dotcom/jarvis-analyst")
    RESULTS_BRANCH = "state-storage"
    RESULTS_PATH = "sanity_check_result.json"
    if not GITHUB_TOKEN:
        log("No GITHUB_TOKEN set, result saved only locally")
        return
    content_b64 = base64.b64encode(json.dumps(result, indent=2).encode()).decode()
    r = requests.get(
        f"https://api.github.com/repos/{GITHUB_REPO}/contents/{RESULTS_PATH}?ref={RESULTS_BRANCH}",
        headers={"Authorization": f"token {GITHUB_TOKEN}"}, timeout=10
    )
    sha = r.json().get("sha") if r.status_code == 200 else None
    payload = {"message": "Sanity check result", "content": content_b64, "branch": RESULTS_BRANCH}
    if sha:
        payload["sha"] = sha
    resp = requests.put(
        f"https://api.github.com/repos/{GITHUB_REPO}/contents/{RESULTS_PATH}",
        headers={"Authorization": f"token {GITHUB_TOKEN}"}, json=payload, timeout=10
    )
    log(f"Pushed to GitHub ({RESULTS_BRANCH}/{RESULTS_PATH}): status {resp.status_code}")


if __name__ == "__main__":
    run_sanity_check()
