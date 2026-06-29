import os, time, json, logging, requests
from datetime import datetime, timezone
from anthropic import Anthropic

ANTHROPIC_KEY  = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT  = os.environ["TELEGRAM_CHAT_ID"]
INTERVAL_MIN   = int(os.environ.get("INTERVAL_MIN", "15"))
SESSION_START  = int(os.environ.get("SESSION_START_UTC", "7"))
SESSION_END    = int(os.environ.get("SESSION_END_UTC", "21"))
OKX_BASE = "https://www.okx.com"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=[logging.StreamHandler()])
log = logging.getLogger("JARVIS")

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
    "YB-USDT","DYDX-USDT","CTC-USDT","ZORA-USDT","BERA-USDT",
    "IP-USDT","LAYER-USDT","VINE-USDT","HYPE-USDT","PI-USDT",
    "TRUMP-USDT","MELANIA-USDT","TST-USDT","PAXG-USDT","LRC-USDT",
    "ZRX-USDT","BAT-USDT","ENJ-USDT","CHZ-USDT","STORJ-USDT",
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
            tg("⏸ Jarvis: нет подходящих пар")
            return
        result = analyze(candidates)
        pairs = result.get("top_pairs", [])
        if not pairs:
            tg("⏸ Jarvis: нет сигналов")
            return
        header = f"🤖 <b>JARVIS ANALYST</b> | {now.strftime('%H:%M UTC')}\n━━━━━━━━━━━━━━━━\n"
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
        tg(f"❌ Ошибка: {str(e)[:100]}")

def main():
    log.info(f"JARVIS ANALYST started | interval={INTERVAL_MIN}min")
    tg(f"🚀 <b>JARVIS ANALYST запущен!</b>\n⏱ Каждые {INTERVAL_MIN} мин\n🕐 Сессия: {SESSION_START}:00-{SESSION_END}:00 UTC\n\nПервый скрининг через 30 сек...")
    time.sleep(30)
    while True:
        run_cycle()
        log.info(f"Next in {INTERVAL_MIN} min")
        time.sleep(INTERVAL_MIN * 60)


from flask import Flask, Response
import threading as _threading

_app = Flask(__name__)

MOBILE_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>JARVIS ANALYST</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Syne:wght@800&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#080b0f;color:#c9d1d9;font-family:'JetBrains Mono',monospace;max-width:430px;margin:0 auto;padding-bottom:60px}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}
@keyframes fadeIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
.fade{animation:fadeIn 0.4s ease}
.header{position:sticky;top:0;z-index:10;background:#080b0f;border-bottom:1px solid #0d1117;padding:16px 16px 12px}
.header-top{display:flex;justify-content:space-between;align-items:flex-start}
.title{font-family:'Syne',sans-serif;font-size:18px;font-weight:800;color:#fff;letter-spacing:2px}
.subtitle{font-size:9px;color:#30363d;letter-spacing:2px}
.live-dot{width:7px;height:7px;border-radius:50%;background:#00e676;animation:pulse 1.5s infinite;display:inline-block;margin-right:6px}
.live-txt{font-size:10px;color:#00e676;letter-spacing:1px}
.timer{font-size:10px;color:#30363d;margin-top:2px;text-align:right}
.stats{display:flex;gap:8px;margin-top:10px}
.stat{flex:1;background:#0d1117;border:1px solid #161b22;border-radius:8px;padding:6px 8px;text-align:center}
.stat-val{font-size:14px;font-weight:700;color:#fff}
.stat-lbl{font-size:8px;color:#30363d;letter-spacing:1px}
.messages{padding:12px 12px 0}
.card{background:#0d1117;border:1px solid #00e67622;border-radius:14px;padding:14px 16px;margin-bottom:12px}
.card-time{font-size:10px;color:#30363d;margin-bottom:10px;letter-spacing:1px}
.card-line{font-size:12px;line-height:1.9}
.empty{background:#0d1117;border:1px solid #161b22;border-radius:14px;padding:40px 20px;text-align:center;margin-top:20px}
.empty-icon{font-size:24px;margin-bottom:12px}
.empty-title{font-size:12px;color:#30363d;letter-spacing:2px;margin-bottom:6px}
.empty-sub{font-size:11px;color:#21262d;line-height:1.6}
.footer{position:fixed;bottom:0;left:50%;transform:translateX(-50%);width:100%;max-width:430px;background:#080b0f;border-top:1px solid #0d1117;padding:10px 16px;text-align:center;font-size:9px;color:#21262d;letter-spacing:1px}
</style>
</head>
<body>
<div class="header">
  <div class="header-top">
    <div>
      <div class="title">JARVIS</div>
      <div class="subtitle">ANALYST · 300 PAIRS · OKX</div>
    </div>
    <div>
      <div><span class="live-dot"></span><span class="live-txt" id="status">CONNECTING</span></div>
      <div class="timer" id="timer"></div>
    </div>
  </div>
  <div class="stats">
    <div class="stat"><div class="stat-val" id="sig-count">0</div><div class="stat-lbl">СИГНАЛОВ</div></div>
    <div class="stat"><div class="stat-val">300</div><div class="stat-lbl">ПАРЫ</div></div>
    <div class="stat"><div class="stat-val">15м</div><div class="stat-lbl">ИНТЕРВАЛ</div></div>
  </div>
</div>

<div class="messages" id="messages">
  <div class="empty">
    <div class="empty-icon">⏳</div>
    <div class="empty-title">ОЖИДАНИЕ</div>
    <div class="empty-sub">Бот сканирует рынок каждые 15 минут<br>Сигналы появятся здесь автоматически</div>
  </div>
</div>

<div class="footer">⚠️ НЕ ЯВЛЯЕТСЯ ФИНАНСОВЫМ СОВЕТОМ · ТОРГУЙ НА СВОЙ РИСК</div>

<script>
const BOT_TOKEN = "8548549782:AAGYu1rr0lF-MT2vQ2ybr1cbqWEtqDoSw5I";
const CHAT_ID = "1974907918";
let lastId = 0;
let messages = [];
let nextScan = null;
let sigCount = 0;

function getLineColor(line) {
  if (line.includes('#1') || line.includes('#2') || line.includes('#3')) return '#00e676';
  if (line.includes('Вход:')) return '#ffffff';
  if (line.includes('SL:')) return '#ff5252';
  if (line.includes('TP:')) return '#69f0ae';
  if (line.includes('RR:') || line.includes('Score:')) return '#58a6ff';
  if (line.includes('JARVIS')) return '#ffffff';
  if (line.startsWith('─')) return '#1e3a4a';
  if (line.includes('💬')) return '#78909c';
  return '#78909c';
}

function formatMsg(text) {
  return text
    .replace(/<b>(.*?)<\/b>/g, '$1')
    .replace(/<[^>]+>/g, '')
    .replace(/━━━━━━━━━━━━━━━━/g, '─────────────');
}

function renderMessages() {
  const container = document.getElementById('messages');
  if (messages.length === 0) {
    container.innerHTML = '<div class="empty"><div class="empty-icon">⏳</div><div class="empty-title">ОЖИДАНИЕ</div><div class="empty-sub">Бот сканирует рынок каждые 15 минут<br>Сигналы появятся здесь автоматически</div></div>';
    return;
  }
  document.getElementById('sig-count').textContent = sigCount;
  container.innerHTML = messages.map((msg, i) => {
    const lines = formatMsg(msg.text).split('\n').filter(l => l.trim());
    const d = new Date(msg.date * 1000);
    const time = d.toLocaleTimeString('ru-RU', {hour:'2-digit',minute:'2-digit'}) + ' · ' + d.toLocaleDateString('ru-RU',{day:'2-digit',month:'2-digit'});
    const linesHtml = lines.map(l => `<div class="card-line" style="color:${getLineColor(l)}">${l}</div>`).join('');
    return `<div class="card ${i===0?'fade':''}"><div class="card-time">${time}</div>${linesHtml}</div>`;
  }).join('');
}

function updateTimer() {
  if (!nextScan) return;
  const diff = Math.max(0, Math.floor((nextScan - Date.now()) / 1000));
  const m = Math.floor(diff / 60);
  const s = diff % 60;
  document.getElementById('timer').textContent = diff > 0 ? `след: ${m}:${String(s).padStart(2,'0')}` : '';
}

async function fetchMessages() {
  try {
    const url = `https://api.telegram.org/bot${BOT_TOKEN}/getUpdates?offset=${lastId+1}&limit=20`;
    const res = await fetch(url);
    const data = await res.json();
    if (data.ok && data.result.length > 0) {
      const newMsgs = data.result.filter(u => u.message?.text).map(u => ({id: u.update_id, text: u.message.text, date: u.message.date}));
      if (newMsgs.length > 0) {
        messages = [...newMsgs.reverse(), ...messages].slice(0, 30);
        sigCount = messages.filter(m => m.text.includes('JARVIS ANALYST')).length;
        const lastSig = messages.find(m => m.text.includes('JARVIS ANALYST'));
        if (lastSig) nextScan = new Date(lastSig.date * 1000 + 15 * 60 * 1000);
        lastId = data.result[data.result.length-1].update_id;
        renderMessages();
      }
      document.getElementById('status').textContent = 'LIVE';
      document.querySelector('.live-dot').style.background = '#00e676';
    }
  } catch(e) {
    document.getElementById('status').textContent = 'ERROR';
    document.querySelector('.live-dot').style.background = '#ff1744';
  }
}

fetchMessages();
setInterval(fetchMessages, 10000);
setInterval(updateTimer, 1000);
</script>
</body>
</html>"""

@_app.route("/")
def dashboard():
    return Response(MOBILE_HTML, mimetype="text/html")

def _run_flask():
    _app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

if __name__ == "__main__":
    _threading.Thread(target=_run_flask, daemon=True).start()
    main()
