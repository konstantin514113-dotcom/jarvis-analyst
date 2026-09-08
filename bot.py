import os, time, json, logging, requests, threading
from datetime import datetime, timezone
from anthropic import Anthropic
from flask import Flask, Response, jsonify

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "konstantin514113-dotcom/jarvis-analyst")
STATE_FILE_PATH = "demo_state.json"
STATE_BRANCH = "state-storage"  # separate branch so Railway does not redeploy on state saves
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
                f"https://api.github.com/repos/{GITHUB_REPO}/contents/{STATE_FILE_PATH}?ref={STATE_BRANCH}",
                headers={"Authorization": f"token {GITHUB_TOKEN}"}, timeout=10
            )
            sha = r.json().get("sha") if r.status_code == 200 else None
            payload = {"message": "Update demo state", "content": content_b64, "branch": STATE_BRANCH}
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
                f"https://api.github.com/repos/{GITHUB_REPO}/contents/{STATE_FILE_PATH}?ref={STATE_BRANCH}",
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
    "day_start_balance": 10000.0,  # balance at start of trading day (for daily loss limit)
    "trading_halted": False,  # True if daily loss limit hit
    "halt_reason": "",
    "day_date": "",  # current trading day date
    "demo_total_fees": 0.0,  # cumulative OKX taker fees paid across all trades
    "signal_alerts": [],  # live Telegram entry/exit alerts (TOP5_ONLY mode), not real trades
    "last_auto_scan": 0,  # timestamp of last automatic scan (TOP5_ONLY mode)
}

# === EXCHANGE FEES (OKX spot, Lv1 / base tier) ===
MAKER_FEE_PCT = 0.08  # % per side — limit order that rests on the book (entry, TP, partial-close targets)
TAKER_FEE_PCT = 0.10  # % per side — market order needed immediately (SL, manual/forced close)
MAKER_RATE = MAKER_FEE_PCT / 100.0
TAKER_RATE = TAKER_FEE_PCT / 100.0

def calc_fee(notional, maker=False):
    """Fee in USD for a given notional (size * leverage). maker=True uses the cheaper resting-order rate."""
    rate = MAKER_RATE if maker else TAKER_RATE
    return round(notional * rate, 2)

PAIRS = []

# === Pair selection mode ===
# TOP5_ONLY=true (default): trade only the 5 most liquid/stable pairs below.
# Set env var TOP5_ONLY=false to go back to scanning the full ~300-pair universe.
TOP5_PAIRS = ["BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "LTC-USDT"]
TOP5_ONLY = os.environ.get("TOP5_ONLY", "true").lower() == "true"

SCREEN_PROMPT = """You are a crypto momentum analyst for OKX spot market.
Select TOP 5 pairs most likely to rise in next 60 minutes.
Data: price, change24h, volume, RSI14(15m), RSI14(1H), MACD(15m), MACD(1H), htf_confirmed, above_MA20, dist_high, score.
RULES: Only select pairs where htf_confirmed=YES. RSI 45-65, MACD bullish on both timeframes, volume spike.
SL = entry * 0.985, TP = entry * 1.035.
Reply ONLY valid JSON: {"top_pairs": [{"symbol": "XXX-USDT", "entry": 0.0, "stop_loss": 0.0, "take_profit": 0.0, "score": 85, "rsi": 55, "rsi_1h": 52, "macd": "bullish", "reason": "one sentence in Russian"}]}"""

DISABLE_TELEGRAM = os.environ.get("DISABLE_TELEGRAM", "false").lower() == "true"

def tg(msg):
    if DISABLE_TELEGRAM:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        log.error(f"Telegram: {e}")

def load_pairs():
    global PAIRS
    if TOP5_ONLY:
        PAIRS = TOP5_PAIRS.copy()
        state["pairs_loaded"] = len(PAIRS)
        log.info(f"TOP5_ONLY mode: trading only {PAIRS}")
        return
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
        return [{"ts":int(c[0]),"o":float(c[1]),"h":float(c[2]),"l":float(c[3]),
                 "c":float(c[4]),"v":float(c[5])} for c in reversed(data)]
    except: return None

# === LIQUIDITY / SPREAD FILTERS ===
MIN_VOL_24H_USDT   = 300000   # minimum 24h quote volume to consider a pair tradeable at real size
SPREAD_MAX_PCT     = 0.15     # max bid/ask spread allowed (%), wider = hidden cost beyond commission
MIN_ORDERBOOK_DEPTH_USD = 15000  # min combined top-5-level bid+ask depth, protects against slippage

def get_orderbook(symbol, sz=5):
    try:
        r = requests.get(f"{OKX_BASE}/api/v5/market/books?instId={symbol}&sz={sz}", timeout=5)
        d = r.json().get("data",[{}])[0]
        bids = d.get("bids",[]); asks = d.get("asks",[])
        if not bids or not asks: return None
        best_bid = float(bids[0][0]); best_ask = float(asks[0][0])
        if best_ask <= 0: return None
        spread_pct = (best_ask - best_bid) / best_ask * 100
        depth_usd = sum(float(p)*float(q) for p,q,*_ in bids) + sum(float(p)*float(q) for p,q,*_ in asks)
        return {"spread_pct": round(spread_pct,4), "depth_usd": round(depth_usd,2)}
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

# === EMA CROSSOVER SIGNAL (used in TOP5_ONLY mode instead of the RSI-neutral-zone filter) ===
# Rationale: the old filter required RSI 45-65, which is a NEUTRAL zone — it doesn't
# actually signal momentum or reversion, just excludes overbought/oversold. This likely
# contributed to the ~3% backtest winrate. EMA9/21 crossover is a concrete, directional
# event instead of a vague zone.
EMA_FAST = 9
EMA_SLOW = 21
EMA_TREND_1H = 50
VOL_CONFIRM_LOOKBACK = 20

def calc_ema_series(closes, period):
    if len(closes) < period: return []
    k = 2/(period+1)
    out = [closes[0]]
    for c in closes[1:]:
        out.append(c*k + out[-1]*(1-k))
    return out

def detect_ema_cross_up(candles):
    """True if EMA9 crossed above EMA21 on the most recently completed 15m candle."""
    if not candles or len(candles) < EMA_SLOW+2: return False
    closes = [c["c"] for c in candles]
    ema9 = calc_ema_series(closes, EMA_FAST)
    ema21 = calc_ema_series(closes, EMA_SLOW)
    if len(ema9) < 2 or len(ema21) < 2: return False
    return ema9[-2] <= ema21[-2] and ema9[-1] > ema21[-1]

def volume_confirmed(candles, lookback=VOL_CONFIRM_LOOKBACK):
    """True if the crossover candle's volume exceeds the average of the prior `lookback` candles."""
    if not candles or len(candles) < lookback+1: return False
    vols = [c["v"] for c in candles]
    avg = sum(vols[-lookback-1:-1]) / lookback
    return vols[-1] > avg

def calc_vwap(candles):
    """Volume-weighted average price over the fetched window (rolling approximation,
    not a strict midnight-UTC session reset — but captures the same 'fair value' concept
    that professional scalpers use VWAP for)."""
    if not candles: return None
    num = sum(((c["h"]+c["l"]+c["c"])/3) * c["v"] for c in candles)
    den = sum(c["v"] for c in candles)
    return num/den if den > 0 else None

# === FUNDING RATE FILTER (perpetual futures sentiment, used as context for spot entries) ===
# Extremely positive funding = market is crowded long / overheated (risk of long squeeze on
# a pullback). Extremely negative = crowded short (often precedes relief rallies, and is at
# least not a headwind for a LONG entry). We only use it to AVOID entering into overheated
# long positioning, not as a standalone signal.
FUNDING_RATE_MAX_PCT = 0.05  # reject LONG entries if funding > 0.05% per 8h (~55% annualized) — crowded longs

def get_funding_rate(symbol):
    """symbol is spot format e.g. BTC-USDT; perpetual swap instId on OKX is BTC-USDT-SWAP."""
    try:
        swap_id = symbol.replace("-USDT", "-USDT-SWAP")
        r = requests.get(f"{OKX_BASE}/api/v5/public/funding-rate?instId={swap_id}", timeout=5)
        data = r.json().get("data", [{}])
        if not data: return None
        return float(data[0].get("fundingRate", 0)) * 100  # as percent
    except Exception:
        return None

def analyze_pair_ema(symbol):
    """EMA9/21 crossover + volume confirmation + 1H trend filter + VWAP confluence.
    This mirrors what real crypto scalpers document using on BTC/ETH/SOL/XRP: no single
    indicator alone (EMA cross by itself is noisy), but EMA cross + volume + VWAP + higher
    timeframe trend agreeing together — 'confluence', in trader terms."""
    t = get_ticker(symbol)
    if not t or t["vol24h"] < MIN_VOL_24H_USDT: return None
    c15 = get_candles(symbol, "15m", 40)
    c1h = get_candles(symbol, "1H", 60)  # need enough bars for EMA50
    if not c15 or not c1h: return None
    if not detect_ema_cross_up(c15): return None
    if not volume_confirmed(c15): return None
    vwap = calc_vwap(c15)
    if vwap is None or t["price"] <= vwap: return None  # must be trading above fair value (bullish bias)
    closes1h = [c["c"] for c in c1h]
    ema50_1h = calc_ema_series(closes1h, EMA_TREND_1H)
    if not ema50_1h: return None
    if t["price"] <= ema50_1h[-1]: return None  # must be above 1H trend
    funding = get_funding_rate(symbol)
    if funding is not None and funding > FUNDING_RATE_MAX_PCT:
        log.info(f"{symbol}: EMA/VWAP/trend all confirmed but REJECTED — funding rate {funding:.4f}% "
                 f"too high (crowded longs, long-squeeze risk)")
        return None
    ob = get_orderbook(symbol)
    if not ob or ob["spread_pct"] > SPREAD_MAX_PCT or ob["depth_usd"] < MIN_ORDERBOOK_DEPTH_USD:
        log.info(f"{symbol}: EMA cross fired but REJECTED on liquidity "
                 f"(spread={ob['spread_pct'] if ob else 'N/A'}%, depth=${ob['depth_usd'] if ob else 'N/A'})")
        return None
    log.info(f"{symbol}: EMA9x21 cross UP + volume + VWAP + 1H trend + funding OK — signal fired")
    return {**t, "signal": "EMA9x21_cross_up_vwap_funding_confluence", "volume_confirmed": True,
            "trend_1h_ok": True, "vwap": round(vwap, 6), "funding_rate_pct": funding, "score": 95.0,
            "spread_pct": ob["spread_pct"], "depth_usd": ob["depth_usd"]}

def analyze_pair(symbol):
    if TOP5_ONLY:
        return analyze_pair_ema(symbol)
    t = get_ticker(symbol)
    if not t or t["vol24h"] < MIN_VOL_24H_USDT: return None
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
    # Liquidity/spread check only for candidates that already passed the mechanical filter
    # (cheap filters first — avoids hammering the OKX orderbook endpoint for all 300 pairs)
    ob = get_orderbook(symbol)
    if not ob or ob["spread_pct"] > SPREAD_MAX_PCT or ob["depth_usd"] < MIN_ORDERBOOK_DEPTH_USD:
        log.info(f"{symbol}: score={score:.1f} PASSED but REJECTED on liquidity "
                 f"(spread={ob['spread_pct'] if ob else 'N/A'}%, depth=${ob['depth_usd'] if ob else 'N/A'})")
        return None
    return {**t,"rsi":rsi15,"rsi_1h":rsi1h,"macd":macd15,"macd_1h":macd1h,
            "htf_bullish":True,"above_ma20":above_ma,"dist_from_high":round(dist,2),"score":round(score,1),
            "spread_pct":ob["spread_pct"],"depth_usd":ob["depth_usd"]}

def scan():
    candidates = []
    scored_pre_liquidity = 0
    for symbol in PAIRS:
        try:
            d = analyze_pair(symbol)
            if d:
                candidates.append(d)
            time.sleep(0.1)
        except Exception as e:
            log.debug(f"analyze_pair error {symbol}: {e}")
    candidates.sort(key=lambda x: x["score"], reverse=True)
    log.info(f"Scan complete: {len(candidates)} candidates passed all filters (score>=89 + liquidity)")
    if not candidates:
        log.warning("Scan found ZERO candidates. Check logs above for 'REJECTED on liquidity' lines, "
                    "or it may simply mean no pair scored >=89 this scan (normal during low-volatility periods).")
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
        "pnl_usd_gross": p.get("pnl_usd_gross", p.get("pnl_usd", 0)),
        "fees_paid": p.get("fees_paid", 0),
        "result": p.get("result"),
        "score": p.get("score"),
        "opened_at": p.get("opened_at"),
        "closed_at": p.get("closed_at"),
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    })
    state["demo_pending_reinvest"] += p.get("pnl_usd", 0)

TRAIL_PCT = 0.8  # trailing stop distance in % below peak

def update_demo_positions():
    """Fallback polling — called only if WebSocket is down."""
    open_positions = [p for p in state["demo_positions"] if p["status"] == "open"]
    if not open_positions: return
    for p in open_positions:
        try:
            t = get_ticker(p["symbol"])
            if t:
                check_position_for_symbol(p["symbol"], t["price"])
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
                exit_fee = calc_fee(p["size"] * p["leverage"])
                p["pnl_usd_gross"] = p["pnl_usd"]
                p["pnl_usd"] = round(p["pnl_usd"] - exit_fee, 2)
                p["fees_paid"] = p.get("fees_paid", 0) + exit_fee
                state["demo_total_fees"] += exit_fee
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

# Fast polling for open positions (5 second interval)

PARTIAL_CLOSE_PCT   = 1.5   # trigger partial close at +1.5%
PARTIAL_CLOSE_RATIO = 0.5   # close 50% of position

def check_position_for_symbol(symbol, price):
    for p in state["demo_positions"]:
        if p["status"] != "open" or p["symbol"] != symbol: continue
        try:
            p["current_price"] = price

            # === TRAILING STOP ===
            if p["direction"] == "LONG":
                peak = p.get("peak_price", p["entry"])
                if price > peak:
                    peak = price
                    p["peak_price"] = peak
                    new_sl = round(peak * (1 - TRAIL_PCT/100), 8)
                    if new_sl > p["stop_loss"]:
                        p["stop_loss"] = new_sl
                        p["trailing"] = True
                pnl_pct = (price - p["entry"]) / p["entry"]
            else:
                trough = p.get("trough_price", p["entry"])
                if price < trough:
                    trough = price
                    p["trough_price"] = trough
                    new_sl = round(trough * (1 + TRAIL_PCT/100), 8)
                    if new_sl < p["stop_loss"]:
                        p["stop_loss"] = new_sl
                        p["trailing"] = True
                pnl_pct = (p["entry"] - price) / p["entry"]

            pnl_pct *= p["leverage"]
            p["pnl_pct"] = round(pnl_pct * 100, 2)
            p["pnl_usd"] = round(p["size"] * pnl_pct, 2)

            # === PARTIAL CLOSE at +1.5% ===
            if not p.get("partial_closed") and pnl_pct * 100 >= PARTIAL_CLOSE_PCT:
                partial_size = p["size"] * PARTIAL_CLOSE_RATIO
                partial_pnl_gross = round(partial_size * pnl_pct, 2)
                exit_fee = calc_fee(partial_size * p["leverage"], maker=True)
                partial_pnl_net = round(partial_pnl_gross - exit_fee, 2)
                state["demo_balance"] += partial_pnl_net
                state["demo_pending_reinvest"] += partial_pnl_net
                state["demo_total_fees"] += exit_fee
                p["fees_paid"] = p.get("fees_paid", 0) + exit_fee
                p["size"] = round(p["size"] * (1 - PARTIAL_CLOSE_RATIO), 2)
                p["partial_closed"] = True
                p["partial_pnl"] = partial_pnl_net
                p["partial_pnl_gross"] = partial_pnl_gross
                # Move SL to breakeven
                p["stop_loss"] = p["entry"]
                p["trailing"] = True
                log.info(f"Partial close {symbol}: 50% at {pnl_pct*100:.2f}%, PnL={partial_pnl_net:+.2f}$ net (fee {exit_fee:.2f}), SL→breakeven")

            # === SL / TP CHECK ===
            hit_tp = (p["direction"]=="LONG" and price >= p["take_profit"]) or (p["direction"]=="SHORT" and price <= p["take_profit"])
            hit_sl = (p["direction"]=="LONG" and price <= p["stop_loss"]) or (p["direction"]=="SHORT" and price >= p["stop_loss"])
            if hit_tp or hit_sl:
                exit_fee = calc_fee(p["size"] * p["leverage"], maker=hit_tp)
                p["pnl_usd_gross"] = p["pnl_usd"]
                p["pnl_usd"] = round(p["pnl_usd"] - exit_fee, 2)
                p["fees_paid"] = p.get("fees_paid", 0) + exit_fee
                state["demo_total_fees"] += exit_fee
                p["status"] = "closed"
                p["result"] = "WIN" if hit_tp else ("BREAKEVEN" if p.get("partial_closed") and p["pnl_usd"] >= 0 else "LOSS")
                p["close_price"] = price
                p["closed_at"] = datetime.now(timezone.utc).strftime("%H:%M UTC")
                state["demo_balance"] += p["pnl_usd"]
                log_journal_entry(p)
                log.info(f"Position closed: {symbol} {p['result']} {p['pnl_usd']:+.2f}$ net (fees total {p['fees_paid']:.2f}, partial: {p.get('partial_pnl',0):+.2f}$)")
                resubscribe_ws()
        except Exception as e:
            log.error(f"check_position error {symbol}: {e}")

def resubscribe_ws():
    pass  # no-op, kept for compatibility

def price_monitor():
    """Fast polling every 5 seconds for open positions only."""
    log.info("Price monitor started (5s polling for open positions)")
    while True:
        time.sleep(5)
        try:
            open_pos = [p for p in state["demo_positions"] if p["status"] == "open"]
            for p in open_pos:
                try:
                    t = get_ticker(p["symbol"])
                    if t:
                        check_position_for_symbol(p["symbol"], t["price"])
                except: pass
        except: pass
        try:
            check_signal_alerts()
        except Exception as e:
            log.error(f"check_signal_alerts error: {e}")

# === LIVE TELEGRAM SIGNAL ALERTS (TOP5_ONLY mode) ===
# NOTE: SL/TP below are NOT yet validated by backtest — v2 backtest (EMA/VWAP/funding
# signal) showed a NEGATIVE edge across all 30 SL/TP combos tested. These alerts are
# sent for live observation while better parameters are being researched (v3 mean-
# reversion test in progress). Every alert message says this explicitly.
ALERT_SL_PCT = 1.5
ALERT_TP_PCT = 3.0
AUTO_SCAN_TIMES = [(7,0), (12,0), (16,0), (20,0)]  # UTC hours:minutes, ~4x/day
AUTO_SCAN_WINDOW_MIN = 10  # only trigger within this many minutes after the scheduled time

def maybe_auto_scan():
    if not TOP5_ONLY: return
    now = datetime.now(timezone.utc)
    for h, m in AUTO_SCAN_TIMES:
        scheduled = now.replace(hour=h, minute=m, second=0, microsecond=0)
        elapsed_min = (now - scheduled).total_seconds() / 60
        if 0 <= elapsed_min <= AUTO_SCAN_WINDOW_MIN:
            key = scheduled.timestamp()
            if state.get("last_auto_scan") == key: continue
            state["last_auto_scan"] = key
            run_auto_scan()
            return

def run_auto_scan():
    log.info("Auto-scan triggered (TOP5_ONLY signal mode)")
    already_open = {a["symbol"] for a in state["signal_alerts"] if a["status"] == "open"}
    for symbol in PAIRS:
        if symbol in already_open: continue
        try:
            d = analyze_pair_ema(symbol)
        except Exception as e:
            log.error(f"auto_scan analyze error {symbol}: {e}")
            continue
        if not d: continue
        entry = d["price"]
        sl = round(entry * (1 - ALERT_SL_PCT/100), 8)
        tp = round(entry * (1 + ALERT_TP_PCT/100), 8)
        alert = {
            "symbol": symbol, "direction": "LONG", "entry": entry,
            "stop_loss": sl, "take_profit": tp, "status": "open",
            "opened_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "vwap": d.get("vwap"), "funding_rate_pct": d.get("funding_rate_pct"),
        }
        state["signal_alerts"].append(alert)
        tg(f"🔵 СИГНАЛ ВХОД (тест, без подтверждённой прибыльности бэктестом)\n\n"
           f"<b>{symbol}</b> LONG\n"
           f"Вход: {entry}\n"
           f"SL: {sl} (-{ALERT_SL_PCT}%)\n"
           f"TP: {tp} (+{ALERT_TP_PCT}%)\n"
           f"Сигнал: EMA9x21 cross + объём + VWAP + 1H тренд + funding OK\n\n"
           f"⚠️ Параметры SL/TP пока не прошли проверку бэктестом на прибыльность — "
           f"наблюдательный режим.")
        log.info(f"Sent entry alert: {symbol} @ {entry}")

def check_signal_alerts():
    for a in state["signal_alerts"]:
        if a["status"] != "open": continue
        t = get_ticker(a["symbol"])
        if not t: continue
        price = t["price"]
        hit_tp = price >= a["take_profit"]
        hit_sl = price <= a["stop_loss"]
        if not (hit_tp or hit_sl): continue
        result = "TP" if hit_tp else "SL"
        pnl_pct = (price - a["entry"]) / a["entry"] * 100
        a["status"] = "closed"
        a["result"] = result
        a["close_price"] = price
        a["closed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        emoji = "✅" if result == "TP" else "🛑"
        tg(f"{emoji} СИГНАЛ ВЫХОД\n\n"
           f"<b>{a['symbol']}</b> LONG закрыт по {result}\n"
           f"Вход: {a['entry']} → Выход: {price}\n"
           f"Результат: {pnl_pct:+.2f}%")
        log.info(f"Alert closed: {a['symbol']} {result} {pnl_pct:+.2f}%")

def main():
    log.info("JARVIS ANALYST v3 starting...")
    load_persistent_state()
    load_pairs()
    if TOP5_ONLY:
        tg(f"JARVIS SIGNALS v1 (тест)\nПары: {', '.join(TOP5_PAIRS)}\n"
           f"Автосканы: {', '.join(f'{h:02d}:{m:02d}' for h,m in AUTO_SCAN_TIMES)} UTC\n"
           f"⚠️ Стратегия пока в стадии бэктест-исследования, без подтверждённой прибыльности")
    else:
        tg(f"JARVIS ANALYST v3\n{len(PAIRS)} пар | Только по команде ПЕРЕСКАН")
    time.sleep(10)
    threading.Thread(target=price_monitor, daemon=True).start()
    while True:
        time.sleep(30)
        try:
            maybe_auto_scan()
        except Exception as e:
            log.error(f"maybe_auto_scan error: {e}")

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

@app.route("/signal-alerts")
def signal_alerts_view():
    return jsonify({"alerts": state["signal_alerts"], "top5_only": TOP5_ONLY, "pairs": PAIRS})

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
            if TOP5_ONLY:
                # EMA/VWAP/funding candidates are already a complete deterministic signal —
                # no need for Claude ranking. Build panel-compatible pairs directly.
                pairs = []
                for c in candidates[:5]:
                    entry = c["price"]
                    pairs.append({
                        "symbol": c["symbol"], "entry": entry,
                        "stop_loss": round(entry * (1 - ALERT_SL_PCT/100), 8),
                        "take_profit": round(entry * (1 + ALERT_TP_PCT/100), 8),
                        "score": c.get("score", 95.0), "rsi": "-", "rsi_1h": "-",
                        "macd": c.get("signal", "EMA9x21_cross"),
                        "reason": f"EMA9x21 cross + объём + VWAP + 1H тренд + funding OK "
                                  f"(VWAP={c.get('vwap','?')}, funding={c.get('funding_rate_pct','?')}%). "
                                  f"⚠️ Параметры SL/TP не подтверждены бэктестом.",
                    })
            else:
                result = analyze_with_claude(candidates)
                pairs = result.get("top_pairs", [])[:5]
            if not pairs:
                state["status"] = "force_scan_no_pairs"
                return
            if not TOP5_ONLY:
                for p in pairs:
                    e = p.get("entry", 0)
                    if e > 0:
                        p["stop_loss"] = round(e * 0.992, 8)
                        p["take_profit"] = round(e * 1.025, 8)
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

# === PROTECTION SETTINGS ===
DAILY_LOSS_LIMIT_PCT = 5.0   # halt trading if daily loss exceeds this %
MAX_PAIR_SIZE_PCT = 20.0     # max % of balance per single pair

def check_daily_loss_limit():
    """Check if daily loss limit is breached. Returns True if trading should halt."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("day_date") != today:
        # New day — reset
        state["day_date"] = today
        state["day_start_balance"] = state["demo_balance"]
        state["trading_halted"] = False
        state["halt_reason"] = ""
    day_start = state.get("day_start_balance", state["demo_balance"])
    if day_start > 0:
        day_pnl_pct = (state["demo_balance"] - day_start) / day_start * 100
        if day_pnl_pct <= -DAILY_LOSS_LIMIT_PCT:
            if not state["trading_halted"]:
                state["trading_halted"] = True
                state["halt_reason"] = f"Дневной лимит потерь -{DAILY_LOSS_LIMIT_PCT}% достигнут"
                tg(f"⛔ ТОРГОВЛЯ ОСТАНОВЛЕНА\nДневной убыток: {day_pnl_pct:.1f}%\nВозобновление завтра.")
                log.warning(f"Daily loss limit hit: {day_pnl_pct:.1f}%")
            return True
    return state.get("trading_halted", False)

def health_check():
    """Pre-session health check. Returns (ok, message)."""
    issues = []
    # Check balance is positive
    if state["demo_balance"] <= 0:
        issues.append("Баланс <= 0")
    # Check no orphaned/corrupt positions
    for p in state["demo_positions"]:
        if p["status"] == "open":
            if p.get("size", 0) <= 0:
                issues.append(f"Позиция {p['symbol']} с size=0")
            if p.get("entry", 0) <= 0:
                issues.append(f"Позиция {p['symbol']} с entry=0")
    if issues:
        return False, "; ".join(issues)
    return True, "OK"

@app.route("/demo/open", methods=["POST"])
def demo_open():
    try:
        # Protection: daily loss limit
        if check_daily_loss_limit():
            return jsonify({"ok": False, "error": state.get("halt_reason", "Торговля остановлена")}), 403
        data = request.get_json(force=True)
        symbol = data["symbol"]
        direction = data.get("direction", "LONG")
        entry = float(data["entry"])
        stop_loss = float(data["stop_loss"])
        take_profit = float(data["take_profit"])
        leverage = float(data.get("leverage", 1.25))
        size = float(data.get("size", 2000))
        # Protection: max size per pair (20% of balance)
        max_size = state["demo_balance"] * MAX_PAIR_SIZE_PCT / 100
        if size > max_size:
            size = round(max_size, 2)
            log.info(f"Size capped to {MAX_PAIR_SIZE_PCT}% of balance: ${size}")
        state["demo_id_counter"] += 1
        entry_fee = calc_fee(size * leverage, maker=True)
        state["demo_balance"] -= entry_fee
        state["demo_total_fees"] += entry_fee
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
            "entry_fee": entry_fee,
            "fees_paid": entry_fee,
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
                exit_fee = calc_fee(p["size"] * p["leverage"])
                p["pnl_usd_gross"] = p["pnl_usd"]
                p["pnl_usd"] = round(p["pnl_usd"] - exit_fee, 2)
                p["fees_paid"] = p.get("fees_paid", 0) + exit_fee
                state["demo_total_fees"] += exit_fee
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
                exit_fee = calc_fee(p["size"] * p["leverage"])
                p["pnl_usd_gross"] = p["pnl_usd"]
                p["pnl_usd"] = round(p["pnl_usd"] - exit_fee, 2)
                p["fees_paid"] = p.get("fees_paid", 0) + exit_fee
                state["demo_total_fees"] += exit_fee
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
        "trading_halted": state.get("trading_halted", False),
        "halt_reason": state.get("halt_reason", ""),
        "day_pnl_pct": round((state["demo_balance"] - state.get("day_start_balance", state["demo_balance"])) / max(state.get("day_start_balance", 1), 1) * 100, 2),
        "total_fees_paid": round(state.get("demo_total_fees", 0), 2),
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
    total_fees = sum(t.get("fees_paid", 0) for t in j)
    return jsonify({
        "trades": len(j),
        "wins": len(wins),
        "losses": len(losses),
        "winrate": round(len(wins) / len(j) * 100, 1) if j else 0,
        "total_pnl": round(total_pnl, 2),
        "total_pnl_gross": round(sum(t.get("pnl_usd_gross", t["pnl_usd"]) for t in j), 2),
        "total_fees_paid": round(total_fees, 2),
        "avg_win": round(sum(t["pnl_usd"] for t in wins) / len(wins), 2) if wins else 0,
        "avg_loss": round(sum(t["pnl_usd"] for t in losses) / len(losses), 2) if losses else 0,
        "by_symbol": by_symbol,
        "by_date": by_date,
    })

threading.Thread(target=lambda: main(), daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",8080)))
