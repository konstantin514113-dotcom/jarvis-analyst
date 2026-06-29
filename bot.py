import os, time, json, logging, requests, threading
from datetime import datetime, timezone
from anthropic import Anthropic
from flask import Flask, Response, jsonify

ANTHROPIC_KEY  = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT  = os.environ["TELEGRAM_CHAT_ID"]
INTERVAL_MIN   = int(os.environ.get("INTERVAL_MIN", "15"))
SESSION_START  = int(os.environ.get("SESSION_START_UTC", "7"))
SESSION_END    = int(os.environ.get("SESSION_END_UTC", "21"))
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

def run_cycle():
    now = datetime.now(timezone.utc)
    if not (SESSION_START <= now.hour < SESSION_END):
        log.info(f"Outside session")
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

        # Double-scan confirmation: only pairs seen in previous scan too
        current_symbols = [c["symbol"] for c in candidates[:10]]
        if state["prev_top_symbols"]:
            confirmed = [c for c in candidates if c["symbol"] in state["prev_top_symbols"]]
            log.info(f"Double-confirmed pairs: {[c['symbol'] for c in confirmed]}")
            if not confirmed:
                log.info("No double-confirmed pairs this scan, waiting...")
                state["prev_top_symbols"] = current_symbols
                state["scan_count"] += 1
                state["next_scan"] = now.timestamp() + INTERVAL_MIN * 60
                return
            candidates_to_analyze = confirmed[:25]
        else:
            log.info("First scan of session, storing symbols for next confirmation")
            state["prev_top_symbols"] = current_symbols
            state["scan_count"] += 1
            state["next_scan"] = now.timestamp() + INTERVAL_MIN * 60
            return

        state["prev_top_symbols"] = current_symbols

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

        # Add to history
        for p in pairs[:3]:
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

DASHBOARD = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>JARVIS ANALYST</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Syne:wght@800&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#080b0f;color:#c9d1d9;font-family:'JetBrains Mono',monospace;max-width:430px;margin:0 auto;padding-bottom:70px}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}
@keyframes fadeIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
.fade{animation:fadeIn 0.4s ease}
.header{position:sticky;top:0;z-index:10;background:#080b0f;border-bottom:1px solid #0d1117;padding:14px 14px 10px}
.row{display:flex;justify-content:space-between;align-items:flex-start}
.title{font-family:'Syne',sans-serif;font-size:18px;font-weight:800;color:#fff;letter-spacing:2px}
.sub{font-size:9px;color:#30363d;letter-spacing:2px}
.dot{width:7px;height:7px;border-radius:50%;display:inline-block;margin-right:6px;animation:pulse 1.5s infinite}
.stats{display:flex;gap:6px;margin-top:10px}
.stat{flex:1;background:#0d1117;border:1px solid #161b22;border-radius:8px;padding:6px 8px;text-align:center}
.stat-v{font-size:13px;font-weight:700;color:#fff}
.stat-l{font-size:8px;color:#30363d;letter-spacing:1px}
.tabs{display:flex;border-bottom:1px solid #161b22;margin:0 14px}
.tab{flex:1;text-align:center;padding:10px 0;font-size:10px;letter-spacing:1px;color:#30363d;cursor:pointer;border-bottom:2px solid transparent;transition:all 0.2s}
.tab.active{color:#00e676;border-bottom-color:#00e676}
.body{padding:12px}
.card{background:#0d1117;border:1px solid #00e67622;border-radius:14px;padding:14px 16px;margin-bottom:12px}
.card-time{font-size:10px;color:#30363d;margin-bottom:10px}
.sig{margin-bottom:14px;padding-bottom:14px;border-bottom:1px solid #161b22}
.sig:last-child{border-bottom:none;margin-bottom:0;padding-bottom:0}
.sig-title{font-size:13px;font-weight:700;color:#00e676;margin-bottom:6px}
.sig-row{display:flex;justify-content:space-between;font-size:11px;margin-bottom:3px}
.sig-label{color:#455a64}
.sig-reason{font-size:10px;color:#546e7a;margin-top:6px;line-height:1.5;padding-top:6px;border-top:1px solid #161b22}
.badge{font-size:9px;padding:2px 5px;border-radius:4px;font-weight:700}
.badge-bull{background:#00e67222;color:#00e672}
.badge-bear{background:#ff174422;color:#ff1744}
.badge-neu{background:#58a6ff22;color:#58a6ff}
.hist-item{background:#0d1117;border-radius:10px;padding:10px 12px;margin-bottom:8px;border-left:3px solid #161b22}
.hist-item.win{border-left-color:#00e676}
.hist-item.loss{border-left-color:#ff1744}
.hist-item.active{border-left-color:#ffea00}
.hist-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
.hist-sym{font-size:12px;font-weight:700;color:#eceff1}
.hist-time{font-size:9px;color:#30363d}
.hist-bars{margin-top:6px}
.bar-row{display:flex;align-items:center;gap:6px;margin-bottom:3px}
.bar-label{font-size:9px;color:#455a64;width:28px}
.bar-track{flex:1;height:4px;background:#161b22;border-radius:2px;overflow:hidden}
.bar-fill{height:100%;border-radius:2px;transition:width 0.3s}
.bar-val{font-size:9px;width:45px;text-align:right}
.pct-badge{font-size:10px;font-weight:700;padding:2px 6px;border-radius:4px}
.empty{background:#0d1117;border:1px solid #161b22;border-radius:14px;padding:30px 20px;text-align:center}
.footer{position:fixed;bottom:0;left:50%;transform:translateX(-50%);width:100%;max-width:430px;background:#080b0f;border-top:1px solid #0d1117;padding:8px;text-align:center;font-size:9px;color:#21262d}
</style>
</head>
<body>
<div class="header">
  <div class="row">
    <div><div class="title">JARVIS</div><div class="sub">ANALYST v2 · RSI · MACD · OKX</div></div>
    <div>
      <div><span class="dot" id="dot" style="background:#ffea00"></span><span id="st" style="font-size:10px;color:#ffea00">LOADING</span></div>
      <div id="tmr" style="font-size:10px;color:#30363d;margin-top:2px;text-align:right"></div>
    </div>
  </div>
  <div class="stats">
    <div class="stat"><div class="stat-v" id="sc">0</div><div class="stat-l">СКАНОВ</div></div>
    <div class="stat"><div class="stat-v" id="hc">0</div><div class="stat-l">СИГНАЛОВ</div></div>
    <div class="stat"><div class="stat-v" id="wc" style="color:#00e676">0</div><div class="stat-l">ПОБЕД</div></div>
    <div class="stat"><div class="stat-v" id="lc" style="color:#ff1744">0</div><div class="stat-l">ПОТЕРЬ</div></div>
  </div>
</div>
<div class="tabs">
  <div class="tab active" id="tab-signals" onclick="switchTab('signals')">СИГНАЛЫ</div>
  <div class="tab" id="tab-history" onclick="switchTab('history')">ИСТОРИЯ ДНЯ</div>
</div>
<div class="body" id="body-signals">
  <div class="empty"><div style="font-size:24px;margin-bottom:10px">⏳</div><div style="font-size:11px;color:#30363d">ЗАГРУЗКА...</div></div>
</div>
<div class="body" id="body-history" style="display:none">
  <div class="empty"><div style="font-size:24px;margin-bottom:10px">📋</div><div style="font-size:11px;color:#30363d">ИСТОРИЯ ПОЯВИТСЯ ПОСЛЕ ПЕРВОГО СИГНАЛА</div></div>
</div>
<div class="footer">⚠️ НЕ ЯВЛЯЕТСЯ ФИНАНСОВЫМ СОВЕТОМ · ТОРГУЙ НА СВОЙ РИСК</div>
<script>
let currentTab = 'signals';
let prevSig = null;
let data = {};

function switchTab(tab) {
  currentTab = tab;
  document.getElementById('tab-signals').className = 'tab' + (tab==='signals'?' active':'');
  document.getElementById('tab-history').className = 'tab' + (tab==='history'?' active':'');
  document.getElementById('body-signals').style.display = tab==='signals' ? 'block' : 'none';
  document.getElementById('body-history').style.display = tab==='history' ? 'block' : 'none';
}

function macdBadge(m) {
  if (!m) return '';
  if (m.includes('bullish')) return '<span class="badge badge-bull">BULL</span>';
  if (m.includes('bearish')) return '<span class="badge badge-bear">BEAR</span>';
  if (m.includes('crossing_up')) return '<span class="badge badge-bull">↑</span>';
  return '<span class="badge badge-neu">↓</span>';
}

function rsiColor(r) {
  if (r >= 70) return '#ff5252';
  if (r <= 30) return '#69f0ae';
  if (r >= 45 && r <= 65) return '#00e676';
  return '#58a6ff';
}

function renderSignals(d) {
  const sigs = d.signals || [];
  if (!sigs.length) {
    document.getElementById('body-signals').innerHTML='<div class="empty"><div style="font-size:24px;margin-bottom:10px">⏳</div><div style="font-size:11px;color:#30363d;letter-spacing:1px">ОЖИДАНИЕ СИГНАЛА</div><div style="font-size:10px;color:#21262d;margin-top:6px">Бот сканирует каждые 15 минут</div></div>';
    return;
  }
  const changed = JSON.stringify(sigs) !== prevSig;
  prevSig = JSON.stringify(sigs);
  document.getElementById('body-signals').innerHTML = '<div class="card '+(changed?'fade':'')+'"><div class="card-time">🤖 JARVIS ANALYST v2 · '+(d.last_scan||'')+'</div>'+
  sigs.map(s => {
    const sym = s.symbol.replace('-USDT','');
    const e=s.entry||0,sl=s.stop_loss||0,tp=s.take_profit||0;
    const rr=Math.abs(sl&&e?(tp-e)/(e-sl):0);
    return '<div class="sig"><div class="sig-title">🟢 #'+s.rank+' '+sym+'/USDT · Score '+s.score+'</div>'+
    '<div class="sig-row"><span class="sig-label">💰 Вход</span><span style="font-weight:700;color:#fff">'+e+'</span></div>'+
    '<div class="sig-row"><span class="sig-label">🛑 SL</span><span style="color:#ff5252;font-weight:700">'+sl+'</span></div>'+
    '<div class="sig-row"><span class="sig-label">🎯 TP</span><span style="color:#69f0ae;font-weight:700">'+tp+'</span></div>'+
    '<div class="sig-row"><span class="sig-label">📊 RR</span><span style="color:#58a6ff">1:'+rr.toFixed(1)+'</span></div>'+
    '<div class="sig-row"><span class="sig-label">📈 RSI 15m</span><span style="color:'+rsiColor(s.rsi||50)+'">'+( s.rsi||'—')+'</span></div>'+
    '<div class="sig-row"><span class="sig-label">📊 RSI 1H</span><span style="color:'+rsiColor(s.rsi_1h||50)+'">'+(s.rsi_1h||'—')+'</span></div>'+
    '<div class="sig-row"><span class="sig-label">📉 MACD</span>'+macdBadge(s.macd)+'</div>'+
    '<div class="sig-row"><span class="sig-label">🕐 1H</span><span style="color:'+(s.htf_bullish?'#00e676':'#ff5252')+'">'+(s.htf_bullish?'✅ ПОДТВЕРЖДЁН':'⚠️ НЕТ')+'</span></div>'+
    '<div class="sig-reason">💬 '+(s.reason||'')+'</div></div>';
  }).join('')+'</div>';
}

function renderHistory(d) {
  const hist = (d.history || []).slice().reverse();
  if (!hist.length) {
    document.getElementById('body-history').innerHTML='<div class="empty"><div style="font-size:24px;margin-bottom:10px">📋</div><div style="font-size:11px;color:#30363d">ИСТОРИЯ ПОЯВИТСЯ ПОСЛЕ ПЕРВОГО СИГНАЛА</div></div>';
    return;
  }
  let wins=0,losses=0;
  hist.forEach(h=>{ if(h.result==='WIN') wins++; if(h.result==='LOSS') losses++; });
  document.getElementById('wc').textContent=wins;
  document.getElementById('lc').textContent=losses;
  document.getElementById('hc').textContent=hist.length;

  document.getElementById('body-history').innerHTML = hist.map(h => {
    const sym = h.symbol.replace('-USDT','');
    const pct = h.pct_change || 0;
    const status = h.status;
    const cls = status==='tp_hit'?'win':status==='sl_hit'?'loss':'active';
    const pctColor = pct > 0 ? '#00e676' : pct < 0 ? '#ff1744' : '#58a6ff';
    const statusLabel = status==='tp_hit'?'✅ TP HIT':status==='sl_hit'?'❌ SL HIT':'🔵 ACTIVE';

    // Progress bars for entry, current, sl, tp
    const range = h.take_profit - h.stop_loss;
    const slPct = range > 0 ? 0 : 0;
    const tpPct = 100;
    const entryPct = range > 0 ? ((h.entry - h.stop_loss) / range * 100) : 50;
    const currPct = range > 0 ? Math.max(0,Math.min(100,(h.current_price - h.stop_loss) / range * 100)) : 50;

    return '<div class="hist-item '+cls+'">'+
      '<div class="hist-header">'+
        '<span class="hist-sym">'+sym+'/USDT</span>'+
        '<span class="hist-time">'+h.scan_time+'</span>'+
      '</div>'+
      '<div class="sig-row">'+
        '<span style="font-size:10px;color:#455a64">'+statusLabel+'</span>'+
        '<span class="pct-badge" style="background:'+pctColor+'22;color:'+pctColor+'">'+(pct>=0?'+':'')+pct.toFixed(2)+'%</span>'+
      '</div>'+
      '<div class="hist-bars">'+
        '<div class="bar-row"><span class="bar-label" style="color:#455a64">SL</span><div class="bar-track"><div class="bar-fill" style="width:0%;background:#ff1744"></div></div><span class="bar-val" style="color:#ff5252;font-size:9px">'+h.stop_loss+'</span></div>'+
        '<div class="bar-row"><span class="bar-label" style="color:#ffea00">NOW</span><div class="bar-track"><div class="bar-fill" style="width:'+currPct.toFixed(0)+'%;background:'+(pct>=0?'#00e676':'#ff1744')+'"></div></div><span class="bar-val" style="color:#fff;font-size:9px">'+h.current_price+'</span></div>'+
        '<div class="bar-row"><span class="bar-label" style="color:#69f0ae">TP</span><div class="bar-track"><div class="bar-fill" style="width:100%;background:#00e67633"></div></div><span class="bar-val" style="color:#69f0ae;font-size:9px">'+h.take_profit+'</span></div>'+
      '</div>'+
      '<div style="font-size:9px;color:#30363d;margin-top:4px">Вход: '+h.entry+' · RSI: '+h.rsi+'</div>'+
    '</div>';
  }).join('');
}

function updateTimer(n) {
  if (!n) return;
  const d = Math.max(0, Math.floor(n - Date.now()/1000));
  document.getElementById('tmr').textContent = d>0 ? 'след: '+Math.floor(d/60)+':'+String(d%60).padStart(2,'0') : 'сканирование...';
}

async function fetchData() {
  try {
    const res = await fetch('/signals');
    data = await res.json();
    document.getElementById('sc').textContent = data.scan_count || 0;
    const dot = document.getElementById('dot');
    const st = document.getElementById('st');
    if ((data.signals||[]).length > 0) { dot.style.background='#00e676'; st.style.color='#00e676'; st.textContent='LIVE'; }
    else { dot.style.background='#ffea00'; st.style.color='#ffea00'; st.textContent='WAITING'; }
    renderSignals(data);
    renderHistory(data);
    setInterval(()=>updateTimer(data.next_scan), 1000);
  } catch(e) { document.getElementById('st').textContent='ERROR'; }
}

fetchData();
setInterval(fetchData, 15000);
</script>
</body>
</html>"""

@app.route("/")
def dashboard():
    return Response(DASHBOARD, mimetype="text/html")

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
