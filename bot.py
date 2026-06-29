import os, time, json, logging, requests, threading
from datetime import datetime, timezone
from anthropic import Anthropic
from flask import Flask, Response, jsonify

ANTHROPIC_KEY  = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT  = os.environ["TELEGRAM_CHAT_ID"]
INTERVAL_MIN   = int(os.environ.get("INTERVAL_MIN", "15"))
SESSION_START  = int(os.environ.get("SESSION_START_UTC", "11"))
SESSION_END    = int(os.environ.get("SESSION_END_UTC", "17"))
OKX_BASE = "https://www.okx.com"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=[logging.StreamHandler()])
log = logging.getLogger("JARVIS")

state = {
    "signals": [],
    "last_scan": None,
    "next_scan": None,
    "scan_count": 0,
    "history": [],
    "prev_top_symbols": [],
    "logs": [],  # last 20 log lines
    "status": "starting",
    "pairs_loaded": 0,
    "last_error": None,
}

def slog(msg, level="INFO"):
    line = f"{datetime.utcnow().strftime('%H:%M:%S')} [{level}] {msg}"
    log.info(msg)
    state["logs"].append(line)
    if len(state["logs"]) > 30:
        state["logs"] = state["logs"][-30:]

PAIRS = []  # loaded dynamically from OKX

BASE_PAIRS = [
    "BTC-USDT","ETH-USDT","SOL-USDT","BNB-USDT","XRP-USDT",
    "DOGE-USDT","ADA-USDT","AVAX-USDT","LINK-USDT","DOT-USDT",
    "MATIC-USDT","UNI-USDT","LTC-USDT","BCH-USDT","ATOM-USDT",
    "ETC-USDT","APT-USDT","ARB-USDT","OP-USDT","SUI-USDT",
    "INJ-USDT","TIA-USDT","SEI-USDT","WLD-USDT","PEPE-USDT",
    "SHIB-USDT","FLOKI-USDT","BONK-USDT","WIF-USDT","JUP-USDT",
    "RNDR-USDT","FET-USDT","GRT-USDT","LDO-USDT","AAVE-USDT",
    "CRV-USDT","MKR-USDT","IMX-USDT","SAND-USDT","MANA-USDT",
    "AXS-USDT","BLUR-USDT","DYDX-USDT","GMX-USDT","PENDLE-USDT",
    "STX-USDT","HBAR-USDT","NEAR-USDT","FTM-USDT","FLOW-USDT",
    "TRX-USDT","THETA-USDT","XLM-USDT","EOS-USDT","NEO-USDT",
    "ORDI-USDT","SATS-USDT","BOME-USDT","MEW-USDT","TURBO-USDT",
    "MEME-USDT","NEIRO-USDT","PNUT-USDT","ACT-USDT","GOAT-USDT",
    "ANIME-USDT","KAITO-USDT","MOVE-USDT","HYPE-USDT","S-USDT",
    "MAGIC-USDT","AEVO-USDT","PUMP-USDT","GAS-USDT","NES-USDT",
    "VINE-USDT","LSK-USDT","SCR-USDT","SENT-USDT","QTUM-USDT",
    "LPT-USDT","WAXP-USDT","ONE-USDT","CHZ-USDT","BAND-USDT",
    "BAL-USDT","ANKR-USDT","CELO-USDT","PEOPLE-USDT","DODO-USDT",
    "TWT-USDT","SUPER-USDT","SONIC-USDT","AIXBT-USDT","BERA-USDT",
    "IP-USDT","LAYER-USDT","PI-USDT","TRUMP-USDT","ZRX-USDT",
    "BAT-USDT","ENJ-USDT","STORJ-USDT","GAS-USDT","ZORA-USDT",
]

def load_pairs():
    global PAIRS
    PAIRS = list(dict.fromkeys(BASE_PAIRS))  # deduplicate
    state["pairs_loaded"] = len(PAIRS)
    state["status"] = "ready"
    slog(f"Using {len(PAIRS)} base pairs")
    # Try to extend with OKX dynamic list
    try:
        r = requests.get(f"{OKX_BASE}/api/v5/public/instruments?instType=SPOT", timeout=10)
        instruments = r.json().get("data", [])
        exclude = {"USDC-USDT","BUSD-USDT","TUSD-USDT","USDP-USDT","DAI-USDT","FRAX-USDT","USDD-USDT","WBTC-USDT","WETH-USDT"}
        extra = [i["instId"] for i in instruments if i["instId"].endswith("-USDT") and i.get("state") == "live" and i["instId"] not in exclude]
        if extra:
            all_pairs = list(dict.fromkeys(BASE_PAIRS + extra))
            PAIRS = all_pairs
            state["pairs_loaded"] = len(PAIRS)
            slog(f"Extended to {len(PAIRS)} pairs from OKX")
    except Exception as e:
        slog(f"OKX dynamic load failed, using base list: {e}", "WARN")

SCREEN_PROMPT = """You are an institutional crypto momentum analyst for OKX spot market.
Analyze the provided pairs with full technical data and select TOP 3 most likely to rise in next 60 minutes.
For each pair you receive: price, change24h, volume, RSI14 (15m), RSI14 (1H), MACD (15m), MACD (1H), 1H_confirmed, MA trend, candle pattern, distance from daily high, funding rate.
Selection criteria: ONLY select pairs where htf_confirmed=YES (both 15m AND 1H bullish). RSI 45-65 on 15m, MACD bullish or crossing up on both timeframes, price above MA20, volume spike, not at daily high.
STRICT RULES:
- NEVER select pairs where htf_confirmed=NO
- SL must be exactly entry * 0.985 (1.5% below entry)
- TP must be exactly entry * 1.035 (3.5% above entry)
- If fewer than 3 pairs pass all filters, return only those that pass. Return empty list if none qualify.
Reply ONLY valid JSON no markdown:
{"top_pairs": [{"symbol": "XXX-USDT", "direction": "LONG", "entry": 0.0, "stop_loss": 0.0, "take_profit": 0.0, "score": 85, "rsi": 55, "rsi_1h": 52, "macd": "bullish", "htf_bullish": true, "reason": "one sentence in Russian"}]}"""

def tg(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        log.error(f"Telegram: {e}")

def get_ticker(symbol):
    try:
        r = requests.get(f"{OKX_BASE}/api/v5/market/ticker?instId={symbol}", timeout=5)
        t = r.json().get("data", [{}])[0]
        if not t: return None
        last = float(t.get("last", 0))
        if last == 0: return None
        open24 = float(t.get("open24h", 0)) or last
        change = ((last - open24) / open24 * 100)
        vol = float(t.get("volCcy24h", 0))
        return {"symbol": symbol, "price": last, "change24h": round(change,2),
                "vol24h": round(vol,0), "high24h": float(t.get("high24h",last)), "low24h": float(t.get("low24h",last))}
    except: return None

def get_candles(symbol, bar="15m", limit=30):
    try:
        r = requests.get(f"{OKX_BASE}/api/v5/market/candles?instId={symbol}&bar={bar}&limit={limit}", timeout=5)
        data = r.json().get("data", [])
        if not data: return None
        return [{"t": c[0], "o": float(c[1]), "h": float(c[2]), "l": float(c[3]), "c": float(c[4]), "v": float(c[5])} for c in reversed(data)]
    except: return None

def calc_rsi(candles, period=14):
    if not candles or len(candles) < period + 1: return 50
    closes = [c["c"] for c in candles]
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0: return 100
    return round(100 - (100 / (1 + avg_gain/avg_loss)), 1)

def calc_macd(candles):
    if not candles or len(candles) < 26: return "unknown"
    closes = [c["c"] for c in candles]
    def ema(data, n):
        k = 2/(n+1); e = data[0]
        for d in data[1:]: e = d*k + e*(1-k)
        return e
    macd = ema(closes[-12:], 12) - ema(closes[-26:], 26)
    prev_macd = ema(closes[-13:-1], 12) - ema(closes[-27:-1], 26)
    if macd > 0 and macd > prev_macd: return "bullish"
    if macd < 0 and macd < prev_macd: return "bearish"
    if macd > prev_macd: return "crossing_up"
    return "crossing_down"

def calc_ma(candles, period=20):
    if not candles or len(candles) < period: return None
    return sum(c["c"] for c in candles[-period:]) / period

def get_funding(symbol):
    try:
        r = requests.get(f"{OKX_BASE}/api/v5/public/funding-rate?instId={symbol.replace('-USDT','-USDT-SWAP')}", timeout=5)
        d = r.json().get("data", [{}])[0]
        return round(float(d.get("fundingRate", 0)) * 100, 4)
    except: return 0.0

def analyze_pair(symbol):
    t = get_ticker(symbol)
    if not t or t["vol24h"] < 5000: return None

    # 15m timeframe
    candles_15m = get_candles(symbol, bar="15m", limit=30)
    rsi_15m = calc_rsi(candles_15m)
    macd_15m = calc_macd(candles_15m)
    ma20 = calc_ma(candles_15m)
    above_ma = t["price"] > ma20 if ma20 else False

    # 1H timeframe confirmation
    candles_1h = get_candles(symbol, bar="1H", limit=30)
    rsi_1h = calc_rsi(candles_1h)
    macd_1h = calc_macd(candles_1h)
    htf_bullish = (40 <= rsi_1h <= 70) and ("bullish" in macd_1h or "crossing_up" in macd_1h)

    funding = get_funding(symbol)
    dist = (t["high24h"] - t["price"]) / t["high24h"] * 100 if t["high24h"] > 0 else 0

    score = 0
    score += min(t["change24h"] * 3, 30)
    score += min(t["vol24h"] / 50000, 20)
    score += 15 if 45 <= rsi_15m <= 65 else (5 if 35 <= rsi_15m < 45 else 0)
    score += 15 if "bullish" in macd_15m or "crossing_up" in macd_15m else 0
    score += 10 if above_ma else 0
    score += 10 if dist > 1 else 0
    score += 20 if htf_bullish else -10  # 1H confirmation bonus/penalty

    return {**t, "rsi": rsi_15m, "rsi_1h": rsi_1h, "macd": macd_15m, "macd_1h": macd_1h,
            "htf_bullish": htf_bullish, "above_ma20": above_ma,
            "funding": funding, "dist_from_high": round(dist,2), "score": round(score,1)}

def update_history_prices():
    """Update current prices for all history entries"""
    for entry in state["history"]:
        if entry.get("status") == "active":
            try:
                t = get_ticker(entry["symbol"])
                if t:
                    entry_price = entry["entry"]
                    current = t["price"]
                    pct = round((current - entry_price) / entry_price * 100, 2)
                    entry["current_price"] = current
                    entry["pct_change"] = pct
                    # Check if hit TP or SL
                    if current >= entry["take_profit"]:
                        entry["status"] = "tp_hit"
                        entry["result"] = "WIN"
                    elif current <= entry["stop_loss"]:
                        entry["status"] = "sl_hit"
                        entry["result"] = "LOSS"
            except: pass

def scan():
    log.info(f"Scanning {len(PAIRS)} pairs...")
    candidates = []
    for symbol in PAIRS:
        try:
            data = analyze_pair(symbol)
            if data and data["score"] > 10 and data["htf_bullish"]:
                candidates.append(data)
            time.sleep(0.1)
        except: pass
    candidates.sort(key=lambda x: x["score"], reverse=True)
    log.info(f"Found {len(candidates)} 1H-confirmed candidates")
    return candidates[:25]

def analyze_with_claude(candidates):
    client = Anthropic(api_key=ANTHROPIC_KEY)
    lines = [f"Market data UTC {datetime.utcnow().strftime('%H:%M')}:\n"]
    for c in candidates:
        lines.append(f"{c['symbol']}: price={c['price']:.8f} change={c['change24h']:+.2f}% vol={c['vol24h']:,.0f} RSI15m={c['rsi']} RSI1H={c['rsi_1h']} MACD15m={c['macd']} MACD1H={c['macd_1h']} htf_confirmed={'YES' if c['htf_bullish'] else 'NO'} above_MA20={'YES' if c['above_ma20'] else 'NO'} funding={c['funding']}% dist_high={c['dist_from_high']:.1f}% score={c['score']}")
    msg = client.messages.create(model="claude-sonnet-4-6", max_tokens=1000,
        system=SCREEN_PROMPT, messages=[{"role": "user", "content": "\n".join(lines)}])
    text = msg.content[0].text.strip().replace("```json","").replace("```","").strip()
    return json.loads(text)

def price_monitor():
    """Update history prices every 2 minutes"""
    while True:
        time.sleep(120)
        try:
            update_history_prices()
        except: pass

def send_daily_top4(now):
    if not state["accumulated"]: return
    sorted_pairs = sorted(state["accumulated"].values(), key=lambda x: (x["count"], x["data"].get("score",0)), reverse=True)
    top4 = [p["data"] for p in sorted_pairs[:4]]
    scan_time = now.strftime("%H:%M UTC")
    for p in top4:
        e = p.get("entry", 0)
        if e > 0:
            p["stop_loss"] = round(e * 0.985, 8)
            p["take_profit"] = round(e * 1.035, 8)
    state["signals"] = [{"rank": i+1, **p, "scan_time": scan_time} for i, p in enumerate(top4)]
    state["last_scan"] = scan_time
    for p in top4:
        state["history"].append({"symbol": p["symbol"], "scan_time": scan_time, "entry": p.get("entry",0), "stop_loss": p.get("stop_loss",0), "take_profit": p.get("take_profit",0), "score": p.get("score",0), "rsi": p.get("rsi",0), "macd": p.get("macd",""), "reason": p.get("reason",""), "current_price": p.get("entry",0), "pct_change": 0.0, "status": "active", "result": None})
    counts = {p["data"]["symbol"]: p["count"] for p in sorted_pairs[:4]}
    msgs = []
    for i, p in enumerate(top4):
        sym = p["symbol"].replace("-USDT","")
        e = p.get("entry",0); sl = p.get("stop_loss",0); tp = p.get("take_profit",0)
        rr = abs((tp-e)/(e-sl)) if abs(e-sl) > 0 else 0
        cnt = counts.get(p["symbol"], 0)
        line = "#" + str(i+1) + " " + sym + "/USDT x" + str(cnt) + " scanов"
        line += "\nВход: " + str(e) + "  SL: " + str(sl) + "  TP: " + str(tp)
        line += "\nRR: 1:" + str(round(rr,1)) + " Score: " + str(p.get("score",0))
        line += "\n" + str(p.get("reason",""))
        msgs.append("\U0001f7e2 " + line)
    header = "\U0001f3af JARVIS TOP-4 | " + scan_time + "\n\U0001f4ca Лучшие пары 11:00-13:00 UTC\n" + "-"*16 + "\n"
    tg(header + "\n\n".join(msgs) + "\n\n" + "-"*16 + "\n\u26a0\ufe0f Не финансовый совет.")
    log.info("Sent daily top-4: " + str([p["symbol"] for p in top4]))

def run_cycle():
    now = datetime.now(timezone.utc)
    if now.hour == SESSION_START and now.minute < INTERVAL_MIN:
        state["history"] = []
        state["accumulated"] = {}
        state["daily_signal_sent"] = False
        state["prev_top_symbols"] = []
        log.info("Daily reset")
    if not (SESSION_START <= now.hour < SESSION_END):
        log.info("Outside session " + str(now.hour) + " UTC")
        return
    if now.hour >= 13 and not state["daily_signal_sent"] and state["accumulated"]:
        send_daily_top4(now)
        state["daily_signal_sent"] = True
        return
    if state["daily_signal_sent"]:
        return
    # Reset history at start of day
    if now.hour == SESSION_START and now.minute < INTERVAL_MIN:
        state["history"] = []
    slog(f"=== Cycle {now.strftime('%H:%M UTC')} ===")
    state["status"] = "scanning"
    try:
        candidates = scan()
        if not candidates:
            state["prev_top_symbols"] = []
            state["status"] = "waiting"
            slog("No candidates found")
            return

        # Accumulate pairs across scans
        for c in candidates[:10]:
            sym = c["symbol"]
            if sym not in state["accumulated"]:
                state["accumulated"][sym] = {"count": 0, "data": c}
            state["accumulated"][sym]["count"] += 1
            state["accumulated"][sym]["data"] = c
        log.info("Accumulated " + str(len(state["accumulated"])) + " pairs, top: " + str([c["symbol"] for c in candidates[:3]]))
        state["scan_count"] += 1
        state["next_scan"] = now.timestamp() + INTERVAL_MIN * 60
        return

        result = analyze_with_claude(candidates_to_analyze)
        pairs = result.get("top_pairs", [])
        if not pairs:
            log.info("Claude found no qualifying pairs")
            return

        # Enforce SL/TP percentages
        for p in pairs:
            entry = p.get("entry", 0)
            if entry > 0:
                p["stop_loss"] = round(entry * 0.985, 8)
                p["take_profit"] = round(entry * 1.035, 8)

        scan_time = now.strftime("%H:%M UTC")
        state["signals"] = [{"rank": i+1, **p, "scan_time": scan_time} for i, p in enumerate(pairs[:3])]
        state["last_scan"] = scan_time
        state["scan_count"] += 1
        state["next_scan"] = now.timestamp() + INTERVAL_MIN * 60

        # Filter out pairs already signaled today
        today_symbols = {h["symbol"] for h in state["history"]}
        new_pairs = [p for p in pairs[:3] if p["symbol"] not in today_symbols]
        if not new_pairs:
            log.info("All top pairs already signaled today, skipping")
            return

        # Add to history
        for p in new_pairs:
            state["signals"] = [{"rank": i+1, **p2, "scan_time": scan_time} for i, p2 in enumerate(new_pairs)]
            state["history"].append({
                "symbol": p["symbol"],
                "scan_time": scan_time,
                "entry": p.get("entry", 0),
                "stop_loss": p.get("stop_loss", 0),
                "take_profit": p.get("take_profit", 0),
                "score": p.get("score", 0),
                "rsi": p.get("rsi", 0),
                "macd": p.get("macd", ""),
                "reason": p.get("reason", ""),
                "current_price": p.get("entry", 0),
                "pct_change": 0.0,
                "status": "active",
                "result": None,
            })

        # Send to Telegram
        header = f"🤖 <b>JARVIS ANALYST v2</b> | {scan_time}\n━━━━━━━━━━━━━━━━\n"
        signals = []
        for i, p in enumerate(pairs[:3]):
            sym = p["symbol"].replace("-USDT","")
            entry = p.get("entry",0); sl = p.get("stop_loss",0); tp = p.get("take_profit",0)
            rr = abs((tp-entry)/(entry-sl)) if abs(entry-sl) > 0 else 0
            htf = "✅ 1H подтверждён" if p.get("htf_bullish") else "⚠️ 1H не подтверждён"
            signals.append(f"🟢 <b>#{i+1} {sym}/USDT</b>\n💰 Вход: <b>{entry}</b>\n🛑 SL: {sl}\n🎯 TP: {tp}\n📊 RR: 1:{rr:.1f} | Score: {p.get('score',0)}\n📈 RSI 15m: {p.get('rsi','—')} | 1H: {p.get('rsi_1h','—')}\n📉 MACD: {p.get('macd','—')} | {htf}\n💬 {p.get('reason','')}")
        tg(header + "\n\n".join(signals) + "\n\n━━━━━━━━━━━━━━━━\n⚠️ Не финансовый совет.")
        log.info(f"Sent {len(pairs)} signals")
    except Exception as e:
        log.error(f"Error: {e}", exc_info=True)

def main():
    log.info(f"JARVIS ANALYST v2 | interval={INTERVAL_MIN}min")
    tg(f"🚀 <b>JARVIS ANALYST v2</b>\n📊 RSI + MACD + История сигналов\n⏱ Каждые {INTERVAL_MIN} мин")
    time.sleep(30)
    threading.Thread(target=price_monitor, daemon=True).start()
    while True:
        run_cycle()
        time.sleep(INTERVAL_MIN * 60)

app = Flask(__name__)

# Auto-start when loaded by gunicorn (not just direct python run)
def _startup():
    load_pairs()
    threading.Thread(target=main, daemon=True).start()

import atexit
_startup_thread = threading.Thread(target=_startup, daemon=True)
_startup_thread.start()

DASHBOARD = open("panel.html").read() if __import__("os").path.exists("panel.html") else "<h1>Panel loading...</h1>"

@app.route("/")
def dashboard():
    try:
        html = open("panel.html").read()
    except:
        html = DASHBOARD
    return Response(html, mimetype="text/html")

@app.route("/v2")
def dashboard_v2():
    html = open("/app/panel.html").read() if __import__("os").path.exists("/app/panel.html") else "<h1>Panel not found</h1>"
    return Response(html, mimetype="text/html")

@app.route("/signals")
def signals():
    return jsonify(state)

@app.route("/status")
def status():
    return jsonify({
        "status": state["status"],
        "pairs_loaded": state["pairs_loaded"],
        "scan_count": state["scan_count"],
        "last_scan": state["last_scan"],
        "last_error": state["last_error"],
        "prev_symbols_count": len(state["prev_top_symbols"]),
        "logs": state["logs"][-20:],
    })

def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    main()
