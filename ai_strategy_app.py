"""
ai_strategy_app.py  —  Quantly Flask server
Run:  python ai_strategy_app.py
Open: http://127.0.0.1:8090
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
    with open(path, encoding="utf-8") as f: html = f.read()
    resp = Response(html, mimetype="text/html; charset=utf-8")
    resp.headers["Cache-Control"] = "no-store"
    return resp

@app.route("/api/status")
def api_status():
    now=datetime.now(); m=now.hour*60+now.minute
    return jsonify({
        "groq_ok":  bool(os.getenv("GROQ_API_KEY")),
        "token_ok": bool(os.getenv("KITE_API_KEY") and os.getenv("KITE_ACCESS_TOKEN")),
        "market_open": now.weekday()<5 and 9*60+15<=m<=15*60+30,
        "time": now.strftime("%H:%M:%S"),
    })

@app.route("/api/token/login-url")
def login_url():
    try:
        from kiteconnect import KiteConnect
        k = os.getenv("KITE_API_KEY","").strip()
        if not k: return jsonify({"status":"error","message":"KITE_API_KEY not set"}),400
        return jsonify({"status":"ok","url":KiteConnect(api_key=k).login_url()})
    except Exception as e: return jsonify({"status":"error","message":str(e)}),500

@app.route("/api/token/generate", methods=["POST"])
def generate_token():
    rt = (request.json or {}).get("request_token","").strip()
    if not rt: return jsonify({"status":"error","message":"request_token required"}),400
    try:
        from kiteconnect import KiteConnect
        ak=os.getenv("KITE_API_KEY","").strip(); sec=os.getenv("KITE_API_SECRET","").strip()
        kite=KiteConnect(api_key=ak)
        sess=kite.generate_session(rt,api_secret=sec)
        tok=sess["access_token"]
        env_path=os.path.join(os.path.dirname(os.path.abspath(__file__)),".env")
        if os.path.exists(env_path):
            with open(env_path) as f: content=f.read()
            if "KITE_ACCESS_TOKEN" in content:
                content=re.sub(r"KITE_ACCESS_TOKEN=.*",f"KITE_ACCESS_TOKEN={tok}",content)
            else: content+=f"\nKITE_ACCESS_TOKEN={tok}\n"
            with open(env_path,"w") as f: f.write(content)
        os.environ["KITE_ACCESS_TOKEN"]=tok
        return jsonify({"status":"ok","message":"Token generated and saved!"})
    except Exception as e: return jsonify({"status":"error","message":str(e)}),500

@app.route("/api/ai/parse", methods=["POST"])
def ai_parse():
    text=(request.json or {}).get("text","").strip()
    if not text: return jsonify({"status":"error","message":"Describe your strategy first"}),400
    if not os.getenv("GROQ_API_KEY"): return jsonify({"status":"error","message":"GROQ_API_KEY not set"}),400
    try:
        return jsonify({"status":"ok","parsed":parse_strategy(text)})
    except Exception as e: return jsonify({"status":"error","message":str(e)}),500

def _instr(exchange):
    now=time.time()
    if exchange in _cache and now-_cache_t.get(exchange,0)<3600: return _cache[exchange]
    data=get_kite().instruments(exchange)
    _cache[exchange]=data; _cache_t[exchange]=now; return data

@app.route("/api/instruments/search")
def instr_search():
    q=request.args.get("q","").strip().upper()
    ex=request.args.get("exchange","NFO")
    if not q: return jsonify({"results":[]})
    try:
        res=[]
        for i in _instr(ex):
            ts=i.get("tradingsymbol",""); nm=i.get("name","")
            if q in ts.upper() or q in nm.upper():
                res.append({"tradingsymbol":ts,"name":nm,"exchange":ex,
                            "instrument_type":i.get("instrument_type",""),
                            "expiry":str(i.get("expiry",""))[:10],
                            "strike":i.get("strike",0),"lot_size":i.get("lot_size",75),
                            "instrument_token":i.get("instrument_token",0)})
        res.sort(key=lambda x:(0 if x["tradingsymbol"].startswith(q) else 1, len(x["tradingsymbol"])))
        return jsonify({"results":res[:60]})
    except Exception as e: return jsonify({"error":str(e),"results":[]}),500

@app.route("/api/instruments/near-options")
def near_options():
    try:
        instruments=_instr("NFO"); result={}
        for ul in ["NIFTY","BANKNIFTY"]:
            opts=[i for i in instruments if i.get("name","").upper()==ul
                  and i.get("instrument_type") in("CE","PE")]
            if not opts: continue
            opts.sort(key=lambda x:(str(x.get("expiry","")),x.get("strike",0)))
            expiries=sorted(set(str(o.get("expiry",""))[:10] for o in opts))
            if not expiries: continue
            ne=expiries[0]; no=[o for o in opts if str(o.get("expiry",""))[:10]==ne]
            strikes=sorted(set(o.get("strike",0) for o in no))
            mid=len(strikes)//2; atm=set(strikes[max(0,mid-4):mid+4])
            result[ul]={"expiry":ne,"options":[
                {"tradingsymbol":o["tradingsymbol"],"name":ul+" Option",
                 "strike":o["strike"],"instrument_type":o["instrument_type"],
                 "expiry":ne,"lot_size":o.get("lot_size",75),
                 "instrument_token":o["instrument_token"],"exchange":"NFO"}
                for o in no if o.get("strike") in atm][:16]}
        return jsonify(result)
    except Exception as e: return jsonify({"error":str(e)}),500

@app.route("/api/backtest/start", methods=["POST"])
def backtest_start():
    global backtest_engine
    # CRITICAL: always reject if already running; never reuse old engine
    if backtest_engine and backtest_engine.running:
        return jsonify({"status":"already_running"}),400

    d=request.json or {}
    parsed=d.get("parsed"); symbol=d.get("symbol","").strip()
    exchange=d.get("exchange","NFO"); token=d.get("instrument_token")

    if not parsed: return jsonify({"status":"error","message":"Parsed strategy required"}),400
    if not symbol or not token: return jsonify({"status":"error","message":"Select an instrument first"}),400

    trade_mode=d.get("trade_mode","intraday")
    if trade_mode not in("intraday","holding"):
        return jsonify({"status":"error","message":"trade_mode must be intraday or holding"}),400

    try: max_tpd=int(d.get("max_trades_per_day",0))
    except: max_tpd=0
    try: max_lp=float(d.get("max_loss_points",0.0))
    except: max_lp=0.0
    if max_tpd<0: return jsonify({"status":"error","message":"max_trades_per_day must be >= 0"}),400
    if max_lp<0:  return jsonify({"status":"error","message":"max_loss_points must be >= 0"}),400

    try: kite=get_kite()
    except Exception as e: return jsonify({"status":"error","message":str(e)}),500

    # Always create a FRESH engine instance
    backtest_engine=AIBacktestEngine(
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
    return jsonify({"status":"started","trade_mode":trade_mode,
                    "max_trades_per_day":max_tpd,"max_loss_points":max_lp})

@app.route("/api/backtest/logs")
def backtest_logs():
    global backtest_engine
    if not backtest_engine: return jsonify({"logs":[],"total":0,"running":False})
    since=int(request.args.get("since",0))
    return jsonify({"logs":backtest_engine.get_logs(since),
                    "total":len(backtest_engine.logs),"running":backtest_engine.running})

@app.route("/api/backtest/result")
def backtest_result():
    global backtest_engine
    if not backtest_engine or backtest_engine.result is None:
        return jsonify({"status":"not_ready"})
    return jsonify({"status":"ok",**backtest_engine.result})

@app.route("/api/backtest/reset", methods=["POST"])
def backtest_reset():
    global backtest_engine
    if backtest_engine and backtest_engine.running:
        return jsonify({"status":"error","message":"Still running"}),400
    backtest_engine=None
    return jsonify({"status":"reset"})

if __name__=="__main__":
    print("\n"+"="*55)
    print("  Quantly — AI Strategy Builder")
    print("  Open: http://127.0.0.1:8090")
    print("="*55+"\n")
    app.run(debug=False,port=8090,host="0.0.0.0")