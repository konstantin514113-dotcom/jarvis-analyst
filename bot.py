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
SIGNAL_HOUR    = int(os.environ.get("SIGNAL_HOUR_UTC", "13"))
OKX_BASE       = "https://www.okx.com"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("JARVIS")

state = {
    "signals": [],
    "last_scan": None,
    "next_scan": None,
    "scan_count": 0,
    "history": [],
    "accumulated": {},
    "daily_sent": False,
    "pairs_loaded": 0,
    "status": "starting",
}

PAIRS = []

SCREEN_PROMPT = """You are a crypto momentum analyst for OKX spot market.
Select TOP 5 pairs most likely to rise in next 60 minutes.
Data: price, change24h, volume, RSI14(15m), RSI14(1H), MACD(15m), MACD(1H), htf_confirmed, above_MA20, dist_high, score.
RULES: Only select pairs where htf_confirmed=YES. RSI 45-65, MACD bullish on both timeframes, volume spike.
SL = entry * 0.985, TP = entry * 1.035.
Reply ONLY valid JSON: {"top_pairs": [{"symbol": "XXX-USDT", "entry": 0.0, "stop_loss": 0.0, "take_profit": 0.0, "score": 85, "rsi": 55, "rsi_1h": 52, "macd": "bullish", "reason": "one sentence in Russian"}]}"""

def tg(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        log.error(f"Telegram: {e}")

def load_pairs():
    global PAIRS
    base = ["BTC-USDT","ETH-USDT","SOL-USDT","BNB-USDT","XRP-USDT","DOGE-USDT","ADA-USDT",
            "AVAX-USDT","LINK-USDT","DOT-USDT","SUI-USDT","APT-USDT","ARB-USDT","OP-USDT",
            "PEPE-USDT","SHIB-USDT","WIF-USDT","BONK-USDT","ORDI-USDT","INJ-USDT","TIA-USDT",
            "NEAR-USDT","HBAR-USDT","STX-USDT","RNDR-USDT","FET-USDT","AAVE-USDT","LDO-USDT",
            "DYDX-USDT","GMX-USDT","PENDLE-USDT","IMX-USDT","BLUR-USDT","JUP-USDT","PYTH-USDT",
            "SEI-USDT","WLD-USDT","MANTA-USDT","ALT-USDT","PIXEL-USDT","PORTAL-USDT","STRK-USDT",
            "BOME-USDT","MEW-USDT","NEIRO-USDT","PNUT-USDT","GOAT-USDT","MEME-USDT","PEPE-USDT",
            "ANIME-USDT","KAITO-USDT","MOVE-USDT","HYPE-USDT","GAS-USDT","VINE-USDT","GRAM-USDT",
            "TRX-USDT","XLM-USDT","LTC-USDT","BCH-USDT","ATOM-USDT","ETC-USDT","FTM-USDT",
            "SAND-USDT","MANA-USDT","AXS-USDT","GALA-USDT","ENJ-USDT","CHZ-USDT","FLOW-USDT",
            "THETA-USDT","HBAR-USDT","ONE-USDT","ZRX-USDT","BAT-USDT","CRV-USDT","MKR-USDT",
            "COMP-USDT","SNX-USDT","UNI-USDT","SUSHI-USDT","BAL-USDT","YFI-USDT","1INCH-USDT",
            "PEOPLE-USDT","TURBO-USDT","FLOKI-USDT","ACT-USDT","PUMP-USDT","SONIC-USDT","BERA-USDT",
            "IP-USDT","LAYER-USDT","PI-USDT","TRUMP-USDT","AIXBT-USDT","ZORA-USDT","SCR-USDT",
            "ETHFI-USDT","TIA-USDT","DORA-USDT","GALFT-USDT","ZRO-USDT","AI-USDT","BASED-USDT"]
    PAIRS = list(dict.fromkeys(base))
    try:
        r = requests.get(f"{OKX_BASE}/api/v5/public/instruments?instType=SPOT", timeout=10)
        extra = [i["instId"] for i in r.json().get("data",[]) 
                 if i["instId"].endswith("-USDT") and i.get("state")=="live"
                 and i["instId"] not in {"USDC-USDT","BUSD-USDT","DAI-USDT","USDD-USDT","TUSD-USDT"}]
        PAIRS = list(dict.fromkeys(PAIRS + extra))
    except:
        pass
    state["pairs_loaded"] = len(PAIRS)
    log.info(f"Loaded {len(PAIRS)} pairs")

def get_ticker(symbol):
    try:
        r = requests.get(f"{OKX_BASE}/api/v5/market/ticker?instId={symbol}", timeout=5)
        t = r.json().get("data",[{}])[0]
        if not t: return None
        last = float(t.get("last",0))
        if last == 0: return None
        open24 = float(t.get("open24h",0)) or last
        return {"symbol":symbol,"price":last,"change24h":round((last-open24)/open24*100,2),
                "vol24h":float(t.get("volCcy24h",0)),"high24h":float(t.get("high24h",last))}
    except: return None

def get_candles(symbol, bar="15m", limit=30):
    try:
        r = requests.get(f"{OKX_BASE}/api/v5/market/candles?instId={symbol}&bar={bar}&limit={limit}", timeout=5)
        data = r.json().get("data",[])
        if not data: return None
        return [{"c":float(c[4]),"v":float(c[5])} for c in reversed(data)]
    except: return None

def calc_rsi(candles, period=14):
    if not candles or len(candles) < period+1: return 50
    closes = [c["c"] for c in candles]
    gains,losses = [],[]
    for i in range(1,len(closes)):
        d = closes[i]-closes[i-1]
        gains.append(max(d,0)); losses.append(max(-d,0))
    ag = sum(gains[-period:])/period; al = sum(losses[-period:])/period
    if al == 0: return 100
    return round(100-(100/(1+ag/al)),1)

def calc_macd(candles):
    if not candles or len(candles) < 26: return "unknown"
    closes = [c["c"] for c in candles]
    def ema(data,n):
        k=2/(n+1); e=data[0]
        for d in data[1:]: e=d*k+e*(1-k)
        return e
    macd = ema(closes[-12:],12)-ema(closes[-26:],26)
    prev = ema(closes[-13:-1],12)-ema(closes[-27:-1],26)
    if macd>0 and macd>prev: return "bullish"
    if macd<0 and macd<prev: return "bearish"
    if macd>prev: return "crossing_up"
    return "crossing_down"

def calc_ma(candles,period=20):
    if not candles or len(candles)<period: return None
    return sum(c["c"] for c in candles[-period:])/period

def analyze_pair(symbol):
    t = get_ticker(symbol)
    if not t or t["vol24h"] < 5000: return None
    c15 = get_candles(symbol,"15m",30)
    c1h = get_candles(symbol,"1H",30)
    rsi15 = calc_rsi(c15); macd15 = calc_macd(c15)
    rsi1h = calc_rsi(c1h); macd1h = calc_macd(c1h)
    ma20 = calc_ma(c15)
    above_ma = t["price"] > ma20 if ma20 else False
    htf = (40<=rsi1h<=70) and ("bullish" in macd1h or "crossing_up" in macd1h)
    if not htf: return None
    dist = (t["high24h"]-t["price"])/t["high24h"]*100 if t["high24h"]>0 else 0
    score = 0
    score += min(t["change24h"]*3,30)
    score += min(t["vol24h"]/50000,20)
    score += 15 if 45<=rsi15<=65 else 0
    score += 15 if "bullish" in macd15 or "crossing_up" in macd15 else 0
    score += 10 if above_ma else 0
    score += 10 if dist>1 else 0
    score += 20
    if score < 89: return None
    return {**t,"rsi":rsi15,"rsi_1h":rsi1h,"macd":macd15,"macd_1h":macd1h,
            "htf_bullish":True,"above_ma20":above_ma,"dist_from_high":round(dist,2),"score":round(score,1)}

def scan():
    candidates = []
    for symbol in PAIRS:
        try:
            d = analyze_pair(symbol)
            if d: candidates.append(d)
            time.sleep(0.1)
        except: pass
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:25]

def analyze_with_claude(candidates):
    client = Anthropic(api_key=ANTHROPIC_KEY)
    lines = [f"Market {datetime.utcnow().strftime('%H:%M UTC')}:"]
    for c in candidates:
        lines.append(f"{c['symbol']}: price={c['price']:.8f} change={c['change24h']:+.2f}% vol={c['vol24h']:,.0f} RSI15m={c['rsi']} RSI1H={c['rsi_1h']} MACD15m={c['macd']} MACD1H={c['macd_1h']} htf_confirmed=YES above_MA20={'YES' if c['above_ma20'] else 'NO'} dist_high={c['dist_from_high']:.1f}% score={c['score']}")
    msg = client.messages.create(model="claude-sonnet-4-6", max_tokens=1500,
        system=SCREEN_PROMPT, messages=[{"role":"user","content":"\n".join(lines)}])
    text = msg.content[0].text.strip().replace("```json","").replace("```","").strip()
    return json.loads(text)

def update_history():
    for h in state["history"]:
        if h.get("status") == "active":
            try:
                t = get_ticker(h["symbol"])
                if t:
                    pct = round((t["price"]-h["entry"])/h["entry"]*100,2)
                    h["current_price"] = t["price"]
                    h["pct_change"] = pct
                    if t["price"] >= h["take_profit"]: h["status"]="tp_hit"; h["result"]="WIN"
                    elif t["price"] <= h["stop_loss"]: h["status"]="sl_hit"; h["result"]="LOSS"
            except: pass

def send_top5(now):
    if not state["accumulated"]: return
    sorted_pairs = sorted(state["accumulated"].values(), key=lambda x:(x["count"],x["data"].get("score",0)), reverse=True)
    top5 = [p["data"] for p in sorted_pairs[:5]]
    scan_time = now.strftime("%H:%M UTC")
    for p in top5:
        e = p.get("entry",0)
        if e > 0:
            p["stop_loss"] = round(e*0.985,8)
            p["take_profit"] = round(e*1.035,8)
    state["signals"] = [{"rank":i+1,**p,"scan_time":scan_time} for i,p in enumerate(top5)]
    state["last_scan"] = scan_time
    state["scan_count"] += 1
    state["next_scan"] = now.timestamp() + INTERVAL_MIN*60
    today_syms = {h["symbol"] for h in state["history"]}
    for p in top5:
        if p["symbol"] not in today_syms:
            state["history"].append({"symbol":p["symbol"],"scan_time":scan_time,
                "entry":p.get("entry",0),"stop_loss":p.get("stop_loss",0),"take_profit":p.get("take_profit",0),
                "score":p.get("score",0),"rsi":p.get("rsi",0),"macd":p.get("macd",""),
                "reason":p.get("reason",""),"current_price":p.get("entry",0),"pct_change":0.0,
                "status":"active","result":None})
    counts = {p["data"]["symbol"]:p["count"] for p in sorted_pairs[:5]}
    msgs = []
    for i,p in enumerate(top5):
        sym = p["symbol"].replace("-USDT","")
        e=p.get("entry",0); sl=p.get("stop_loss",0); tp=p.get("take_profit",0)
        rr = abs((tp-e)/(e-sl)) if abs(e-sl)>0 else 0
        cnt = counts.get(p["symbol"],0)
        msgs.append(f"#{i+1} {sym}/USDT x{cnt} сканов\nВход: {e} | SL: {sl} | TP: {tp}\nRR: 1:{rr:.1f} | Score: {p.get('score',0)}\nRSI: {p.get('rsi','?')} | 1H: {p.get('rsi_1h','?')}\n{p.get('reason','')}")
    header = f"JARVIS TOP-5 | {scan_time}\nЛучшие пары 07:00-13:00 UTC\n" + "-"*16 + "\n"
    tg(header + "\n\n".join(msgs) + "\n\n" + "-"*16 + "\nНе финансовый совет.")
    log.info(f"Sent top-5: {[p['symbol'] for p in top5]}")

def run_cycle():
    now = datetime.now(timezone.utc)
    if now.hour == SESSION_START and now.minute < INTERVAL_MIN:
        state["history"] = []
        log.info("Daily reset")
    if not (SESSION_START <= now.hour < SESSION_END):
        state["status"] = "outside_session"; return
    state["status"] = "scanning"
    log.info(f"Scanning {len(PAIRS)} pairs...")
    try:
        candidates = scan()
        log.info(f"Found {len(candidates)} candidates")
        if not candidates:
            state["scan_count"] += 1
            state["next_scan"] = now.timestamp() + INTERVAL_MIN*60
            state["status"] = "waiting"; return
        result = analyze_with_claude(candidates)
        pairs = result.get("top_pairs",[])[:5]
        if not pairs:
            state["scan_count"] += 1
            state["next_scan"] = now.timestamp() + INTERVAL_MIN*60
            state["status"] = "waiting"; return

        for p in pairs:
            e = p.get("entry", 0)
            if e > 0:
                p["stop_loss"] = round(e * 0.985, 8)
                p["take_profit"] = round(e * 1.035, 8)

        scan_time = now.strftime("%H:%M UTC")
        state["signals"] = [{"rank":i+1,**p,"scan_time":scan_time} for i,p in enumerate(pairs)]
        state["last_scan"] = scan_time
        state["scan_count"] += 1
        state["next_scan"] = now.timestamp() + INTERVAL_MIN*60

        today_syms = {h["symbol"] for h in state["history"]}
        new_pairs = [p for p in pairs if p["symbol"] not in today_syms]
        for p in new_pairs:
            state["history"].append({"symbol":p["symbol"],"scan_time":scan_time,
                "entry":p.get("entry",0),"stop_loss":p.get("stop_loss",0),"take_profit":p.get("take_profit",0),
                "score":p.get("score",0),"rsi":p.get("rsi",0),"macd":p.get("macd",""),
                "reason":p.get("reason",""),"current_price":p.get("entry",0),"pct_change":0.0,
                "status":"active","result":None})

        if new_pairs:
            msgs = []
            for i, p in enumerate(pairs):
                sym = p["symbol"].replace("-USDT","")
                e=p.get("entry",0); sl=p.get("stop_loss",0); tp=p.get("take_profit",0)
                rr = abs((tp-e)/(e-sl)) if abs(e-sl)>0 else 0
                msgs.append(f"#{i+1} {sym}/USDT\nВход: {e} | SL: {sl} | TP: {tp}\nRR: 1:{rr:.1f} | Score: {p.get('score',0)}\nRSI: {p.get('rsi','?')} | 1H: {p.get('rsi_1h','?')}\n{p.get('reason','')}")
            header = f"JARVIS SIGNAL | {scan_time}\n" + "-"*16 + "\n"
            tg(header + "\n\n".join(msgs) + "\n\n" + "-"*16 + "\nНе финансовый совет.")
            log.info(f"Sent signal: {[p['symbol'] for p in pairs]}")

        state["status"] = "waiting"
    except Exception as e:
        log.error(f"Cycle error: {e}", exc_info=True)

def price_monitor():
    while True:
        time.sleep(120)
        try: update_history()
        except: pass

def main():
    log.info("JARVIS ANALYST v3 starting...")
    load_pairs()
    tg(f"JARVIS ANALYST v3\n{len(PAIRS)} пар | Сигнал в 13:00 UTC\nКаждые {INTERVAL_MIN} мин")
    time.sleep(10)
    threading.Thread(target=price_monitor, daemon=True).start()
    while True:
        try: run_cycle()
        except Exception as e: log.error(f"Main error: {e}")
        time.sleep(INTERVAL_MIN*60)

app = Flask(__name__)

@app.route("/")
def dashboard():
    try:
        return Response(open("panel.html").read(), mimetype="text/html")
    except:
        return Response("<h1>JARVIS ANALYST v3 - Loading...</h1>", mimetype="text/html")

@app.route("/signals")
def signals():
    return jsonify(state)

@app.route("/status")
def status():
    return jsonify({"status":state["status"],"pairs":state["pairs_loaded"],
                    "scans":state["scan_count"],"accumulated":len(state["accumulated"]),
                    "daily_sent":state["daily_sent"],"last_scan":state["last_scan"]})

threading.Thread(target=lambda: main(), daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",8080)))
