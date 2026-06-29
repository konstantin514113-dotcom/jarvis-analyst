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
            if data and data["score"] >= 89 and data["htf_bullish"]:
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
<title>JARVIS</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Syne:wght@800&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#080b0f;color:#c9d1d9;font-family:'JetBrains Mono',monospace;max-width:430px;margin:0 auto;min-height:100vh;padding-bottom:20px}
.header{padding:16px 16px 10px;display:flex;justify-content:space-between;align-items:center}
.title{font-family:'Syne',sans-serif;font-size:22px;font-weight:800;color:#fff;letter-spacing:3px}
.live{display:flex;align-items:center;gap:6px;font-size:10px}
.dot{width:7px;height:7px;border-radius:50%;display:inline-block;animation:pulse 1.5s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.3}}
.timer{text-align:center;padding:6px;font-size:11px;color:#30363d;letter-spacing:1px}
.timer span{color:#58a6ff}
.cards{padding:8px 12px;display:flex;flex-direction:column;gap:10px}
.card{background:#0d1117;border:1px solid #161b22;border-radius:16px;padding:16px;position:relative;overflow:hidden}
.card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px}
.card.r1::before{background:linear-gradient(90deg,#00e676,#69f0ae)}
.card.r2::before{background:linear-gradient(90deg,#58a6ff,#79c0ff)}
.card.r3::before{background:linear-gradient(90deg,#ffea00,#ffd740)}
.card-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.pair{font-size:18px;font-weight:700;color:#fff}
.score{font-size:11px;padding:3px 8px;border-radius:20px;font-weight:700}
.sc1{background:#00e67211;color:#00e676}
.sc2{background:#58a6ff11;color:#58a6ff}
.sc3{background:#ffea0011;color:#ffea00}
.confirm{font-size:10px;color:#00e676;margin-bottom:12px}
.prices{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:14px}
.price-box{background:#080b0f;border-radius:10px;padding:10px;text-align:center}
.price-label{font-size:8px;color:#455a64;letter-spacing:1px;margin-bottom:4px}
.price-val{font-size:12px;font-weight:700}
.pv-e{color:#fff}.pv-s{color:#ff5252}.pv-t{color:#00e676}
.btns{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.btn-trade{background:linear-gradient(135deg,#00e676,#00c853);border:none;border-radius:10px;padding:13px;font-size:12px;font-weight:700;color:#000;font-family:'JetBrains Mono',monospace;cursor:pointer;letter-spacing:1px}
.btn-skip{background:#161b22;border:1px solid #21262d;border-radius:10px;padding:13px;font-size:12px;color:#546e7a;font-family:'JetBrains Mono',monospace;cursor:pointer}
.day-result{margin:8px 12px 16px;background:#0d1117;border-radius:12px;padding:12px 16px;display:flex;justify-content:space-between;align-items:center}
.day-label{font-size:9px;color:#30363d;letter-spacing:2px}
.day-val{font-size:16px;font-weight:700}
.waiting{padding:80px 20px;text-align:center}
.waiting-icon{font-size:36px;margin-bottom:14px}
.waiting-text{font-size:11px;color:#30363d;letter-spacing:2px;margin-bottom:6px}
.waiting-sub{font-size:10px;color:#21262d}
</style>
</head>
<body>
<div class="header">
  <div class="title">JARVIS</div>
  <div class="live"><div class="dot" id="dot" style="background:#ffea00"></div><span id="st" style="color:#ffea00;font-size:10px">LOADING</span></div>
</div>
<div class="timer">след. скан через <span id="tmr">—</span></div>
<div class="cards" id="cards">
  <div class="waiting"><div class="waiting-icon">⏳</div><div class="waiting-text">ОЖИДАНИЕ СИГНАЛА</div><div class="waiting-sub">Бот сканирует каждые 15 минут</div></div>
</div>
<div class="day-result">
  <div class="day-label">ИТОГ ДНЯ</div>
  <div class="day-val" id="day-val" style="color:#30363d">—</div>
</div>
<script>
function fmt(n){return n!==undefined&&n!==null?parseFloat(n).toPrecision(6)*1:0}
function trade(sym){window.open('https://www.okx.com/trade-swap/'+sym.toLowerCase().replace('-usdt','')+'-usdt-swap','_blank')}

function renderCards(sigs){
  if(!sigs||!sigs.length){
    document.getElementById('cards').innerHTML='<div class="waiting"><div class="waiting-icon">⏳</div><div class="waiting-text">ОЖИДАНИЕ СИГНАЛА</div><div class="waiting-sub">Бот сканирует каждые 15 минут</div></div>';
    return;
  }
  const cls=['r1 sc1','r2 sc2','r3 sc3'];
  const scCls=['sc1','sc2','sc3'];
  document.getElementById('cards').innerHTML = sigs.map((s,i)=>{
    const sym=s.symbol.replace('-USDT','');
    return '<div class="card '+( i===0?'r1':i===1?'r2':'r3')+'">'+
      '<div class="card-head"><div class="pair">'+sym+'/USDT</div><div class="score '+(i===0?'sc1':i===1?'sc2':'sc3')+'">SCORE '+s.score+'</div></div>'+
      '<div class="confirm">✅ 1H подтверждён · двойной скан</div>'+
      '<div class="prices">'+
        '<div class="price-box"><div class="price-label">ВХОД</div><div class="price-val pv-e">'+s.entry+'</div></div>'+
        '<div class="price-box"><div class="price-label">СТОП</div><div class="price-val pv-s">'+s.stop_loss+'</div></div>'+
        '<div class="price-box"><div class="price-label">ТЕЙК</div><div class="price-val pv-t">'+s.take_profit+'</div></div>'+
      '</div>'+
      '<div class="btns">'+
        '<button class="btn-trade" onclick="trade(''+s.symbol+'')">▲ ТОРГОВАТЬ</button>'+
        '<button class="btn-skip">ПРОПУСТИТЬ</button>'+
      '</div>'+
    '</div>';
  }).join('');
}

function updateTimer(next){
  if(!next){document.getElementById('tmr').textContent='—';return;}
  const d=Math.max(0,Math.floor(next-Date.now()/1000));
  document.getElementById('tmr').textContent=d>0?Math.floor(d/60)+':'+String(d%60).padStart(2,'0'):'сканирование...';
}

async function fetchData(){
  try{
    const r=await fetch('/signals');
    const d=await r.json();
    const sigs=d.signals||[];
    const dot=document.getElementById('dot');
    const st=document.getElementById('st');
    if(sigs.length){dot.style.background='#00e676';st.style.color='#00e676';st.textContent='LIVE';}
    else{dot.style.background='#ffea00';st.style.color='#ffea00';st.textContent='WAITING';}
    renderCards(sigs);
    updateTimer(d.next_scan);
    // Day result
    const hist=d.history||[];
    let total=0;
    hist.forEach(h=>{if(h.pct_change)total+=h.pct_change;});
    const dv=document.getElementById('day-val');
    dv.textContent=(total>=0?'+':'')+total.toFixed(2)+'%';
    dv.style.color=total>=0?'#00e676':'#ff1744';
  }catch(e){document.getElementById('st').textContent='ERROR';}
}

fetchData();
setInterval(fetchData,15000);
setInterval(()=>{fetch('/signals').then(r=>r.json()).then(d=>updateTimer(d.next_scan)).catch(()=>{})},1000);
</script>
</body>
</html>"""

@app.route("/")
def dashboard():
    return Response(DASHBOARD, mimetype="text/html")

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
