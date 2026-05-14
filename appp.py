"""
app.py — Intraday Options Bot
Run: python app.py
Open: http://localhost:8080
"""

import os, sys, re, threading, time
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, Response

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine import BacktestEngine

app = Flask(__name__, static_folder="static")


# ══════════════════════════════════════════════════════════════════════════════
# KITE HELPER
# ══════════════════════════════════════════════════════════════════════════════

def get_kite():
    from kiteconnect import KiteConnect
    api_key   = os.getenv("KITE_API_KEY",      "").strip()
    api_token = os.getenv("KITE_ACCESS_TOKEN", "").strip()
    if not api_key or not api_token:
        raise ValueError("KITE_API_KEY or KITE_ACCESS_TOKEN missing in .env")
    k = KiteConnect(api_key=api_key)
    k.set_access_token(api_token)
    return k

def _ts():
    return datetime.now().strftime("%H:%M:%S")


# ══════════════════════════════════════════════════════════════════════════════
# GLOBAL STATE
# ══════════════════════════════════════════════════════════════════════════════

backtest_engine = None

paper_running  = False
paper_logs     = []
paper_position = 0
paper_entry_px = 0.0
paper_entry_dt = None
paper_trades   = []
paper_ltp      = 0.0
paper_signal   = "HOLD"
paper_config   = {}
MAX_LOGS       = 400

def _plog(msg, level="INFO"):
    paper_logs.append({"time": _ts(), "level": level, "message": msg})
    if len(paper_logs) > MAX_LOGS:
        paper_logs.pop(0)


# ══════════════════════════════════════════════════════════════════════════════
# DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
    if not os.path.exists(path):
        return Response("<h2>dashboard.html not found</h2>", mimetype="text/html")
    with open(path, encoding="utf-8") as f:
        html = f.read()
    resp = Response(html, mimetype="text/html; charset=utf-8")
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ══════════════════════════════════════════════════════════════════════════════
# STATUS / TOKEN
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/status")
def api_status():
    api_key   = os.getenv("KITE_API_KEY",      "")
    api_token = os.getenv("KITE_ACCESS_TOKEN", "")
    now       = datetime.now()
    mins      = now.hour * 60 + now.minute
    mkt_open  = now.weekday() < 5 and 9*60+15 <= mins <= 15*60+30
    return jsonify({
        "token_ok":    bool(api_key and api_token),
        "market_open": mkt_open,
        "time":        now.strftime("%H:%M:%S"),
        "date":        now.strftime("%Y-%m-%d"),
    })

@app.route("/api/token/login-url")
def login_url():
    try:
        from kiteconnect import KiteConnect
        api_key = os.getenv("KITE_API_KEY", "").strip()
        if not api_key:
            return jsonify({"status": "error", "message": "KITE_API_KEY not set in .env"}), 400
        return jsonify({"status": "ok", "url": KiteConnect(api_key=api_key).login_url()})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/token/generate", methods=["POST"])
def generate_token():
    data          = request.json or {}
    request_token = data.get("request_token", "").strip()
    if not request_token:
        return jsonify({"status": "error", "message": "request_token required"}), 400
    try:
        from kiteconnect import KiteConnect
        api_key    = os.getenv("KITE_API_KEY",    "").strip()
        api_secret = os.getenv("KITE_API_SECRET", "").strip()
        if not api_key or not api_secret:
            return jsonify({"status": "error", "message": "API key/secret missing in .env"}), 400
        kite         = KiteConnect(api_key=api_key)
        sess         = kite.generate_session(request_token, api_secret=api_secret)
        access_token = sess["access_token"]
        env_path     = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                content = f.read()
            if "KITE_ACCESS_TOKEN" in content:
                content = re.sub(r"KITE_ACCESS_TOKEN=.*",
                                 f"KITE_ACCESS_TOKEN={access_token}", content)
            else:
                content += f"\nKITE_ACCESS_TOKEN={access_token}\n"
            with open(env_path, "w") as f:
                f.write(content)
        os.environ["KITE_ACCESS_TOKEN"] = access_token
        return jsonify({"status": "ok", "message": "Token generated and saved!"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# INSTRUMENTS
# ══════════════════════════════════════════════════════════════════════════════

_cache = {}
_cache_time = {}

def _instruments(exchange):
    now = time.time()
    if exchange in _cache and now - _cache_time.get(exchange, 0) < 3600:
        return _cache[exchange]
    kite = get_kite()
    data = kite.instruments(exchange)
    _cache[exchange]      = data
    _cache_time[exchange] = now
    return data

@app.route("/api/instruments/search")
def instr_search():
    q        = request.args.get("q", "").strip().upper()
    exchange = request.args.get("exchange", "NFO")
    if not q:
        return jsonify({"results": []})
    try:
        results = []
        for i in _instruments(exchange):
            ts    = i.get("tradingsymbol", "")
            name  = i.get("name",           "")
            itype = i.get("instrument_type","")
            if q in ts.upper() or q in name.upper():
                results.append({
                    "tradingsymbol":    ts,
                    "name":             name,
                    "exchange":         exchange,
                    "instrument_type":  itype,
                    "expiry":           str(i.get("expiry", ""))[:10],
                    "strike":           i.get("strike",           0),
                    "lot_size":         i.get("lot_size",        75),
                    "instrument_token": i.get("instrument_token", 0),
                })
        results.sort(key=lambda x: (
            0 if x["tradingsymbol"].startswith(q) else 1,
            len(x["tradingsymbol"])
        ))
        return jsonify({"results": results[:60]})
    except Exception as e:
        return jsonify({"error": str(e), "results": []}), 500

@app.route("/api/instruments/near-options")
def near_options():
    try:
        instruments = _instruments("NFO")
        result = {}
        for underlying in ["NIFTY", "BANKNIFTY"]:
            opts = [i for i in instruments
                    if i.get("name","").upper() == underlying
                    and i.get("instrument_type") in ("CE","PE")]
            if not opts: continue
            opts.sort(key=lambda x: (str(x.get("expiry","")), x.get("strike",0)))
            expiries  = sorted(set(str(o.get("expiry",""))[:10] for o in opts))
            if not expiries: continue
            near_exp  = expiries[0]
            near_opts = [o for o in opts if str(o.get("expiry",""))[:10] == near_exp]
            strikes   = sorted(set(o.get("strike",0) for o in near_opts))
            mid       = len(strikes) // 2
            atm       = set(strikes[max(0,mid-4):mid+4])
            result[underlying] = {
                "expiry": near_exp,
                "options": [
                    {"tradingsymbol": o["tradingsymbol"], "name": underlying+" Option",
                     "strike": o["strike"], "instrument_type": o["instrument_type"],
                     "type": o["instrument_type"], "expiry": near_exp,
                     "lot_size": o.get("lot_size",75),
                     "instrument_token": o["instrument_token"], "exchange": "NFO"}
                    for o in near_opts if o.get("strike") in atm
                ][:16]
            }
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# BACKTEST
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/backtest/start", methods=["POST"])
def backtest_start():
    global backtest_engine
    if backtest_engine and backtest_engine.running:
        return jsonify({"status": "already_running"}), 400

    data          = request.json or {}
    symbol        = data.get("symbol",        "").strip()
    exchange      = data.get("exchange",      "NFO")
    token         = data.get("instrument_token")
    start_date    = data.get("start_date",    "2025-01-01")
    end_date      = data.get("end_date",      "2025-12-31")
    session_start = data.get("session_start", "09:45")
    session_end   = data.get("session_end",   "15:00")
    lot_size      = int(data.get("lot_size",   75))
    lots          = int(data.get("lots",        1))
    capital       = float(data.get("capital", 200000))
    ma_period     = int(data.get("ma_period",   7))
    candle_count  = int(data.get("candle_count", 2))  # NEW

    if not symbol:
        return jsonify({"status": "error", "message": "symbol required"}), 400
    if not token:
        return jsonify({"status": "error",
                        "message": "instrument_token required — select from search"}), 400
    try:
        kite = get_kite()
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

    backtest_engine = BacktestEngine(
        kite             = kite,
        instrument_token = token,
        tradingsymbol    = symbol,
        exchange         = exchange,
        start_date       = start_date,
        end_date         = end_date,
        session_start    = session_start,
        session_end      = session_end,
        lot_size         = lot_size,
        lots             = lots,
        capital          = capital,
        ma_period        = ma_period,
        candle_count     = candle_count,   # PASSED TO ENGINE
    )
    backtest_engine.start()
    return jsonify({"status": "started", "ma_period": ma_period, "candle_count": candle_count})

@app.route("/api/backtest/logs")
def backtest_logs():
    global backtest_engine
    if not backtest_engine:
        return jsonify({"logs": [], "total": 0, "running": False})
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
        return jsonify({"status": "not_ready"})
    return jsonify({"status": "ok", **backtest_engine.result})

@app.route("/api/backtest/status")
def backtest_status():
    global backtest_engine
    if not backtest_engine:
        return jsonify({"running": False, "done": False})
    return jsonify(backtest_engine.get_status())

@app.route("/api/backtest/reset", methods=["POST"])
def backtest_reset():
    global backtest_engine
    if backtest_engine and backtest_engine.running:
        return jsonify({"status": "error", "message": "Still running"}), 400
    backtest_engine = None
    return jsonify({"status": "reset"})


# ══════════════════════════════════════════════════════════════════════════════
# PAPER TRADING ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def _check_buy_paper(df, n):
    """Check last N candles all green and above SMA."""
    if len(df) < n:
        return False
    for k in range(n):
        row = df.iloc[-(k+1)]
        if pd.isna(row["ma"]):
            return False
        if not bool(row["green"]) or not bool(row["above_ma"]):
            return False
    return True

def _check_sell_paper(df, n):
    """Check last N candles all red and below SMA."""
    if len(df) < n:
        return False
    for k in range(n):
        row = df.iloc[-(k+1)]
        if pd.isna(row["ma"]):
            return False
        if not bool(row["red"]) or not bool(row["below_ma"]):
            return False
    return True

import pandas as _pd_global

def _paper_loop():
    global paper_running, paper_position, paper_entry_px
    global paper_entry_dt, paper_trades, paper_ltp, paper_signal

    cfg          = paper_config
    symbol       = cfg["symbol"]
    exchange     = cfg["exchange"]
    token        = int(cfg["instrument_token"])
    ss_str       = cfg.get("session_start", "09:45")
    se_str       = cfg.get("session_end",   "15:00")
    ss_h, ss_m   = int(ss_str.split(":")[0]), int(ss_str.split(":")[1])
    se_h, se_m   = int(se_str.split(":")[0]), int(se_str.split(":")[1])
    ss_mins      = ss_h * 60 + ss_m
    se_mins      = se_h * 60 + se_m
    lot_size     = int(cfg.get("lot_size",      75))
    lots         = int(cfg.get("lots",           1))
    qty          = lot_size * lots
    max_loss     = float(cfg.get("max_loss",   5000))
    tick_secs    = int(cfg.get("tick_seconds",   20))
    ma_period    = int(cfg.get("ma_period",       7))
    candle_count = int(cfg.get("candle_count",    2))   # NEW
    slippage     = 1.0
    brokerage    = 20.0

    import pandas as pd

    _plog(f"=== PAPER ENGINE START ===")
    _plog(f"Instrument   : {symbol} ({exchange})")
    _plog(f"MA Period    : SMA({ma_period})")
    _plog(f"Candle Count : {candle_count} consecutive candle(s) for entry")
    _plog(f"Session      : {ss_str} - {se_str}")
    _plog(f"Qty          : {qty}  Max Loss: Rs {max_loss}")

    try:
        kite = get_kite()
    except Exception as e:
        _plog(f"Kite error: {e}", "ERROR")
        paper_running = False
        return

    while paper_running:
        now      = datetime.now()
        cur_mins = now.hour * 60 + now.minute

        try:
            key      = f"{exchange}:{symbol}"
            ltp_data = kite.ltp([key])
            ltp      = float(ltp_data[key]["last_price"])
            paper_ltp = ltp

            # ── EOD FORCE EXIT ─────────────────────────────────────────────
            if paper_position != 0 and cur_mins >= se_mins:
                exit_px = ltp - slippage if paper_position == 1 else ltp + slippage
                pts     = (exit_px - paper_entry_px) if paper_position == 1 else (paper_entry_px - exit_px)
                pnl     = pts * qty - brokerage
                paper_trades.append({
                    "direction":    "LONG" if paper_position == 1 else "SHORT",
                    "entry_price":  round(paper_entry_px, 2),
                    "exit_price":   round(exit_px, 2),
                    "points":       round(pts, 2),
                    "realised_pnl": round(pnl, 2),
                    "result":       "WIN" if pnl > 0 else "LOSS",
                    "exit_reason":  "EOD",
                    "time":         now.strftime("%H:%M:%S"),
                })
                _plog(f"EOD EXIT {'LONG' if paper_position==1 else 'SHORT'} @ {exit_px:.2f}  P&L={pnl:+.0f}", "WARNING")
                paper_position = 0
                paper_signal   = "EOD_EXIT"

            # ── Max loss ───────────────────────────────────────────────────
            total_r = sum(t["realised_pnl"] for t in paper_trades)
            if max_loss > 0 and total_r <= -abs(max_loss):
                _plog(f"Max loss hit (Rs {total_r:.0f}). Stopping.", "ERROR")
                paper_running = False
                break

            # ── Fetch candles ──────────────────────────────────────────────
            from_dt = (now - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
            to_dt   = now.strftime("%Y-%m-%d %H:%M:%S")
            raw     = kite.historical_data(
                instrument_token=token, from_date=from_dt, to_date=to_dt,
                interval="3minute", continuous=False, oi=False,
            )
            min_req = ma_period + candle_count + 2
            if len(raw) < min_req:
                _plog(f"Only {len(raw)} candles, need {min_req}...")
                time.sleep(tick_secs)
                continue

            df             = pd.DataFrame(raw)
            df["date"]     = pd.to_datetime(df["date"])
            df["ma"]       = df["close"].rolling(ma_period).mean()
            df["green"]    = df["close"] > df["open"]
            df["red"]      = df["close"] < df["open"]
            df["above_ma"] = df["close"] > df["ma"]
            df["below_ma"] = df["close"] < df["ma"]

            last = df.iloc[-1]
            if pd.isna(last["ma"]):
                time.sleep(tick_secs)
                continue

            sma_val    = float(last["ma"])
            last_above = bool(last["above_ma"])
            last_below = bool(last["below_ma"])

            # Signal display
            buy_sig  = _check_buy_paper(df, candle_count)
            sell_sig = _check_sell_paper(df, candle_count)
            paper_signal = "BUY_SIGNAL" if buy_sig else "SELL_SIGNAL" if sell_sig else "HOLD"

            _plog(
                f"LTP={ltp:.2f}  SMA({ma_period})={sma_val:.2f}  "
                f"{paper_signal}  "
                f"{'LONG' if paper_position==1 else 'SHORT' if paper_position==-1 else 'FLAT'}"
            )

            # ── EXIT — single candle crosses SMA ───────────────────────────
            if paper_position == 1 and last_below:
                exit_px = ltp - slippage
                pnl     = (exit_px - paper_entry_px) * qty - brokerage
                paper_trades.append({
                    "direction": "LONG", "entry_price": round(paper_entry_px,2),
                    "exit_price": round(exit_px,2), "points": round(exit_px-paper_entry_px,2),
                    "realised_pnl": round(pnl,2), "result": "WIN" if pnl>0 else "LOSS",
                    "exit_reason": "SIGNAL", "time": now.strftime("%H:%M:%S"),
                })
                _plog(f"EXIT LONG @ {exit_px:.2f}  P&L={pnl:+.0f}", "INFO" if pnl>=0 else "WARNING")
                paper_position = 0

            elif paper_position == -1 and last_above:
                exit_px = ltp + slippage
                pnl     = (paper_entry_px - exit_px) * qty - brokerage
                paper_trades.append({
                    "direction": "SHORT", "entry_price": round(paper_entry_px,2),
                    "exit_price": round(exit_px,2), "points": round(paper_entry_px-exit_px,2),
                    "realised_pnl": round(pnl,2), "result": "WIN" if pnl>0 else "LOSS",
                    "exit_reason": "SIGNAL", "time": now.strftime("%H:%M:%S"),
                })
                _plog(f"EXIT SHORT @ {exit_px:.2f}  P&L={pnl:+.0f}", "INFO" if pnl>=0 else "WARNING")
                paper_position = 0

            # ── ENTRY — inside session only ────────────────────────────────
            if paper_position == 0 and ss_mins <= cur_mins < se_mins:
                if buy_sig:
                    entry_px       = ltp + slippage
                    paper_position = 1
                    paper_entry_px = entry_px
                    paper_entry_dt = now
                    _plog(f"BUY @ {entry_px:.2f}  SMA({ma_period})={sma_val:.2f}  ({candle_count} green)", "INFO")
                elif sell_sig:
                    entry_px       = ltp - slippage
                    paper_position = -1
                    paper_entry_px = entry_px
                    paper_entry_dt = now
                    _plog(f"SELL @ {entry_px:.2f}  SMA({ma_period})={sma_val:.2f}  ({candle_count} red)", "INFO")

        except Exception as e:
            _plog(f"Tick error: {e}", "WARNING")

        time.sleep(tick_secs)

    _plog("Paper engine stopped.", "WARNING")


@app.route("/api/paper/start", methods=["POST"])
def paper_start():
    global paper_running, paper_logs, paper_position, paper_entry_px
    global paper_entry_dt, paper_trades, paper_ltp, paper_signal, paper_config

    if paper_running:
        return jsonify({"status": "already_running"})
    data = request.json or {}
    if not data.get("symbol"):
        return jsonify({"status": "error", "message": "symbol required"}), 400
    if not data.get("instrument_token"):
        return jsonify({"status": "error", "message": "instrument_token required"}), 400

    paper_logs     = []
    paper_position = 0
    paper_entry_px = 0.0
    paper_entry_dt = None
    paper_trades   = []
    paper_ltp      = 0.0
    paper_signal   = "HOLD"
    paper_config   = data
    paper_running  = True

    threading.Thread(target=_paper_loop, daemon=True).start()
    return jsonify({
        "status":       "started",
        "ma_period":    data.get("ma_period",    7),
        "candle_count": data.get("candle_count", 2),
    })

@app.route("/api/paper/stop", methods=["POST"])
def paper_stop():
    global paper_running
    paper_running = False
    return jsonify({"status": "stopped"})

@app.route("/api/paper/exit", methods=["POST"])
def paper_manual_exit():
    global paper_position, paper_entry_px, paper_ltp, paper_trades
    if paper_position == 0:
        return jsonify({"status": "ok", "message": "No open position"})
    qty = paper_config.get("lot_size", 75) * paper_config.get("lots", 1)
    pts = (paper_ltp - paper_entry_px) if paper_position == 1 else (paper_entry_px - paper_ltp)
    pnl = pts * qty - 20
    paper_trades.append({
        "direction":    "LONG" if paper_position == 1 else "SHORT",
        "entry_price":  round(paper_entry_px, 2),
        "exit_price":   round(paper_ltp, 2),
        "points":       round(pts, 2),
        "realised_pnl": round(pnl, 2),
        "result":       "WIN" if pnl > 0 else "LOSS",
        "exit_reason":  "FORCED",
        "time":         datetime.now().strftime("%H:%M:%S"),
    })
    _plog(f"MANUAL EXIT @ {paper_ltp:.2f}  P&L={pnl:+.0f}", "WARNING")
    paper_position = 0
    return jsonify({"status": "ok"})

@app.route("/api/paper/status")
def paper_status():
    total_r  = sum(t["realised_pnl"] for t in paper_trades)
    wins     = [t for t in paper_trades if t["result"] == "WIN"]
    losses   = [t for t in paper_trades if t["result"] == "LOSS"]
    win_rate = round(len(wins)/len(paper_trades)*100, 1) if paper_trades else 0.0
    open_pos = None
    if paper_position != 0 and paper_ltp:
        qty = paper_config.get("lot_size", 75) * paper_config.get("lots", 1)
        pts = (paper_ltp-paper_entry_px) if paper_position==1 else (paper_entry_px-paper_ltp)
        open_pos = {
            "direction":     "LONG" if paper_position==1 else "SHORT",
            "entry_price":   round(paper_entry_px, 2),
            "current_price": round(paper_ltp,      2),
            "points":        round(pts,             2),
            "unrealised_pnl": round(pts * paper_config.get("lot_size",75)*paper_config.get("lots",1), 2),
            "qty":           paper_config.get("lot_size",75)*paper_config.get("lots",1),
        }
    return jsonify({
        "running":        paper_running,
        "ltp":            round(paper_ltp, 2),
        "last_signal":    paper_signal,
        "net_pnl":        round(total_r, 2),
        "total_realised": round(total_r, 2),
        "unrealised":     round(open_pos["unrealised_pnl"] if open_pos else 0, 2),
        "open_position":  open_pos,
        "closed_trades":  paper_trades,
        "total_trades":   len(paper_trades),
        "winners":        len(wins),
        "losers":         len(losses),
        "win_rate":       win_rate,
        "ma_period":      paper_config.get("ma_period",    7),
        "candle_count":   paper_config.get("candle_count", 2),
    })

@app.route("/api/paper/logs")
def paper_logs_route():
    since = int(request.args.get("since", 0))
    return jsonify({"logs": paper_logs[since:], "total": len(paper_logs), "running": paper_running})


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("\n" + "="*55)
    print("  Quantly — Intraday Options Bot")
    print("  Configurable: SMA period + candle count")
    print("  Open: http://127.0.0.1:8080")
    print("="*55 + "\n")
    app.run(debug=False, port=8080, host="0.0.0.0")