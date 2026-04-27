"""
engine.py — Intraday SMA Backtest Engine
=========================================
Strategy (fully configurable):
  BUY  : N consecutive GREEN candles ALL closing above SMA(period)
  SELL : N consecutive RED   candles ALL closing below SMA(period)
  EXIT LONG  : any single candle closes BELOW SMA(period)
  EXIT SHORT : any single candle closes ABOVE SMA(period)

Where N = candle_count (user chosen: 1, 2, 3, ...)

Session rules (STRICT):
  - New entries ONLY between session_start and session_end each day
  - Any open position FORCE-CLOSED at session_end every single day
  - Zero overnight holding ever
"""

import threading
import time
from datetime import datetime, timedelta

import pandas as pd


def _ts():
    return datetime.now().strftime("%H:%M:%S")


class BacktestEngine:

    def __init__(self, kite, instrument_token, tradingsymbol, exchange,
                 start_date, end_date,
                 session_start="09:45", session_end="15:00",
                 lot_size=75, lots=1, capital=200000,
                 ma_period=7, candle_count=2):

        self.kite             = kite
        self.instrument_token = int(instrument_token)
        self.tradingsymbol    = tradingsymbol
        self.exchange         = exchange
        self.start_date       = start_date
        self.end_date         = end_date
        self.session_start    = session_start
        self.session_end      = session_end
        self.lot_size         = int(lot_size)
        self.lots             = int(lots)
        self.qty              = self.lot_size * self.lots
        self.capital          = float(capital)
        self.ma_period        = int(ma_period)
        self.candle_count     = max(1, int(candle_count))   # N candles

        self.logs    = []
        self.result  = None
        self.running = False
        self.error   = None

    def _log(self, msg, level="INFO"):
        self.logs.append({"time": _ts(), "level": level, "message": msg})
        if len(self.logs) > 500:
            self.logs.pop(0)

    def start(self):
        self.running = True
        self.logs    = []
        self.result  = None
        self.error   = None
        threading.Thread(target=self._run, daemon=True).start()

    def get_logs(self, since=0):
        return self.logs[since:]

    def get_status(self):
        return {
            "running":   self.running,
            "log_count": len(self.logs),
            "done":      self.result is not None,
            "error":     self.error,
        }

    @staticmethod
    def _hm(hhmm):
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)

    @staticmethod
    def _candle_mins(ts):
        return ts.hour * 60 + ts.minute

    def _fetch(self):
        CHUNK       = 95
        d_start     = datetime.strptime(self.start_date, "%Y-%m-%d")
        d_end       = datetime.strptime(self.end_date,   "%Y-%m-%d")
        all_raw     = []
        cur         = d_start
        chunk_num   = 1

        while cur < d_end:
            nxt = min(cur + timedelta(days=CHUNK), d_end)
            self._log(f"Chunk {chunk_num}: {cur.strftime('%Y-%m-%d')} -> {nxt.strftime('%Y-%m-%d')}")
            try:
                raw = self.kite.historical_data(
                    instrument_token = self.instrument_token,
                    from_date        = cur.strftime("%Y-%m-%d"),
                    to_date          = nxt.strftime("%Y-%m-%d"),
                    interval         = "3minute",
                    continuous       = False,
                    oi               = False,
                )
                all_raw.extend(raw)
                self._log(f"  {len(raw)} candles fetched")
            except Exception as e:
                self._log(f"  Chunk {chunk_num} error: {e}", "WARNING")
            cur       = nxt + timedelta(days=1)
            chunk_num += 1
            time.sleep(0.35)

        return all_raw

    def _check_buy(self, df, i):
        """
        Returns True if last N=candle_count candles are ALL green AND ALL above SMA.
        Works for any N >= 1.
        """
        n = self.candle_count
        if i < n:
            return False
        for k in range(n):
            row = df.iloc[i - k]
            if pd.isna(row["ma"]):
                return False
            if not bool(row["green"]):
                return False
            if not bool(row["above_ma"]):
                return False
        return True

    def _check_sell(self, df, i):
        """
        Returns True if last N=candle_count candles are ALL red AND ALL below SMA.
        """
        n = self.candle_count
        if i < n:
            return False
        for k in range(n):
            row = df.iloc[i - k]
            if pd.isna(row["ma"]):
                return False
            if not bool(row["red"]):
                return False
            if not bool(row["below_ma"]):
                return False
        return True

    def _run(self):
        try:
            p = self.ma_period
            n = self.candle_count

            self._log(f"=== BACKTEST START ===")
            self._log(f"Instrument  : {self.tradingsymbol} ({self.exchange})")
            self._log(f"Dates       : {self.start_date} to {self.end_date}")
            self._log(f"MA Period   : SMA({p})")
            self._log(f"Candles     : {n} consecutive candle(s) for entry signal")
            self._log(f"Session     : {self.session_start} - {self.session_end}")
            self._log(f"Qty         : {self.qty} ({self.lots} lot x {self.lot_size})")
            self._log(f"Capital     : Rs {self.capital:,.0f}")
            self._log(f"Strategy    : {n}-candle crossover | SMA({p})")

            # ── 1. Fetch ───────────────────────────────────────────────────
            raw = self._fetch()
            if not raw:
                self._log("No data returned. Check token and instrument.", "ERROR")
                self.error = "No data returned"
                return

            # ── 2. Build dataframe ─────────────────────────────────────────
            df = pd.DataFrame(raw)
            df["date"] = pd.to_datetime(df["date"])
            df = (df.drop_duplicates(subset=["date"])
                    .sort_values("date")
                    .reset_index(drop=True))
            self._log(f"Total candles: {len(df)}")

            # ── 3. Indicators — using user's SMA period ────────────────────
            df["ma"]       = df["close"].rolling(p).mean()
            df["green"]    = (df["close"] > df["open"]).astype(bool)
            df["red"]      = (df["close"] < df["open"]).astype(bool)
            df["above_ma"] = (df["close"] > df["ma"]).astype(bool)
            df["below_ma"] = (df["close"] < df["ma"]).astype(bool)

            self._log(f"SMA({p}) calculated. Running {n}-candle simulation...")

            # ── 4. Session constants ───────────────────────────────────────
            sess_start_mins = self._hm(self.session_start)
            sess_end_mins   = self._hm(self.session_end)

            # ── 5. Simulation ──────────────────────────────────────────────
            position = 0
            entry_px = 0.0
            entry_dt = None
            trades   = []
            equity   = [self.capital]

            # Need enough warmup for SMA + candle lookback
            warmup = p + n + 1

            for i in range(warmup, len(df)):
                row  = df.iloc[i]
                close = float(row["close"])

                if pd.isna(row["ma"]):
                    continue

                candle_ts   = pd.Timestamp(row["date"])
                candle_mins = self._candle_mins(candle_ts)

                # ── A. EOD FORCE EXIT — always first ───────────────────────
                if position != 0 and candle_mins >= sess_end_mins:
                    if position == 1:
                        pnl = (close - entry_px) * self.qty
                        trades.append(self._trade(
                            entry_dt, row["date"], "LONG",
                            entry_px, close, pnl, "EOD"))
                    else:
                        pnl = (entry_px - close) * self.qty
                        trades.append(self._trade(
                            entry_dt, row["date"], "SHORT",
                            entry_px, close, pnl, "EOD"))
                    equity.append(round(equity[-1] + trades[-1]["pnl"], 2))
                    self._log(
                        f"EOD EXIT {trades[-1]['direction']} "
                        f"@ {close:.2f}  P&L={trades[-1]['pnl']:+.0f}",
                        "WARNING"
                    )
                    position = 0
                    continue

                # ── B. EXIT — single candle crosses SMA ────────────────────
                if position == 1 and bool(row["below_ma"]):
                    pnl = (close - entry_px) * self.qty
                    trades.append(self._trade(
                        entry_dt, row["date"], "LONG",
                        entry_px, close, pnl, "SIGNAL"))
                    equity.append(round(equity[-1] + trades[-1]["pnl"], 2))
                    self._log(
                        f"EXIT LONG  @ {close:.2f}  SMA({p})={float(row['ma']):.2f}  "
                        f"P&L={trades[-1]['pnl']:+.0f}",
                        "INFO" if trades[-1]["pnl"] >= 0 else "WARNING"
                    )
                    position = 0
                    continue

                if position == -1 and bool(row["above_ma"]):
                    pnl = (entry_px - close) * self.qty
                    trades.append(self._trade(
                        entry_dt, row["date"], "SHORT",
                        entry_px, close, pnl, "SIGNAL"))
                    equity.append(round(equity[-1] + trades[-1]["pnl"], 2))
                    self._log(
                        f"EXIT SHORT @ {close:.2f}  SMA({p})={float(row['ma']):.2f}  "
                        f"P&L={trades[-1]['pnl']:+.0f}",
                        "INFO" if trades[-1]["pnl"] >= 0 else "WARNING"
                    )
                    position = 0
                    continue

                # ── C. ENTRY — only inside session ─────────────────────────
                if position == 0 and sess_start_mins <= candle_mins < sess_end_mins:

                    if self._check_buy(df, i):
                        position = 1
                        entry_px = close
                        entry_dt = row["date"]
                        self._log(
                            f"BUY  @ {close:.2f}  SMA({p})={float(row['ma']):.2f}  "
                            f"{str(candle_ts)[11:16]}  ({n} green candle{'s' if n>1 else ''})"
                        )
                        continue

                    if self._check_sell(df, i):
                        position = -1
                        entry_px = close
                        entry_dt = row["date"]
                        self._log(
                            f"SELL @ {close:.2f}  SMA({p})={float(row['ma']):.2f}  "
                            f"{str(candle_ts)[11:16]}  ({n} red candle{'s' if n>1 else ''})"
                        )
                        continue

            # Force close open position at end of data
            if position != 0:
                last  = df.iloc[-1]
                close = float(last["close"])
                if position == 1:
                    pnl = (close - entry_px) * self.qty
                    trades.append(self._trade(
                        entry_dt, last["date"], "LONG",
                        entry_px, close, pnl, "EOD"))
                else:
                    pnl = (entry_px - close) * self.qty
                    trades.append(self._trade(
                        entry_dt, last["date"], "SHORT",
                        entry_px, close, pnl, "EOD"))
                equity.append(round(equity[-1] + trades[-1]["pnl"], 2))

            # ── 6. Summary ────────────────────────────────────────────────
            if not trades:
                self._log(
                    "No trades generated. Try wider dates, "
                    "smaller SMA period, or fewer candles required.", "WARNING"
                )
                self.result = {"trades": [], "summary": None}
                return

            tdf    = pd.DataFrame(trades)
            wins   = tdf[tdf["result"] == "WIN"]
            losses = tdf[tdf["result"] == "LOSS"]
            eods   = tdf[tdf["exit_reason"] == "EOD"]
            total  = float(tdf["pnl"].sum())
            wr     = len(wins) / len(tdf) * 100
            eq_s   = pd.Series(equity)
            dd     = float((eq_s - eq_s.cummax()).min())
            gw     = float(wins["pnl"].sum())   if len(wins)   else 0.0
            gl     = abs(float(losses["pnl"].sum())) if len(losses) else 0.0
            pf     = (gw / gl) if gl > 0 else 999.0

            summary = {
                "ma_period":     p,
                "candle_count":  n,
                "total_trades":  len(tdf),
                "winners":       int(len(wins)),
                "losers":        int(len(losses)),
                "eod_exits":     int(len(eods)),
                "signal_exits":  int(len(tdf) - len(eods)),
                "win_rate":      round(wr, 1),
                "total_pnl":     round(total, 2),
                "return_pct":    round(total / self.capital * 100, 2),
                "final_capital": round(self.capital + total, 2),
                "best_trade":    round(float(tdf["pnl"].max()), 2),
                "worst_trade":   round(float(tdf["pnl"].min()), 2),
                "max_drawdown":  round(dd, 2),
                "profit_factor": round(pf, 2),
                "avg_win":       round(float(wins["pnl"].mean())   if len(wins)   else 0.0, 2),
                "avg_loss":      round(float(losses["pnl"].mean()) if len(losses) else 0.0, 2),
                "equity":        [round(x, 2) for x in equity],
                "eod_pnl":       round(float(eods["pnl"].sum()) if len(eods) else 0.0, 2),
            }

            self._log(
                f"=== DONE === {len(tdf)} trades | "
                f"P&L Rs {total:+,.0f} | WR {wr:.1f}% | "
                f"SMA({p}) | {n}-candle | EOD: {len(eods)}"
            )
            self.result = {"trades": trades, "summary": summary}

        except Exception as ex:
            import traceback
            self._log(f"FATAL: {ex}", "ERROR")
            self._log(traceback.format_exc()[-500:], "ERROR")
            self.error = str(ex)
        finally:
            self.running = False

    @staticmethod
    def _trade(entry_dt, exit_dt, direction, entry_px, exit_px, pnl, reason):
        points = round(
            (exit_px - entry_px) if direction == "LONG"
            else (entry_px - exit_px), 2
        )
        return {
            "entry_date":  str(entry_dt)[:19],
            "exit_date":   str(exit_dt)[:19],
            "direction":   direction,
            "entry_price": round(entry_px, 2),
            "exit_price":  round(exit_px,  2),
            "points":      points,
            "pnl":         round(pnl, 2),
            "result":      "WIN" if pnl > 0 else "LOSS",
            "exit_reason": reason,
        }