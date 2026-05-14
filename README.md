<<<<<<< HEAD
# Quantly — Intraday Options Engine
## SMA7 | 3-Min | NSE/NFO | Intraday Only

---

## Strategy
- **BUY**: 2 consecutive GREEN candles, both close above SMA(7)
- **EXIT LONG**: any single candle closes below SMA(7)
- **SELL**: 2 consecutive RED candles, both close below SMA(7)  
- **EXIT SHORT**: any single candle closes above SMA(7)
- **EOD**: All positions FORCE-CLOSED at your set time, every day, no exceptions

---

## Setup (First Time)

```powershell
cd C:\Users\ssswa\Desktop\intraday_bot

# Create virtual environment
python -m venv venv
venv\Scripts\activate

# Install dependencies
pip install flask kiteconnect python-dotenv pandas numpy

# Edit .env file - add your Kite API keys
notepad .env
```

## .env file
```
KITE_API_KEY=your_api_key
KITE_API_SECRET=your_api_secret
KITE_ACCESS_TOKEN=         ← filled daily via UI
```

---

## Daily Startup

```powershell
cd C:\Users\ssswa\Desktop\intraday_bot
venv\Scripts\activate
python app.py
```

Open: **http://localhost:6000**

1. Go to **Token Setup** tab → Generate daily token each morning
2. Go to **Backtest** tab → Select instrument, dates, session times → Run
3. Go to **Paper Trade** → Test live without real money
4. Go to **Live Trade** → Real orders (confirm first!)

---

## Key Rules
- Port: **6000** (not 5000, to avoid conflict with old bot)
- Token: Generate fresh each morning before 9:15 AM
- Session default: 09:45 - 15:00 (fully customisable)
- No overnight positions — EVER
=======
# StrategyTesterBot
>>>>>>> cb65792a7fd83974fe56e63baf8352dfe97dd3ce
