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

# Store last signals
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

SCREEN_PROMPT = """You are a crypto momentum analyst for OKX spot market.
Analyze the provided pairs and select TOP 3 most likely to rise in next 60 minutes.
Criteria: positive momentum today, high volume, not at daily high, bullish structure.
Reply ONLY valid JSON no markdown:
{"top_pairs": [{"symbol": "XXX-USDT", "direction": "LONG", "entry": 0.0, "stop_loss": 0.0, "take_profit": 0.0, "score": 85, "reason": "one sentence in Russian"}]}"""

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

def scan():
    log.info(f"Scanning {len(PAIRS)} pairs...")
    candidates = []
    for symbol in PAIRS:
        t = get_ticker(symbol)
        if not t: continue
        if t["vol24h"] < 5000: continue
        dist = (t["high24h"] - t["price"]) / t["high24h"] * 100 if t["high24h"] > 0 else 0
        score = t["change24h"] * 3 + min(t["vol24h"]/50000, 30) + (10 if dist > 2 else 0)
        candidates.append({**t, "score": round(score,1), "dist_from_high": round(dist,2)})
        time.sleep(0.05)
    candidates.sort(key=lambda x: x["score"], reverse=True)
    log.info(f"Found {len(candidates)} candidates")
    return candidates[:25]

def analyze(candidates):
    client = Anthropic(api_key=ANTHROPIC_KEY)
    lines = [f"Market data UTC {datetime.utcnow().strftime('%H:%M')}:\n"]
    for c in candidates:
        lines.append(f"{c['symbol']}: price={c['price']:.8f} change={c['change24h']:+.2f}% vol={c['vol24h']:,.0f} dist_from_high={c['dist_from_high']:.1f}%")
    msg = client.messages.create(model="claude-sonnet-4-6", max_tokens=800,
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
        if not candidates:
            return
        result = analyze(candidates)
        pairs = result.get("top_pairs", [])
        if not pairs:
            return

        # Store signals in state
        scan_time = now.strftime("%H:%M UTC")
        state["signals"] = [{"rank": i+1, **p, "scan_time": scan_time} for i, p in enumerate(pairs[:3])]
        state["last_scan"] = scan_time
        state["scan_count"] += 1
        next_time = datetime.now(timezone.utc).timestamp() + INTERVAL_MIN * 60
        state["next_scan"] = next_time

        # Send to Telegram
        header = f"🤖 <b>JARVIS ANALYST</b> | {scan_time}\n━━━━━━━━━━━━━━━━\n"
        signals = []
        for i, p in enumerate(pairs[:3]):
            sym = p["symbol"].replace("-USDT","")
            entry = p.get("entry", 0)
            sl = p.get("stop_loss", 0)
            tp = p.get("take_profit", 0)
            rr = abs((tp-entry)/(entry-sl)) if abs(entry-sl) > 0 else 0
            signals.append(f"🟢 <b>#{i+1} {sym}/USDT</b>\n💰 Вход: <b>{entry}</b>\n🛑 SL: {sl}\n🎯 TP: {tp}\n📊 RR: 1:{rr:.1f} | Score: {p.get('score',0)}\n💬 {p.get('reason','')}")
        tg(header + "\n\n".join(signals) + "\n\n━━━━━━━━━━━━━━━━\n⚠️ Не финансовый совет.")
        log.info(f"Sent {len(pairs)} signals")
    except Exception as e:
        log.error(f"Error: {e}", exc_info=True)

def main():
    log.info(f"JARVIS ANALYST | interval={INTERVAL_MIN}min")
    tg(f"🚀 <b>JARVIS ANALYST запущен!</b>\n⏱ Каждые {INTERVAL_MIN} мин\n🌐 Dashboard доступен")
    time.sleep(30)
    while True:
        run_cycle()
        log.info(f"Next in {INTERVAL_MIN} min")
        time.sleep(INTERVAL_MIN * 60)

# Flask web dashboard
app = Flask(__name__)

DASHBOARD = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
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
.status{font-size:10px;letter-spacing:1px}
.timer{font-size:10px;color:#30363d;margin-top:2px;text-align:right}
.stats{display:flex;gap:8px;margin-top:10px}
.stat{flex:1;background:#0d1117;border:1px solid #161b22;border-radius:8px;padding:6px 8px;text-align:center}
.stat-v{font-size:14px;font-weight:700;color:#fff}
.stat-l{font-size:8px;color:#30363d;letter-spacing:1px}
.body{padding:12px}
.card{background:#0d1117;border:1px solid #00e67622;border-radius:14px;padding:14px 16px;margin-bottom:12px}
.card-time{font-size:10px;color:#30363d;margin-bottom:8px}
.sig{margin-bottom:16px;padding-bottom:16px;border-bottom:1px solid #0d1117}
.sig:last-child{border-bottom:none;margin-bottom:0;padding-bottom:0}
.sig-title{font-size:14px;font-weight:700;color:#00e676;margin-bottom:6px}
.sig-row{display:flex;justify-content:space-between;font-size:12px;margin-bottom:3px}
.sig-label{color:#455a64}
.sig-val{font-weight:700}
.sig-reason{font-size:11px;color:#546e7a;margin-top:6px;line-height:1.5}
.empty{background:#0d1117;border:1px solid #161b22;border-radius:14px;padding:40px 20px;text-align:center}
.empty-icon{font-size:28px;margin-bottom:12px}
.empty-t{font-size:12px;color:#30363d;letter-spacing:2px;margin-bottom:6px}
.empty-s{font-size:11px;color:#21262d;line-height:1.6}
.footer{position:fixed;bottom:0;left:50%;transform:translateX(-50%);width:100%;max-width:430px;background:#080b0f;border-top:1px solid #0d1117;padding:10px;text-align:center;font-size:9px;color:#21262d;letter-spacing:1px}
</style>
</head>
<body>
<div class="header">
  <div class="row">
    <div><div class="title">JARVIS</div><div class="sub">ANALYST · 300 PAIRS · OKX</div></div>
    <div>
      <div><span class="dot" id="dot" style="background:#ffea00"></span><span class="status" id="st" style="color:#ffea00">LOADING</span></div>
      <div class="timer" id="tmr"></div>
    </div>
  </div>
  <div class="stats">
    <div class="stat"><div class="stat-v" id="sc">0</div><div class="stat-l">СКАНОВ</div></div>
    <div class="stat"><div class="stat-v">300</div><div class="stat-l">ПАРЫ</div></div>
    <div class="stat"><div class="stat-v">15м</div><div class="stat-l">ИНТЕРВАЛ</div></div>
  </div>
</div>
<div class="body" id="body">
  <div class="empty"><div class="empty-icon">⏳</div><div class="empty-t">ЗАГРУЗКА</div><div class="empty-s">Получаем данные...</div></div>
</div>
<div class="footer">⚠️ НЕ ЯВЛЯЕТСЯ ФИНАНСОВЫМ СОВЕТОМ · ТОРГУЙ НА СВОЙ РИСК</div>
<script>
let prevSignals = null;

function fmtRR(entry, sl, tp) {
  const r = sl && entry ? Math.abs((tp-entry)/(entry-sl)) : 0;
  return r > 0 ? `1:${r.toFixed(1)}` : '—';
}

function renderSignals(data) {
  const body = document.getElementById('body');
  const sigs = data.signals || [];
  
  document.getElementById('sc').textContent = data.scan_count || 0;
  
  const dot = document.getElementById('dot');
  const st = document.getElementById('st');
  if (sigs.length > 0) {
    dot.style.background = '#00e676';
    st.style.color = '#00e676';
    st.textContent = 'LIVE';
  } else {
    dot.style.background = '#ffea00';
    st.style.color = '#ffea00';
    st.textContent = 'WAITING';
  }

  if (sigs.length === 0) {
    body.innerHTML = '<div class="empty"><div class="empty-icon">⏳</div><div class="empty-t">ОЖИДАНИЕ СИГНАЛА</div><div class="empty-s">Бот сканирует рынок каждые 15 минут<br>Сигналы появятся здесь автоматически</div></div>';
    return;
  }

  const changed = JSON.stringify(sigs) !== prevSignals;
  prevSignals = JSON.stringify(sigs);

  body.innerHTML = `<div class="card ${changed ? 'fade' : ''}">
    <div class="card-time">🤖 JARVIS ANALYST · ${data.last_scan || ''}</div>
    ${sigs.map(s => {
      const sym = s.symbol.replace('-USDT','');
      const rr = fmtRR(s.entry, s.stop_loss, s.take_profit);
      return `<div class="sig">
        <div class="sig-title">🟢 #${s.rank} ${sym}/USDT · Score ${s.score}</div>
        <div class="sig-row"><span class="sig-label">💰 Вход</span><span class="sig-val" style="color:#fff">${s.entry}</span></div>
        <div class="sig-row"><span class="sig-label">🛑 SL</span><span class="sig-val" style="color:#ff5252">${s.stop_loss}</span></div>
        <div class="sig-row"><span class="sig-label">🎯 TP</span><span class="sig-val" style="color:#69f0ae">${s.take_profit}</span></div>
        <div class="sig-row"><span class="sig-label">📊 RR</span><span class="sig-val" style="color:#58a6ff">${rr}</span></div>
        <div class="sig-reason">💬 ${s.reason || ''}</div>
      </div>`;
    }).join('')}
  </div>`;
}

function updateTimer(nextScan) {
  if (!nextScan) return;
  const diff = Math.max(0, Math.floor(nextScan - Date.now()/1000));
  const m = Math.floor(diff/60);
  const s = diff%60;
  document.getElementById('tmr').textContent = diff > 0 ? `след: ${m}:${String(s).padStart(2,'0')}` : 'сканирование...';
}

async function fetchData() {
  try {
    const res = await fetch('/signals');
    const data = await res.json();
    renderSignals(data);
    setInterval(() => updateTimer(data.next_scan), 1000);
  } catch(e) {
    document.getElementById('st').textContent = 'ERROR';
  }
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
