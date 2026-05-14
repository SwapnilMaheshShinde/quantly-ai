"""
platform_app.py  —  Quantly Platform Server  v1.0
=================================================
Integrates authentication, broker flow, and the existing
AI Strategy Builder engine into one cohesive web platform.

Run:  python platform_app.py
Open: http://127.0.0.1:8090
"""
import os, sys, re, json, time
from datetime import datetime
from flask import Flask, jsonify, request, Response, render_template, redirect, url_for, make_response, send_from_directory
from dotenv import load_dotenv

load_dotenv()

# Add current directory to path so engine imports work
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from models import init_db, get_session_user, get_broker
from auth_routes import auth_bp
from ai_strategy_engine import parse_strategy, AIBacktestEngine

# ── App setup ─────────────────────────────────────────────────────────────────
app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.getenv("FLASK_SECRET", "quantly-dev-secret-change-in-prod")
app.register_blueprint(auth_bp)

# Ensure DB exists
init_db()


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _current_user():
    token = request.cookies.get("q_session")
    return get_session_user(token)

def _is_guest():
    return request.cookies.get("q_mode") == "guest"

def _require_auth_or_guest():
    """Returns (user_or_None, is_guest, redirect_response_or_None)"""
    user = _current_user()
    if user:
        return user, False, None
    if _is_guest():
        return None, True, None
    return None, False, redirect("/auth")


# ── Kite factory: uses user's broker or env defaults ─────────────────────────

def get_kite_for_user(user=None):
    from kiteconnect import KiteConnect

    api_key = ""
    api_token = ""

    # Try user's broker connection first
    if user:
        broker = get_broker(user["id"])
        if broker and broker["broker_name"].lower() in ("zerodha", "zerodha kite"):
            api_key   = broker["api_key"] or ""
            api_token = broker["access_token"] or ""

    # Fall back to environment (guest mode / default)
    if not api_key:
        api_key   = os.getenv("KITE_API_KEY", "").strip()
        api_token = os.getenv("KITE_ACCESS_TOKEN", "").strip()

    if not api_key or not api_token:
        raise ValueError("No broker API configured. Connect your broker or set KITE_API_KEY in .env")

    k = KiteConnect(api_key=api_key)
    k.set_access_token(api_token)
    return k


# ── Page Routes ───────────────────────────────────────────────────────────────

@app.route("/")
def home():
    return render_template("home.html")

@app.route("/auth")
def auth_page():
    user = _current_user()
    if user:
        return redirect("/dashboard")
    return render_template("auth.html")

@app.route("/broker")
def broker_page():
    user = _current_user()
    if not user:
        return redirect("/auth")
    return render_template("broker.html", user=user)

@app.route("/dashboard")
def dashboard():
    user, is_guest, redir = _require_auth_or_guest()
    if redir:
        return redir
    return render_template("dashboard.html", user=user, is_guest=is_guest)


# ── Status API ────────────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    now = datetime.now(); m = now.hour * 60 + now.minute
    user = _current_user()
    broker = get_broker(user["id"]) if user else None
    has_token = bool(os.getenv("KITE_API_KEY") and os.getenv("KITE_ACCESS_TOKEN"))
    if broker and broker.get("access_token"):
        has_token = True
    return jsonify({
        "groq_ok":    bool(os.getenv("GROQ_API_KEY")),
        "token_ok":   has_token,
        "market_open": now.weekday() < 5 and 9*60+15 <= m <= 15*60+30,
        "time":        now.strftime("%H:%M:%S"),
        "user":        user["full_name"] if user else "Guest",
        "is_guest":    not bool(user),
        "broker":      broker["broker_name"] if broker else None,
    })


# ── Token management (Zerodha-specific) ──────────────────────────────────────

@app.route("/api/token/login-url")
def login_url():
    try:
        from kiteconnect import KiteConnect
        user = _current_user()
        k = ""
        if user:
            broker = get_broker(user["id"])
            if broker:
                k = broker["api_key"] or ""
        if not k:
            k = os.getenv("KITE_API_KEY", "").strip()
        if not k:
            return jsonify({"status": "error", "message": "KITE_API_KEY not set"}), 400
        return jsonify({"status": "ok", "url": KiteConnect(api_key=k).login_url()})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/token/generate", methods=["POST"])
def generate_token():
    rt = (request.json or {}).get("request_token", "").strip()
    if not rt:
        return jsonify({"status": "error", "message": "request_token required"}), 400
    try:
        from kiteconnect import KiteConnect
        user = _current_user()
        ak, sec = "", ""
        if user:
            broker = get_broker(user["id"])
            if broker:
                ak  = broker["api_key"] or ""
                sec = broker["api_secret"] or ""
        if not ak:
            ak  = os.getenv("KITE_API_KEY", "").strip()
            sec = os.getenv("KITE_API_SECRET", "").strip()

        kite = KiteConnect(api_key=ak)
        sess = kite.generate_session(rt, api_secret=sec)
        tok  = sess["access_token"]

        # Save to env + optionally persist to broker record
        os.environ["KITE_ACCESS_TOKEN"] = tok
        if user:
            from models import save_broker
            save_broker(user["id"], "Zerodha Kite", ak, sec, tok)

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
        return jsonify({"status": "ok", "message": "Token generated and saved!"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ── Instrument search (reuse original logic with per-user kite) ───────────────

_cache = {}; _cache_t = {}

def _instr(exchange, user=None):
    cache_key = f"{exchange}_{user['id'] if user else 'guest'}"
    now = time.time()
    if cache_key in _cache and now - _cache_t.get(cache_key, 0) < 3600:
        return _cache[cache_key]
    data = get_kite_for_user(user).instruments(exchange)
    _cache[cache_key] = data
    _cache_t[cache_key] = now
    return data

def _strip_expiry_strike(symbol: str) -> str:
    s = symbol.upper()
    s = re.sub(r'\d{4,6}(CE|PE)$', r' \1', s)
    s = re.sub(r'\d{2}[A-Z]{3}', ' ', s)
    s = re.sub(r'\d{5,}', ' ', s)
    s = re.sub(r'\d{2,4}\b', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s

def _score_instrument(i: dict, query_words: list, raw_query: str) -> int:
    ts  = (i.get("tradingsymbol") or "").upper()
    nm  = (i.get("name") or "").upper()
    itype = (i.get("instrument_type") or "").upper()
    stripped = _strip_expiry_strike(ts)
    rq = raw_query.upper()
    score = 0
    if ts == rq: score += 100
    if ts.startswith(rq): score += 60
    if nm == rq: score += 80
    if all(w in ts for w in query_words): score += 50
    if all(w in stripped for w in query_words): score += 40
    if all(w in nm for w in query_words): score += 35
    for w in query_words:
        if w in ts: score += 10
        if w in nm: score += 8
        if w in stripped: score += 6
    for w in query_words:
        if w in ("CE", "PE", "FUT", "EQ") and itype == w:
            score += 25
    return score

@app.route("/api/instruments/search")
def instr_search():
    raw_q    = request.args.get("q", "").strip()
    exchange = request.args.get("exchange", "NFO").upper()
    if not raw_q:
        return jsonify({"results": []})
    query_words = [w for w in re.split(r'[\s\-_/]+', raw_q.upper()) if w]
    user = _current_user()
    try:
        instruments = _instr(exchange, user)
    except Exception as e:
        return jsonify({"error": str(e), "results": []}), 500
    extra = []
    if exchange == "NFO":
        try: extra = _instr("NSE", user)
        except: pass
    scored = []
    for inst in instruments + extra:
        s = _score_instrument(inst, query_words, raw_q)
        if s > 0:
            scored.append((s, inst))
    scored.sort(key=lambda x: (-x[0], len(x[1].get("tradingsymbol", "") or "")))
    results = []; seen = set()
    for _, i in scored[:80]:
        tok = i.get("instrument_token", 0)
        if tok in seen: continue
        seen.add(tok)
        results.append({
            "tradingsymbol":    i.get("tradingsymbol", ""),
            "name":             i.get("name", ""),
            "exchange":         i.get("exchange", ""),
            "instrument_type":  i.get("instrument_type", ""),
            "expiry":           str(i.get("expiry", ""))[:10],
            "strike":           i.get("strike", 0),
            "lot_size":         i.get("lot_size", 75),
            "instrument_token": i.get("instrument_token", 0),
        })
    return jsonify({"results": results[:60]})

@app.route("/api/instruments/near-options")
def near_options():
    user = _current_user()
    try:
        instruments = _instr("NFO", user)
        result = {}
        for ul in ["NIFTY", "BANKNIFTY"]:
            opts = [i for i in instruments
                    if (i.get("name","") or "").upper() == ul
                    and (i.get("instrument_type","") or "") in ("CE","PE")]
            if not opts: continue
            opts.sort(key=lambda x: (str(x.get("expiry","")), x.get("strike",0)))
            expiries = sorted(set(str(o.get("expiry",""))[:10] for o in opts))
            if not expiries: continue
            ne = expiries[0]
            ne_opts = [o for o in opts if str(o.get("expiry",""))[:10] == ne]
            strikes = sorted(set(o.get("strike",0) for o in ne_opts))
            mid = len(strikes)//2
            atm_set = set(strikes[max(0,mid-4):mid+4])
            result[ul] = {
                "expiry": ne,
                "options": [{
                    "tradingsymbol": o["tradingsymbol"], "name": ul+" Option",
                    "strike": o["strike"], "instrument_type": o["instrument_type"],
                    "expiry": ne, "lot_size": o.get("lot_size",75),
                    "instrument_token": o["instrument_token"], "exchange": "NFO",
                } for o in ne_opts if o.get("strike") in atm_set][:16]
            }
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── AI Parse ──────────────────────────────────────────────────────────────────

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


# ── Backtest ──────────────────────────────────────────────────────────────────

# Per-session backtest engines (keyed by session token)
_engines = {}

@app.route("/api/backtest/start", methods=["POST"])
def backtest_start():
    user, is_guest, redir = _require_auth_or_guest()
    token = request.cookies.get("q_session", "guest")
    engine = _engines.get(token)
    if engine and engine.running:
        return jsonify({"status":"already_running"}), 400

    d = request.json or {}
    parsed   = d.get("parsed")
    symbol   = d.get("symbol","").strip()
    exchange = d.get("exchange","NFO")
    tok_inst = d.get("instrument_token")

    if not parsed: return jsonify({"status":"error","message":"Parsed strategy required"}), 400
    if not symbol or not tok_inst: return jsonify({"status":"error","message":"Select an instrument first"}), 400

    trade_mode = d.get("trade_mode","intraday")
    if trade_mode not in ("intraday","holding"):
        return jsonify({"status":"error","message":"trade_mode must be intraday or holding"}), 400

    try:   max_tpd = int(d.get("max_trades_per_day",0))
    except: max_tpd = 0
    try:   max_lp  = float(d.get("max_loss_points",0.0))
    except: max_lp  = 0.0

    try:
        kite = get_kite_for_user(user)
    except Exception as e:
        return jsonify({"status":"error","message":str(e)}), 500

    engine = AIBacktestEngine(
        kite=kite, parsed=parsed,
        instrument_token=tok_inst, tradingsymbol=symbol, exchange=exchange,
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
    _engines[token] = engine
    engine.start()
    return jsonify({"status":"started","trade_mode":trade_mode})

@app.route("/api/backtest/logs")
def backtest_logs():
    token = request.cookies.get("q_session","guest")
    engine = _engines.get(token)
    if not engine: return jsonify({"logs":[],"total":0,"running":False})
    since = int(request.args.get("since",0))
    return jsonify({"logs":engine.get_logs(since),"total":len(engine.logs),"running":engine.running})

@app.route("/api/backtest/result")
def backtest_result():
    token = request.cookies.get("q_session","guest")
    engine = _engines.get(token)
    if not engine or engine.result is None: return jsonify({"status":"not_ready"})
    return jsonify({"status":"ok",**engine.result})

@app.route("/api/backtest/reset", methods=["POST"])
def backtest_reset():
    token = request.cookies.get("q_session","guest")
    engine = _engines.get(token)
    if engine and engine.running: return jsonify({"status":"error","message":"Still running"}), 400
    _engines.pop(token, None)
    return jsonify({"status":"reset"})


if __name__ == "__main__":
    print("\n" + "="*55)
    print("  Quantly Platform  v1.0")
    print("  Open: http://127.0.0.1:8090")
    print("="*55 + "\n")
    app.run(debug=False, port=8090, host="0.0.0.0")