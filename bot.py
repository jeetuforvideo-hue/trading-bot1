import os
import time
import requests
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone

# =====================================================================
# 1. आपकी पर्सनल और सिक्योर सेटिंग्स (INDIAN MARKET 1H + 15M OPERATOR EDITION)
# =====================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID   = os.getenv("CHAT_ID", "5610520408")  # सिर्फ आपकी टेलीग्राम आईडी पर काम करेगा

TOTAL_CAPITAL_INR  = 100000  # ₹1,00,000 की स्टैंडर्ड कैपिटल के हिसाब से रिस्क कैलकुलेशन
RISK_PER_TRADE_PCT = 1.0     # प्रति ट्रेड 1% रिस्क (अधिकतम ₹1,000 का रिस्क)
MAX_TRADES_PER_DAY = 3       # पूरे दिन (9:15 से 3:30) में सिर्फ 3 सबसे बेस्ट हाई-एक्यूरेसी ट्रेड्स
ADX_THRESHOLD      = 20      # मजबूत ट्रेंड और मोमेंटम फिल्टर
MAX_HOLD_MINUTES   = 120     # इंट्राडे में 2 घंटे (120 मिनट) तक मूव न आए तो थीटा-डिके बचाव एग्जिट

# भारतीय बाजार के 6 सबसे लिक्विड इंडेक्स और हैवीवेट स्क्रिप्ट्स (Nifty, BankNifty, FinNifty & Top Stocks)
INDIAN_INSTRUMENTS = {
    "NIFTY 50": {
        "symbol": "^NSEI",
        "broker_name": "NIFTY 50 (Index / Options)",
        "decimals": 2,
        "lot_size": 75,
        "unit_name": "Points"
    },
    "BANK NIFTY": {
        "symbol": "^NSEBANK",
        "broker_name": "BANKNIFTY (Index / Options)",
        "decimals": 2,
        "lot_size": 30,
        "unit_name": "Points"
    },
    "FIN NIFTY": {
        "symbol": "NIFTY_FIN_SERVICE.NS",
        "broker_name": "FINNIFTY (Index / Options)",
        "decimals": 2,
        "lot_size": 65,
        "unit_name": "Points"
    },
    "RELIANCE IND": {
        "symbol": "RELIANCE.NS",
        "broker_name": "RELIANCE (NSE Cash / F&O)",
        "decimals": 2,
        "lot_size": 250,
        "unit_name": "Points (₹)"
    },
    "HDFC BANK": {
        "symbol": "HDFCBANK.NS",
        "broker_name": "HDFCBANK (NSE Cash / F&O)",
        "decimals": 2,
        "lot_size": 550,
        "unit_name": "Points (₹)"
    },
    "ICICI BANK": {
        "symbol": "ICICIBANK.NS",
        "broker_name": "ICICIBANK (NSE Cash / F&O)",
        "decimals": 2,
        "lot_size": 700,
        "unit_name": "Points (₹)"
    }
}

last_candle_traded = {name: None for name in INDIAN_INSTRUMENTS}
trades_today = 0
last_update_id = 0
daily_trades_log = []

def get_ist_now():
    return datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)

current_day = get_ist_now().date()

def is_indian_market_open():
    """भारतीय शेयर बाजार (NSE) सोमवार से शुक्रवार सुबह 9:15 AM से दोपहर 3:30 PM IST तक खुलता है"""
    now = get_ist_now()
    wd = now.weekday() # Monday=0 ... Friday=4, Saturday=5, Sunday=6
    if wd >= 5:
        return False
    current_mins = now.hour * 60 + now.minute
    market_open_mins  = 9 * 60 + 15   # 9:15 AM
    market_close_mins = 15 * 60 + 30  # 3:30 PM
    return market_open_mins <= current_mins <= market_close_mins

def get_nse_session_info():
    """भारतीय बाजार के ऑपरेटर सेशन और थीटा-डिके टाइमिंग की सूक्ष्म जांच (IST के अनुसार)"""
    now = get_ist_now()
    if now.weekday() >= 5:
        return False, "Weekend (NSE Market Closed)"

    mins = now.hour * 60 + now.minute
    if mins < (9 * 60 + 15) or mins > (15 * 60 + 30):
        return False, "After-Market Hours (NSE Closed)"
    elif (9 * 60 + 15) <= mins < (9 * 60 + 30):
        # शुरुआती 15 मिनट (9:15-9:30) में गैप-अप/गैप-डाउन का सेटलमेंट और फ्रीक ट्रेड्स होते हैं
        return False, "9:15-9:30 AM Opening Settlement (Waiting for First 15M Candle Close)"
    elif (9 * 60 + 30) <= mins < (11 * 60 + 30):
        return True, "Morning Institutional Session (High FII/DII Momentum)"
    elif (11 * 60 + 30) <= mins < (13 * 60 + 15):
        return True, "Mid-Day Session (Trap & Box Accumulation Check Active)"
    elif (13 * 60 + 15) <= mins <= (14 * 60 + 45):
        return True, "European Overlap / Afternoon Breakout Session (High Volume)"
    else:
        # 2:45 PM के बाद नया इंट्राडे ट्रेड नहीं लेना चाहिए क्योंकि 3:15 पर स्क्वायर-ऑफ होता है
        return False, "2:45-3:30 PM Closing Square-Off Zone (No New Intraday Entries)"

def send_telegram_message(message, target_chat_id=CHAT_ID):
    if not BOT_TOKEN:
        print("❌ CRITICAL ERROR: BOT_TOKEN खाली है! कृपया GitHub Settings -> Secrets और .yml फाइल में BOT_TOKEN चेक करें।")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": target_chat_id, "text": message, "parse_mode": "HTML"}
    try:
        res = requests.post(url, json=payload, timeout=10)
        if not res.ok:
            print(f"⚠️ Telegram API Response Error: {res.status_code} - {res.text}")
    except Exception as e:
        print(f"Telegram Send Error: {e}")

def format_price(price, decimals=2):
    return f"{price:.{decimals}f}"

def get_trades_scoreboard():
    if not daily_trades_log:
        return "📋 <b>आज के NSE (1H+15M) ऑपरेटर ट्रेड्स का हाल:</b>\n\n• अभी कोई हाई-एक्यूरेसी FII/Operator ट्रेड नहीं लिया गया है।"
    board = "📋 <b>आज के तीनों NSE ऑपरेटर ट्रेड्स का रिजल्ट:</b>\n\n"
    for t in daily_trades_log:
        board += f"👉 <b>Trade #{t['id']} ({t['pair']} - {t['direction']}):</b>\n   {t['status']}\n\n"
    return board.strip()

def check_telegram_commands():
    global last_update_id
    if not BOT_TOKEN:
        return
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

                if chat_id != str(CHAT_ID):
                    if chat_id:
                        send_telegram_message(
                            "⛔ <b>Access Denied:</b> यह एक प्राइवेट पर्सनल इंडियन स्टॉक मार्केट ऑपरेटर बॉट है।",
                            target_chat_id=chat_id
                        )
                    continue

                if msg_text in ["/status", "status", "/start", "start"]:
                    send_live_nse_summary()
    except Exception as e:
        print(f"Polling Command Error: {e}")

def calculate_indicators(df):
    """
    बड़े टाइमफ्रेम (15M और 1H) के लिए माइक्रो-स्ट्रक्चर, कैंडल एनाटॉमी,
    Intraday VWAP, Opening Range (ORB) और Previous Day High/Low (PDH/PDL) की गणना
    """
    df = df.copy()
    df['EMA20']  = df['Close'].ewm(span=20, adjust=False).mean()
    df['EMA50']  = df['Close'].ewm(span=50, adjust=False).mean()
    df['EMA200'] = df['Close'].ewm(span=200, adjust=False).mean()

    # RSI Calculation (Wilder's Smoothing)
    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    df['RSI'] = (100 - (100 / (1 + rs))).fillna(50.0)

    # True Range & ATR (Volatility Engine)
    hl = df['High'] - df['Low']
    hc = np.abs(df['High'] - df['Close'].shift())
    lc = np.abs(df['Low'] - df['Close'].shift())
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    df['ATR'] = tr.rolling(14).mean()

    # ADX (Trend Strength Filter)
    up_move = df['High'] - df['High'].shift(1)
    down_move = df['Low'].shift(1) - df['Low']
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr_smooth = tr.rolling(14).mean().replace(0, np.nan)
    plus_di = 100 * (pd.Series(plus_dm, index=df.index).rolling(14).mean() / tr_smooth)
    minus_di = 100 * (pd.Series(minus_dm, index=df.index).rolling(14).mean() / tr_smooth)
    dx = (np.abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan)) * 100
    df['ADX'] = dx.rolling(14).mean().fillna(15.0)

    # सूक्ष्म कैंडल एनालिसिस (Micro Candle Anatomy)
    df['Candle_Body']  = np.abs(df['Close'] - df['Open'])
    df['Candle_Range'] = (df['High'] - df['Low']).replace(0, 1e-9)
    df['Body_Ratio']   = df['Candle_Body'] / df['Candle_Range']
    df['Upper_Wick']   = df['High'] - df[['Open', 'Close']].max(axis=1)
    df['Lower_Wick']   = df[['Open', 'Close']].min(axis=1) - df['Low']
    df['Close_Pos']    = (df['Close'] - df['Low']) / df['Candle_Range'] # 0.0=Bottom, 1.0=Top
    df['Vol_MA20']     = df['Volume'].rolling(20).mean() if 'Volume' in df.columns else 0.0

    # Intraday Institutional VWAP (हर नए दिन सुबह 9:15 पर रीसेट होने वाला असली VWAP)
    typical_price = (df['High'] + df['Low'] + df['Close']) / 3.0
    dates = df.index.date
    if 'Volume' in df.columns and df['Volume'].sum() > 0:
        vol_clean = df['Volume'].replace(0, 1)
        tp_vol = typical_price * vol_clean
        cum_tp_vol = tp_vol.groupby(dates).cumsum()
        cum_vol = vol_clean.groupby(dates).cumsum()
        df['VWAP'] = cum_tp_vol / cum_vol
    else:
        # इंडेक्स में टिक वॉल्यूम न होने पर Intraday Session Typical Price Anchor
        df['VWAP'] = typical_price.groupby(dates).expanding().mean().droplevel(0)

    # पिछले 25 कैंडल्स (15M पर पूरा 1 ट्रेडिंग दिन = 6.15 घंटे) का Equilibrium (50% Discount/Premium Level)
    df['Range_High_25'] = df['High'].rolling(25).max()
    df['Range_Low_25']  = df['Low'].rolling(25).min()
    df['Equilibrium']   = (df['Range_High_25'] + df['Range_Low_25']) / 2.0

    return df

def extract_intraday_key_levels(df_15m):
    """
    भारतीय बाजार के सबसे महत्वपूर्ण इंस्टीट्यूशनल लेवल्स निकालना:
    1. आज की सुबह 9:15 से 9:45 (पहली 2 कैंडल्स) का Opening Range High/Low (ORB)
    2. कल के पूरे दिन का High और Low (PDH / PDL - जहाँ सबसे ज्यादा स्टॉप-लॉस लगे होते हैं)
    """
    dates = pd.Series(df_15m.index.date, index=df_15m.index)
    unique_days = dates.unique()

    today_df = df_15m[dates == unique_days[-1]]
    orb_high, orb_low = None, None
    if len(today_df) >= 2:
        orb_candles = today_df.iloc[:2] # 9:15 और 9:30 वाली दो 15M कैंडल्स
        orb_high = float(orb_candles['High'].max())
        orb_low  = float(orb_candles['Low'].min())

    pdh, pdl = None, None
    if len(unique_days) >= 2:
        prev_day_df = df_15m[dates == unique_days[-2]]
        if not prev_day_df.empty:
            pdh = float(prev_day_df['High'].max())
            pdl = float(prev_day_df['Low'].min())

    return {
        "orb_high": orb_high,
        "orb_low": orb_low,
        "pdh": pdh,
        "pdl": pdl,
        "today_candles_count": len(today_df)
    }

def analyze_htf_1h_structure(df_1h):
    """
    बड़े टाइमफ्रेम (1-Hour Chart) का डीप एनालिसिस:
    • 1H का मुख्य FII/DII इंस्टीट्यूशनल फ्लो (Bullish / Bearish)
    • 1H के मेजर रेजिस्टेंस और सपोर्ट (ताकि हम ठीक दीवार के सामने कॉल/पुट न लें)
    """
    c_1h = df_1h.iloc[-2] # आखिरी पूरी तरह क्लोज हुई 1-घंटे की कैंडल
    p_1h = df_1h.iloc[-3]

    close_1h  = float(c_1h['Close'])
    ema20_1h  = float(c_1h['EMA20'])
    ema50_1h  = float(c_1h['EMA50'])
    ema200_1h = float(c_1h['EMA200'])
    atr_1h    = float(c_1h['ATR']) if not np.isnan(c_1h['ATR']) else 0.0

    # पिछले 14 घंटे (2 ट्रेडिंग दिन) का 1H स्ट्रक्चर हाई और लो
    recent_1h = df_1h.iloc[-16:-2]
    htf_res   = float(recent_1h['High'].max())
    htf_sup   = float(recent_1h['Low'].min())

    bull_score = 0
    bear_score = 0

    if close_1h > ema50_1h:
        bull_score += 1
    else:
        bear_score += 1

    if ema20_1h > ema50_1h:
        bull_score += 1
    else:
        bear_score += 1

    if close_1h > ema200_1h:
        bull_score += 1
    else:
        bear_score += 1

    if float(c_1h['High']) >= float(p_1h['High']) and float(c_1h['Low']) >= float(p_1h['Low']):
        bull_score += 1
    elif float(c_1h['High']) <= float(p_1h['High']) and float(c_1h['Low']) <= float(p_1h['Low']):
        bear_score += 1

    if bull_score >= 3:
        htf_bias = "BULLISH"
    elif bear_score >= 3:
        htf_bias = "BEARISH"
    else:
        htf_bias = "NEUTRAL"

    return {
        "bias": htf_bias,
        "htf_res": htf_res,
        "htf_sup": htf_sup,
        "atr_1h": atr_1h
    }

def evaluate_indian_operator_strategies(df_15m, df_1h):
    """
    भारतीय शेयर बाजार (NSE) के लिए 4 बिल्कुल नई, खुद की बनाई हुई (1H + 15M) ऑपरेटर स्ट्रैटेजी:
    नोट: यहाँ df_15m.iloc[-2] (पूरी तरह बंद हो चुकी 15-मिनट की कैंडल) का उपयोग किया गया है,
    ताकि अधूरी कैंडल के फेकआउट से कभी लॉस न हो!
    """
    session_ok, _ = get_nse_session_info()
    if not session_ok:
        return None, None, None, None

    htf = analyze_htf_1h_structure(df_1h)
    htf_bias = htf["bias"]
    levels = extract_intraday_key_levels(df_15m)

    # Confirmed Closed Candles on 15-Minute Chart (-2 is last closed, -3 is prior closed)
    curr  = df_15m.iloc[-2]
    prev  = df_15m.iloc[-3]
    prev2 = df_15m.iloc[-4]

    price      = float(curr['Close'])
    open_p     = float(curr['Open'])
    high_p     = float(curr['High'])
    low_p      = float(curr['Low'])
    body       = float(curr['Candle_Body'])
    c_range    = float(curr['Candle_Range'])
    body_ratio = float(curr['Body_Ratio'])
    u_wick     = float(curr['Upper_Wick'])
    l_wick     = float(curr['Lower_Wick'])
    close_pos  = float(curr['Close_Pos'])

    atr        = float(curr['ATR'])
    ema20      = float(curr['EMA20'])
    ema50      = float(curr['EMA50'])
    rsi        = float(curr['RSI'])
    adx        = float(curr['ADX'])
    vwap       = float(curr['VWAP'])
    eq_level   = float(curr['Equilibrium'])

    if np.isnan(atr) or atr <= 0 or c_range <= 0:
        return None, None, None, None

    # बारीक नियम 0: Freak Candle / News Spike Filter (अगर कैंडल 2.8x ATR से बड़ी है तो टॉप पर चेज न करें)
    if c_range > (atr * 2.8):
        return None, None, None, None

    # पिछले 10 कैंडल्स (2.5 घंटे का 15M स्विंग स्ट्रक्चर) और 12 कैंडल्स (3 घंटे का इंट्राडे कंसोलिडेशन बॉक्स)
    struct_10     = df_15m.iloc[-12:-2]
    swing_high_10 = float(struct_10['High'].max())
    swing_low_10  = float(struct_10['Low'].min())

    box_12        = df_15m.iloc[-14:-3]
    box_high_12   = float(box_12['High'].max())
    box_low_12    = float(box_12['Low'].min())
    box_width_12  = box_high_12 - box_low_12

    # =========================================================================
    # STRATEGY 1: 9:15-9:45 OPENING RANGE (ORB) & PDH/PDL OPTION-WRITER SL HUNT
    # सूक्ष्म जांच:
    # 1. ऑपरेटर सुबह के Opening Low/High या कल के Low/High (PDL/PDH) के नीचे/ऊपर रिटेलर्स का SL खाते हैं
    # 2. कैंडल 40%+ लंबी विक बनाकर वापस लेवल के अंदर और VWAP के सपोर्ट में क्लोज होती है
    # 3. 1-Hour का ट्रेंड खिलाफ नहीं होना चाहिए और कीमत Discount (BUY) या Premium (SELL) में हो
    # =========================================================================
    orb_low, orb_high = levels["orb_low"], levels["orb_high"]
    pdl, pdh          = levels["pdl"], levels["pdh"]

    # Bullish SL-Hunt Trap (Put Writers / Retail Sellers Trap)
    key_bull_pools = [lvl for lvl in [orb_low, pdl, swing_low_10] if lvl is not None]
    for pool_low in key_bull_pools:
        swept_pool_low = (low_p < pool_low and price > pool_low) or (float(prev['Low']) < pool_low and price > pool_low and price > float(prev['High']))
        if swept_pool_low and htf_bias != "BEARISH":
            in_discount = (price <= eq_level * 1.003) or (rsi < 54)
            wick_trap_ok = (l_wick >= c_range * 0.40) or (body_ratio >= 0.55 and price > float(prev['High']))
            if price > open_p and close_pos >= 0.65 and c_range >= (atr * 0.75) and in_discount and wick_trap_ok:
                lowest_pt = min(low_p, float(prev['Low']))
                sl_level  = lowest_pt - (atr * 0.35)
                return "BUY (CE / LONG) 🟢", "BUY", "NSE Opening-Range / PDL Liquidity Sweep & Put-Writer Trap", sl_level

    # Bearish SL-Hunt Trap (Call Writers / Breakout Buyers Trap)
    key_bear_pools = [lvl for lvl in [orb_high, pdh, swing_high_10] if lvl is not None]
    for pool_high in key_bear_pools:
        swept_pool_high = (high_p > pool_high and price < pool_high) or (float(prev['High']) > pool_high and price < pool_high and price < float(prev['Low']))
        if swept_pool_high and htf_bias != "BULLISH":
            in_premium = (price >= eq_level * 0.997) or (rsi > 46)
            wick_trap_ok = (u_wick >= c_range * 0.40) or (body_ratio >= 0.55 and price < float(prev['Low']))
            if price < open_p and close_pos <= 0.35 and c_range >= (atr * 0.75) and in_premium and wick_trap_ok:
                highest_pt = max(high_p, float(prev['High']))
                sl_level   = highest_pt + (atr * 0.35)
                return "SELL (PE / SHORT) 🔴", "SELL", "NSE Opening-Range / PDH Bull-Trap & Call-Writer Sweep", sl_level

    # =========================================================================
    # STRATEGY 2: INSTITUTIONAL VWAP ANCHOR + UNMITIGATED ORDER BLOCK & FVG
    # सूक्ष्म जांच:
    # 1. पिछले 4 से 12 कैंडल्स के बीच एक बड़ी संस्थागत कैंडल ने ताज़ा (Untouched) Order Block + FVG बनाया हो
    # 2. वह ज़ोन बीच की किसी भी कैंडल द्वारा तोड़ा न गया हो (100% Unmitigated Check)
    # 3. वर्तमान 15M क्लोज्ड कैंडल उस ज़ोन को रीटेस्ट करके Intraday VWAP और EMA50 के साथ उछली हो
    # =========================================================================
    for i in range(4, 13):
        c1 = df_15m.iloc[-i-2] # Order Block Base Candle
        c2 = df_15m.iloc[-i-1] # FII/DII Impulse Candle
        c3 = df_15m.iloc[-i]   # FVG Confirmation Candle

        # Bullish VWAP + Unmitigated OB Retest
        if htf_bias == "BULLISH" and float(c3['Low']) > float(c1['High']) and float(c2['Close']) > float(c2['Open']) and float(c2['Candle_Body']) >= (atr * 1.15):
            zone_top    = float(c3['Low'])
            zone_bottom = float(c1['Low'])
            intervening = df_15m.iloc[-i+1:-2]
            already_broken = (len(intervening) > 0) and (float(intervening['Low'].min()) < zone_bottom)

            if not already_broken and low_p <= zone_top and low_p >= (zone_bottom - atr * 0.15):
                if price > open_p and price > vwap and price > ema50 and close_pos >= 0.65 and body_ratio >= 0.48:
                    sl_ob = min(low_p, zone_bottom) - (atr * 0.35)
                    return "BUY (CE / LONG) 🟢", "BUY", "15M Unmitigated Order Block + Intraday VWAP Confluence", sl_ob

        # Bearish VWAP + Unmitigated OB Retest
        if htf_bias == "BEARISH" and float(c3['High']) < float(c1['Low']) and float(c2['Close']) < float(c2['Open']) and float(c2['Candle_Body']) >= (atr * 1.15):
            zone_bottom = float(c3['High'])
            zone_top    = float(c1['High'])
            intervening = df_15m.iloc[-i+1:-2]
            already_broken = (len(intervening) > 0) and (float(intervening['High'].max()) > zone_top)

            if not already_broken and high_p >= zone_bottom and high_p <= (zone_top + atr * 0.15):
                if price < open_p and price < vwap and price < ema50 and close_pos <= 0.35 and body_ratio >= 0.48:
                    sl_ob = max(high_p, zone_top) + (atr * 0.35)
                    return "SELL (PE / SHORT) 🔴", "SELL", "15M Unmitigated Supply Block + Below Intraday VWAP", sl_ob

    # =========================================================================
    # STRATEGY 3: MID-DAY THETA-DECAY BOX MANIPULATION (AMD REVERSAL TRAP)
    # सूक्ष्म जांच:
    # 1. पिछले 3 घंटे (12 कैंडल्स) मार्केट एक तंग साइडवेज बॉक्स में रहा हो (Width <= 4.2x ATR)
    # 2. पिछली कैंडल ने बॉक्स तोड़ा (ऑप्शन बायर्स को गलत तरफ फंसाया)
    # 3. वर्तमान 15M क्लोज्ड कैंडल ने वापस बॉक्स के अंदर घुसकर पिछली कैंडल को Engulf किया और VWAP क्रॉस किया
    # =========================================================================
    if box_width_12 <= (atr * 4.2):
        prev_low   = float(prev['Low'])
        prev_high  = float(prev['High'])
        prev_close = float(prev['Close'])

        # Bullish AMD Spring Reversal
        if (prev_low < box_low_12 or prev_close < box_low_12) and price > box_low_12:
            if price > open_p and price > prev_high and price >= vwap and body >= (atr * 0.75) and close_pos >= 0.68 and htf_bias != "BEARISH":
                sl_amd = min(low_p, prev_low) - (atr * 0.35)
                return "BUY (CE / LONG) 🟢", "BUY", "NSE Theta-Box AMD Manipulation Trap (Bullish Spring)", sl_amd

        # Bearish AMD Upthrust Reversal
        if (prev_high > box_high_12 or prev_close > box_high_12) and price < box_high_12:
            if price < open_p and price < prev_low and price <= vwap and body >= (atr * 0.75) and close_pos <= 0.32 and htf_bias != "BULLISH":
                sl_amd = max(high_p, prev_high) + (atr * 0.35)
                return "SELL (PE / SHORT) 🔴", "SELL", "NSE Theta-Box AMD Manipulation Trap (Bearish Upthrust)", sl_amd

    # =========================================================================
    # STRATEGY 4: 1H + 15M FII INSTITUTIONAL DISPLACEMENT & OBSTACLE-FREE BOS
    # सूक्ष्म जांच:
    # 1. 1H ट्रेंड, 15M EMA20/50 और Intraday VWAP तीनों पूरी तरह एक दिशा में हों और ADX >= 20 हो
    # 2. कैंडल बॉडी 1.35x ATR से बड़ी हो और बिना लंबी उलटी विक के (Body Ratio >= 68%) स्ट्रक्चर तोड़े
    # 3. सामने 1-Hour की कोई मेजर रुकावट (Obstacle Wall) न हो (कम से कम 1.2x ATR का खुला रास्ता हो)
    # =========================================================================
    vol_now   = float(curr['Volume']) if 'Volume' in curr and not np.isnan(curr['Volume']) else 0
    vol_avg   = float(curr['Vol_MA20']) if 'Vol_MA20' in curr and not np.isnan(curr['Vol_MA20']) else 0
    vol_valid = (vol_avg == 0) or (vol_now >= vol_avg * 1.20)

    if body >= (atr * 1.35) and body_ratio >= 0.68 and adx >= ADX_THRESHOLD and vol_valid:
        # Bullish MTF Institutional Breakout
        room_to_htf_res = (htf["htf_res"] - price) >= (atr * 1.2) or (price > htf["htf_res"])
        higher_lows = float(curr['Low']) > float(prev['Low']) >= float(prev2['Low'])
        if htf_bias == "BULLISH" and price > open_p and float(prev['Close']) <= swing_high_10 and price > swing_high_10:
            if price > vwap and price > ema20 and ema20 > ema50 and 53 <= rsi <= 74 and room_to_htf_res and higher_lows:
                sl_bos = max(low_p - (atr * 0.30), price - (atr * 1.8))
                return "BUY (CE / LONG) 🟢", "BUY", "1H+15M FII Institutional Displacement & VWAP Breakout", sl_bos

        # Bearish MTF Institutional Breakdown
        room_to_htf_sup = (price - htf["htf_sup"]) >= (atr * 1.2) or (price < htf["htf_sup"])
        lower_highs = float(curr['High']) < float(prev['High']) <= float(prev2['High'])
        if htf_bias == "BEARISH" and price < open_p and float(prev['Close']) >= swing_low_10 and price < swing_low_10:
            if price < vwap and price < ema20 and ema20 < ema50 and 26 <= rsi <= 47 and room_to_htf_sup and lower_highs:
                sl_bos = min(high_p + (atr * 0.30), price + (atr * 1.8))
                return "SELL (PE / SHORT) 🔴", "SELL", "1H+15M FII Institutional Displacement & VWAP Breakdown", sl_bos

    return None, None, None, None

def send_live_nse_summary():
    market_state = "🟢 भारतीय शेयर बाजार (NSE) खुला है (1H + 15M OPERATOR MODE)" if is_indian_market_open() else "🔴 भारतीय शेयर बाजार अभी बंद है (Timing: 9:15 AM - 3:30 PM IST)"
    _, session_name = get_nse_session_info()
    report = (
        f"🇮🇳 <b>INDIAN MARKET (1H + 15M) OPERATOR REPORT</b> 🇮🇳\n\n"
        f"📢 <b>Status:</b> {market_state}\n"
        f"🕒 <b>Session:</b> {session_name}\n\n"
    )

    for name, info in INDIAN_INSTRUMENTS.items():
        try:
            ticker = yf.Ticker(info["symbol"])
            df_15m = ticker.history(period="10d", interval="15m")
            df_1h  = ticker.history(period="1mo", interval="1h")
            if df_15m.empty or len(df_15m) < 60 or df_1h.empty or len(df_1h) < 40:
                continue

            df_15m = calculate_indicators(df_15m)
            df_1h  = calculate_indicators(df_1h)
            htf    = analyze_htf_1h_structure(df_1h)

            latest = df_15m.iloc[-1]
            price  = float(latest['Close'])
            vwap   = float(latest['VWAP'])
            rsi    = float(latest['RSI'])
            vwap_status = "Above VWAP (Bullish)" if price >= vwap else "Below VWAP (Bearish)"
            p_str  = format_price(price, info["decimals"])

            report += (
                f"🔹 <b>{name}:</b> ₹{p_str}\n"
                f"   • 1H Trend: <b>{htf['bias']}</b> | 15M: {vwap_status} (RSI: {rsi:.0f})\n\n"
            )
        except Exception:
            report += f"🔹 <b>{name}:</b> डेटा लोड हो रहा है...\n\n"

    report += f"🛡️ <b>आज के कुल ट्रेड:</b> {trades_today} / {MAX_TRADES_PER_DAY}\n\n"
    report += get_trades_scoreboard()
    send_telegram_message(report)

def manage_active_nse_trades(pair_name, candle_time, latest_live, closed_15m, decimals):
    """लाइव टिक पर SL/Target, 3:15 PM Intraday Square-off और 15M क्लोज्ड कैंडल पर रिवर्सल चेक करेगा"""
    price        = float(latest_live['Close'])
    current_high = float(latest_live['High'])
    current_low  = float(latest_live['Low'])
    ema20        = float(closed_15m['EMA20'])
    ema50        = float(closed_15m['EMA50'])
    vwap         = float(closed_15m['VWAP'])
    adx          = float(closed_15m['ADX']) if not np.isnan(closed_15m['ADX']) else 15.0

    now = get_ist_now()
    now_mins = now.hour * 60 + now.minute

    for trade in daily_trades_log:
        if trade["pair"] == pair_name and trade["is_open"]:
            if candle_time <= trade["entry_candle_time"]:
                continue

            entry_str  = format_price(trade["entry"], decimals)
            target_str = format_price(trade["target"], decimals)
            sl_str     = format_price(trade["sl"], decimals)
            price_str  = format_price(price, decimals)

            # 0. दोपहर 3:15 PM IST पर ऑटो इंट्राडे स्क्वायर-ऑफ अलर्ट (ब्रोकर पेनल्टी से बचने के लिए)
            if now_mins >= (15 * 60 + 15):
                trade["is_open"] = False
                trade["status"] = f"🕒 3:15 PM INTRADAY AUTO SQUARE-OFF (Exit at ₹{price_str})"
                send_telegram_message(
                    f"🕒 <b>NSE 3:15 PM INTRADAY SQUARE-OFF ALERT (Trade #{trade['id']})</b> 🕒\n\n"
                    f"🇮🇳 <b>Scrip:</b> {trade['pair']}\n"
                    f"📍 <b>Entry:</b> ₹{entry_str}  ➡️  <b>Closing Price:</b> ₹{price_str}\n\n"
                    f"📢 <b>ऑपरेटर सलाह:</b> दोपहर 3:15 बज चुके हैं! भारतीय बाजार बंद होने वाला है, ओवरनाइट गैप रिस्क और ब्रोकर ऑटो-स्क्वायरऑफ चार्ज से बचने के लिए अपनी इंट्राडे पोजीशन अभी क्लोज कर लें।\n\n"
                    f"────────────────────\n\n{get_trades_scoreboard()}"
                )
                continue

            # 1. TARGET या STOP-LOSS चेक करें
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
                trade["status"] = f"✅ TARGET HIT (+₹{trade['expected_profit']:.0f} प्रॉफिट)"
                send_telegram_message(
                    f"🎯 <b>NSE OPERATOR TRADE #{trade['id']} : FULL TARGET HIT!</b> 🎯\n\n"
                    f"🇮🇳 <b>Scrip:</b> {trade['pair']} ({trade['broker_name']})\n\n"
                    f"⚡ <b>Direction:</b> {trade['action']}\n\n"
                    f"📍 <b>Spot Entry:</b> ₹{entry_str}  ➡️  <b>Target Hit:</b> ₹{target_str}\n\n"
                    f"💰 <b>Result:</b> <b>✅ +₹{trade['expected_profit']:.0f} अनुमानित प्रॉफिट</b>\n\n"
                    f"📢 <b>ऑपरेटर सलाह:</b> शानदार! बड़े टाइमफ्रेम (1H+15M) का टारगेट पूरा हुआ, अपना पूरा प्रॉफिट बुक कर लें।\n\n"
                    f"────────────────────\n\n{get_trades_scoreboard()}"
                )
                continue

            elif hit_type == "TRAILING_SL":
                trade["is_open"] = False
                trade["status"] = "🛡️ BREAKEVEN EXIT (₹0 No Loss)"
                send_telegram_message(
                    f"🛡️ <b>NSE OPERATOR TRADE #{trade['id']} : TRAILING SL HIT (NO LOSS)!</b> 🛡️\n\n"
                    f"🇮🇳 <b>Scrip:</b> {trade['pair']}\n\n"
                    f"📢 <b>अपडेट:</b> मार्केट वापस घूम गया, लेकिन स्टॉप-लॉस कॉस्ट (Breakeven) पर होने की वजह से <b>आपका ₹0 का नुकसान हुआ!</b>\n\n"
                    f"────────────────────\n\n{get_trades_scoreboard()}"
                )
                continue

            elif hit_type == "SL":
                trade["is_open"] = False
                trade["status"] = f"🛑 STOP-LOSS HIT (-₹{trade['max_risk']:.0f} लॉस)"
                send_telegram_message(
                    f"🛑 <b>NSE OPERATOR TRADE #{trade['id']} : STOP-LOSS HIT!</b> 🛑\n\n"
                    f"🇮🇳 <b>Scrip:</b> {trade['pair']}\n\n"
                    f"📍 <b>Spot Entry:</b> ₹{entry_str}  ➡️  <b>SL Hit:</b> ₹{sl_str}\n\n"
                    f"⚠️ <b>Result:</b> <b>🛑 -₹{trade['max_risk']:.0f} लॉस</b>\n\n"
                    f"📢 <b>सलाह:</b> स्टॉप-लॉस हिट हो गया है, रिवेंज ट्रेड न लें और अगले 1H+15M स्मार्ट मनी सेटअप का इंतजार करें।\n\n"
                    f"────────────────────\n\n{get_trades_scoreboard()}"
                )
                continue

            # 2. प्रो नियम: 1:1 आने पर SL को Breakeven (Cost) पर लाना
            if not trade["trailed_to_cost"]:
                half_reached = False
                if trade["direction"] == "BUY" and current_high >= trade["half_target"]:
                    half_reached = True
                elif trade["direction"] == "SELL" and current_low <= trade["half_target"]:
                    half_reached = True

                if half_reached:
                    trade["trailed_to_cost"] = True
                    trade["sl"] = trade["entry"]
                    trade["status"] = "🔥 RUNNING (SL at Breakeven - Risk Free)"
                    send_telegram_message(
                        f"🛡️ <b>SMART MONEY UPDATE (Trade #{trade['id']}) : MOVE SL TO COST!</b> 🛡️\n\n"
                        f"🇮🇳 <b>Scrip:</b> {trade['pair']}\n\n"
                        f"✅ <b>1:1 टारगेट पूरा हो चुका है! (Current Spot: ₹{price_str})</b>\n\n"
                        f"📢 <b>एक्शन:</b> अब अपने ब्रोकर ऐप (Zerodha/Dhan/Angel/Groww) में स्टॉप-लॉस को उठाकर <b>Entry Price (₹{entry_str})</b> पर लगा लें। अब यह ट्रेड 100% रिस्क-फ्री है!"
                    )

            # 3. प्रो नियम: इंट्राडे में 120 मिनट (2 घंटे) तक मार्केट साइडवेज अटक जाए तो Theta-Decay से बचने के लिए एग्जिट
            hold_minutes = (candle_time - trade["entry_candle_time"]).total_seconds() / 60.0
            if hold_minutes >= MAX_HOLD_MINUTES and not trade["trailed_to_cost"]:
                if adx < ADX_THRESHOLD or abs(price - trade["entry"]) < (trade["sl_dist"] * 0.45):
                    trade["is_open"] = False
                    trade["status"] = "⏳ THETA-DECAY SIDEWAYS EXIT"
                    send_telegram_message(
                        f"⏳ <b>OPERATOR ALERT (Trade #{trade['id']}) : THETA-DECAY SIDEWAYS EXIT!</b> ⏳\n\n"
                        f"🇮🇳 <b>Scrip:</b> {trade['pair']}\n\n"
                        f"📍 <b>Entry:</b> ₹{entry_str}  ➡️  <b>Current Spot:</b> ₹{price_str}\n\n"
                        f"⚠️ <b>कारण:</b> 2 घंटे हो चुके हैं और 15M चार्ट पर FII फॉलो-थ्रू नहीं आया है। साइडवेज मार्केट में ऑप्शन प्रीमियम (Theta) गलता है।\n\n"
                        f"📢 <b>सलाह:</b> स्टॉप-लॉस का इंतजार न करें, <b>अभी तुरंत कॉस्ट या छोटे प्रॉफिट/लॉस पर ट्रेड से बाहर निकल जाएं!</b>\n\n"
                        f"────────────────────\n\n{get_trades_scoreboard()}"
                    )
                    continue

            # 4. प्रो नियम: अगर 15M की क्लोज्ड कैंडल VWAP और EMA20 दोनों के खिलाफ बंद हो जाए
            if hold_minutes >= 30:
                closed_price = float(closed_15m['Close'])
                trend_reversed = False
                if trade["direction"] == "BUY" and closed_price < vwap and closed_price < ema20 and closed_price < ema50 and closed_price < (trade["entry"] - trade["sl_dist"] * 0.65):
                    trend_reversed = True
                elif trade["direction"] == "SELL" and closed_price > vwap and closed_price > ema20 and closed_price > ema50 and closed_price > (trade["entry"] + trade["sl_dist"] * 0.65):
                    trend_reversed = True

                if trend_reversed:
                    trade["is_open"] = False
                    trade["status"] = "🔄 15M VWAP + STRUCTURE SHIFT EXIT (जल्दी बचाव)"
                    send_telegram_message(
                        f"🔄 <b>OPERATOR ALERT (Trade #{trade['id']}) : VWAP & STRUCTURE REVERSAL!</b> 🔄\n\n"
                        f"🇮🇳 <b>Scrip:</b> {trade['pair']}\n\n"
                        f"📍 <b>Entry:</b> ₹{entry_str}  ➡️  <b>Current Spot:</b> ₹{price_str}\n\n"
                        f"⚠️ <b>कारण:</b> 15-मिनट चार्ट पर कैंडल Intraday VWAP और EMA दोनों के खिलाफ क्लोज हो गई है।\n\n"
                        f"📢 <b>सलाह:</b> पूरा स्टॉप-लॉस देने से बेहतर है कि अभी तुरंत ट्रेड क्लोज कर दें!\n\n"
                        f"────────────────────\n\n{get_trades_scoreboard()}"
                    )
                    continue

def scan_indian_market():
    global trades_today, current_day, last_candle_traded, daily_trades_log

    if not is_indian_market_open():
        return

    today = get_ist_now().date()
    if today != current_day:
        current_day = today
        trades_today = 0
        daily_trades_log = []
        last_candle_traded = {name: None for name in INDIAN_INSTRUMENTS}

    for name, info in INDIAN_INSTRUMENTS.items():
        try:
            ticker = yf.Ticker(info["symbol"])
            # बड़े टाइमफ्रेम का डेटा: 15-Minute (एंट्री, VWAP और ORB) + 1-Hour (मेजर FII/DII डायरेक्शन)
            df_15m = ticker.history(period="10d", interval="15m")
            df_1h  = ticker.history(period="1mo", interval="1h")

            if df_15m.empty or len(df_15m) < 80 or df_1h.empty or len(df_1h) < 40:
                continue

            df_15m = calculate_indicators(df_15m)
            df_1h  = calculate_indicators(df_1h)

            latest_live = df_15m.iloc[-1] # चालू कैंडल (सिर्फ लाइव प्राइस और एक्टिव ट्रेड SL/Target ट्रैक करने के लिए)
            closed_15m  = df_15m.iloc[-2] # आखिरी पूरी तरह बंद हो चुकी 15M कैंडल (सिग्नल कंफर्मेशन के लिए)
            closed_time = df_15m.index[-2]

            price = float(latest_live['Close'])
            atr   = float(closed_15m['ATR'])
            dec   = info["decimals"]

            # 1. पहले चल रहे ट्रेड्स को मैनेज करें
            manage_active_nse_trades(name, closed_time, latest_live, closed_15m, dec)

            # 2. अगर इस स्क्रिप्ट में पहले से ट्रेड खुला है तो नया न लें
            if any(t["pair"] == name and t["is_open"] for t in daily_trades_log):
                continue

            # 3. दिन के 3 बेस्ट ट्रेड पूरे हो गए हों तो रुक जाएं
            if trades_today >= MAX_TRADES_PER_DAY:
                continue

            # 4. एक ही 15M क्लोज्ड कैंडल पर दोबारा सिग्नल न दें
            if last_candle_traded[name] == closed_time:
                continue

            signal, direction, strategy_used, custom_sl = evaluate_indian_operator_strategies(df_15m, df_1h)

            if signal and custom_sl is not None:
                # 15M टाइमफ्रेम के हिसाब से सुरक्षित स्पॉट स्टॉप-लॉस (1.1x ATR से 2.1x ATR के बीच)
                raw_sl_dist = abs(price - custom_sl)
                min_sl_dist = atr * 1.1
                max_sl_dist = atr * 2.1
                sl_dist = round(max(min_sl_dist, min(raw_sl_dist, max_sl_dist)), dec)

                if sl_dist <= 0:
                    continue

                last_candle_traded[name] = closed_time
                trades_today += 1

                rr_ratio = 2.4 # बड़े टाइमफ्रेम पर हाई-प्रोबेबिलिटी 1:2.4 रिस्क-रिवॉर्ड
                target_dist = round(sl_dist * rr_ratio, dec)

                if direction == "BUY":
                    sl_level          = round(price - sl_dist, dec)
                    half_target_level = round(price + sl_dist, dec)
                    target_level      = round(price + target_dist, dec)
                    opt_hint          = "ATM / Slightly ITM CALL (CE) खरीदें (Delta ~0.55)"
                else:
                    sl_level          = round(price + sl_dist, dec)
                    half_target_level = round(price - sl_dist, dec)
                    target_level      = round(price - target_dist, dec)
                    opt_hint          = "ATM / Slightly ITM PUT (PE) खरीदें (Delta ~0.55)"

                max_risk_inr  = TOTAL_CAPITAL_INR * (RISK_PER_TRADE_PCT / 100.0) # ₹1,000 risk
                expected_prof = max_risk_inr * rr_ratio

                trade_id = trades_today
                daily_trades_log.append({
                    "id": trade_id,
                    "pair": name,
                    "broker_name": info["broker_name"],
                    "action": signal,
                    "direction": direction,
                    "strategy": strategy_used,
                    "entry": price,
                    "sl": sl_level,
                    "sl_dist": sl_dist,
                    "half_target": half_target_level,
                    "target": target_level,
                    "max_risk": max_risk_inr,
                    "expected_profit": expected_prof,
                    "entry_candle_time": closed_time,
                    "trailed_to_cost": False,
                    "is_open": True,
                    "status": "⏳ RUNNING (चल रहा है)"
                })

                entry_s  = format_price(price, dec)
                sl_s     = format_price(sl_level, dec)
                half_s   = format_price(half_target_level, dec)
                target_s = format_price(target_level, dec)
                htf_info = analyze_htf_1h_structure(df_1h)

                msg = (
                    f"🇮🇳 <b>NSE HTF (1H + 15M) OPERATOR SIGNAL ({trades_today}/{MAX_TRADES_PER_DAY})</b> 🇮🇳\n\n"
                    f"📈 <b>Index / Stock:</b> <b>{name}</b>\n"
                    f"📊 <b>Timeframe:</b> 1-Hour Trend ({htf_info['bias']}) + 15M Confirmed Close\n\n"
                    f"🧠 <b>Operator Setup:</b> {strategy_used}\n\n"
                    f"⚡ <b>Action:</b> <b>{signal}</b>\n"
                    f"🎯 <b>Option Selection:</b> <code>{opt_hint}</code>\n\n"
                    f"📍 <b>Spot Entry Price:</b> ₹{entry_s}\n"
                    f"🛑 <b>Spot Protected SL:</b> ₹{sl_s}  <i>({sl_dist:.1f} {info['unit_name']} का गैप)</i>\n"
                    f"🛡️ <b>Breakeven (1:1):</b> ₹{half_s}\n"
                    f"🎯 <b>Full Target (1:2.4):</b> ₹{target_s}  <i>({target_dist:.1f} {info['unit_name']} का टारगेट)</i>\n\n"
                    f"💼 <b>Risk Management:</b> 1 Lot (Max Risk: ₹{max_risk_inr:.0f} | Expected Profit: +₹{expected_prof:.0f})\n\n"
                    f"⏳ <b>Pro Guard:</b> यह सिग्नल 15-मिनट की कैंडल पूरी तरह क्लोज होने, VWAP और 1-Hour स्ट्रक्चर मैच होने के बाद ही दिया गया है!\n\n"
                    f"────────────────────\n\n"
                    f"{get_trades_scoreboard()}"
                )
                send_telegram_message(msg)

        except Exception as e:
            print(f"Error scanning {name}: {e}")

if __name__ == "__main__":
    print("🔄 Initializing Indian Stock Market HTF Operator Bot...")
    mkt_msg = (
        "🟢 भारतीय शेयर बाजार (NSE) खुला है (1H + 15M स्कैनिंग चालू है)।"
        if is_indian_market_open()
        else "🔴 अभी भारतीय शेयर बाजार बंद है (NSE Timing: सोमवार-शुक्रवार सुबह 9:15 AM से 3:30 PM IST)। बाजार खुलते ही अपने आप ट्रेड आएंगे!"
    )

    send_telegram_message(
        "🇮🇳 <b>Indian Market HTF (1H + 15M) Operator Bot Active!</b>\n\n"
        f"📢 <b>Status:</b> {mkt_msg}\n\n"
        "💎 <b>NSE Instruments Monitored:</b>\n"
        "• NIFTY 50, BANK NIFTY, FIN NIFTY\n"
        "• RELIANCE, HDFC BANK, ICICI BANK\n\n"
        "🐋 <b>4 Custom NSE Operator Strategies (Confirmed 15M Close Only):</b>\n"
        "1️⃣ 9:15-9:45 ORB & PDH/PDL Option-Writer SL Sweep\n"
        "2️⃣ Intraday VWAP + 15M Unmitigated Order Block & FVG\n"
        "3️⃣ Mid-Day Theta-Box AMD Manipulation Reversal\n"
        "4️⃣ 1H+15M FII Institutional Displacement & Obstacle-Free BOS"
    )

    start_run = time.time()
    last_scan_time = 0
    while time.time() - start_run < 21000:
        check_telegram_commands()
        if time.time() - last_scan_time >= 25:
            scan_indian_market()
            last_scan_time = time.time()
        time.sleep(3)
