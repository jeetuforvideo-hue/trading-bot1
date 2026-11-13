import os
import io
import sys
import math
import time
import subprocess
import requests
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone

try:
    import matplotlib
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "matplotlib"])
    import matplotlib

matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
CHART_AVAILABLE = True

# =====================================================================
# 1. INDIAN MARKET (NSE/BSE) REAL-TIME OPTIONS SCALPING ENGINE
# =====================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID   = "5610520408"

DEFAULT_LOTS_TO_TRADE      = 1
BASE_SCALP_RR_RATIO        = 1.35      # 1:1.35 Realistic Scalping Target
LOCK_PROFIT_R              = 0.60      # 0.60R पर SL को Cost पर लॉक करें
NEAR_TARGET_ALERT_RATIO    = 0.78      # सामने की रुकावट (Wall) से पहले Easy Target 1
SL_BUFFER_ATR              = 0.20      # Right-Shoulder / EMA20 के पीछे सटीक छोटा SL बफर

MAX_CONCURRENT_TRADES      = 2
MIN_GAP_BETWEEN_TRADES_MIN = 10
DAILY_MAX_SL_COUNT         = 3

INDIAN_INDICES = {
    "NIFTY 50": {
        "mt5_name": "NIFTY 50",
        "nse_symbol": "NIFTY",
        "sub_title": "NSE Nifty 50 Index • Spot",
        "cmd_keys": ["/nifty", "nifty", "/nifty50", "nifty50"],
        "tv_ticker": "NSE:NIFTY",
        "tv_screener": "india",
        "yf_symbol": "^NSEI",
        "strike_step": 50,
        "lot_size": 75,
        "expiry_weekday": 3,
        "decimals": 2,
        "min_sl_pts": 18.0,
        "max_sl_pts": 35.0
    },
    "BANK NIFTY": {
        "mt5_name": "BANKNIFTY",
        "nse_symbol": "BANKNIFTY",
        "sub_title": "NSE Nifty Bank Index • Spot",
        "cmd_keys": ["/banknifty", "banknifty", "/bnf", "bnf"],
        "tv_ticker": "NSE:BANKNIFTY",
        "tv_screener": "india",
        "yf_symbol": "^NSEBANK",
        "strike_step": 100,
        "lot_size": 30,
        "expiry_weekday": 2,
        "decimals": 2,
        "min_sl_pts": 40.0,
        "max_sl_pts": 80.0
    },
    "SENSEX": {
        "mt5_name": "SENSEX",
        "nse_symbol": "SENSEX",
        "sub_title": "BSE Sensex 30 Index • Spot",
        "cmd_keys": ["/sensex", "sensex", "/bse", "bse"],
        "tv_ticker": "BSE:SENSEX",
        "tv_screener": "india",
        "yf_symbol": "^BSESN",
        "strike_step": 100,
        "lot_size": 20,
        "expiry_weekday": 4,
        "decimals": 2,
        "min_sl_pts": 55.0,
        "max_sl_pts": 115.0
    },
    "FINNIFTY": {
        "mt5_name": "FINNIFTY",
        "nse_symbol": "FINNIFTY",
        "sub_title": "NSE Nifty Financial Services • Spot",
        "cmd_keys": ["/finnifty", "finnifty", "/fin", "fin"],
        "tv_ticker": "NSE:FINNIFTY",
        "tv_screener": "india",
        "yf_symbol": "NIFTY_FIN_SERVICE.NS",
        "strike_step": 50,
        "lot_size": 65,
        "expiry_weekday": 1,
        "decimals": 2,
        "min_sl_pts": 20.0,
        "max_sl_pts": 42.0
    }
}

last_candle_traded     = {name: None for name in INDIAN_INDICES}
last_structure_traded  = {name: None for name in INDIAN_INDICES}
pair_cooldown_until    = {name: datetime.min.replace(tzinfo=timezone.utc) for name in INDIAN_INDICES}
sl_direction_lock      = {name: {"BUY": datetime.min.replace(tzinfo=timezone.utc), "SELL": datetime.min.replace(tzinfo=timezone.utc)} for name in INDIAN_INDICES}
last_global_trade_time = datetime.min.replace(tzinfo=timezone.utc)

# Real-Time Tick Memory (ताकि 5M कैंडल में 1 सेकंड का भी डिले न रहे)
live_bars_memory       = {name: {} for name in INDIAN_INDICES}

chart_rotation_index   = 0
trades_today           = 0
sl_count_today         = 0
last_update_id         = 0
daily_trades_log       = []
daily_realized_pnl_inr = 0.0

nse_session            = None
last_nse_cookie_time   = 0

def get_utc_now():
    return datetime.now(timezone.utc)

def get_ist_now():
    return get_utc_now() + timedelta(hours=5, minutes=30)

def to_utc_timestamp(ts):
    if ts is None:
        return get_utc_now()
    if getattr(ts, "tzinfo", None) is not None:
        return ts.tz_convert(timezone.utc)
    return ts.replace(tzinfo=timezone.utc)

def to_ist_timestamp(ts):
    return to_utc_timestamp(ts) + timedelta(hours=5, minutes=30)

current_day = get_ist_now().date()

def is_indian_market_open():
    now = get_ist_now()
    if now.weekday() >= 5:
        return False, "Weekend Closed (Sat-Sun)"
    hr_min = now.hour + (now.minute / 60.0)
    if hr_min < 9.25:
        return False, "Pre-Market (Opens at 09:15 AM IST)"
    if hr_min > 15.50:
        return False, "Market Closed (03:30 PM IST)"
    return True, "LIVE NSE/BSE SESSION"

def is_scalping_entry_window_ok():
    """
    FIX: सुबह 09:15 से 09:25 AM तक की पहली 2 कैंडल्स में सिर्फ Opening Range बनने दें।
    09:15 वाली कैंडल के क्लोज (09:20-09:21) पर कभी लेट एंट्री न लें क्योंकि उसका मूव
    9:15 पर ही खत्म हो चुका होता है! असली इंट्राडे ट्रेड 09:25 AM के बाद ही शुरू होगा।
    """
    now = get_ist_now()
    if now.weekday() >= 5:
        return False, "Weekend"
    mins_from_midnight = now.hour * 60 + now.minute
    if mins_from_midnight < (9 * 60 + 25):
        return False, "9:15-9:25 Opening Range Formation Filter (Avoiding 9:15 Spike Chase)"
    if mins_from_midnight > (15 * 60 + 18):
        return False, "Late Closing Square-Off Window"
    return True, "Active Scalping Window"

def send_telegram_message(message, target_chat_id=CHAT_ID):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": target_chat_id, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram Send Error: {e}")

def format_price(price, decimals=2):
    return f"{price:.{decimals}f}"

# =====================================================================
# 2. REAL-TIME TICK RECORDER & LIVE NSE OPTION CHAIN ENGINE
# =====================================================================
def fetch_tradingview_india_live_spot(tv_ticker):
    url = "https://scanner.tradingview.com/india/scan"
    payload = {
        "symbols": {"tickers": [tv_ticker], "query": {"types": []}},
        "columns": ["close", "high", "low", "open", "RSI"]
    }
    headers = {"User-Agent": "Mozilla/5.0", "Cache-Control": "no-cache"}
    try:
        res = requests.post(url, json=payload, headers=headers, timeout=5)
        if res.status_code == 200:
            data = res.json().get("data", [])
            if data and data[0].get("d"):
                spot_close = float(data[0]["d"][0])
                if spot_close > 0:
                    return spot_close
    except Exception:
        pass
    return None

def update_realtime_tick_memory():
    """
    हर कुछ सेकंड में सभी चारों इंडेक्स का लाइव टिक प्राइस लेकर खुद रियल-टाइम 5-मिनट कैंडल
    का Open, High, Low, Close रिकॉर्ड करता है ताकि Yahoo Finance के डिले से 100% छुटकारा मिले!
    """
    mkt_open, _ = is_indian_market_open()
    if not mkt_open:
        return

    now_utc = get_utc_now()
    bucket_min = (now_utc.minute // 5) * 5
    bucket_ts  = now_utc.replace(minute=bucket_min, second=0, microsecond=0)

    for name, info in INDIAN_INDICES.items():
        spot = fetch_tradingview_india_live_spot(info["tv_ticker"])
        if spot is not None and spot > 0:
            mem = live_bars_memory[name]
            if bucket_ts not in mem:
                mem[bucket_ts] = {"Open": spot, "High": spot, "Low": spot, "Close": spot, "Volume": 0.0}
            else:
                mem[bucket_ts]["Close"] = spot
                if spot > mem[bucket_ts]["High"]:
                    mem[bucket_ts]["High"] = spot
                if spot < mem[bucket_ts]["Low"]:
                    mem[bucket_ts]["Low"] = spot

def fetch_live_india_vix():
    vix = fetch_tradingview_india_live_spot("NSE:INDIAVIX")
    if vix is not None and 8.0 <= vix <= 45.0:
        return vix
    return 14.2

def fetch_nse_live_option_data(nse_symbol, strike_price, opt_type):
    """
    NSE की ऑप्शन चेन से उस Strike का लाइव LTP और आज का High/Low लाता है।
    """
    global nse_session, last_nse_cookie_time
    if nse_symbol not in ["NIFTY", "BANKNIFTY", "FINNIFTY"]:
        return None
    try:
        now_ts = time.time()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "application/json",
            "Referer": "https://www.nseindia.com/option-chain"
        }
        if nse_session is None or (now_ts - last_nse_cookie_time) > 240:
            nse_session = requests.Session()
            nse_session.get("https://www.nseindia.com/option-chain", headers=headers, timeout=4)
            last_nse_cookie_time = now_ts

        url = f"https://www.nseindia.com/api/option-chain-indices?symbol={nse_symbol}"
        res = nse_session.get(url, headers=headers, timeout=4)
        if res.status_code == 200:
            records = res.json().get("records", {}).get("data", [])
            for row in records:
                if int(row.get("strikePrice", 0)) == int(strike_price):
                    leg = row.get(opt_type, {})
                    ltp = float(leg.get("lastPrice", 0.0))
                    if ltp > 1.0:
                        return ltp
    except Exception:
        pass
    return None

def calculate_option_strike_and_premium_levels(index_name, info, direction, df_5m, spot_price, index_sl, index_tp1, index_tp2, index_lock):
    """
    1. सही ATM / Slight-ITM CE या PE स्ट्राइक चुनता है।
    2. पिछले 2 कैंडल्स के High/Low से चेक करता है कि कहीं इस ऑप्शन ने अभी-अभी
       अपना टारगेट पहले ही हिट तो नहीं कर दिया है (जैसे 9:15 पर 263 टच करके 225 पर आना)!
    """
    step = info["strike_step"]
    nearest_atm = int(round(spot_price / step) * step)

    if direction == "BUY":
        opt_type = "CE"
        strike_price  = nearest_atm if spot_price >= nearest_atm else (nearest_atm - step)
        intrinsic_val = max(0.0, spot_price - strike_price)
        # पिछले 2 कैंडल्स में इंडेक्स कितना ऊपर जाकर वापस गिरा है?
        recent_peak_spot = float(df_5m.iloc[-3:]["High"].max())
        already_moved_pts = max(0.0, recent_peak_spot - spot_price)
    else:
        opt_type = "PE"
        strike_price  = nearest_atm if spot_price <= nearest_atm else (nearest_atm + step)
        intrinsic_val = max(0.0, strike_price - spot_price)
        # पिछले 2 कैंडल्स में इंडेक्स कितना नीचे जाकर वापस ऊपर उठा है? (जैसे 9:15 का Low!)
        recent_low_spot = float(df_5m.iloc[-3:]["Low"].min())
        already_moved_pts = max(0.0, spot_price - recent_low_spot)

    strike_label = f"{info['mt5_name']} {strike_price} {opt_type}"

    live_opt_ltp = fetch_nse_live_option_data(info["nse_symbol"], strike_price, opt_type)
    prem_source  = "NSE Live Option Chain"

    if live_opt_ltp is None:
        prem_source = "Live VIX + Delta Pricing"
        vix = fetch_live_india_vix() / 100.0
        now_ist = get_ist_now()
        exp_wd  = info.get("expiry_weekday", 3)
        days_to_exp = (exp_wd - now_ist.weekday()) % 7
        T_years  = max(0.65, float(days_to_exp) + 0.5) / 365.0
        time_val = 0.40 * spot_price * vix * math.sqrt(T_years)
        live_opt_ltp = round(max(15.0, intrinsic_val + time_val), 1)

    idx_sl_pts   = abs(spot_price - index_sl)
    idx_lock_pts = abs(index_lock - spot_price)
    idx_tp1_pts  = abs(index_tp1 - spot_price)
    idx_tp2_pts  = abs(index_tp2 - spot_price)

    opt_sl_pts   = round(max(6.0, idx_sl_pts * 0.50), 1)
    opt_lock_pts = round(max(5.0, idx_lock_pts * 0.54), 1)
    opt_tp1_pts  = round(max(8.0, idx_tp1_pts * 0.56), 1)
    opt_tp2_pts  = round(max(12.0, idx_tp2_pts * 0.58), 1)

    opt_entry = round(live_opt_ltp, 1)
    opt_sl    = round(max(2.0, opt_entry - opt_sl_pts), 1)
    opt_lock  = round(opt_entry + opt_lock_pts, 1)
    opt_tp1   = round(opt_entry + opt_tp1_pts, 1)
    opt_tp2   = round(opt_entry + opt_tp2_pts, 1)

    # पिछले 2 कैंडल्स में इस ऑप्शन ने क्या High छुआ था? (जैसे 225 से पहले 263 छुआ था!)
    estimated_recent_opt_high = round(opt_entry + (already_moved_pts * 0.58), 1)
    # अगर पिछले 2 कैंडल्स का ऑप्शन हाई पहले ही Target 1 के 60% से ऊपर जा चुका है -> मतलब टारगेट पहले ही हिट हो गया है!
    target_already_touched = (already_moved_pts >= (idx_tp1_pts * 0.55))

    qty       = info["lot_size"] * DEFAULT_LOTS_TO_TRADE
    risk_inr  = round(opt_sl_pts * qty, 0)
    prof1_inr = round(opt_tp1_pts * qty, 0)
    prof2_inr = round(opt_tp2_pts * qty, 0)

    return {
        "strike_price": strike_price,
        "opt_type": opt_type,
        "strike_label": strike_label,
        "prem_source": prem_source,
        "opt_entry": opt_entry,
        "opt_sl": opt_sl,
        "opt_lock": opt_lock,
        "opt_tp1": opt_tp1,
        "opt_tp2": opt_tp2,
        "opt_sl_pts": opt_sl_pts,
        "opt_tp1_pts": opt_tp1_pts,
        "opt_tp2_pts": opt_tp2_pts,
        "recent_opt_high": estimated_recent_opt_high,
        "target_already_touched": target_already_touched,
        "qty": qty,
        "lots": DEFAULT_LOTS_TO_TRADE,
        "risk_inr": risk_inr,
        "prof1_inr": prof1_inr,
        "prof2_inr": prof2_inr
    }

# =====================================================================
# 3. ZERO-SHIFT AUTHENTIC CANDLE ENGINE (MERGED WITH REAL-TIME TICKS)
# =====================================================================
def clean_index_candles(df):
    """
    FIX: पुरानी कैंडल्स के High/Low को कभी काटता (Clip) या शिफ्ट नहीं करता ताकि
    9:15 जैसी बड़ी कैंडल का असली High और Low 100% सटीक रहे!
    """
    if df is None or df.empty:
        return df
    df = df.copy()
    df = df[~df.index.duplicated(keep="last")]
    df.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)

    active_mask = (df["High"] > df["Low"]) | (df["Open"] != df["Close"])
    if active_mask.sum() >= 40:
        df = df[active_mask]

    return df

def get_clean_spot_data(index_name, info):
    """
    1. Yahoo Finance से बेस कैंडल्स लेता है (बिना किसी गलत basis_diff शिफ्टिंग के!)
    2. उस पर हमारे Real-Time TradingView Tick Memory के ताज़ा कैंडल्स जोड़ता है
       ताकि अगर Yahoo 5-10 मिनट पीछे भी हो, तब भी बॉट के पास 100% लाइव कैंडल हो!
    """
    try:
        tkr = yf.Ticker(info["yf_symbol"])
        df_5m  = tkr.history(period="5d", interval="5m", auto_adjust=False)
        df_15m = tkr.history(period="5d", interval="15m", auto_adjust=False)
    except Exception:
        return None, None, None, ""

    df_5m  = clean_index_candles(df_5m)
    df_15m = clean_index_candles(df_15m)

    if df_5m is None or df_5m.empty or len(df_5m) < 40 or df_15m is None or df_15m.empty or len(df_15m) < 25:
        return None, None, None, ""

    tv_spot = fetch_tradingview_india_live_spot(info["tv_ticker"])
    update_realtime_tick_memory()

    # Merge real-time recorded 5m bars into df_5m so there is ZERO Yahoo delay
    mem = live_bars_memory.get(index_name, {})
    for b_ts, b_vals in sorted(mem.items()):
        if b_ts in df_5m.index:
            df_5m.loc[b_ts, "Close"] = b_vals["Close"]
            df_5m.loc[b_ts, "High"]  = max(float(df_5m.loc[b_ts, "High"]), b_vals["High"])
            df_5m.loc[b_ts, "Low"]   = min(float(df_5m.loc[b_ts, "Low"]), b_vals["Low"])
        elif b_ts > df_5m.index[-1]:
            new_row = pd.DataFrame([b_vals], index=[b_ts])
            df_5m   = pd.concat([df_5m, new_row])

    if tv_spot is not None and tv_spot > 0:
        last_idx = df_5m.index[-1]
        df_5m.loc[last_idx, "Close"] = tv_spot
        if tv_spot > float(df_5m.loc[last_idx, "High"]):
            df_5m.loc[last_idx, "High"] = tv_spot
        if tv_spot < float(df_5m.loc[last_idx, "Low"]):
            df_5m.loc[last_idx, "Low"] = tv_spot
        live_spot_price = tv_spot
    else:
        live_spot_price = float(df_5m.iloc[-1]["Close"])

    return df_15m, df_5m, live_spot_price, "NSE/BSE Zero-Delay Live Tick"

# =====================================================================
# 4. RIGHT-SHOULDER TIGHT SL & SWING-WALL FRONT-RUN TARGET ENGINE
# =====================================================================
def calculate_smart_sl_and_easy_tp(df_5m, df_15m, info, direction, live_spot, raw_structure_sl, atr_5m, allow_wall_block=True):
    dec = info["decimals"]

    raw_sl_dist = abs(live_spot - raw_structure_sl)
    min_sl      = max(atr_5m * 0.80, info.get("min_sl_pts", atr_5m * 0.80))
    max_sl      = max(atr_5m * 1.70, info.get("max_sl_pts", atr_5m * 1.70))
    sl_dist     = round(max(min_sl, min(raw_sl_dist, max_sl)), dec)

    recent_m5       = df_5m.iloc[-36:-2]
    major_high_wall = float(recent_m5["High"].max())
    major_low_wall  = float(recent_m5["Low"].min())

    default_tp2_dist = round(sl_dist * BASE_SCALP_RR_RATIO, dec)
    default_tp1_dist = round(default_tp2_dist * NEAR_TARGET_ALERT_RATIO, dec)

    if direction == "BUY":
        room_to_wall = major_high_wall - live_spot

        if allow_wall_block and (atr_5m * 0.15) < room_to_wall < (sl_dist * 0.85):
            return None, 0, None, None, None, 0, 0

        if (sl_dist * 0.85) <= room_to_wall <= (sl_dist * 1.55):
            tp1_dist = round(room_to_wall * 0.86, dec)
            tp2_dist = round(max(room_to_wall * 1.05, sl_dist * 1.25), dec)
        else:
            tp1_dist = default_tp1_dist
            tp2_dist = default_tp2_dist

        actual_rr    = round(tp2_dist / max(sl_dist, 1e-9), 2)
        sl_level     = round(live_spot - sl_dist, dec)
        lock_level   = round(live_spot + (sl_dist * LOCK_PROFIT_R), dec)
        near_tp_lvl  = round(live_spot + tp1_dist, dec)
        target_level = round(live_spot + tp2_dist, dec)

    else:
        room_to_wall = live_spot - major_low_wall

        if allow_wall_block and (atr_5m * 0.15) < room_to_wall < (sl_dist * 0.85):
            return None, 0, None, None, None, 0, 0

        if (sl_dist * 0.85) <= room_to_wall <= (sl_dist * 1.55):
            tp1_dist = round(room_to_wall * 0.86, dec)
            tp2_dist = round(max(room_to_wall * 1.05, sl_dist * 1.25), dec)
        else:
            tp1_dist = default_tp1_dist
            tp2_dist = default_tp2_dist

        actual_rr    = round(tp2_dist / max(sl_dist, 1e-9), 2)
        sl_level     = round(live_spot + sl_dist, dec)
        lock_level   = round(live_spot - (sl_dist * LOCK_PROFIT_R), dec)
        near_tp_lvl  = round(live_spot - tp1_dist, dec)
        target_level = round(live_spot - tp2_dist, dec)

    return sl_level, sl_dist, lock_level, near_tp_lvl, target_level, tp2_dist, actual_rr

# =====================================================================
# 5. TECHNICAL INDICATORS & ANTI-EXHAUSTION SCALPING SETUPS
# =====================================================================
def calculate_chart_indicators(df):
    df = df.copy()
    df["EMA9"]  = df["Close"].ewm(span=9, adjust=False).mean()
    df["EMA20"] = df["Close"].ewm(span=20, adjust=False).mean()
    df["EMA50"] = df["Close"].ewm(span=50, adjust=False).mean()

    hl = df["High"] - df["Low"]
    hc = np.abs(df["High"] - df["Close"].shift())
    lc = np.abs(df["Low"] - df["Close"].shift())
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    df["ATR"] = tr.rolling(14).mean()

    df["Body"]       = np.abs(df["Close"] - df["Open"])
    df["Range"]      = (df["High"] - df["Low"]).replace(0, 1e-9)
    df["Body_Ratio"] = df["Body"] / df["Range"]
    df["Upper_Wick"] = df["High"] - df[["Open", "Close"]].max(axis=1)
    df["Lower_Wick"] = df[["Open", "Close"]].min(axis=1) - df["Low"]
    df["Close_Pos"]  = (df["Close"] - df["Low"]) / df["Range"]

    avg_body = df["Body"].rolling(16).mean().replace(0, 1e-9)
    df["Disp_Power"] = df["Body"] / avg_body

    delta = df["Close"].diff()
    gain = (delta.where(delta > 0, 0)).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    df["RSI"] = (100 - (100 / (1 + rs))).fillna(50.0)

    return df

def get_today_intraday_candles(df_5m):
    """सिर्फ आज की तारीख (Same-Day IST) के कैंडल्स छांटता है ताकि कल के 3:25 PM बॉक्स से आज 9:15 की तुलना न हो!"""
    today_date = get_ist_now().date()
    ist_dates  = [(to_utc_timestamp(ts) + timedelta(hours=5, minutes=30)).date() for ts in df_5m.index]
    mask       = [d == today_date for d in ist_dates]
    return df_5m[mask]

def is_signal_already_exhausted_or_target_hit(df_5m, direction, live_spot, atr_5m):
    """
    FIX FOR 9:15 / 9:21 BUG:
    यह फंक्शन 3 चीज़ें चेक करता है ताकि जब टारगेट पहले ही हिट हो चुका हो (जैसे 263 टच होकर 225 पर आ गया),
    तब बॉट गलती से भी लेट ट्रेड न दे:
    1. अगर सिग्नल वाली कैंडल (c0) खुद ही 1.85x ATR से बड़ी बन चुकी है -> मतलब सारा मूव उसी कैंडल में खत्म हो गया!
    2. अगर SELL (PE) सिग्नल में कैंडल ने नीचे बहुत बड़ा Low मारा था और वहां से 0.45x ATR ऊपर रिकवर हो चुकी है
       (यानी PE ने 263 हाई मार दिया और अब गिरकर 225 पर आ गया है) -> तो तुरंत BLOCK करो!
    3. अगर BUY (CE) सिग्नल में कैंडल ने ऊपर बहुत बड़ा High मारा था और वहां से 0.45x ATR नीचे गिर चुकी है -> तुरंत BLOCK करो!
    """
    c0     = df_5m.iloc[-2]
    c_last = df_5m.iloc[-1]

    rng_0 = float(c0["Range"])
    # 1. Giant Exhausted Candle Check
    if rng_0 >= (atr_5m * 1.85):
        return True, "Signal candle itself is too large (Move already exhausted)"

    # 2. Target Already Hit by Wick & Reversing Check
    recent_2_low  = min(float(c0["Low"]), float(c_last["Low"]))
    recent_2_high = max(float(c0["High"]), float(c_last["High"]))

    if direction == "SELL":
        # अगर इंडेक्स नीचे जाकर पहले ही 0.45x ATR ऊपर बाउंस कर चुका है (मतलब PE अपना High मारकर गिर रहा है!)
        bounce_from_low = live_spot - recent_2_low
        if bounce_from_low >= (atr_5m * 0.45):
            return True, "PE already spiked at candle low and index is bouncing back"

    if direction == "BUY":
        # अगर इंडेक्स ऊपर जाकर पहले ही 0.45x ATR नीचे गिर चुका है (मतलब CE अपना High मारकर गिर रहा है!)
        drop_from_high = recent_2_high - live_spot
        if drop_from_high >= (atr_5m * 0.45):
            return True, "CE already spiked at candle high and index is dropping back"

    return False, "Fresh Move"

def detect_fibonacci_swing_structure(df_5m, lookback=32):
    window = df_5m.iloc[-(lookback + 2):-2]
    if len(window) < 16:
        return None

    highs = window["High"].values
    lows  = window["Low"].values
    times = window.index

    max_idx = int(np.argmax(highs))
    min_idx = int(np.argmin(lows))

    swing_high = float(highs[max_idx])
    swing_low  = float(lows[min_idx])
    swing_high_time = times[max_idx]
    swing_low_time  = times[min_idx]

    swing_range = swing_high - swing_low
    atr_5m = float(df_5m.iloc[-2]["ATR"])
    if np.isnan(atr_5m) or atr_5m <= 0:
        return None

    if swing_range < (atr_5m * 2.1):
        return None

    if min_idx < max_idx and (len(window) - max_idx) >= 2:
        return {
            "type": "BULLISH_SWING",
            "swing_id": f"BULL_FIB_{swing_low_time}_{swing_high_time}",
            "swing_high": swing_high,
            "swing_low": swing_low,
            "fib_0": swing_high,
            "fib_382": swing_high - (swing_range * 0.382),
            "fib_500": swing_high - (swing_range * 0.500),
            "fib_618": swing_high - (swing_range * 0.618),
            "fib_786": swing_high - (swing_range * 0.786),
            "fib_100": swing_low
        }

    if max_idx < min_idx and (len(window) - min_idx) >= 2:
        return {
            "type": "BEARISH_SWING",
            "swing_id": f"BEAR_FIB_{swing_high_time}_{swing_low_time}",
            "swing_high": swing_high,
            "swing_low": swing_low,
            "fib_0": swing_low,
            "fib_382": swing_low + (swing_range * 0.382),
            "fib_500": swing_low + (swing_range * 0.500),
            "fib_618": swing_low + (swing_range * 0.618),
            "fib_786": swing_low + (swing_range * 0.786),
            "fib_100": swing_high
        }

    return None

def detect_reversal_neckline_breakout(df_5m):
    if len(df_5m) < 40:
        return None

    c0 = df_5m.iloc[-2]
    price      = float(c0["Close"])
    open_0     = float(c0["Open"])
    high_0     = float(c0["High"])
    low_0      = float(c0["Low"])
    atr_5m     = float(c0["ATR"])
    ema9       = float(c0["EMA9"])
    ema20      = float(c0["EMA20"])
    rsi        = float(c0["RSI"])
    body_ratio = float(c0["Body_Ratio"])
    close_pos  = float(c0["Close_Pos"])
    disp_power = float(c0["Disp_Power"])

    if np.isnan(atr_5m) or atr_5m <= 0:
        return None

    win   = df_5m.iloc[-26:-2]
    highs = win["High"].values
    lows  = win["Low"].values
    times = win.index
    n_len = len(win)

    min_idx   = int(np.argmin(lows))
    max_idx   = int(np.argmax(highs))
    head_low  = float(lows[min_idx])
    head_high = float(highs[max_idx])
    total_rng = head_high - head_low

    if total_rng < (atr_5m * 2.3):
        return None

    if 5 <= (n_len - min_idx) <= 20:
        post_head_highs = highs[min_idx + 1:]
        rs_window_lows  = df_5m.iloc[-7:-2]["Low"].values

        if len(post_head_highs) >= 3 and len(rs_window_lows) >= 3:
            neckline_res       = float(np.max(post_head_highs[:-1])) if len(post_head_highs) > 1 else float(np.max(post_head_highs))
            right_shoulder_low = float(np.min(rs_window_lows))

            valid_structure = (right_shoulder_low > head_low) and ((neckline_res - head_low) >= atr_5m * 1.1) and ((neckline_res - head_low) <= atr_5m * 3.5)
            broke_neckline  = (price > neckline_res) and (float(df_5m.iloc[-3]["Close"]) <= neckline_res * 1.0003)
            strong_ignition = (price > open_0) and (price > ema9) and (price > ema20) and (body_ratio >= 0.52) and (close_pos >= 0.68) and (0.95 <= disp_power <= 1.80)

            if valid_structure and broke_neckline and strong_ignition and (44.0 <= rsi <= 72.0):
                rs_anchor = min(right_shoulder_low, ema20, low_0)
                sl_level  = rs_anchor - (atr_5m * SL_BUFFER_ATR)
                return {
                    "direction": "BUY",
                    "action": "BUY CALL (CE) 🟢",
                    "struct_id": f"IHNS_BUY_{times[min_idx]}",
                    "short_tag": "Inverse H&S Breakout",
                    "setup_text": f"Inverse H&S Neckline Breakout ({neckline_res:.1f}) | RSI: {rsi:.0f}",
                    "sl_raw": sl_level
                }

    if 5 <= (n_len - max_idx) <= 20:
        post_head_lows  = lows[max_idx + 1:]
        rs_window_highs = df_5m.iloc[-7:-2]["High"].values

        if len(post_head_lows) >= 3 and len(rs_window_highs) >= 3:
            neckline_sup        = float(np.min(post_head_lows[:-1])) if len(post_head_lows) > 1 else float(np.min(post_head_lows))
            right_shoulder_high = float(np.max(rs_window_highs))

            valid_structure = (right_shoulder_high < head_high) and ((head_high - neckline_sup) >= atr_5m * 1.1) and ((head_high - neckline_sup) <= atr_5m * 3.5)
            broke_neckline  = (price < neckline_sup) and (float(df_5m.iloc[-3]["Close"]) >= neckline_sup * 0.9997)
            strong_collapse = (price < open_0) and (price < ema9) and (price < ema20) and (body_ratio >= 0.52) and (close_pos <= 0.32) and (0.95 <= disp_power <= 1.80)

            if valid_structure and broke_neckline and strong_collapse and (28.0 <= rsi <= 56.0):
                rs_anchor = max(right_shoulder_high, ema20, high_0)
                sl_level  = rs_anchor + (atr_5m * SL_BUFFER_ATR)
                return {
                    "direction": "SELL",
                    "action": "BUY PUT (PE) 🔴",
                    "struct_id": f"HNS_SELL_{times[max_idx]}",
                    "short_tag": "H&S Neckline Breakdown",
                    "setup_text": f"H&S Neckline Breakdown ({neckline_sup:.1f}) | RSI: {rsi:.0f}",
                    "sl_raw": sl_level
                }

    return None

def analyze_real_price_action(df_5m, info, live_spot):
    dec    = info["decimals"]
    c0     = df_5m.iloc[-2]
    atr_5m = float(c0["ATR"])
    ema9   = float(c0["EMA9"])
    ema20  = float(c0["EMA20"])
    rsi    = float(c0["RSI"])

    win_20   = df_5m.iloc[-22:-1]
    res_wall = float(win_20["High"].max())
    sup_wall = float(win_20["Low"].min())
    rs_low   = float(df_5m.iloc[-6:-1]["Low"].min())
    rs_high  = float(df_5m.iloc[-6:-1]["High"].max())

    if ema9 >= ema20:
        direction  = "BUY"
        short_tag  = f"Support {format_price(rs_low, dec)} | Res {format_price(res_wall, dec)}"
        setup_text = f"Intraday Support {format_price(rs_low, dec)}  •  Resistance Wall {format_price(res_wall, dec)} | RSI: {rsi:.0f}"
        custom_sl  = min(rs_low, ema20) - (atr_5m * SL_BUFFER_ATR)
    else:
        direction  = "SELL"
        short_tag  = f"Res {format_price(rs_high, dec)} | Sup {format_price(sup_wall, dec)}"
        setup_text = f"Intraday Resistance {format_price(rs_high, dec)}  •  Support Wall {format_price(sup_wall, dec)} | RSI: {rsi:.0f}"
        custom_sl  = max(rs_high, ema20) + (atr_5m * SL_BUFFER_ATR)

    return direction, setup_text, short_tag, custom_sl

def evaluate_all_market_setups(index_name, info, df_15m, df_5m, live_spot, ignore_market_hours=False):
    now_utc = get_utc_now()
    if not ignore_market_hours:
        if now_utc < pair_cooldown_until.get(index_name, datetime.min.replace(tzinfo=timezone.utc)):
            return None, None, None, None, None, None
        window_ok, _ = is_scalping_entry_window_ok()
        if not window_ok:
            return None, None, None, None, None, None

    c0     = df_5m.iloc[-2]
    c1     = df_5m.iloc[-3]
    c2     = df_5m.iloc[-4]
    c0_ist = to_ist_timestamp(df_5m.index[-2])

    # NEVER trigger a late signal on the 09:15 or 09:20 opening volatility candles!
    if not ignore_market_hours and c0_ist.hour == 9 and c0_ist.minute in [15, 20]:
        return None, None, None, None, None, None

    price      = float(c0["Close"])
    open_0     = float(c0["Open"])
    high_0     = float(c0["High"])
    low_0      = float(c0["Low"])
    atr_5m     = float(c0["ATR"])
    ema9       = float(c0["EMA9"])
    ema20      = float(c0["EMA20"])
    ema50      = float(c0["EMA50"])
    rsi        = float(c0["RSI"])
    close_pos  = float(c0["Close_Pos"])
    body_ratio = float(c0["Body_Ratio"])
    disp_power = float(c0["Disp_Power"])
    l_wick     = float(c0["Lower_Wick"])
    u_wick     = float(c0["Upper_Wick"])

    if np.isnan(atr_5m) or atr_5m <= 0:
        return None, None, None, None, None, None

    today_df   = get_today_intraday_candles(df_5m)
    today_bars = len(today_df)

    fib = detect_fibonacci_swing_structure(df_5m, lookback=32)

    # -----------------------------------------------------------------
    # SETUP 1: OPENING RANGE BREAKOUT (ORB - After 09:25 AM on Today's Range)
    # -----------------------------------------------------------------
    if 3 <= today_bars <= 12:
        orb_bars = today_df.iloc[:2]  # 09:15 और 09:20 की पहली 2 कैंडल्स की रेंज
        orb_high = float(orb_bars["High"].max())
        orb_low  = float(orb_bars["Low"].min())
        orb_rng  = orb_high - orb_low
        orb_id   = f"ORB_{get_ist_now().date()}_{orb_low}_{orb_high}"

        if (atr_5m * 0.8) <= orb_rng <= (atr_5m * 2.8) and last_structure_traded.get(index_name) != orb_id:
            # Bullish ORB Breakout
            if price > orb_high and float(c1["Close"]) <= orb_high and price > open_0 and close_pos >= 0.68 and body_ratio >= 0.52:
                exhausted, _ = is_signal_already_exhausted_or_target_hit(df_5m, "BUY", live_spot, atr_5m)
                if not exhausted and now_utc >= sl_direction_lock[index_name]["BUY"]:
                    sl_raw = max(orb_low, min(low_0, ema20)) - (atr_5m * SL_BUFFER_ATR)
                    setup_text = f"Morning Opening Range Breakout Above {orb_high:.1f} | RSI: {rsi:.0f}"
                    return "BUY CALL (CE) 🟢", "BUY", setup_text, "Opening Range Breakout", sl_raw, {"swing_id": orb_id}

            # Bearish ORB Breakdown
            if price < orb_low and float(c1["Close"]) >= orb_low and price < open_0 and close_pos <= 0.32 and body_ratio >= 0.52:
                exhausted, _ = is_signal_already_exhausted_or_target_hit(df_5m, "SELL", live_spot, atr_5m)
                if not exhausted and now_utc >= sl_direction_lock[index_name]["SELL"]:
                    sl_raw = min(orb_high, max(high_0, ema20)) + (atr_5m * SL_BUFFER_ATR)
                    setup_text = f"Morning Opening Range Breakdown Below {orb_low:.1f} | RSI: {rsi:.0f}"
                    return "BUY PUT (PE) 🔴", "SELL", setup_text, "Opening Range Breakdown", sl_raw, {"swing_id": orb_id}

    # -----------------------------------------------------------------
    # SETUP 2: INVERSE H&S / TOP REVERSAL NECKLINE BREAKOUT
    # -----------------------------------------------------------------
    if today_bars >= 6 or ignore_market_hours:
        rev = detect_reversal_neckline_breakout(df_5m)
        if rev is not None:
            r_dir = rev["direction"]
            exhausted, _ = is_signal_already_exhausted_or_target_hit(df_5m, r_dir, live_spot, atr_5m)
            if not exhausted and now_utc >= sl_direction_lock[index_name][r_dir] and last_structure_traded.get(index_name) != rev["struct_id"]:
                return rev["action"], r_dir, rev["setup_text"], rev["short_tag"], rev["sl_raw"], {"swing_id": rev["struct_id"]}

    m15_c     = df_15m.iloc[-2]
    m15_close = float(m15_c["Close"])
    m15_ema20 = float(m15_c["EMA20"])
    m15_ema50 = float(m15_c["EMA50"])

    htf_bullish = (m15_close >= m15_ema20) or (ema9 > ema20 > ema50)
    htf_bearish = (m15_close <= m15_ema20) or (ema9 < ema20 < ema50)

    # -----------------------------------------------------------------
    # SETUP 3: FIBONACCI (0.382-0.618) + EMA 9/20 SCALPING PULLBACK
    # -----------------------------------------------------------------
    if fib is not None and last_structure_traded.get(index_name) != fib["swing_id"]:
        ema_separation = abs(ema9 - ema20)
        if ema_separation >= (atr_5m * 0.10):
            if htf_bullish and fib["type"] == "BULLISH_SWING" and now_utc >= sl_direction_lock[index_name]["BUY"]:
                recent_3_low  = min(low_0, float(c1["Low"]), float(c2["Low"]))
                in_fib_zone   = (recent_3_low <= fib["fib_382"]) and (recent_3_low >= fib["fib_786"])
                ema_support   = (price > ema9) and (ema9 >= ema20 * 0.9995)
                green_trigger = (price > open_0) and (close_pos >= 0.62) and (body_ratio >= 0.45) and (price > float(c1["High"]) or price > float(c1["Close"]))
                exhausted, _  = is_signal_already_exhausted_or_target_hit(df_5m, "BUY", live_spot, atr_5m)
                if not exhausted and in_fib_zone and ema_support and green_trigger and (40.0 <= rsi <= 68.0):
                    sl_raw = recent_3_low - (atr_5m * SL_BUFFER_ATR)
                    setup_text = f"Fibonacci Golden Zone (0.50-0.618) + EMA 9/20 Bounce | RSI: {rsi:.0f}"
                    return "BUY CALL (CE) 🟢", "BUY", setup_text, "Fib 0.618 + EMA Bounce", sl_raw, fib

            if htf_bearish and fib["type"] == "BEARISH_SWING" and now_utc >= sl_direction_lock[index_name]["SELL"]:
                recent_3_high = max(high_0, float(c1["High"]), float(c2["High"]))
                in_fib_zone   = (recent_3_high >= fib["fib_382"]) and (recent_3_high <= fib["fib_786"])
                ema_resist    = (price < ema9) and (ema9 <= ema20 * 1.0005)
                red_trigger   = (price < open_0) and (close_pos <= 0.38) and (body_ratio >= 0.45) and (price < float(c1["Low"]) or price < float(c1["Close"]))
                exhausted, _  = is_signal_already_exhausted_or_target_hit(df_5m, "SELL", live_spot, atr_5m)
                if not exhausted and in_fib_zone and ema_resist and red_trigger and (32.0 <= rsi <= 60.0):
                    sl_raw = recent_3_high + (atr_5m * SL_BUFFER_ATR)
                    setup_text = f"Fibonacci Golden Zone (0.50-0.618) + EMA 9/20 Reject | RSI: {rsi:.0f}"
                    return "BUY PUT (PE) 🔴", "SELL", setup_text, "Fib 0.618 + EMA Reject", sl_raw, fib

    # -----------------------------------------------------------------
    # SETUP 4: SAME-DAY CONSOLIDATION BOX BREAKOUT & LIQUIDITY SWEEP
    # (Uses strictly TODAY'S candles so yesterday's 3:25 PM box never triggers at 9:21 AM!)
    # -----------------------------------------------------------------
    if today_bars >= 7:
        box_6    = today_df.iloc[-8:-2]
        box_high = float(box_6["High"].max())
        box_low  = float(box_6["Low"].min())
        box_rng  = box_high - box_low
        box_id   = f"BOX_{get_ist_now().date()}_{box_low}_{box_high}"

        if box_rng <= (atr_5m * 2.1) and last_structure_traded.get(index_name) != box_id:
            if htf_bullish and price > box_high and price > open_0 and ema9 > ema20 and body_ratio >= 0.55 and close_pos >= 0.68 and (1.05 <= disp_power <= 1.80) and (46.0 <= rsi <= 72.0):
                exhausted, _ = is_signal_already_exhausted_or_target_hit(df_5m, "BUY", live_spot, atr_5m)
                if not exhausted and now_utc >= sl_direction_lock[index_name]["BUY"]:
                    sl_raw = max(box_low, min(low_0, ema20)) - (atr_5m * SL_BUFFER_ATR)
                    setup_text = f"Intraday Box Breakout Above {box_high:.1f} | RSI: {rsi:.0f}"
                    return "BUY CALL (CE) 🟢", "BUY", setup_text, "Intraday Box Breakout", sl_raw, {"swing_id": box_id}

            if htf_bearish and price < box_low and price < open_0 and ema9 < ema20 and body_ratio >= 0.55 and close_pos <= 0.32 and (1.05 <= disp_power <= 1.80) and (28.0 <= rsi <= 54.0):
                exhausted, _ = is_signal_already_exhausted_or_target_hit(df_5m, "SELL", live_spot, atr_5m)
                if not exhausted and now_utc >= sl_direction_lock[index_name]["SELL"]:
                    sl_raw = min(box_high, max(high_0, ema20)) + (atr_5m * SL_BUFFER_ATR)
                    setup_text = f"Intraday Box Breakdown Below {box_low:.1f} | RSI: {rsi:.0f}"
                    return "BUY PUT (PE) 🔴", "SELL", setup_text, "Intraday Box Breakdown", sl_raw, {"swing_id": box_id}

    return None, None, None, None, None, None

# =====================================================================
# 6. ZERODHA KITE / DHAN MOBILE STYLE CHART GENERATOR
# =====================================================================
def generate_indian_broker_chart(df_5m, info, direction, entry_p, sl_p, tp_p, near_tp_p, short_tag, opt_data, is_live_signal=True):
    try:
        sub = df_5m.iloc[-52:].copy()
        if sub.empty or len(sub) < 25:
            return None

        dec      = info["decimals"]
        idx_sym  = info["mt5_name"]
        sub_Desc = info.get("sub_title", "NSE Index Spot")

        fig = plt.figure(figsize=(6.9, 10.6), dpi=140, facecolor="#ffffff")
        ax  = fig.add_axes([0.03, 0.07, 0.79, 0.75], facecolor="#ffffff")

        n_bars         = len(sub)
        x_vals         = np.arange(n_bars)
        proj_start_idx = n_bars - 2
        right_edge_idx = n_bars + 14

        ax.grid(True, linestyle=":", linewidth=0.7, color="#d1d5db", alpha=0.9, zorder=1)
        for spine in ["top", "left"]:
            ax.spines[spine].set_visible(False)
        ax.spines["right"].set_color("#9ca3af")
        ax.spines["bottom"].set_color("#9ca3af")

        win_hist = sub.iloc[-32:-2]
        res_line = float(win_hist["High"].max())
        sup_line = float(win_hist["Low"].min())
        ax.plot([n_bars - 32, right_edge_idx], [res_line, res_line], color="#dc2626", linewidth=1.0, alpha=0.55, zorder=2)
        ax.plot([n_bars - 32, right_edge_idx], [sup_line, sup_line], color="#dc2626", linewidth=1.0, alpha=0.55, zorder=2)

        if "EMA9" in sub.columns and "EMA20" in sub.columns:
            ax.plot(x_vals, sub["EMA9"].values, color="#16a34a", linewidth=1.15, alpha=0.85, zorder=3)
            ax.plot(x_vals, sub["EMA20"].values, color="#dc2626", linewidth=1.15, alpha=0.85, zorder=3)

        c_highs     = sub["High"].values
        c_lows      = sub["Low"].values
        candle_span = max(float(np.max(c_highs) - np.min(c_lows)), 1.0)
        min_body_h  = candle_span * 0.0045

        for idx in range(n_bars):
            row = sub.iloc[idx]
            o, h, l, c = float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"])
            is_bull  = c >= o
            wick_col = "#0d9488" if is_bull else "#e11d48"
            body_col = "#14b8a6" if is_bull else "#ef4444"

            ax.plot([idx, idx], [l, h], color=wick_col, linewidth=1.05, zorder=4)
            body_low = min(o, c)
            body_h   = max(abs(c - o), min_body_h)
            ax.add_patch(mpatches.Rectangle(
                (idx - 0.35, body_low), 0.70, body_h,
                facecolor=body_col, edgecolor=wick_col, linewidth=0.55, zorder=4
            ))

        proj_width      = right_edge_idx - proj_start_idx
        sl_low, sl_high = min(entry_p, sl_p), max(entry_p, sl_p)
        ax.add_patch(mpatches.Rectangle(
            (proj_start_idx, sl_low), proj_width, (sl_high - sl_low),
            facecolor="#ef4444", alpha=0.20, edgecolor="#dc2626", linewidth=1.0, zorder=2
        ))
        ax.plot([proj_start_idx - 6, right_edge_idx], [sl_p, sl_p], color="#dc2626", linewidth=1.5, zorder=5)

        tp_low, tp_high = min(entry_p, tp_p), max(entry_p, tp_p)
        ax.add_patch(mpatches.Rectangle(
            (proj_start_idx, tp_low), proj_width, (tp_high - tp_low),
            facecolor="#22c55e", alpha=0.20, edgecolor="#16a34a", linewidth=1.0, zorder=2
        ))
        ax.plot([proj_start_idx, right_edge_idx], [near_tp_p, near_tp_p], color="#15803d", linestyle="--", linewidth=1.2, zorder=5)
        ax.plot([proj_start_idx - 6, right_edge_idx], [tp_p, tp_p], color="#16a34a", linewidth=1.5, zorder=5)

        ax.axhline(entry_p, color="#0d9488", linewidth=1.1, zorder=5)

        arrow_x       = proj_start_idx + (proj_width * 0.55)
        arrow_start_y = entry_p + (tp_p - entry_p) * 0.12
        arrow_end_y   = entry_p + (tp_p - entry_p) * 0.88
        ax.annotate(
            "", xy=(arrow_x, arrow_end_y), xytext=(arrow_x, arrow_start_y),
            arrowprops=dict(arrowstyle="->,head_width=0.5,head_length=0.5", color="#15803d", lw=2.2),
            zorder=6
        )

        entry_s   = format_price(entry_p, dec)
        sl_s      = format_price(sl_p, dec)
        near_tp_s = format_price(near_tp_p, dec)
        tp_s      = format_price(tp_p, dec)

        label_x = proj_start_idx + (proj_width * 0.48)
        sl_va   = "bottom" if direction == "SELL" else "top"
        ax.text(
            label_x, sl_p,
            f" INDEX SL: {sl_s} | OPT SL: ₹{opt_data['opt_sl']:.1f} ",
            fontsize=7.6, fontweight="bold", color="#ffffff", ha="center", va=sl_va,
            bbox=dict(boxstyle="round,pad=0.22", facecolor="#dc2626", edgecolor="none"), zorder=8
        )

        ax.text(
            label_x, entry_p,
            f" {opt_data['strike_label']} @ ₹{opt_data['opt_entry']:.1f} ",
            fontsize=7.8, fontweight="bold", color="#111827", ha="center", va="center",
            bbox=dict(boxstyle="round,pad=0.22", facecolor="#fde047", edgecolor="#ca8a04"), zorder=8
        )

        tp_va = "top" if direction == "SELL" else "bottom"
        ax.text(
            label_x, tp_p,
            f" OPT TP1: ₹{opt_data['opt_tp1']:.1f} | TP2: ₹{opt_data['opt_tp2']:.1f} ",
            fontsize=7.6, fontweight="bold", color="#ffffff", ha="center", va=tp_va,
            bbox=dict(boxstyle="round,pad=0.22", facecolor="#16a34a", edgecolor="none"), zorder=8
        )

        ax.yaxis.tick_right()
        ax.tick_params(axis="y", labelsize=9.0, colors="#1f2937")
        ax.set_xlim(-0.8, right_edge_idx)

        all_y = list(c_highs) + list(c_lows) + [tp_p, sl_p, entry_p]
        y_min, y_max = min(all_y), max(all_y)
        y_pad = (y_max - y_min) * 0.14
        ax.set_ylim(y_min - y_pad, y_max + y_pad)

        for tag_y, tag_txt, tag_bg in [
            (sl_p, f" {sl_s} ", "#dc2626"),
            (entry_p, f" {entry_s} ", "#0d9488"),
            (near_tp_p, f" {near_tp_s} ", "#16a34a"),
            (tp_p, f" {tp_s} ", "#15803d")
        ]:
            ax.text(
                right_edge_idx + 0.25, tag_y, tag_txt,
                fontsize=8.2, fontweight="bold", color="white", va="center", ha="left",
                bbox=dict(boxstyle="square,pad=0.22", facecolor=tag_bg, edgecolor="none"),
                clip_on=False, zorder=9
            )

        tick_indices = [2, int(n_bars * 0.45), n_bars - 2]
        tick_labels  = []
        for ti in tick_indices:
            ts_ist = to_ist_timestamp(sub.index[ti])
            tick_labels.append(ts_ist.strftime("%d %b %H:%M"))
        ax.set_xticks(tick_indices)
        ax.set_xticklabels(tick_labels, fontsize=8.8, color="#374151")

        ax.text(0.02, 0.975, f"{idx_sym} • M5 ({short_tag})", transform=ax.transAxes, fontsize=11.0, fontweight="bold", color="#0284c7", va="top")
        ax.text(0.02, 0.942, sub_Desc, transform=ax.transAxes, fontsize=8.8, color="#374151", va="top")

        header_ax = fig.add_axes([0.03, 0.84, 0.94, 0.13], facecolor="#ffffff")
        header_ax.axis("off")

        now_ist_top = get_ist_now().strftime("%I:%M:%S %p IST")
        mode_title  = "🟢 CONFIRMED LIVE TRADE" if is_live_signal else "⏳ CHART PREVIEW (WAITING FOR BREAKOUT)"
        header_ax.text(0.01, 0.88, f"{mode_title}  •  {idx_sym}", fontsize=10.0, fontweight="bold", color="#111827", va="center")
        header_ax.text(0.99, 0.88, now_ist_top, fontsize=9.0, fontweight="bold", color="#4b5563", ha="right", va="center")

        badge_col = ("#16a34a" if opt_data["opt_type"] == "CE" else "#ea580c") if is_live_signal else "#4b5563"
        header_ax.add_patch(mpatches.Rectangle((0.0, 0.06), 0.46, 0.64, facecolor=badge_col, edgecolor="none"))
        header_ax.text(0.23, 0.50, f"{'BUY' if is_live_signal else 'WATCH'} {opt_data['strike_label']}", color="white", fontweight="bold", fontsize=11.0, ha="center", va="center")
        header_ax.text(0.23, 0.20, f"LTP: ₹{opt_data['opt_entry']:.1f}  |  Qty: {opt_data['qty']} ({opt_data['lots']} Lot)", color="#fef08a", fontweight="bold", fontsize=8.5, ha="center", va="center")

        header_ax.add_patch(mpatches.Rectangle((0.48, 0.06), 0.52, 0.64, facecolor="#1f2937", edgecolor="none"))
        header_ax.text(0.74, 0.50, f"OPT SL: ₹{opt_data['opt_sl']:.1f} (-{opt_data['opt_sl_pts']:.0f} pts)", color="#fca5a5", fontweight="bold", fontsize=9.5, ha="center", va="center")
        header_ax.text(0.74, 0.20, f"OPT TP1: ₹{opt_data['opt_tp1']:.1f} | TP2: ₹{opt_data['opt_tp2']:.1f}", color="#86efac", fontweight="bold", fontsize=9.5, ha="center", va="center")

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=140, facecolor="#ffffff")
        plt.close(fig)
        buf.seek(0)
        return buf
    except Exception as e:
        print(f"Chart Generation Error: {e}")
        return None

def send_telegram_signal_with_chart(caption_html, chart_buf, target_chat_id=CHAT_ID):
    if chart_buf is not None:
        photo_bytes = chart_buf.getvalue()
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
        try:
            if len(caption_html) <= 980:
                files = {"photo": ("indian_scalp.png", photo_bytes, "image/png")}
                data  = {"chat_id": target_chat_id, "caption": caption_html, "parse_mode": "HTML"}
                res   = requests.post(url, data=data, files=files, timeout=20)
                if res.status_code == 200 and res.json().get("ok"):
                    return

            files = {"photo": ("indian_scalp.png", photo_bytes, "image/png")}
            data  = {"chat_id": target_chat_id, "caption": "📊 <b>Live NSE/BSE Index & Option Strike Chart</b>", "parse_mode": "HTML"}
            requests.post(url, data=data, files=files, timeout=20)
            send_telegram_message(caption_html, target_chat_id)
            return
        except Exception as e:
            print(f"Telegram Photo Send Error: {e}")

    send_telegram_message(caption_html, target_chat_id)

# =====================================================================
# 7. COMMANDS (/nifty, /banknifty) WITH CLEAR "NO ACTIVE TRADE" BADGE
# =====================================================================
def send_instant_preview_chart(index_key=None):
    global chart_rotation_index
    index_names_list = list(INDIAN_INDICES.keys())

    if index_key is None:
        selected_key = index_names_list[chart_rotation_index % len(index_names_list)]
        chart_rotation_index += 1
        index_key = selected_key

    info = INDIAN_INDICES.get(index_key, INDIAN_INDICES["NIFTY 50"])
    df_15m, df_5m, live_spot, src = get_clean_spot_data(index_key, info)
    if df_5m is None or live_spot is None:
        send_telegram_message("⚠️ NSE/BSE डेटा लोड हो रहा है, कृपया 5 सेकंड बाद प्रयास करें।")
        return

    df_5m  = calculate_chart_indicators(df_5m)
    df_15m = calculate_chart_indicators(df_15m)

    sig, direction, setup_text, short_tag, custom_sl, _ = evaluate_all_market_setups(
        index_key, info, df_15m, df_5m, live_spot, ignore_market_hours=True
    )
    c0     = df_5m.iloc[-2]
    atr_5m = float(c0["ATR"])

    is_confirmed_signal = (sig is not None)
    if direction is None:
        direction, setup_text, short_tag, custom_sl = analyze_real_price_action(df_5m, info, live_spot)

    sl_level, sl_dist, lock_level, near_tp_lvl, target_level, tp_dist, actual_rr = calculate_smart_sl_and_easy_tp(
        df_5m, df_15m, info, direction, live_spot, custom_sl, atr_5m, allow_wall_block=False
    )

    opt_data = calculate_option_strike_and_premium_levels(
        index_key, info, direction, df_5m, live_spot, sl_level, near_tp_lvl, target_level, lock_level
    )

    chart_buf = generate_indian_broker_chart(
        df_5m, info, direction, live_spot, sl_level, target_level, near_tp_lvl, short_tag, opt_data, is_live_signal=is_confirmed_signal
    )

    dec = info["decimals"]
    status_header = (
        "🟢 <b>CONFIRMED LIVE BREAKOUT SIGNAL</b> 🟢"
        if is_confirmed_signal else
        "⏳ <b>NO ACTIVE TRADE RIGHT NOW — WAITING FOR BREAKOUT</b> ⏳\n<i>(यह केवल आपके देखने के लिए चार्ट प्रीव्यू है, अभी ट्रेड न लें)</i>"
    )

    caption = (
        f"📊 <b>{index_key} — LIVE M5 CHART CHECK</b>\n"
        f"{status_header}\n\n"
        f"🧠 <b>Structure:</b> {setup_text}\n"
        f"📈 <b>Index Spot:</b> <code>{format_price(live_spot, dec)}</code>\n\n"
        f"🎯 <b>WATCHLIST STRIKE ({opt_data['prem_source']}):</b>\n"
        f"👉 <b>Strike:</b> <code>{opt_data['strike_label']}</code>\n"
        f"📍 <b>Current LTP:</b> <code>₹{opt_data['opt_entry']:.1f}</code>\n"
        f"🛑 <b>Projected SL:</b> <code>₹{opt_data['opt_sl']:.1f}</code>  |  🎯 <b>TP1:</b> <code>₹{opt_data['opt_tp1']:.1f}</code>  |  <b>TP2:</b> <code>₹{opt_data['opt_tp2']:.1f}</code>"
    )
    send_telegram_signal_with_chart(caption, chart_buf)

def send_live_status_summary():
    mkt_open, mkt_txt = is_indian_market_open()
    vix = fetch_live_india_vix()

    report = (
        f"🇮🇳 <b>INDIAN MARKET SCALPER STATUS</b> 🇮🇳\n\n"
        f"📢 <b>Session:</b> {mkt_txt}  |  <b>India VIX:</b> <code>{vix:.2f}</code>\n"
        f"💡 <i>Chart Preview Commands:</i> <code>/nifty</code>, <code>/banknifty</code>, <code>/sensex</code>, <code>/finnifty</code>\n\n"
    )

    for name, info in INDIAN_INDICES.items():
        try:
            df_15m, df_5m, live_spot, _ = get_clean_spot_data(name, info)
            if df_5m is None or df_15m is None:
                continue
            df_5m = calculate_chart_indicators(df_5m)
            direction, _, short_tag, _ = analyze_real_price_action(df_5m, info, live_spot)
            step  = info["strike_step"]
            atm   = int(round(live_spot / step) * step)
            ce_pe = "CE" if direction == "BUY" else "PE"
            p_str = format_price(live_spot, info["decimals"])
            report += f"🔹 <b>{name}:</b> <code>{p_str}</code> | Watch: <b>{atm} {ce_pe}</b> ({short_tag})\n"
        except Exception:
            pass

    send_telegram_message(report)

def check_telegram_commands():
    global last_update_id
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    params = {"offset": last_update_id + 1, "timeout": 2}
    try:
        response = requests.get(url, params=params, timeout=5).json()
        if response.get("ok"):
            for result in response.get("result", []):
                last_update_id = result["update_id"]
                message = result.get("message", {})
                chat_id = str(message.get("chat", {}).get("id", ""))
                msg_text = message.get("text", "").strip().lower()

                if chat_id != CHAT_ID:
                    continue

                if msg_text in ["/status", "status", "/start", "start"]:
                    send_live_status_summary()
                elif msg_text in ["/chart", "chart"]:
                    send_instant_preview_chart(None)
                else:
                    for idx_name, idx_info in INDIAN_INDICES.items():
                        if msg_text in idx_info.get("cmd_keys", []) or msg_text == f"/chart {idx_info['mt5_name'].lower()}":
                            send_instant_preview_chart(idx_name)
                            break
    except Exception:
        pass

def manage_open_trades(index_name, candle_time, live_spot, latest_5m, decimals):
    global daily_realized_pnl_inr, pair_cooldown_until, sl_direction_lock, sl_count_today

    now_utc      = get_utc_now()
    current_high = max(live_spot, float(latest_5m["High"]))
    current_low  = min(live_spot, float(latest_5m["Low"]))

    for trade in daily_trades_log:
        if trade["pair"] == index_name and trade["is_open"]:
            if to_utc_timestamp(candle_time) <= trade["entry_candle_time"]:
                continue

            opt = trade["opt_data"]

            if not trade["trailed_to_cost"]:
                half_reached = False
                if trade["direction"] == "BUY" and current_high >= trade["half_target"]:
                    half_reached = True
                elif trade["direction"] == "SELL" and current_low <= trade["half_target"]:
                    half_reached = True

                if half_reached:
                    trade["trailed_to_cost"] = True
                    trade["sl"] = trade["entry"]
                    send_telegram_message(
                        f"🛡️ <b>SAFE COST LOCK REACHED — {opt['strike_label']}</b> 🛡️\n\n"
                        f"📈 <b>Index Spot:</b> <code>{format_price(live_spot, decimals)}</code>\n"
                        f"🎯 <b>Option Premium:</b> ~<code>₹{opt['opt_lock']:.1f}</code>\n"
                        f"📢 <b>Action:</b> अपना Option SL उठाकर <b>Entry Price (₹{opt['opt_entry']:.1f})</b> पर लॉक कर दें!"
                    )

            if not trade.get("near_tp_alerted", False):
                near_hit = False
                if trade["direction"] == "BUY" and current_high >= trade["near_target"] and current_high < trade["target"]:
                    near_hit = True
                elif trade["direction"] == "SELL" and current_low <= trade["near_target"] and current_low > trade["target"]:
                    near_hit = True

                if near_hit:
                    trade["near_tp_alerted"] = True
                    send_telegram_message(
                        f"⚡ <b>OPTION TARGET 1 HIT (+{opt['opt_tp1_pts']:.0f} Pts) — BOOK PROFIT!</b> ⚡\n\n"
                        f"👉 <b>Strike:</b> <b>{opt['strike_label']}</b>\n"
                        f"🎯 <b>Option Entry:</b> ₹{opt['opt_entry']:.1f}  ➡️  <b>TP1 Hit:</b> <b><code>₹{opt['opt_tp1']:.1f}</code></b>\n"
                        f"💰 <b>Profit:</b> <b>+₹{opt['prof1_inr']:.0f}</b> (80% प्रॉफिट तुरंत बुक करें!)"
                    )

            hit_type = None
            if trade["direction"] == "BUY":
                if current_high >= trade["target"]:
                    hit_type = "TARGET"
                elif current_low <= trade["sl"]:
                    hit_type = "TRAILING_SL" if trade["trailed_to_cost"] else "SL"
            elif trade["direction"] == "SELL":
                if current_low <= trade["target"]:
                    hit_type = "TARGET"
                elif current_high >= trade["sl"]:
                    hit_type = "TRAILING_SL" if trade["trailed_to_cost"] else "SL"

            if hit_type == "TARGET":
                trade["is_open"] = False
                pair_cooldown_until[index_name] = now_utc + timedelta(minutes=25)
                daily_realized_pnl_inr += opt["prof2_inr"]
                send_telegram_message(
                    f"🎯 <b>FULL OPTION TARGET 2 HIT (+₹{opt['prof2_inr']:.0f})</b> 🎯\n\n"
                    f"👉 <b>Strike:</b> <b>{opt['strike_label']}</b>\n"
                    f"📍 <b>Option Entry:</b> ₹{opt['opt_entry']:.1f}  ➡️  <b>TP2 Hit:</b> <b>₹{opt['opt_tp2']:.1f} (+{opt['opt_tp2_pts']:.0f} Pts)</b>\n"
                    f"💰 <b>Net Profit:</b> <b>+₹{opt['prof2_inr']:.0f}</b>"
                )
                continue

            elif hit_type == "TRAILING_SL":
                trade["is_open"] = False
                pair_cooldown_until[index_name] = now_utc + timedelta(minutes=20)
                lock_win = round(opt["prof1_inr"] * 0.5, 0)
                daily_realized_pnl_inr += lock_win
                send_telegram_message(
                    f"🛡️ <b>COST-TO-COST SAFE EXIT — {opt['strike_label']}</b> 🛡️\n\n"
                    f"📍 <b>Exit at Cost:</b> ₹{opt['opt_entry']:.1f}  |  <b>Locked Gain:</b> <b>+₹{lock_win:.0f}</b>"
                )
                continue

            elif hit_type == "SL":
                trade["is_open"] = False
                sl_count_today += 1
                pair_cooldown_until[index_name] = now_utc + timedelta(minutes=40)
                sl_direction_lock[index_name][trade["direction"]] = now_utc + timedelta(minutes=75)
                daily_realized_pnl_inr -= opt["risk_inr"]
                send_telegram_message(
                    f"🛑 <b>STOP-LOSS HIT — {opt['strike_label']}</b> 🛑\n\n"
                    f"📍 <b>Option Entry:</b> ₹{opt['opt_entry']:.1f}  ➡️  <b>Option SL:</b> ₹{opt['opt_sl']:.1f}\n"
                    f"⚖️ <b>Risk:</b> -₹{opt['risk_inr']:.0f} (-{opt['opt_sl_pts']:.0f} Pts)"
                )
                continue

def scan_markets():
    global trades_today, sl_count_today, current_day, last_candle_traded, last_structure_traded, daily_trades_log, daily_realized_pnl_inr, last_global_trade_time

    today = get_ist_now().date()
    if today != current_day:
        current_day            = today
        trades_today           = 0
        sl_count_today         = 0
        daily_trades_log       = []
        daily_realized_pnl_inr = 0.0
        last_candle_traded     = {name: None for name in INDIAN_INDICES}
        for k in live_bars_memory:
            live_bars_memory[k] = {}

    mkt_open, _ = is_indian_market_open()
    if not mkt_open:
        return

    if sl_count_today >= DAILY_MAX_SL_COUNT:
        return

    for name, info in INDIAN_INDICES.items():
        try:
            df_15m, df_5m, live_spot, source_used = get_clean_spot_data(name, info)
            if df_5m is None or df_15m is None or live_spot is None:
                continue

            df_15m = calculate_chart_indicators(df_15m)
            df_5m  = calculate_chart_indicators(df_5m)

            latest_5m   = df_5m.iloc[-1]
            closed_5m   = df_5m.iloc[-2]
            closed_time = to_utc_timestamp(df_5m.index[-2])
            atr_5m      = float(closed_5m["ATR"])
            dec         = info["decimals"]

            manage_open_trades(name, closed_time, live_spot, latest_5m, dec)

            if any(t["pair"] == name and t["is_open"] for t in daily_trades_log):
                continue

            open_trades_count = sum(1 for t in daily_trades_log if t["is_open"])
            if open_trades_count >= MAX_CONCURRENT_TRADES:
                continue

            if (get_utc_now() - last_global_trade_time).total_seconds() < (MIN_GAP_BETWEEN_TRADES_MIN * 60):
                continue

            if last_candle_traded[name] == closed_time:
                continue

            signal, direction, setup_text, short_tag, custom_sl, struct_info = evaluate_all_market_setups(
                name, info, df_15m, df_5m, live_spot, ignore_market_hours=False
            )

            if signal and custom_sl is not None:
                if direction == "BUY" and custom_sl >= live_spot:
                    continue
                if direction == "SELL" and custom_sl <= live_spot:
                    continue

                sl_level, sl_dist, half_target_level, near_tp_level, target_level, target_dist, actual_rr = calculate_smart_sl_and_easy_tp(
                    df_5m, df_15m, info, direction, live_spot, custom_sl, atr_5m, allow_wall_block=True
                )

                if sl_level is None or sl_dist <= 0 or target_dist <= 0:
                    continue

                opt_data = calculate_option_strike_and_premium_levels(
                    name, info, direction, df_5m, live_spot, sl_level, near_tp_level, target_level, half_target_level
                )

                # FINAL SAFETY GUARD: अगर पिछली कैंडल के High/Low में ऑप्शन पहले ही अपना टारगेट छू चुका है, तो ट्रेड मत दो!
                if opt_data.get("target_already_touched", False):
                    continue

                last_candle_traded[name] = closed_time
                if struct_info is not None and "swing_id" in struct_info:
                    last_structure_traded[name] = struct_info["swing_id"]
                last_global_trade_time = get_utc_now()
                trades_today += 1

                daily_trades_log.append({
                    "id": trades_today,
                    "pair": name,
                    "action": signal,
                    "direction": direction,
                    "strategy": setup_text,
                    "entry": live_spot,
                    "sl": sl_level,
                    "half_target": half_target_level,
                    "near_target": near_tp_level,
                    "target": target_level,
                    "opt_data": opt_data,
                    "entry_candle_time": closed_time,
                    "trailed_to_cost": False,
                    "near_tp_alerted": False,
                    "is_open": True
                })

                entry_s   = format_price(live_spot, dec)
                sl_s      = format_price(sl_level, dec)
                near_tp_s = format_price(near_tp_level, dec)
                target_s  = format_price(target_level, dec)

                msg = (
                    f"🇮🇳 <b>LIVE SCALPING SIGNAL — {info['mt5_name']}</b> 🇮🇳\n\n"
                    f"📢 <b>Action:</b> <b>{signal}</b>\n"
                    f"🧠 <b>Setup:</b> {setup_text}\n\n"
                    f"🎯 <b>OPTION STRIKE TO BUY ({opt_data['prem_source']}):</b>\n"
                    f"👉 <b>Strike:</b> <b><code>{opt_data['strike_label']}</code></b> ({opt_data['qty']} Qty / {opt_data['lots']} Lot)\n"
                    f"📍 <b>Option Entry:</b> <b><code>₹{opt_data['opt_entry']:.1f}</code></b>\n"
                    f"🛑 <b>Option SL:</b> <b><code>₹{opt_data['opt_sl']:.1f}</code></b>  <i>(-{opt_data['opt_sl_pts']:.0f} Pts नीचे)</i>\n"
                    f"🛡️ <b>Safe Cost Lock:</b> <code>₹{opt_data['opt_lock']:.1f}</code>\n"
                    f"🎯 <b>Option Target 1 (Easy TP):</b> <b><code>₹{opt_data['opt_tp1']:.1f}</code></b>  <i>(+{opt_data['opt_tp1_pts']:.0f} Pts / +₹{opt_data['prof1_inr']:.0f})</i>\n"
                    f"🎯 <b>Option Target 2 (Full TP):</b> <b><code>₹{opt_data['opt_tp2']:.1f}</code></b>  <i>(+{opt_data['opt_tp2_pts']:.0f} Pts / +₹{opt_data['prof2_inr']:.0f})</i>\n\n"
                    f"📈 <b>Index Spot Reference ({info['mt5_name']}):</b>\n"
                    f"• <b>Spot Entry:</b> <code>{entry_s}</code>  |  <b>Spot SL:</b> <code>{sl_s}</code> (-{sl_dist:.0f} pts)\n"
                    f"• <b>Spot TP1:</b> <code>{near_tp_s}</code>  |  <b>Spot TP2:</b> <code>{target_s}</code> (+{target_dist:.0f} pts)"
                )

                chart_buf = generate_indian_broker_chart(
                    df_5m, info, direction, live_spot, sl_level, target_level,
                    near_tp_level, short_tag, opt_data, is_live_signal=True
                )
                send_telegram_signal_with_chart(msg, chart_buf)

        except Exception as e:
            print(f"Error scanning {name}: {e}")

if __name__ == "__main__":
    send_telegram_message("🟢 <b>Indian Market (NSE/BSE) Options Scalper Online & Active</b>")

    start_run = time.time()
    last_scan_time = 0
    while time.time() - start_run < 21000:
        check_telegram_commands()
        update_realtime_tick_memory()
        if time.time() - last_scan_time >= 15:
            scan_markets()
            last_scan_time = time.time()
        time.sleep(3)
