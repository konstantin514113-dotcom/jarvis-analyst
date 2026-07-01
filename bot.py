import os, time, json, logging, requests, threading
from datetime import datetime, timezone
from anthropic import Anthropic
from flask import Flask, Response, jsonify

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "konstantin514113-dotcom/jarvis-analyst")
STATE_FILE_PATH = "demo_state.json"
LOCAL_STATE_FILE = "/tmp/demo_state.json"

def save_persistent_state():
    """Save demo balance/journal to local disk and GitHub for durability across restarts/deploys."""
    try:
        snapshot = {
            "demo_balance": state["demo_balance"],
            "demo_journal": state["demo_journal"],
            "demo_id_counter": state["demo_id_counter"],
            "demo_pending_reinvest": state["demo_pending_reinvest"],
            "last_session_snapshot": state.get("last_session_snapshot"),
        }
        with open(LOCAL_STATE_FILE, "w") as f:
            json.dump(snapshot, f)
        if GITHUB_TOKEN:
            content_b64 = __import__("base64").b64encode(json.dumps(snapshot).encode()).decode()
            r = requests.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/contents/{STATE_FILE_PATH}",
                headers={"Authorization": f"token {GITHUB_TOKEN}"}, timeout=10
            )
            sha = r.json().get("sha") if r.status_code == 200 else None
            payload = {"message": "Update demo state", "content": content_b64}
            if sha:
                payload["sha"] = sha
            requests.put(
                f"https://api.github.com/repos/{GITHUB_REPO}/contents/{STATE_FILE_PATH}",
                headers={"Authorization": f"token {GITHUB_TOKEN}"}, json=payload, timeout=10
            )
    except Exception as e:
        log.error(f"save_persistent_state failed: {e}")

def load_persistent_state():
    """Load demo state from GitHub (or local disk fallback) on startup."""
    try:
        if GITHUB_TOKEN:
            r = requests.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/contents/{STATE_FILE_PATH}",
                headers={"Authorization": f"token {GITHUB_TOKEN}"}, timeout=10
            )
            if r.status_code == 200:
                import base64 as b64mod
                snapshot = json.loads(b64mod.b64decode(r.json()["content"]))
                state["demo_balance"] = snapshot.get("demo_balance", 10000.0)
                state["demo_journal"] = snapshot.get("demo_journal", [])
                state["demo_id_counter"] = snapshot.get("demo_id_counter", 0)
                state["demo_pending_reinvest"] = snapshot.get("demo_pending_reinvest", 0.0)
                state["last_session_snapshot"] = snapshot.get("last_session_snapshot")
                log.info(f"Loaded persistent state: balance=${state['demo_balance']:.2f}, journal={len(state['demo_journal'])} trades")
                return
    except Exception as e:
        log.error(f"load_persistent_state from GitHub failed: {e}")
    try:
        if os.path.exists(LOCAL_STATE_FILE):
            with open(LOCAL_STATE_FILE) as f:
                snapshot = json.load(f)
            state["demo_balance"] = snapshot.get("demo_balance", 10000.0)
            state["demo_journal"] = snapshot.get("demo_journal", [])
            state["demo_id_counter"] = snapshot.get("demo_id_counter", 0)
            state["demo_pending_reinvest"] = snapshot.get("demo_pending_reinvest", 0.0)
            log.info("Loaded persistent state from local disk fallback")
    except Exception as e:
        log.error(f"load_persistent_state local fallback failed: {e}")

ANTHROPIC_KEY  = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT  = os.environ["TELEGRAM_CHAT_ID"]
INTERVAL_MIN   = int(os.environ.get("INTERVAL_MIN", "20"))
SESSION_START  = int(os.environ.get("SESSION_START_UTC", "10"))
SESSION_END    = int(os.environ.get("SESSION_END_UTC", "13"))
SIGNAL_HOUR    = int(os.environ.get("SIGNAL_HOUR_UTC", "12"))
SIGNAL_MINUTE  = int(os.environ.get("SIGNAL_MINUTE_UTC", "50"))
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
    "demo_positions": [],   # active + closed paper trades
    "demo_balance": 10000.0,
    "demo_id_counter": 0,
    "demo_journal": [],      # full trade history log for analysis
    "demo_pending_reinvest": 0.0,  # accumulated daily PnL not yet reinvested
    "last_session_snapshot": None,  # snapshot of journal+balance at last daily reset
    "session_size": 2000.0,  # fixed size per pair for current session, set once when session starts
    "session_start_balance": 10000.0,  # balance at the moment current session began
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
    MAX_PAIRS = 300
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
    PAIRS = PAIRS[:MAX_PAIRS]
    state["pairs_loaded"] = len(PAIRS)
    log.info(f"Loaded {len(PAIRS)} pairs (capped at {MAX_PAIRS})")

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
    if score < 75: return None
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
    client = Anthropic(api_key=ANTHROPIC_KEY, timeout=30.0)
    lines = [f"Market {datetime.utcnow().strftime('%H:%M UTC')}:"]
    for c in candidates:
        lines.append(f"{c['symbol']}: price={c['price']:.8f} change={c['change24h']:+.2f}% vol={c['vol24h']:,.0f} RSI15m={c['rsi']} RSI1H={c['rsi_1h']} MACD15m={c['macd']} MACD1H={c['macd_1h']} htf_confirmed=YES above_MA20={'YES' if c['above_ma20'] else 'NO'} dist_high={c['dist_from_high']:.1f}% score={c['score']}")
    msg = client.messages.create(model="claude-sonnet-4-6", max_tokens=1500,
        system=SCREEN_PROMPT, messages=[{"role":"user","content":"\n".join(lines)}])
    text = msg.content[0].text.strip().replace("```json","").replace("```","").strip()
    # Robust parse: extract only the first valid JSON object, ignore any trailing text
    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(text)
        return obj
    except json.JSONDecodeError as e:
        log.error(f"JSON parse failed even with raw_decode: {e}. Raw text: {text[:300]}")
        return {"top_pairs": []}

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
    update_demo_positions()

def log_journal_entry(p):
    _log_journal_entry_impl(p)
    threading.Thread(target=save_persistent_state, daemon=True).start()

def _log_journal_entry_impl(p):
    """Append a closed position to the permanent journal for later analysis."""
    state["demo_journal"].append({
        "id": p["id"],
        "symbol": p["symbol"],
        "direction": p["direction"],
        "entry": p["entry"],
        "close_price": p.get("close_price"),
        "stop_loss": p["stop_loss"],
        "take_profit": p["take_profit"],
        "leverage": p["leverage"],
        "size": p["size"],
        "pnl_pct": p.get("pnl_pct", 0),
        "pnl_usd": p.get("pnl_usd", 0),
        "result": p.get("result"),
        "score": p.get("score"),
        "opened_at": p.get("opened_at"),
        "closed_at": p.get("closed_at"),
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    })
    state["demo_pending_reinvest"] += p.get("pnl_usd", 0)

def update_demo_positions():
    for p in state["demo_positions"]:
        if p["status"] != "open": continue
        try:
            t = get_ticker(p["symbol"])
            if not t: continue
            price = t["price"]
            p["current_price"] = price
            if p["direction"] == "LONG":
                pnl_pct = (price - p["entry"]) / p["entry"]
            else:
                pnl_pct = (p["entry"] - price) / p["entry"]
            pnl_pct *= p["leverage"]
            p["pnl_pct"] = round(pnl_pct * 100, 2)
            p["pnl_usd"] = round(p["size"] * pnl_pct, 2)
            hit_tp = (p["direction"]=="LONG" and price >= p["take_profit"]) or (p["direction"]=="SHORT" and price <= p["take_profit"])
            hit_sl = (p["direction"]=="LONG" and price <= p["stop_loss"]) or (p["direction"]=="SHORT" and price >= p["stop_loss"])
            if hit_tp or hit_sl:
                p["status"] = "closed"
                p["result"] = "WIN" if hit_tp else "LOSS"
                p["close_price"] = price
                p["closed_at"] = datetime.now(timezone.utc).strftime("%H:%M UTC")
                state["demo_balance"] += p["pnl_usd"]
                log_journal_entry(p)
        except: pass

def start_new_session():
    """Auto-reinvest pending PnL and force-close any still-open positions before starting a new 5-pair session."""
    for p in state["demo_positions"]:
        if p["status"] == "open":
            try:
                t = get_ticker(p["symbol"])
                price = t["price"] if t else p["current_price"]
                if p["direction"] == "LONG":
                    pnl_pct = (price - p["entry"]) / p["entry"]
                else:
                    pnl_pct = (p["entry"] - price) / p["entry"]
                pnl_pct *= p["leverage"]
                p["pnl_pct"] = round(pnl_pct * 100, 2)
                p["pnl_usd"] = round(p["size"] * pnl_pct, 2)
                p["status"] = "closed"
                p["result"] = "WIN" if p["pnl_usd"] >= 0 else "LOSS"
                p["close_price"] = price
                p["closed_at"] = datetime.now(timezone.utc).strftime("%H:%M UTC")
                state["demo_balance"] += p["pnl_usd"]
                log_journal_entry(p)
            except Exception as e:
                log.error(f"start_new_session close error: {e}")
    state["demo_pending_reinvest"] = 0.0
    state["demo_positions"] = [p for p in state["demo_positions"] if p["status"] == "open"]
    state["session_size"] = round(state["demo_balance"] / 5, 2)
    state["session_start_balance"] = state["demo_balance"]
    log.info(f"New session started, balance reinvested: ${state['demo_balance']:.2f}, session_size=${state['session_size']:.2f}")

def send_top5(now):
    start_new_session()
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
        if state["history"]:
            today_journal = [t for t in state["demo_journal"] if t.get("date") == datetime.now(timezone.utc).strftime("%Y-%m-%d")]
            state["last_session_snapshot"] = {
                "signals": state["history"],
                "trades": today_journal,
                "balance_at_reset": round(state["demo_balance"], 2),
                "timestamp": now.strftime("%Y-%m-%d %H:%M UTC"),
            }
        state["history"] = []; state["accumulated"] = {}
        state["daily_sent"] = False; state["status"] = "ready"
        log.info("Daily reset")
    # Check signal time FIRST, before session bounds (signal hour may equal session end)
    signal_time_reached = (now.hour > SIGNAL_HOUR) or (now.hour == SIGNAL_HOUR and now.minute >= SIGNAL_MINUTE)
    if signal_time_reached and not state["daily_sent"] and state["accumulated"]:
        send_top5(now); state["daily_sent"] = True; return
    if state["daily_sent"]: return
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
        for p in result.get("top_pairs",[])[:5]:
            sym = p.get("symbol","")
            if sym not in state["accumulated"]:
                state["accumulated"][sym] = {"count":0,"data":p}
            state["accumulated"][sym]["count"] += 1
            state["accumulated"][sym]["data"] = p
        log.info(f"Accumulated: {len(state['accumulated'])} pairs")
        state["scan_count"] += 1
        state["next_scan"] = now.timestamp() + INTERVAL_MIN*60
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
    load_persistent_state()
    load_pairs()
    tg(f"JARVIS ANALYST v3\n{len(PAIRS)} пар | Только по команде ПЕРЕСКАН")
    time.sleep(10)
    threading.Thread(target=price_monitor, daemon=True).start()
    # No automatic scanning — signals only via /force-scan (ПЕРЕСКАН button)
    while True:
        time.sleep(60)

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

@app.route("/force-scan")
def force_scan():
    def do_force():
        try:
            state["status"] = "force_scanning"
            log.info("FORCE SCAN triggered manually (will retry until candidates found, max 5 tries)")
            candidates = []
            for attempt in range(5):
                log.info(f"Force scan attempt {attempt+1}/5")
                candidates = scan()
                log.info(f"Attempt {attempt+1}: found {len(candidates)} candidates")
                if candidates:
                    break
                state["status"] = f"force_scan_retry_{attempt+1}"
                time.sleep(60)
            if not candidates:
                state["status"] = "force_scan_no_candidates"
                log.info("Force scan exhausted all 5 attempts, no candidates found")
                return
            result = analyze_with_claude(candidates)
            pairs = result.get("top_pairs", [])[:5]
            if not pairs:
                state["status"] = "force_scan_no_pairs"
                return
            for p in pairs:
                e = p.get("entry", 0)
                if e > 0:
                    p["stop_loss"] = round(e * 0.985, 8)
                    p["take_profit"] = round(e * 1.035, 8)
            now = datetime.now(timezone.utc)
            scan_time = now.strftime("%H:%M UTC")
            start_new_session()
            state["signals"] = [{"rank": i+1, **p, "scan_time": scan_time} for i, p in enumerate(pairs)]
            state["last_scan"] = scan_time
            today_syms = {h["symbol"] for h in state["history"]}
            for p in pairs:
                if p["symbol"] not in today_syms:
                    state["history"].append({"symbol": p["symbol"], "scan_time": scan_time,
                        "entry": p.get("entry",0), "stop_loss": p.get("stop_loss",0), "take_profit": p.get("take_profit",0),
                        "score": p.get("score",0), "rsi": p.get("rsi",0), "macd": p.get("macd",""),
                        "reason": p.get("reason",""), "current_price": p.get("entry",0), "pct_change": 0.0,
                        "status": "active", "result": None})
            msgs = []
            for i, p in enumerate(pairs):
                sym = p["symbol"].replace("-USDT","")
                e=p.get("entry",0); sl=p.get("stop_loss",0); tp=p.get("take_profit",0)
                rr = abs((tp-e)/(e-sl)) if abs(e-sl)>0 else 0
                msgs.append(f"#{i+1} {sym}/USDT\nВход: {e} | SL: {sl} | TP: {tp}\nRR: 1:{rr:.1f} | Score: {p.get('score',0)}\nRSI: {p.get('rsi','?')} | 1H: {p.get('rsi_1h','?')}\n{p.get('reason','')}")
            header = f"JARVIS FORCE SIGNAL | {scan_time}\n" + "-"*16 + "\n"
            tg(header + "\n\n".join(msgs) + "\n\n" + "-"*16 + "\nНе финансовый совет.")
            state["status"] = "force_scan_sent"
            log.info(f"Force scan sent: {[p['symbol'] for p in pairs]}")
        except Exception as e:
            log.error(f"Force scan error: {e}", exc_info=True)
            state["status"] = "force_scan_error"
    threading.Thread(target=do_force, daemon=True).start()
    return jsonify({"ok": True, "message": "Force scan started, check /status in 2-3 minutes"})

@app.route("/status")
def status():
    return jsonify({"status":state["status"],"pairs":state["pairs_loaded"],
                    "scans":state["scan_count"],"accumulated":len(state["accumulated"]),
                    "daily_sent":state["daily_sent"],"last_scan":state["last_scan"]})

from flask import request

@app.route("/demo/open", methods=["POST"])
def demo_open():
    try:
        data = request.get_json(force=True)
        symbol = data["symbol"]
        direction = data.get("direction", "LONG")
        entry = float(data["entry"])
        stop_loss = float(data["stop_loss"])
        take_profit = float(data["take_profit"])
        leverage = float(data.get("leverage", 1.25))
        size = float(data.get("size", 2000))
        state["demo_id_counter"] += 1
        pos = {
            "id": state["demo_id_counter"],
            "symbol": symbol,
            "direction": direction,
            "entry": entry,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "leverage": leverage,
            "size": size,
            "current_price": entry,
            "pnl_pct": 0.0,
            "pnl_usd": 0.0,
            "status": "open",
            "result": None,
            "opened_at": datetime.now(timezone.utc).strftime("%H:%M UTC"),
        }
        state["demo_positions"].append(pos)
        return jsonify({"ok": True, "position": pos})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

@app.route("/demo/close", methods=["POST"])
def demo_close():
    try:
        data = request.get_json(force=True)
        pos_id = int(data["id"])
        for p in state["demo_positions"]:
            if p["id"] == pos_id and p["status"] == "open":
                t = get_ticker(p["symbol"])
                price = t["price"] if t else p["current_price"]
                if p["direction"] == "LONG":
                    pnl_pct = (price - p["entry"]) / p["entry"]
                else:
                    pnl_pct = (p["entry"] - price) / p["entry"]
                pnl_pct *= p["leverage"]
                p["pnl_pct"] = round(pnl_pct * 100, 2)
                p["pnl_usd"] = round(p["size"] * pnl_pct, 2)
                p["status"] = "closed"
                p["result"] = "WIN" if p["pnl_usd"] >= 0 else "LOSS"
                p["close_price"] = price
                p["closed_at"] = datetime.now(timezone.utc).strftime("%H:%M UTC")
                state["demo_balance"] += p["pnl_usd"]
                log_journal_entry(p)
                return jsonify({"ok": True, "position": p})
        return jsonify({"ok": False, "error": "Position not found or already closed"}), 404
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

@app.route("/demo/close_all", methods=["POST"])
def demo_close_all():
    closed = []
    for p in state["demo_positions"]:
        if p["status"] == "open":
            try:
                t = get_ticker(p["symbol"])
                price = t["price"] if t else p["current_price"]
                if p["direction"] == "LONG":
                    pnl_pct = (price - p["entry"]) / p["entry"]
                else:
                    pnl_pct = (p["entry"] - price) / p["entry"]
                pnl_pct *= p["leverage"]
                p["pnl_pct"] = round(pnl_pct * 100, 2)
                p["pnl_usd"] = round(p["size"] * pnl_pct, 2)
                p["status"] = "closed"
                p["result"] = "WIN" if p["pnl_usd"] >= 0 else "LOSS"
                p["close_price"] = price
                p["closed_at"] = datetime.now(timezone.utc).strftime("%H:%M UTC")
                state["demo_balance"] += p["pnl_usd"]
                log_journal_entry(p)
                closed.append(p)
            except: pass
    return jsonify({"ok": True, "closed": closed, "balance": state["demo_balance"]})

@app.route("/demo/reinvest", methods=["POST"])
def demo_reinvest():
    """Fold pending realized PnL into the working balance and clear the pending counter."""
    amount = state["demo_pending_reinvest"]
    state["demo_pending_reinvest"] = 0.0
    return jsonify({"ok": True, "reinvested": round(amount, 2), "balance": round(state["demo_balance"], 2)})

@app.route("/demo/state")
def demo_state():
    open_positions = [p for p in state["demo_positions"] if p["status"] == "open"]
    suggested_size = round(state["session_size"], 2)
    return jsonify({
        "balance": round(state["demo_balance"], 2),
        "positions": state["demo_positions"],
        "pending_reinvest": round(state["demo_pending_reinvest"], 2),
        "suggested_size": suggested_size,
        "session_start_balance": round(state["session_start_balance"], 2),
    })

@app.route("/demo/journal")
def demo_journal():
    return jsonify({"journal": state["demo_journal"]})

@app.route("/last-session")
def last_session():
    return jsonify(state["last_session_snapshot"] or {"message": "No previous session recorded yet"})

@app.route("/demo/stats")
def demo_stats():
    j = state["demo_journal"]
    if not j:
        return jsonify({"trades": 0})
    wins = [t for t in j if t["result"] == "WIN"]
    losses = [t for t in j if t["result"] == "LOSS"]
    total_pnl = sum(t["pnl_usd"] for t in j)
    by_symbol = {}
    for t in j:
        s = t["symbol"]
        by_symbol.setdefault(s, {"trades": 0, "wins": 0, "pnl": 0.0})
        by_symbol[s]["trades"] += 1
        by_symbol[s]["wins"] += 1 if t["result"] == "WIN" else 0
        by_symbol[s]["pnl"] += t["pnl_usd"]
    by_date = {}
    for t in j:
        d = t["date"]
        by_date.setdefault(d, {"trades": 0, "wins": 0, "pnl": 0.0})
        by_date[d]["trades"] += 1
        by_date[d]["wins"] += 1 if t["result"] == "WIN" else 0
        by_date[d]["pnl"] += t["pnl_usd"]
    return jsonify({
        "trades": len(j),
        "wins": len(wins),
        "losses": len(losses),
        "winrate": round(len(wins) / len(j) * 100, 1) if j else 0,
        "total_pnl": round(total_pnl, 2),
        "avg_win": round(sum(t["pnl_usd"] for t in wins) / len(wins), 2) if wins else 0,
        "avg_loss": round(sum(t["pnl_usd"] for t in losses) / len(losses), 2) if losses else 0,
        "by_symbol": by_symbol,
        "by_date": by_date,
    })

threading.Thread(target=lambda: main(), daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",8080)))
