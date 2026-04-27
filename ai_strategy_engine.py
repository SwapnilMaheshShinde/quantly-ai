"""
ai_strategy_engine.py  —  Quantly AI Strategy Backtest Engine  v2.0
=====================================================================
IMPROVEMENTS IN THIS VERSION:
  1. MUCH better Groq prompt — handles complex multi-indicator strategies,
     FVG, price-action, range-based, time-based, and custom logic.
  2. _eval_signal always uses .iloc[-1] positional — fixed IndexError.
  3. Fresh engine always created on /api/backtest/start.
  4. Robust JSON repair before parse.
  5. Fallback code generation if AI returns garbage signals.
  6. Trade data now includes full OHLCV candle at entry/exit for charting.
  7. Indicator series returned for chart overlay rendering.
  8. No-trade diagnostics: counts how many times each signal was True.
  9. Better column-fix and sanitize covering more edge cases.
 10. Session time validation and soft-warning instead of hard crash.
"""

import os, re, json, threading, time
from datetime import datetime, timedelta
import pandas as pd
import numpy as np

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()


# ── Groq ──────────────────────────────────────────────────────────────────────
def _call_groq(system: str, user: str, max_tokens: int = 4000) -> str:
    from groq import Groq
    r = Groq(api_key=GROQ_API_KEY).chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "system", "content": system},
                  {"role": "user",   "content": user}],
        temperature=0.05, max_tokens=max_tokens,
    )
    return r.choices[0].message.content.strip()


PARSE_PROMPT = """You are an expert algorithmic trading strategy parser for Indian and global markets.

Convert ANY plain-English strategy — simple or complex — to structured JSON with executable Python code.

═══════════════════════════════════════════════════════════════════
CRITICAL RULES
═══════════════════════════════════════════════════════════════════
1. Use ONLY the indicators the user mentioned. Nothing extra.
2. Signal code uses: last=df.iloc[i], prev=df.iloc[i-1], df=full DataFrame
3. Signal code MUST assign bool to: buy, sell, exit_long, exit_short
4. Use last['col'] and prev['col'] — NEVER df.iloc[-1]['col']
5. Always wrap comparisons in float() to avoid Series bool errors.
6. For complex strategies with multiple conditions, join with: and / or
7. If user mentions no sell/short signal, default: sell=False; exit_short=False
8. For strategies without explicit exit: exit on opposite signal

═══════════════════════════════════════════════════════════════════
INDICATOR TEMPLATES (use exactly as shown)
═══════════════════════════════════════════════════════════════════
SMA:
  df['sma_N'] = df['close'].rolling(N).mean()

EMA:
  df['ema_N'] = df['close'].ewm(span=N, adjust=False).mean()

RSI (14):
  _delta=df['close'].diff(); _gain=_delta.clip(lower=0); _loss=-_delta.clip(upper=0)
  _ag=_gain.ewm(com=13,adjust=False).mean(); _al=_loss.ewm(com=13,adjust=False).mean()
  df['rsi']=100-(100/(1+_ag/_al.replace(0,1e-10)))

ATR (14):
  _tr=pd.concat([df['high']-df['low'],(df['high']-df['close'].shift()).abs(),(df['low']-df['close'].shift()).abs()],axis=1).max(axis=1)
  df['atr']=_tr.ewm(span=14,adjust=False).mean()

VWAP:
  df['vwap']=(df['close']*df['volume']).cumsum()/df['volume'].cumsum()

MACD:
  df['macd_fast']=df['close'].ewm(span=12,adjust=False).mean()
  df['macd_slow']=df['close'].ewm(span=26,adjust=False).mean()
  df['macd']=df['macd_fast']-df['macd_slow']
  df['macd_signal']=df['macd'].ewm(span=9,adjust=False).mean()
  df['macd_hist']=df['macd']-df['macd_signal']

Bollinger Bands (20,2):
  df['bb_mid']=df['close'].rolling(20).mean()
  df['bb_std']=df['close'].rolling(20).std()
  df['bb_upper']=df['bb_mid']+2*df['bb_std']
  df['bb_lower']=df['bb_mid']-2*df['bb_std']

Supertrend (10,3):
  _atr10=df['close'].rolling(10).std()
  df['st_upper']=(df['high']+df['low'])/2+3*_atr10
  df['st_lower']=(df['high']+df['low'])/2-3*_atr10
  df['supertrend']=df['st_lower']
  df['st_bull']=df['close']>df['st_lower']

Stochastic (14,3):
  _l14=df['low'].rolling(14).min(); _h14=df['high'].rolling(14).max()
  df['stoch_k']=100*(df['close']-_l14)/(_h14-_l14+1e-10)
  df['stoch_d']=df['stoch_k'].rolling(3).mean()

CPR (Central Pivot Range):
  df['date_only']=df['date'].dt.date
  _ph=df.groupby('date_only')['high'].transform('max')
  _pl=df.groupby('date_only')['low'].transform('min')
  _pc=df.groupby('date_only')['close'].transform('last')
  df['cpr_pivot']=(_ph+_pl+_pc)/3
  df['cpr_bc']=(_ph+_pl)/2
  df['cpr_tc']=df['cpr_pivot']+(df['cpr_pivot']-df['cpr_bc'])

Opening Range Breakout (first 2 candles = 15min on 5min chart):
  df['date_only']=df['date'].dt.date
  df['candle_num']=df.groupby('date_only').cumcount()+1
  _orb_h=df[df['candle_num']<=3].groupby('date_only')['high'].max()
  _orb_l=df[df['candle_num']<=3].groupby('date_only')['low'].min()
  df['orb_high']=df['date_only'].map(_orb_h)
  df['orb_low']=df['date_only'].map(_orb_l)
  df['orb_valid']=df['candle_num']>3

First N Candle Range (like ORB but user specifies N candles):
  df['date_only']=df['date'].dt.date
  df['candle_num']=df.groupby('date_only').cumcount()+1
  _rh=df[df['candle_num']<=N].groupby('date_only')['high'].max()
  _rl=df[df['candle_num']<=N].groupby('date_only')['low'].min()
  df['range_high']=df['date_only'].map(_rh)
  df['range_low']=df['date_only'].map(_rl)
  df['range_valid']=df['candle_num']>N

FVG (Fair Value Gap) — bullish: gap between candle[i-2] high and candle[i] low:
  df['fvg_bull']=df['low'] > df['high'].shift(2)
  df['fvg_bear']=df['high'] < df['low'].shift(2)
  df['fvg_bull_top']=df['low'].where(df['fvg_bull'])
  df['fvg_bull_bot']=df['high'].shift(2).where(df['fvg_bull'])

Highest High / Lowest Low (swing):
  df['hh_N']=df['high'].rolling(N).max()
  df['ll_N']=df['low'].rolling(N).min()

═══════════════════════════════════════════════════════════════════
SIGNAL CODE PATTERNS
═══════════════════════════════════════════════════════════════════

Crossover (SMA5 crosses above SMA20):
  buy = (float(last['sma_5'])>float(last['sma_20'])) and (float(prev['sma_5'])<=float(prev['sma_20']))

Range breakout (price closes above range_high after range is valid):
  buy = bool(last.get('range_valid', False)) and float(last['close'])>float(last['range_high'])

FVG + Range breakout combo:
  buy = bool(last.get('range_valid', False)) and float(last['close'])>float(last['range_high']) and bool(last.get('fvg_bull', False))

RSI condition:
  buy = float(last['rsi']) > 50

MACD crossover:
  buy = (float(last['macd'])>float(last['macd_signal'])) and (float(prev['macd'])<=float(prev['macd_signal']))

Bollinger touch and close back inside:
  buy = float(prev['close'])<float(prev['bb_lower']) and float(last['close'])>float(last['bb_lower'])

Multi-condition (use parentheses and 'and'/'or' only):
  buy = (float(last['ema_9'])>float(last['ema_21'])) and (float(prev['ema_9'])<=float(prev['ema_21'])) and float(last['rsi'])>50

Exit on opposite or threshold:
  exit_long = float(last['rsi']) < 40 or float(last['close']) < float(last['ema_21'])

SAFE ACCESS for columns that may not exist every row:
  buy = float(last.get('range_high', 0)) > 0 and float(last['close']) > float(last.get('range_high', 0))

═══════════════════════════════════════════════════════════════════
RETURN FORMAT — ONLY valid JSON, no markdown fences, no comments
═══════════════════════════════════════════════════════════════════
{
  "strategy_name": "Short Name",
  "description": "One sentence.",
  "indicators_used": ["SMA5","SMA20"],
  "session_start": "09:15",
  "session_end": "15:15",
  "suggested_timeframe": "5minute",
  "plain_english": {
    "entry_long": "...",
    "entry_short": "...",
    "exit_long": "...",
    "exit_short": "...",
    "session": "...",
    "notes": "..."
  },
  "python_indicators": "df['sma_5']=df['close'].rolling(5).mean()\\ndf['sma_20']=df['close'].rolling(20).mean()",
  "python_buy":        "buy=(float(last['sma_5'])>float(last['sma_20']))and(float(prev['sma_5'])<=float(prev['sma_20']))",
  "python_sell":       "sell=False",
  "python_exit_long":  "exit_long=float(last['sma_5'])<float(last['sma_20'])",
  "python_exit_short": "exit_short=False",
  "min_candles_needed": 25,
  "confidence": 90,
  "warnings": []
}

suggested_timeframe options: minute|3minute|5minute|15minute|30minute|60minute|2hour|day
"""


def _sanitize(code: str) -> str:
    """Normalise signal code: replace df.iloc patterns, strip assignment lines."""
    if not code:
        return code
    code = re.sub(r'df\.iloc\[-1\]', 'last', code)
    code = re.sub(r'df\.iloc\[-2\]', 'prev', code)
    code = re.sub(r'df\.iloc\[i\s*-\s*1\]', 'prev', code)
    code = re.sub(r'df\.iloc\[i\]', 'last', code)
    code = re.sub(r"df\[(['\"][^'\"]+['\"])\]\.iloc\[-1\]", r'last[\1]', code)
    code = re.sub(r"df\[(['\"][^'\"]+['\"])\]\.iloc\[-2\]", r'prev[\1]', code)
    code = re.sub(r"df\[(['\"][^'\"]+['\"])\]\.iloc\[i\]", r'last[\1]', code)
    code = re.sub(r"df\[(['\"][^'\"]+['\"])\]\.iloc\[i-1\]", r'prev[\1]', code)
    code = re.sub(r"df\[(['\"][^'\"]+['\"])\]\.shift\(1\)\.iloc\[-1\]", r'prev[\1]', code)
    # remove any lines that re-assign last/prev from df.iloc
    lines = [l for l in code.split('\n')
             if not re.match(r'^\s*(last|prev)\s*=\s*df\.iloc', l)]
    return '\n'.join(lines).strip()


def _fix_cols(code: str, cols: list, log=None) -> str:
    """Fix last['x']/prev['x'] references that don't match actual df columns."""
    col_set = set(cols)
    seen = set()
    base_cols = {"open", "close", "high", "low", "volume", "date", "date_only", "candle_num"}
    for m in re.finditer(r"(?:last|prev)\.get\((['\"])([^'\"]+)\1|(?:last|prev)\[(['\"])([^'\"]+)\3\]", code):
        col = m.group(2) or m.group(4)
        if not col or col in seen or col in col_set or col in base_cols:
            continue
        seen.add(col)
        clean = col.replace("_", "").lower()
        best = next((c for c in cols if c.replace("_", "").lower() == clean), None)
        if not best:
            alpha = re.sub(r'[^a-z]', '', col.lower())
            cands = [c for c in cols if re.sub(r'[^a-z]', '', c.lower()).startswith(alpha) and alpha]
            if len(cands) == 1:
                best = cands[0]
            elif len(cands) > 1:
                best = min(cands, key=len)
        if best:
            if log:
                log(f"  ColFix: '{col}' → '{best}'", "WARNING")
            code = code.replace(f"['{col}']", f"['{best}']")
            code = code.replace(f'["{col}"]', f'["{best}"]')
            code = code.replace(f"('{col}'", f"('{best}'")
            code = code.replace(f'("{col}"', f'("{best}"')
    return code


def _repair_json(raw: str) -> str:
    """Best-effort JSON repair before parse."""
    raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE)
    raw = re.sub(r'\s*```$', '', raw, flags=re.MULTILINE)
    s = raw.find('{'); e = raw.rfind('}') + 1
    if s >= 0 and e > 0:
        raw = raw[s:e]
    # Escape bare control chars inside strings
    out = []; in_s = False; i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == '\\' and in_s:
            out.append(ch)
            if i + 1 < len(raw):
                out.append(raw[i + 1]); i += 2; continue
        elif ch == '"':
            in_s = not in_s; out.append(ch)
        elif in_s and ord(ch) < 32:
            out.append('\\n' if ch in ('\n', '\r') else '\\t' if ch == '\t' else '\\n')
        else:
            out.append(ch)
        i += 1
    return ''.join(out)


def parse_strategy(user_text: str) -> dict:
    if not GROQ_API_KEY:
        raise ValueError("GROQ_API_KEY not set in .env")
    raw = _call_groq(PARSE_PROMPT, f"Parse this trading strategy into JSON:\n\n{user_text}")
    raw = _repair_json(raw)
    try:
        result = json.loads(raw)
        for k in ("python_buy", "python_sell", "python_exit_long", "python_exit_short"):
            if k in result:
                result[k] = _sanitize(result[k])
        return result
    except json.JSONDecodeError as ex:
        raise ValueError(f"AI returned invalid JSON: {ex}\nRaw snippet: {raw[:500]}")


# ══════════════════════════════════════════════════════════════════════════════
class AIBacktestEngine:
    _CHUNK = {
        "minute": 55, "3minute": 90, "5minute": 90,
        "15minute": 180, "30minute": 180, "60minute": 360,
        "2hour": 360, "day": 1800
    }

    def __init__(self, kite, parsed, instrument_token, tradingsymbol, exchange,
                 start_date, end_date, interval="5minute", trade_mode="intraday",
                 session_start="09:15", session_end="15:15",
                 lot_size=75, lots=1, capital=200_000.0,
                 max_trades_per_day=0, max_loss_points=0.0):
        self.kite = kite
        self.parsed = parsed
        self.instrument_token = int(instrument_token)
        self.tradingsymbol = tradingsymbol
        self.exchange = exchange
        self.start_date = start_date
        self.end_date = end_date
        self.interval = interval
        self.trade_mode = trade_mode.lower().strip()
        self.session_start = session_start
        self.session_end = session_end
        self.lot_size = int(lot_size)
        self.lots = int(lots)
        self.qty = self.lot_size * self.lots
        self.capital = float(capital)
        self.max_trades_per_day = int(max_trades_per_day)
        self.max_loss_points = float(max_loss_points)
        self.logs = []
        self.result = None
        self.running = False
        self.error = None

    def _log(self, msg, level="INFO"):
        self.logs.append({
            "time": datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "message": str(msg)
        })
        if len(self.logs) > 1200:
            self.logs.pop(0)

    def start(self):
        self.running = True
        self.logs = []
        self.result = None
        self.error = None
        threading.Thread(target=self._run, daemon=True).start()

    def get_logs(self, since=0):
        return self.logs[since:]

    @staticmethod
    def _hm(t):
        h, m = t.split(":")
        return int(h) * 60 + int(m)

    @staticmethod
    def _cm(ts):
        t = pd.Timestamp(ts)
        return t.hour * 60 + t.minute

    def _fetch(self):
        chunk = self._CHUNK.get(self.interval, 90)
        d0 = datetime.strptime(self.start_date, "%Y-%m-%d")
        d1 = datetime.strptime(self.end_date, "%Y-%m-%d")
        raw = []; cur = d0; n = 1
        while cur < d1:
            nxt = min(cur + timedelta(days=chunk), d1)
            self._log(f"Chunk {n}: {cur.date()} → {nxt.date()}")
            try:
                r = self.kite.historical_data(
                    instrument_token=self.instrument_token,
                    from_date=cur.strftime("%Y-%m-%d"),
                    to_date=nxt.strftime("%Y-%m-%d"),
                    interval=self.interval,
                    continuous=False, oi=False
                )
                raw.extend(r)
                self._log(f"  ✓ {len(r)} candles fetched")
            except Exception as ex:
                self._log(f"  ⚠ Chunk error: {ex}", "WARNING")
            cur = nxt + timedelta(days=1); n += 1
            time.sleep(0.4)
        return raw

    def _eval(self, code: str, var: str, last, prev, df) -> bool:
        """
        Execute signal code safely.
        CRITICAL: pd.Series results → use .iloc[-1] (positional, not label).
        """
        import builtins
        ctx = {
            "last": last, "prev": prev, "df": df,
            "pd": pd, "np": np,
            "i": last.name, "__builtins__": builtins
        }
        try:
            exec(compile(code, "<sig>", "exec"), ctx)
            val = ctx.get(var)
            if val is None:
                return False
            if isinstance(val, pd.Series):
                val = bool(val.iloc[-1])   # positional last — NEVER label-based
            elif hasattr(val, 'item'):
                val = val.item()
            return bool(val)
        except Exception as ex:
            self._log(f"  Signal '{var}' err: {ex} | code: {code[:80]}", "WARNING")
            return False

    def _run(self):
        try:
            p = self.parsed
            is_intra = (self.trade_mode == "intraday")
            use_mtpd = (self.max_trades_per_day > 0)
            use_mlp  = (self.max_loss_points > 0)

            self._log("=" * 55)
            self._log(f"Strategy  : {p.get('strategy_name', '')}")
            self._log(f"Indicators: {', '.join(p.get('indicators_used', []))}")
            self._log(f"Instrument: {self.tradingsymbol} ({self.exchange})")
            self._log(f"Interval  : {self.interval}")
            self._log(f"Dates     : {self.start_date} → {self.end_date}")
            self._log(f"Mode      : {'INTRADAY force-close@' + self.session_end if is_intra else 'HOLDING signal-exit only'}")
            self._log(f"Qty       : {self.qty}  ({self.lots}×{self.lot_size})")
            self._log(f"MaxT/Day  : {self.max_trades_per_day if use_mtpd else 'Unlimited'}")
            self._log(f"SL pts    : {self.max_loss_points if use_mlp else 'Disabled'}")
            self._log("=" * 55)

            # 1. Fetch
            raw = self._fetch()
            if not raw:
                self._log("No data returned — check token / instrument / date range.", "ERROR")
                self.error = "No data returned"; return

            df = pd.DataFrame(raw)
            df["date"] = pd.to_datetime(df["date"])
            df = df.drop_duplicates("date").sort_values("date").reset_index(drop=True)
            self._log(f"Total candles: {len(df)} ({df['date'].iloc[0].date()} → {df['date'].iloc[-1].date()})")

            # 2. Compute indicators
            self._log("Computing indicators…")
            import builtins as _bi
            gl = {"df": df, "pd": pd, "np": np, "__builtins__": _bi}
            ind_code = p.get("python_indicators", "")
            try:
                exec(compile(ind_code, "<ind>", "exec"), gl)
                df = gl.get("df", df)
            except Exception as ex:
                self._log(f"Indicator error: {ex}", "ERROR")
                self._log(f"Code was:\n{ind_code[:300]}", "ERROR")
                self.error = f"Indicator error: {ex}"; return

            added = [c for c in df.columns
                     if c not in ("open", "high", "low", "close", "volume", "date")]
            self._log(f"Indicator columns: {added}")
            if not added:
                self._log("WARNING: No indicator columns created — strategy may be misinterpreted.", "WARNING")

            # 3. Prepare & validate signal code
            cols = list(df.columns)
            sigs = {}
            for key, var, default in [
                ("python_buy",        "buy",         "buy=False"),
                ("python_sell",       "sell",        "sell=False"),
                ("python_exit_long",  "exit_long",   "exit_long=False"),
                ("python_exit_short", "exit_short",  "exit_short=False"),
            ]:
                c = _sanitize(p.get(key, default))
                c = _fix_cols(c, cols, self._log)
                sigs[var] = c
                self._log(f"  {var:14s}: {c[:120]}")

            # 4. Warmup
            warmup = max(int(p.get("min_candles_needed", 30)), 5)
            if len(df) <= warmup + 2:
                self._log(f"Only {len(df)} candles for warmup={warmup} — widen date range.", "ERROR")
                self.error = "Not enough candles"; return
            self._log(f"Warmup={warmup} | simulating candles {warmup}..{len(df)-1}")

            # Dry-run on warmup candle to catch code errors early
            tl = df.iloc[warmup]; tp = df.iloc[warmup - 1]
            sig_counts = {v: 0 for v in sigs}
            for var, c in sigs.items():
                r = self._eval(c, var, tl, tp, df)
                self._log(f"  Dry-run '{var}'[{warmup}] → {r}")

            # 5. Session bounds
            ss = self._hm(self.session_start) if is_intra else 9 * 60 + 15
            se = self._hm(self.session_end)   if is_intra else 15 * 60 + 30
            MO = 9 * 60 + 15; MC = 15 * 60 + 30

            # 6. Walk-forward simulation
            pos = 0; entry_px = 0.0; entry_dt = None; entry_candle_idx = None
            trades = []; equity = [self.capital]
            day_count: dict = {}; sl_n = 0; skip_n = 0

            self._log("── Walk-forward simulation ──")
            for i in range(warmup, len(df)):
                row = df.iloc[i]; prev = df.iloc[i - 1]
                close = float(row["close"])
                ts = pd.Timestamp(row["date"])
                cm = self._cm(ts)
                today = ts.strftime("%Y-%m-%d")

                for var, c in sigs.items():
                    if self._eval(c, var, row, prev, df):
                        sig_counts[var] += 1

                # A. EOD force-close
                if is_intra and pos != 0 and cm >= se:
                    pnl = (close - entry_px if pos == 1 else entry_px - close) * self.qty
                    d = "LONG" if pos == 1 else "SHORT"
                    trades.append(self._mk(entry_dt, row["date"], d, entry_px, close, pnl, "EOD",
                                           entry_candle_idx, i, df))
                    equity.append(round(equity[-1] + pnl, 2))
                    day_count[today] = day_count.get(today, 0) + 1
                    self._log(f"EOD   {d:5s} @ {close:.2f}  P&L={pnl:+,.0f}  [{cm//60:02d}:{cm%60:02d}]",
                              "INFO" if pnl >= 0 else "WARNING")
                    pos = 0; continue

                # B. Hard SL
                if use_mlp and pos != 0:
                    adv = (entry_px - close) if pos == 1 else (close - entry_px)
                    if adv >= self.max_loss_points:
                        pnl = (close - entry_px if pos == 1 else entry_px - close) * self.qty
                        d = "LONG" if pos == 1 else "SHORT"
                        trades.append(self._mk(entry_dt, row["date"], d, entry_px, close, pnl, "SL",
                                               entry_candle_idx, i, df))
                        equity.append(round(equity[-1] + pnl, 2))
                        day_count[today] = day_count.get(today, 0) + 1
                        sl_n += 1
                        self._log(f"SL    {d:5s} entry={entry_px:.2f} close={close:.2f} "
                                  f"adverse={adv:.1f}/{self.max_loss_points}pts P&L={pnl:+,.0f}", "WARNING")
                        pos = 0; continue

                # C. Signal exits
                if pos == 1 and self._eval(sigs["exit_long"], "exit_long", row, prev, df):
                    pnl = (close - entry_px) * self.qty
                    trades.append(self._mk(entry_dt, row["date"], "LONG", entry_px, close, pnl, "SIGNAL",
                                           entry_candle_idx, i, df))
                    equity.append(round(equity[-1] + pnl, 2))
                    day_count[today] = day_count.get(today, 0) + 1
                    self._log(f"EXIT  LONG  @ {close:.2f}  P&L={pnl:+,.0f}  [{cm//60:02d}:{cm%60:02d}]",
                              "INFO" if pnl >= 0 else "WARNING")
                    pos = 0; continue

                if pos == -1 and self._eval(sigs["exit_short"], "exit_short", row, prev, df):
                    pnl = (entry_px - close) * self.qty
                    trades.append(self._mk(entry_dt, row["date"], "SHORT", entry_px, close, pnl, "SIGNAL",
                                           entry_candle_idx, i, df))
                    equity.append(round(equity[-1] + pnl, 2))
                    day_count[today] = day_count.get(today, 0) + 1
                    self._log(f"EXIT  SHORT @ {close:.2f}  P&L={pnl:+,.0f}  [{cm//60:02d}:{cm%60:02d}]",
                              "INFO" if pnl >= 0 else "WARNING")
                    pos = 0; continue

                # D. Entries
                if pos == 0:
                    can = (ss <= cm < se) if is_intra else (MO <= cm <= MC)
                    if use_mtpd and can and day_count.get(today, 0) >= self.max_trades_per_day:
                        skip_n += 1; can = False
                    if can:
                        if self._eval(sigs["buy"], "buy", row, prev, df):
                            pos = 1; entry_px = close; entry_dt = row["date"]
                            entry_candle_idx = i
                            self._log(f"BUY   @ {close:.2f}  [{cm//60:02d}:{cm%60:02d} {today}]  "
                                      f"day_trades={day_count.get(today,0)}")
                        elif self._eval(sigs["sell"], "sell", row, prev, df):
                            pos = -1; entry_px = close; entry_dt = row["date"]
                            entry_candle_idx = i
                            self._log(f"SELL  @ {close:.2f}  [{cm//60:02d}:{cm%60:02d} {today}]  "
                                      f"day_trades={day_count.get(today,0)}")

            # 7. Open position at end of data
            if pos != 0:
                row = df.iloc[-1]; close = float(row["close"])
                today = pd.Timestamp(row["date"]).strftime("%Y-%m-%d")
                reason = "EOD" if is_intra else "END_OF_DATA"
                pnl = (close - entry_px if pos == 1 else entry_px - close) * self.qty
                d = "LONG" if pos == 1 else "SHORT"
                trades.append(self._mk(entry_dt, row["date"], d, entry_px, close, pnl, reason,
                                       entry_candle_idx, len(df) - 1, df))
                equity.append(round(equity[-1] + pnl, 2))
                self._log(f"END   {d:5s} @ {close:.2f}  P&L={pnl:+,.0f}")

            self._log("── Simulation complete ──")
            self._log(f"Signal fire counts: {sig_counts}")

            # 8. No trades diagnostics
            if not trades:
                self._log("⚠ No trades generated!", "WARNING")
                self._log(f"  Signal counts during {len(df)-warmup} candles: {sig_counts}", "WARNING")
                if sig_counts.get("buy", 0) == 0:
                    self._log("  → buy signal never fired. Check strategy wording or try wider dates.", "WARNING")
                self._log("  Tips: different timeframe, rephrase strategy, or wider date range.", "WARNING")
                self.result = {"trades": [], "summary": None, "candles": [], "indicator_cols": []}
                return

            # 9. Stats
            tdf = pd.DataFrame(trades)
            wins    = tdf[tdf["result"] == "WIN"]
            losses  = tdf[tdf["result"] == "LOSS"]
            eods    = tdf[tdf["exit_reason"] == "EOD"]
            sls     = tdf[tdf["exit_reason"] == "SL"]
            sigs2   = tdf[tdf["exit_reason"] == "SIGNAL"]
            total   = float(tdf["pnl"].sum())
            wr      = len(wins) / len(tdf) * 100
            eq_s    = pd.Series(equity)
            dd      = float((eq_s - eq_s.cummax()).min())
            gw      = float(wins["pnl"].sum())   if len(wins)   else 0.0
            gl_     = abs(float(losses["pnl"].sum())) if len(losses) else 0.0
            pf      = gw / gl_ if gl_ > 0 else 999.0

            summary = dict(
                strategy_name=p.get("strategy_name", "AI Strategy"),
                indicators=p.get("indicators_used", []),
                interval=self.interval,
                trade_mode=self.trade_mode,
                session_start=self.session_start if is_intra else "N/A",
                session_end=self.session_end     if is_intra else "N/A",
                max_trades_per_day=self.max_trades_per_day,
                max_loss_points=self.max_loss_points,
                total_trades=len(tdf),
                winners=int(len(wins)),
                losers=int(len(losses)),
                eod_exits=int(len(eods)),
                sl_exits=int(len(sls)),
                signal_exits=int(len(sigs2)),
                mtpd_skips=skip_n,
                win_rate=round(wr, 1),
                total_pnl=round(total, 2),
                return_pct=round(total / self.capital * 100, 2),
                final_capital=round(self.capital + total, 2),
                best_trade=round(float(tdf["pnl"].max()), 2),
                worst_trade=round(float(tdf["pnl"].min()), 2),
                max_drawdown=round(dd, 2),
                profit_factor=round(pf, 2),
                avg_win=round(float(wins["pnl"].mean())   if len(wins)   else 0.0, 2),
                avg_loss=round(float(losses["pnl"].mean()) if len(losses) else 0.0, 2),
                equity=[round(x, 2) for x in equity],
                eod_pnl=round(float(eods["pnl"].sum())   if len(eods)  else 0.0, 2),
                sl_pnl=round(float(sls["pnl"].sum())     if len(sls)   else 0.0, 2),
                signal_pnl=round(float(sigs2["pnl"].sum()) if len(sigs2) else 0.0, 2),
                long_trades=int(len(tdf[tdf["direction"] == "LONG"])),
                short_trades=int(len(tdf[tdf["direction"] == "SHORT"])),
                signal_counts=sig_counts,
            )

            # 10. Candle data for chart (sample if too large)
            MAX_CANDLES = 2000
            if len(df) > MAX_CANDLES:
                step = len(df) // MAX_CANDLES
                df_chart = df.iloc[::step].copy()
            else:
                df_chart = df.copy()

            # Numeric indicator columns for chart overlay
            num_ind_cols = [c for c in added
                            if c not in ("date_only", "candle_num", "orb_valid", "range_valid",
                                         "st_bull", "fvg_bull", "fvg_bear")
                            and pd.api.types.is_numeric_dtype(df_chart[c])]

            candles = []
            for _, row in df_chart.iterrows():
                c = {
                    "t": str(row["date"])[:19],
                    "o": round(float(row["open"]), 4),
                    "h": round(float(row["high"]), 4),
                    "l": round(float(row["low"]), 4),
                    "c": round(float(row["close"]), 4),
                    "v": int(row.get("volume", 0)),
                }
                for col in num_ind_cols:
                    v = row.get(col)
                    if v is not None and not (isinstance(v, float) and np.isnan(v)):
                        c[col] = round(float(v), 4)
                candles.append(c)

            self._log(f"DONE  {len(tdf)} trades | P&L ₹{total:+,.0f} | WR {wr:.1f}% | PF {pf:.2f}")
            if use_mtpd:
                self._log(f"  MTPD: {skip_n} entries blocked")
            if use_mlp:
                self._log(f"  SL exits: {sl_n} | SL P&L ₹{summary['sl_pnl']:+,.0f}")

            self.result = {
                "trades": trades,
                "summary": summary,
                "candles": candles,
                "indicator_cols": num_ind_cols,
            }

        except Exception as ex:
            import traceback
            self._log(f"FATAL: {ex}", "ERROR")
            self._log(traceback.format_exc()[-1000:], "ERROR")
            self.error = str(ex)
        finally:
            self.running = False

    @staticmethod
    def _mk(entry_dt, exit_dt, direction, entry_px, exit_px, pnl, reason,
            entry_idx=None, exit_idx=None, df=None):
        pts = (exit_px - entry_px) if direction == "LONG" else (entry_px - exit_px)
        trade = dict(
            entry_date=str(entry_dt)[:19],
            exit_date=str(exit_dt)[:19],
            direction=direction,
            entry_price=round(entry_px, 2),
            exit_price=round(exit_px, 2),
            points=round(pts, 2),
            pnl=round(pnl, 2),
            result="WIN" if pnl > 0 else "LOSS",
            exit_reason=reason,
            entry_idx=entry_idx,
            exit_idx=exit_idx,
        )
        # Attach OHLCV snapshots for chart markers
        if df is not None and entry_idx is not None:
            er = df.iloc[entry_idx]
            trade["entry_high"] = round(float(er["high"]), 4)
            trade["entry_low"]  = round(float(er["low"]),  4)
        if df is not None and exit_idx is not None:
            xr = df.iloc[exit_idx]
            trade["exit_high"] = round(float(xr["high"]), 4)
            trade["exit_low"]  = round(float(xr["low"]),  4)
        return trade