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

state = {"signals": [], "last_scan": None, "next_scan": None, "scan_count": 0}

PAIRS = [
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
    "BASED-USDT","LSK-USDT","SCR-USDT","SENT-USDT","PROS-USDT",
    "QTUM-USDT","LPT-USDT","WAXP-USDT","ONE-USDT","CHZ-USDT",
    "BAND-USDT","BAL-USDT","ANKR-USDT","CELO-USDT","AUDIO-USDT",
    "DODO-USDT","XVS-USDT","TWT-USDT","REEF-USDT","SUPER-USDT",
    "PEOPLE-USDT","GTC-USDT","CLV-USDT","UNFI-USDT","DENT-USDT",
    "OL-USDT","ROBO-USDT","KMNO-USDT","SONIC-USDT","AIXBT-USDT",
    "YB-USDT","CTC-USDT","ZORA-USDT","BERA-USDT","IP-USDT",
    "LAYER-USDT","VINE-USDT","PI-USDT","TRUMP-USDT","LRC-USDT",
    "ZRX-USDT","BAT-USDT","ENJ-USDT","STORJ-USDT","PAXG-USDT",
]

SCREEN_PROMPT = """You are an institutional crypto momentum analyst for OKX spot market.
Analyze the provided pairs with full technical data and select TOP 3 most likely to rise in next 60 minutes.

For each pair you receive:
- price, change24h, volume
- RSI14 (overbought >70, oversold <30)
- MACD signal (bullish/bearish crossover)
- MA trend (price vs MA20: above=bullish, below=bearish)
- Candle pattern (last 3 candles: direction and size)
- Distance from daily high (room to grow)
- Funding rate (negative=shorts paying=bullish)

Selection criteria:
1. RSI between 45-65 (momentum but not overbought)
2. MACD bullish or crossing up
3. Price above MA20
4. Volume spike on recent candles
5. Not at daily high (room to grow >1%)
6. Negative or low funding rate

Reply ONLY valid JSON no markdown:
{"top_pairs": [{"symbol": "XXX-USDT", "direction": "LONG", "entry": 0.0, "stop_loss": 0.0, "take_profit": 0.0, "score": 85, "rsi": 55, "macd": "bullish", "reason": "one sentence in Russian"}]}"""

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

def get_candles(symbol, bar="15m", limit=20):
    try:
        r = requests.get(f"{OKX_BASE}/api/v5/market/candles?instId={symbol}&bar={bar}&limit={limit}", timeout=5)
        data = r.json().get("data", [])
        if not data: return None
        candles = [{"t": c[0], "o": float(c[1]), "h": float(c[2]), "l": float(c[3]), "c": float(c[4]), "v": float(c[5])} for c in reversed(data)]
        return candles
    except: return None

def calc_rsi(candles, period=14):
    if len(candles) < period + 1: return 50
    closes = [c["c"] for c in candles]
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0: return 100
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)

def calc_macd(candles):
    if len(candles) < 26: return "unknown"
    closes = [c["c"] for c in candles]
    def ema(data, n):
        k = 2/(n+1)
        e = data[0]
        for d in data[1:]: e = d*k + e*(1-k)
        return e
    ema12 = ema(closes[-12:], 12)
    ema26 = ema(closes[-26:], 26)
    macd = ema12 - ema26
    prev_ema12 = ema(closes[-13:-1], 12)
    prev_ema26 = ema(closes[-27:-1], 26)
    prev_macd = prev_ema12 - prev_ema26
    if macd > 0 and macd > prev_macd: return "bullish"
    if macd < 0 and macd < prev_macd: return "bearish"
    if macd > prev_macd: return "crossing_up"
    return "crossing_down"

def calc_ma(candles, period=20):
    if len(candles) < period: return None
    closes = [c["c"] for c in candles[-period:]]
    return sum(closes) / period

def candle_pattern(candles):
    if len(candles) < 3: return "unknown"
    last3 = candles[-3:]
    directions = ["green" if c["c"] > c["o"] else "red" for c in last3]
    sizes = [abs(c["c"] - c["o"]) / c["o"] * 100 for c in last3]
    pattern = f"{directions[-3]},{directions[-2]},{directions[-1]} sizes:{sizes[-1]:.1f}%"
    return pattern

def get_funding(symbol):
    try:
        inst = symbol.replace("-USDT", "-USDT-SWAP")
        r = requests.get(f"{OKX_BASE}/api/v5/public/funding-rate?instId={inst}", timeout=5)
        d = r.json().get("data", [{}])[0]
        return round(float(d.get("fundingRate", 0)) * 100, 4)
    except: return 0.0

def analyze_pair(symbol):
    t = get_ticker(symbol)
    if not t: return None
    if t["vol24h"] < 5000: return None

    candles = get_candles(symbol, "15m", 30)
    rsi = calc_rsi(candles) if candles else 50
    macd = calc_macd(candles) if candles else "unknown"
    ma20 = calc_ma(candles) if candles else None
    pattern = candle_pattern(candles) if candles else "unknown"
    above_ma = t["price"] > ma20 if ma20 else False
    funding = get_funding(symbol)

    dist = (t["high24h"] - t["price"]) / t["high24h"] * 100 if t["high24h"] > 0 else 0

    # Score
    score = 0
    score += min(t["change24h"] * 3, 30)
    score += min(t["vol24h"] / 50000, 20)
    score += 15 if 45 <= rsi <= 65 else (5 if 35 <= rsi < 45 else 0)
    score += 15 if "bullish" in macd or "crossing_up" in macd else 0
    score += 10 if above_ma else 0
    score += 10 if dist > 1 else 0

    return {
        "symbol": symbol,
        "price": t["price"],
        "change24h": t["change24h"],
        "vol24h": t["vol24h"],
        "high24h": t["high24h"],
        "low24h": t["low24h"],
        "rsi": rsi,
        "macd": macd,
        "above_ma20": above_ma,
        "ma20": round(ma20, 8) if ma20 else None,
        "candle_pattern": pattern,
        "funding": funding,
        "dist_from_high": round(dist, 2),
        "score": round(score, 1),
    }

def scan():
    log.info(f"Scanning {len(PAIRS)} pairs with technical analysis...")
    candidates = []
    for symbol in PAIRS:
        try:
            data = analyze_pair(symbol)
            if data and data["score"] > 10:
                candidates.append(data)
            time.sleep(0.1)
        except Exception as e:
            pass
    candidates.sort(key=lambda x: x["score"], reverse=True)
    log.info(f"Found {len(candidates)} candidates")
    return candidates[:25]

def analyze_with_claude(candidates):
    client = Anthropic(api_key=ANTHROPIC_KEY)
    lines = [f"Market data UTC {datetime.utcnow().strftime('%H:%M')}:\n"]
    for c in candidates:
        lines.append(
            f"{c['symbol']}: price={c['price']:.8f} change={c['change24h']:+.2f}% vol={c['vol24h']:,.0f} "
            f"RSI={c['rsi']} MACD={c['macd']} above_MA20={'YES' if c['above_ma20'] else 'NO'} "
            f"pattern={c['candle_pattern']} funding={c['funding']}% dist_high={c['dist_from_high']:.1f}% score={c['score']}"
        )
    msg = client.messages.create(model="claude-sonnet-4-6", max_tokens=1000,
        system=SCREEN_PROMPT, messages=[{"role": "user", "content": "\n".join(lines)}])
    text = msg.content[0].text.strip().replace("```json","").replace("```","").strip()
    return json.loads(text)

def run_cycle():
    now = datetime.now(timezone.utc)
    if not (SESSION_START <= now.hour < SESSION_END):
        log.info(f"Outside session {SESSION_START}-{SESSION_END} UTC")
        return
    log.info(f"=== Cycle {now.strftime('%H:%M UTC')} ===")
    try:
        candidates = scan()
        if not candidates: return
        result = analyze_with_claude(candidates)
        pairs = result.get("top_pairs", [])
        if not pairs: return

        scan_time = now.strftime("%H:%M UTC")
        state["signals"] = [{"rank": i+1, **p, "scan_time": scan_time} for i, p in enumerate(pairs[:3])]
        state["last_scan"] = scan_time
        state["scan_count"] += 1
        state["next_scan"] = now.timestamp() + INTERVAL_MIN * 60

        header = f"🤖 <b>JARVIS ANALYST</b> | {scan_time}\n━━━━━━━━━━━━━━━━\n"
        signals = []
        for i, p in enumerate(pairs[:3]):
            sym = p["symbol"].replace("-USDT","")
            entry = p.get("entry", 0)
            sl = p.get("stop_loss", 0)
            tp = p.get("take_profit", 0)
            rr = abs((tp-entry)/(entry-sl)) if abs(entry-sl) > 0 else 0
            rsi = p.get("rsi", "—")
            macd = p.get("macd", "—")
            signals.append(
                f"🟢 <b>#{i+1} {sym}/USDT</b>\n"
                f"💰 Вход: <b>{entry}</b>\n"
                f"🛑 SL: {sl}\n"
                f"🎯 TP: {tp}\n"
                f"📊 RR: 1:{rr:.1f} | Score: {p.get('score',0)}\n"
                f"📈 RSI: {rsi} | MACD: {macd}\n"
                f"💬 {p.get('reason','')}"
            )
        tg(header + "\n\n".join(signals) + "\n\n━━━━━━━━━━━━━━━━\n⚠️ Не финансовый совет.")
        log.info(f"Sent {len(pairs)} signals")
    except Exception as e:
        log.error(f"Error: {e}", exc_info=True)

def main():
    log.info(f"JARVIS ANALYST v2 | interval={INTERVAL_MIN}min | RSI+MACD+Candles enabled")
    tg(f"🚀 <b>JARVIS ANALYST v2 запущен!</b>\n📊 RSI + MACD + Свечи + Funding Rate\n⏱ Каждые {INTERVAL_MIN} мин")
    time.sleep(30)
    while True:
        run_cycle()
        log.info(f"Next in {INTERVAL_MIN} min")
        time.sleep(INTERVAL_MIN * 60)

app = Flask(__name__)

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
body{background:#080b0f;color:#c9d1d9;font-family:'JetBrains Mono',monospace;max-width:430px;margin:0 auto;padding-bottom:60px}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}
@keyframes fadeIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
.fade{animation:fadeIn 0.4s ease}
.header{position:sticky;top:0;z-index:10;background:#080b0f;border-bottom:1px solid #0d1117;padding:16px 16px 12px}
.row{display:flex;justify-content:space-between;align-items:flex-start}
.title{font-family:'Syne',sans-serif;font-size:18px;font-weight:800;color:#fff;letter-spacing:2px}
.sub{font-size:9px;color:#30363d;letter-spacing:2px}
.dot{width:7px;height:7px;border-radius:50%;display:inline-block;margin-right:6px;animation:pulse 1.5s infinite}
.stats{display:flex;gap:8px;margin-top:10px}
.stat{flex:1;background:#0d1117;border:1px solid #161b22;border-radius:8px;padding:6px 8px;text-align:center}
.stat-v{font-size:14px;font-weight:700;color:#fff}
.stat-l{font-size:8px;color:#30363d;letter-spacing:1px}
.body{padding:12px}
.card{background:#0d1117;border:1px solid #00e67622;border-radius:14px;padding:14px 16px;margin-bottom:12px}
.card-time{font-size:10px;color:#30363d;margin-bottom:10px}
.sig{margin-bottom:16px;padding-bottom:16px;border-bottom:1px solid #161b22}
.sig:last-child{border-bottom:none;margin-bottom:0;padding-bottom:0}
.sig-title{font-size:14px;font-weight:700;color:#00e676;margin-bottom:8px}
.sig-row{display:flex;justify-content:space-between;font-size:12px;margin-bottom:4px}
.sig-label{color:#455a64}
.sig-reason{font-size:11px;color:#546e7a;margin-top:8px;line-height:1.5;padding-top:8px;border-top:1px solid #161b22}
.badge{font-size:9px;padding:2px 6px;border-radius:4px;font-weight:700;letter-spacing:1px}
.badge-bull{background:#00e67622;color:#00e676}
.badge-bear{background:#ff174422;color:#ff1744}
.badge-neu{background:#58a6ff22;color:#58a6ff}
.empty{background:#0d1117;border:1px solid #161b22;border-radius:14px;padding:40px 20px;text-align:center}
.footer{position:fixed;bottom:0;left:50%;transform:translateX(-50%);width:100%;max-width:430px;background:#080b0f;border-top:1px solid #0d1117;padding:10px;text-align:center;font-size:9px;color:#21262d}
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
    <div class="stat"><div class="stat-v">300</div><div class="stat-l">ПАРЫ</div></div>
    <div class="stat"><div class="stat-v">15м</div><div class="stat-l">ИНТЕРВАЛ</div></div>
  </div>
</div>
<div class="body" id="body">
  <div class="empty"><div style="font-size:24px;margin-bottom:12px">⏳</div><div style="font-size:12px;color:#30363d;letter-spacing:2px">ЗАГРУЗКА</div></div>
</div>
<div class="footer">⚠️ НЕ ЯВЛЯЕТСЯ ФИНАНСОВЫМ СОВЕТОМ · ТОРГУЙ НА СВОЙ РИСК</div>
<script>
let prev = null;
function macdBadge(m) {
  if (!m) return '';
  if (m.includes('bullish')) return '<span class="badge badge-bull">BULL</span>';
  if (m.includes('bearish')) return '<span class="badge badge-bear">BEAR</span>';
  if (m.includes('crossing_up')) return '<span class="badge badge-bull">↑ CROSS</span>';
  if (m.includes('crossing_down')) return '<span class="badge badge-bear">↓ CROSS</span>';
  return '<span class="badge badge-neu">'+m+'</span>';
}
function rsiColor(r) {
  if (r >= 70) return '#ff5252';
  if (r <= 30) return '#69f0ae';
  if (r >= 45 && r <= 65) return '#00e676';
  return '#58a6ff';
}
function render(data) {
  const sigs = data.signals || [];
  document.getElementById('sc').textContent = data.scan_count || 0;
  const dot = document.getElementById('dot');
  const st = document.getElementById('st');
  if (sigs.length > 0) { dot.style.background='#00e676'; st.style.color='#00e676'; st.textContent='LIVE'; }
  else { dot.style.background='#ffea00'; st.style.color='#ffea00'; st.textContent='WAITING'; }
  if (!sigs.length) {
    document.getElementById('body').innerHTML='<div class="empty"><div style="font-size:24px;margin-bottom:12px">⏳</div><div style="font-size:12px;color:#30363d;letter-spacing:2px">ОЖИДАНИЕ СИГНАЛА</div><div style="font-size:11px;color:#21262d;margin-top:6px;line-height:1.6">Бот сканирует рынок каждые 15 минут</div></div>';
    return;
  }
  const changed = JSON.stringify(sigs) !== prev;
  prev = JSON.stringify(sigs);
  document.getElementById('body').innerHTML = '<div class="card '+(changed?'fade':'')+'"><div class="card-time">🤖 JARVIS ANALYST v2 · '+(data.last_scan||'')+'</div>'+
  sigs.map(s => {
    const sym = s.symbol.replace('-USDT','');
    const entry = s.entry||0, sl = s.stop_loss||0, tp = s.take_profit||0;
    const rr = Math.abs(sl&&entry ? (tp-entry)/(entry-sl) : 0);
    return '<div class="sig"><div class="sig-title">🟢 #'+s.rank+' '+sym+'/USDT · Score '+s.score+'</div>'+
    '<div class="sig-row"><span class="sig-label">💰 Вход</span><span style="font-weight:700;color:#fff">'+entry+'</span></div>'+
    '<div class="sig-row"><span class="sig-label">🛑 SL</span><span style="font-weight:700;color:#ff5252">'+sl+'</span></div>'+
    '<div class="sig-row"><span class="sig-label">🎯 TP</span><span style="font-weight:700;color:#69f0ae">'+tp+'</span></div>'+
    '<div class="sig-row"><span class="sig-label">📊 RR</span><span style="color:#58a6ff">1:'+rr.toFixed(1)+'</span></div>'+
    '<div class="sig-row"><span class="sig-label">📈 RSI</span><span style="color:'+rsiColor(s.rsi||50)+'">'+( s.rsi||'—')+'</span></div>'+
    '<div class="sig-row"><span class="sig-label">📉 MACD</span>'+macdBadge(s.macd)+'</div>'+
    '<div class="sig-reason">💬 '+(s.reason||'')+'</div></div>';
  }).join('')+'</div>';
}
function updateTimer(n) {
  if (!n) return;
  const d = Math.max(0, Math.floor(n - Date.now()/1000));
  document.getElementById('tmr').textContent = d>0 ? 'след: '+Math.floor(d/60)+':'+String(d%60).padStart(2,'0') : '';
}
async function fetchData() {
  try {
    const res = await fetch('/signals');
    const data = await res.json();
    render(data);
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

@app.route("/signals")
def signals():
    return jsonify(state)

def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    main()
