"""
ai_strategy_app.py  —  Quantly Flask server  v3.0
Run:  python ai_strategy_app.py
Open: http://127.0.0.1:8090

FIXES in v3.0:
  - Instrument search completely rewritten:
      • Kite uses names like NIFTY25MARFUT, BANKNIFTY25300CE, etc.
      • Search now matches against tradingsymbol, name, and a "clean" version
        of the tradingsymbol with year/month/strike stripped out
      • Supports partial matches: "nifty fut", "banknifty 25300 ce", "reliance"
      • NSE index tokens (NIFTY 50, NIFTY BANK) also included
      • Results grouped: FUT → CE → PE → EQ → others
      • quick-pick loads both NFO and NSE near-ATM options correctly
"""
import os, sys, re, json, time
from datetime import datetime
from flask import Flask, jsonify, request, Response
from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ai_strategy_engine import parse_strategy, AIBacktestEngine

app = Flask(__name__)

def get_kite():
    from kiteconnect import KiteConnect
    api_key   = os.getenv("KITE_API_KEY","").strip()
    api_token = os.getenv("KITE_ACCESS_TOKEN","").strip()
    if not api_key or not api_token:
        raise ValueError("KITE_API_KEY or KITE_ACCESS_TOKEN missing in .env")
    k = KiteConnect(api_key=api_key)
    k.set_access_token(api_token)
    return k

backtest_engine = None
_cache = {}; _cache_t = {}

@app.route("/")
def index():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_strategy.html")
    if not os.path.exists(path):
        return Response("<h2>ai_strategy.html not found</h2>", mimetype="text/html")
    with open(path, encoding="utf-8") as f:
        html = f.read()
    resp = Response(html, mimetype="text/html; charset=utf-8")
    resp.headers["Cache-Control"] = "no-store"
    return resp

@app.route("/api/status")
def api_status():
    now = datetime.now(); m = now.hour * 60 + now.minute
    return jsonify({
        "groq_ok":    bool(os.getenv("GROQ_API_KEY")),
        "token_ok":   bool(os.getenv("KITE_API_KEY") and os.getenv("KITE_ACCESS_TOKEN")),
        "market_open": now.weekday() < 5 and 9*60+15 <= m <= 15*60+30,
        "time":        now.strftime("%H:%M:%S"),
    })

@app.route("/api/token/login-url")
def login_url():
    try:
        from kiteconnect import KiteConnect
        k = os.getenv("KITE_API_KEY","").strip()
        if not k:
            return jsonify({"status":"error","message":"KITE_API_KEY not set"}), 400
        return jsonify({"status":"ok","url":KiteConnect(api_key=k).login_url()})
    except Exception as e:
        return jsonify({"status":"error","message":str(e)}), 500

@app.route("/api/token/generate", methods=["POST"])
def generate_token():
    rt = (request.json or {}).get("request_token","").strip()
    if not rt:
        return jsonify({"status":"error","message":"request_token required"}), 400
    try:
        from kiteconnect import KiteConnect
        ak  = os.getenv("KITE_API_KEY","").strip()
        sec = os.getenv("KITE_API_SECRET","").strip()
        kite = KiteConnect(api_key=ak)
        sess = kite.generate_session(rt, api_secret=sec)
        tok  = sess["access_token"]
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                content = f.read()
            if "KITE_ACCESS_TOKEN" in content:
                content = re.sub(r"KITE_ACCESS_TOKEN=.*", f"KITE_ACCESS_TOKEN={tok}", content)
            else:
                content += f"\nKITE_ACCESS_TOKEN={tok}\n"
            with open(env_path, "w") as f:
                f.write(content)
        os.environ["KITE_ACCESS_TOKEN"] = tok
        return jsonify({"status":"ok","message":"Token generated and saved!"})
    except Exception as e:
        return jsonify({"status":"error","message":str(e)}), 500

@app.route("/api/ai/parse", methods=["POST"])
def ai_parse():
    text = (request.json or {}).get("text","").strip()
    if not text:
        return jsonify({"status":"error","message":"Describe your strategy first"}), 400
    if not os.getenv("GROQ_API_KEY"):
        return jsonify({"status":"error","message":"GROQ_API_KEY not set"}), 400
    try:
        return jsonify({"status":"ok","parsed":parse_strategy(text)})
    except Exception as e:
        return jsonify({"status":"error","message":str(e)}), 500


# ── Instrument cache ──────────────────────────────────────────────────────────
def _instr(exchange):
    now = time.time()
    if exchange in _cache and now - _cache_t.get(exchange, 0) < 3600:
        return _cache[exchange]
    data = get_kite().instruments(exchange)
    _cache[exchange] = data
    _cache_t[exchange] = now
    return data


def _strip_expiry_strike(symbol: str) -> str:
    """
    Strip year/month/strike from Kite tradingsymbol so that
    "NIFTY25MARFUT"  → "NIFTY FUT"
    "BANKNIFTY25300CE" → "BANKNIFTY CE"
    "NIFTY2541522500CE" → "NIFTY CE"
    Allows plain-text queries like "nifty fut" or "banknifty ce" to match.
    """
    s = symbol.upper()
    # Remove strike price (4-6 digit number before CE/PE)
    s = re.sub(r'\d{4,6}(CE|PE)$', r' \1', s)
    # Remove expiry: e.g. 25MAR, 25APR, 2541500, 25300 etc.
    s = re.sub(r'\d{2}[A-Z]{3}', ' ', s)   # 25MAR style
    s = re.sub(r'\d{5,}', ' ', s)           # long numeric expiry
    s = re.sub(r'\d{2,4}\b', ' ', s)        # remaining short numbers
    # Normalize FUT suffix
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _score_instrument(i: dict, query_words: list, raw_query: str) -> int:
    """
    Score an instrument for relevance to a query.
    Higher = better match. Returns 0 if no match at all.
    """
    ts  = (i.get("tradingsymbol") or "").upper()
    nm  = (i.get("name")          or "").upper()
    itype = (i.get("instrument_type") or "").upper()
    stripped = _strip_expiry_strike(ts)

    rq = raw_query.upper()
    score = 0

    # Exact tradingsymbol match
    if ts == rq:
        score += 100

    # Tradingsymbol starts with query
    if ts.startswith(rq):
        score += 60

    # Name exact match
    if nm == rq:
        score += 80

    # All query words in tradingsymbol
    if all(w in ts for w in query_words):
        score += 50

    # All query words in stripped symbol
    if all(w in stripped for w in query_words):
        score += 40

    # All query words in name
    if all(w in nm for w in query_words):
        score += 35

    # Partial: at least one word matches
    for w in query_words:
        if w in ts:   score += 10
        if w in nm:   score += 8
        if w in stripped: score += 6

    # Instrument type boost if user typed CE/PE/FUT/EQ
    for w in query_words:
        if w in ("CE", "PE", "FUT", "EQ") and itype == w:
            score += 25

    return score


@app.route("/api/instruments/search")
def instr_search():
    raw_q   = request.args.get("q", "").strip()
    exchange = request.args.get("exchange", "NFO").upper()

    if not raw_q:
        return jsonify({"results": []})

    # Normalise query — split into words, uppercase
    query_words = [w for w in re.split(r'[\s\-_/]+', raw_q.upper()) if w]

    try:
        instruments = _instr(exchange)
    except Exception as e:
        return jsonify({"error": str(e), "results": []}), 500

    # Also search NSE if user is looking for index/equity
    extra = []
    if exchange == "NFO":
        try:
            extra = _instr("NSE")
        except Exception:
            extra = []

    all_instruments = instruments + extra
    scored = []

    for inst in all_instruments:
        s = _score_instrument(inst, query_words, raw_q)
        if s > 0:
            scored.append((s, inst))

    # Sort by score descending, then tradingsymbol length ascending
    scored.sort(key=lambda x: (-x[0], len(x[1].get("tradingsymbol","") or "")))

    results = []
    seen_tokens = set()
    for _, i in scored[:80]:
        token = i.get("instrument_token", 0)
        if token in seen_tokens:
            continue
        seen_tokens.add(token)
        results.append({
            "tradingsymbol":   i.get("tradingsymbol",""),
            "name":            i.get("name",""),
            "exchange":        i.get("exchange",""),
            "instrument_type": i.get("instrument_type",""),
            "expiry":          str(i.get("expiry",""))[:10],
            "strike":          i.get("strike", 0),
            "lot_size":        i.get("lot_size", 75),
            "instrument_token":i.get("instrument_token", 0),
        })

    return jsonify({"results": results[:60]})


@app.route("/api/instruments/near-options")
def near_options():
    try:
        instruments = _instr("NFO")
        result = {}

        for ul in ["NIFTY", "BANKNIFTY"]:
            opts = [i for i in instruments
                    if (i.get("name","") or "").upper() == ul
                    and (i.get("instrument_type","") or "") in ("CE","PE")]
            if not opts:
                continue

            opts.sort(key=lambda x: (str(x.get("expiry","")), x.get("strike", 0)))
            expiries = sorted(set(str(o.get("expiry",""))[:10] for o in opts))
            if not expiries:
                continue

            ne = expiries[0]
            ne_opts = [o for o in opts if str(o.get("expiry",""))[:10] == ne]

            # Find ATM strikes
            strikes = sorted(set(o.get("strike", 0) for o in ne_opts))
            mid = len(strikes) // 2
            atm_set = set(strikes[max(0, mid-4): mid+4])

            result[ul] = {
                "expiry": ne,
                "options": [
                    {
                        "tradingsymbol":    o["tradingsymbol"],
                        "name":             ul + " Option",
                        "strike":           o["strike"],
                        "instrument_type":  o["instrument_type"],
                        "expiry":           ne,
                        "lot_size":         o.get("lot_size", 75),
                        "instrument_token": o["instrument_token"],
                        "exchange":         "NFO",
                    }
                    for o in ne_opts if o.get("strike") in atm_set
                ][:16]
            }

        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Backtest ──────────────────────────────────────────────────────────────────
@app.route("/api/backtest/start", methods=["POST"])
def backtest_start():
    global backtest_engine
    if backtest_engine and backtest_engine.running:
        return jsonify({"status":"already_running"}), 400

    d = request.json or {}
    parsed   = d.get("parsed")
    symbol   = d.get("symbol","").strip()
    exchange = d.get("exchange","NFO")
    token    = d.get("instrument_token")

    if not parsed:
        return jsonify({"status":"error","message":"Parsed strategy required"}), 400
    if not symbol or not token:
        return jsonify({"status":"error","message":"Select an instrument first"}), 400

    trade_mode = d.get("trade_mode","intraday")
    if trade_mode not in ("intraday","holding"):
        return jsonify({"status":"error","message":"trade_mode must be intraday or holding"}), 400

    try:    max_tpd = int(d.get("max_trades_per_day", 0))
    except: max_tpd = 0
    try:    max_lp  = float(d.get("max_loss_points", 0.0))
    except: max_lp  = 0.0

    if max_tpd < 0: return jsonify({"status":"error","message":"max_trades_per_day must be >= 0"}), 400
    if max_lp  < 0: return jsonify({"status":"error","message":"max_loss_points must be >= 0"}), 400

    try:
        kite = get_kite()
    except Exception as e:
        return jsonify({"status":"error","message":str(e)}), 500

    backtest_engine = AIBacktestEngine(
        kite=kite, parsed=parsed,
        instrument_token=token, tradingsymbol=symbol, exchange=exchange,
        start_date=d.get("start_date","2025-01-01"),
        end_date=d.get("end_date","2025-12-31"),
        interval=d.get("interval","5minute"),
        trade_mode=trade_mode,
        session_start=d.get("session_start","09:15"),
        session_end=d.get("session_end","15:15"),
        lot_size=int(d.get("lot_size",75)),
        lots=int(d.get("lots",1)),
        capital=float(d.get("capital",200000)),
        max_trades_per_day=max_tpd,
        max_loss_points=max_lp,
    )
    backtest_engine.start()
    return jsonify({
        "status":            "started",
        "trade_mode":        trade_mode,
        "max_trades_per_day":max_tpd,
        "max_loss_points":   max_lp,
    })

@app.route("/api/backtest/logs")
def backtest_logs():
    global backtest_engine
    if not backtest_engine:
        return jsonify({"logs":[],"total":0,"running":False})
    since = int(request.args.get("since", 0))
    return jsonify({
        "logs":    backtest_engine.get_logs(since),
        "total":   len(backtest_engine.logs),
        "running": backtest_engine.running,
    })

@app.route("/api/backtest/result")
def backtest_result():
    global backtest_engine
    if not backtest_engine or backtest_engine.result is None:
        return jsonify({"status":"not_ready"})
    return jsonify({"status":"ok", **backtest_engine.result})

@app.route("/api/backtest/reset", methods=["POST"])
def backtest_reset():
    global backtest_engine
    if backtest_engine and backtest_engine.running:
        return jsonify({"status":"error","message":"Still running"}), 400
    backtest_engine = None
    return jsonify({"status":"reset"})


if __name__ == "__main__":
    print("\n" + "="*55)
    print("  Quantly — AI Strategy Builder  v3.0")
    print("  Open: http://127.0.0.1:8090")
    print("="*55 + "\n")
    app.run(debug=False, port=8090, host="0.0.0.0")