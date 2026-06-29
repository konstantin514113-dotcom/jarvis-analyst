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

if __name__ == "__main__":
    main()
