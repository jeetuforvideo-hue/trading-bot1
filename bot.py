import os
import time
import requests
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone

# =====================================================================
# 1. आपकी पर्सनल और सिक्योर सेटिंग्स
# =====================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "9166955310:AAFQzHViDc6nwjC1QAgKDEOzPeDfq5PK2_U")
CHAT_ID   = "5610520408"  # सिर्फ आपकी टेलीग्राम आईडी पर काम करेगा

TOTAL_CAPITAL      = 100000
RISK_PER_TRADE_PCT = 1.0
MAX_TRADES_PER_DAY = 3
ADX_THRESHOLD      = 20

# प्रो-ट्रेडर नियम सेटिंग्स
MAX_HOLD_MINUTES   = 30   # 30 मिनट में मूव नहीं आया तो प्रीमियम डीके से बचने के लिए एग्जिट

INDICES = {
    "NIFTY 50": {"symbol": "^NSEI", "lot_size": 75, "kite_name": "NIFTY"},
    "BANK NIFTY": {"symbol": "^NSEBANK", "lot_size": 30, "kite_name": "BANKNIFTY"},
    "SENSEX": {"symbol": "^BSESN", "lot_size": 20, "kite_name": "SENSEX"},
    "MIDCAP NIFTY": {"symbol": "NIFTY_MID_SELECT.NS", "lot_size": 120, "kite_name": "MIDCPNIFTY"}
}

last_candle_traded = {name: None for name in INDICES}
trades_today = 0
last_update_id = 0
daily_trades_log = []

def get_ist_now():
    return datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)

current_day = get_ist_now().date()

def is_market_open():
    now = get_ist_now()
    if now.weekday() >= 5:
        return False
    market_start = now.replace(hour=9, minute=15, second=0, microsecond=0)
    market_end   = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return market_start <= now <= market_end

def is_no_new_entry_time():
    """सुबह 9:20 से पहले (भारी उतार-चढ़ाव) और दोपहर 2:45 के बाद (प्रीमियम डीके) नया ट्रेड न लें"""
    now = get_ist_now().time()
    early_morning = datetime.strptime("09:20", "%H:%M").time()
    late_afternoon = datetime.strptime("14:45", "%H:%M").time()
    lunch_start = datetime.strptime("12:00", "%H:%M").time()
    lunch_end   = datetime.strptime("13:15", "%H:%M").time()
    
    if now < early_morning or now > late_afternoon:
        return True
    if lunch_start <= now <= lunch_end:
        return True
    return False

def send_telegram_message(message, target_chat_id=CHAT_ID):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": target_chat_id, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram Send Error: {e}")

def get_trades_scoreboard():
    if not daily_trades_log:
        return "📋 <b>आज के ट्रेड्स का हाल:</b>\n\n• अभी कोई ट्रेड नहीं लिया गया है।"
    board = "📋 <b>आज के तीनों ट्रेड्स का रिजल्ट:</b>\n\n"
    for t in daily_trades_log:
        board += f"👉 <b>Trade #{t['id']} ({t['strike']}):</b>\n   {t['status']}\n\n"
    return board.strip()

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
                    if chat_id:
                        send_telegram_message(
                            "⛔ <b>Access Denied:</b> यह एक प्राइवेट पर्सनल ट्रेडिंग बॉट है।",
                            target_chat_id=chat_id
                        )
                    continue

                if msg_text in ["/status", "status", "/start", "start"]:
                    send_live_market_summary()
    except Exception:
        pass

def calculate_indicators(df):
    df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()
    df['EMA50'] = df['Close'].ewm(span=50, adjust=False).mean()

    tp = (df['High'] + df['Low'] + df['Close']) / 3
    if df['Volume'].sum() > 0:
        df['VWAP'] = (tp * df['Volume']).cumsum() / df['Volume'].cumsum()
    else:
        df['VWAP'] = tp.rolling(window=20).mean()

    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))

    ema12 = df['Close'].ewm(span=12, adjust=False).mean()
    ema26 = df['Close'].ewm(span=26, adjust=False).mean()
    df['MACD'] = ema12 - ema26
    df['MACD_Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()

    df['BB_Mid'] = df['Close'].rolling(window=20).mean()
    df['BB_Std'] = df['Close'].rolling(window=20).std()
    df['BB_Upper'] = df['BB_Mid'] + (2 * df['BB_Std'])
    df['BB_Lower'] = df['BB_Mid'] - (2 * df['BB_Std'])

    hl = df['High'] - df['Low']
    hc = np.abs(df['High'] - df['Close'].shift())
    lc = np.abs(df['Low'] - df['Close'].shift())
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    df['ATR'] = tr.rolling(14).mean()

    up_move = df['High'] - df['High'].shift(1)
    down_move = df['Low'].shift(1) - df['Low']
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr_smooth = tr.rolling(14).mean()
    plus_di = 100 * (pd.Series(plus_dm, index=df.index).rolling(14).mean() / tr_smooth)
    minus_di = 100 * (pd.Series(minus_dm, index=df.index).rolling(14).mean() / tr_smooth)
    dx = (np.abs(plus_di - minus_di) / (plus_di + minus_di)) * 100
    df['ADX'] = dx.rolling(14).mean()
    return df

def is_strong_candle(latest, direction):
    """फेक ब्रेकआउट से बचने का नियम: कैंडल में लंबी सुई (Rejection Wick) नहीं होनी चाहिए"""
    high_p = float(latest['High'])
    low_p  = float(latest['Low'])
    open_p = float(latest['Open'])
    close_p = float(latest['Close'])
    
    total_range = high_p - low_p
    if total_range == 0:
        return False
        
    body = abs(close_p - open_p)
    # कैंडल की बॉडी कम से कम पूरे साइज की 45% होनी चाहिए (Doji या पिनबार न हो)
    if (body / total_range) < 0.45:
        return False

    if direction == "CALL":
        # CALL के लिए क्लोजिंग ऊपरी हिस्से में हो (ऊपर से सेलिंग प्रेशर न हो)
        upper_wick = high_p - close_p
        return close_p > open_p and (upper_wick / total_range) < 0.30
    else:
        # PUT के लिए क्लोजिंग निचले हिस्से में हो (नीचे से बाइंग प्रेशर न हो)
        lower_wick = close_p - low_p
        return close_p < open_p and (lower_wick / total_range) < 0.30

def evaluate_multi_strategies(df):
    latest = df.iloc[-1]
    prev   = df.iloc[-2]

    price    = float(latest['Close'])
    open_p   = float(latest['Open'])
    high_p   = float(latest['High'])
    low_p    = float(latest['Low'])
    ema20    = float(latest['EMA20'])
    ema50    = float(latest['EMA50'])
    vwap     = float(latest['VWAP'])
    rsi      = float(latest['RSI'])
    macd     = float(latest['MACD'])
    macd_sig = float(latest['MACD_Signal'])
    bb_upper = float(latest['BB_Upper'])
    bb_lower = float(latest['BB_Lower'])
    atr      = float(latest['ATR'])

    # अगर कैंडल बहुत ही छोटी (मरी हुई) है तो ट्रेड न लें
    if (high_p - low_p) < (atr * 0.5):
        return None, None, None

    today_date = df.index[-1].date()
    today_df = df[df.index.date == today_date]
    orb_high, orb_low = None, None
    if len(today_df) >= 4:
        first_15m = today_df.iloc[:3]
        orb_high = float(first_15m['High'].max())
        orb_low  = float(first_15m['Low'].min())

    # 1. Trend + MACD Breakout (फेक ब्रेकआउट फिल्टर के साथ)
    if price > vwap and price > ema20 and ema20 > ema50 and 56 < rsi < 70 and macd > macd_sig:
        if is_strong_candle(latest, "CALL"):
            return "BUY CALL (CE)", "CALL", "Trend + MACD Strong Breakout"
    if price < vwap and price < ema20 and ema20 < ema50 and 30 < rsi < 44 and macd < macd_sig:
        if is_strong_candle(latest, "PUT"):
            return "BUY PUT (PE)", "PUT", "Downtrend + MACD Breakdown"

    # 2. Morning 15-Min ORB Breakout
    if orb_high and orb_low:
        if prev['Close'] <= orb_high and price > orb_high and price > vwap and rsi > 58:
            if is_strong_candle(latest, "CALL"):
                return "BUY CALL (CE)", "CALL", "Morning 15-Min High Breakout"
        if prev['Close'] >= orb_low and price < orb_low and price < vwap and rsi < 42:
            if is_strong_candle(latest, "PUT"):
                return "BUY PUT (PE)", "PUT", "Morning 15-Min Low Breakdown"

    # 3. Bollinger Band Fast Momentum
    if prev['Close'] <= prev['BB_Upper'] and price > bb_upper and rsi > 60 and macd > macd_sig:
        if is_strong_candle(latest, "CALL"):
            return "BUY CALL (CE)", "CALL", "Bollinger Band Fast Breakout"
    if prev['Close'] >= prev['BB_Lower'] and price < bb_lower and rsi < 40 and macd < macd_sig:
        if is_strong_candle(latest, "PUT"):
            return "BUY PUT (PE)", "PUT", "Bollinger Band Fast Breakdown"

    # 4. 20 EMA + VWAP Sniper Pullback
    if ema20 > ema50 and price > vwap and low_p <= ema20 and price > ema20 and 52 < rsi < 66:
        if is_strong_candle(latest, "CALL"):
            return "BUY CALL (CE)", "CALL", "20 EMA + VWAP Support Bounce"
    if ema20 < ema50 and price < vwap and high_p >= ema20 and price < ema20 and 34 < rsi < 48:
        if is_strong_candle(latest, "PUT"):
            return "BUY PUT (PE)", "PUT", "20 EMA + VWAP Resistance Drop"

    return None, None, None

def send_live_market_summary():
    market_state = "🟢 मार्केट अभी खुला है (LIVE)" if is_market_open() else "🔴 मार्केट अभी बंद है (CLOSED)"
    report = f"📊 <b>PRO TRADER STATUS REPORT</b> 📊\n\n📢 <b>Status:</b> {market_state}\n\n"
    
    for name, info in INDICES.items():
        try:
            df = yf.Ticker(info["symbol"]).history(period="5d", interval="5m")
            if df.empty or len(df) < 50:
                continue
            df = calculate_indicators(df)
            latest = df.iloc[-1]
            price = float(latest['Close'])
            rsi = float(latest['RSI'])
            adx = float(latest['ADX']) if not np.isnan(latest['ADX']) else 15.0
            mood = "⚠️ साइडवेज (No Trade)" if adx < ADX_THRESHOLD else "🔥 ट्रेंडिंग (Fast Move)"
            
            report += f"🔹 <b>{name}:</b> {price:.0f}\n   • मूड: {mood} (RSI: {rsi:.0f})\n\n"
        except Exception:
            report += f"🔹 <b>{name}:</b> लोड हो रहा है...\n\n"
            
    report += f"🛡️ <b>आज के कुल ट्रेड:</b> {trades_today} / {MAX_TRADES_PER_DAY}\n\n"
    report += get_trades_scoreboard()
    send_telegram_message(report)

def manage_active_trades_like_pro(index_name, candle_time, latest):
    """प्रोफेशनल ट्रेडर की तरह चलते हुए ट्रेड को मैनेज करने वाला फंक्शन"""
    price       = float(latest['Close'])
    current_high = float(latest['High'])
    current_low  = float(latest['Low'])
    ema20       = float(latest['EMA20'])
    vwap        = float(latest['VWAP'])
    adx         = float(latest['ADX']) if not np.isnan(latest['ADX']) else 15.0
    now_time    = get_ist_now().time()
    square_off_time = datetime.strptime("15:15", "%H:%M").time()

    for trade in daily_trades_log:
        if trade["index"] == index_name and trade["is_open"]:
            if candle_time <= trade["entry_candle_time"]:
                continue

            # -------------------------------------------------------------
            # 1. TARGET या STOP-LOSS चेक करें
            # -------------------------------------------------------------
            hit_type = None
            if trade["direction"] == "CALL":
                if current_high >= trade["target"]:
                    hit_type = "TARGET"
                elif current_low <= trade["sl"]:
                    hit_type = "TRAILING_SL" if trade["trailed_to_cost"] else "SL"
            elif trade["direction"] == "PUT":
                if current_low <= trade["target"]:
                    hit_type = "TARGET"
                elif current_high >= trade["sl"]:
                    hit_type = "TRAILING_SL" if trade["trailed_to_cost"] else "SL"

            if hit_type == "TARGET":
                trade["is_open"] = False
                trade["status"] = f"✅ TARGET HIT (+₹{trade['expected_profit']:.0f} प्रॉफिट)"
                send_telegram_message(
                    f"🎯 <b>TRADE #{trade['id']} : FULL TARGET HIT!</b> 🎯\n\n"
                    f"📊 <b>Index:</b> {trade['index']}\n\n"
                    f"⚡ <b>Trade:</b> {trade['strike']}\n\n"
                    f"📍 <b>Entry:</b> {trade['entry']:.0f}  ➡️  <b>Target Hit:</b> {trade['target']:.0f}\n\n"
                    f"💰 <b>Result:</b> <b>✅ +₹{trade['expected_profit']:.0f} अनुमानित प्रॉफिट</b>\n\n"
                    f"📢 <b>प्रो सलाह:</b> पूरा प्रॉफिट बुक करके ट्रेड बंद कर दें!\n\n"
                    f"────────────────────\n\n{get_trades_scoreboard()}"
                )
                continue

            elif hit_type == "TRAILING_SL":
                trade["is_open"] = False
                trade["status"] = "🛡️ COST-TO-COST EXIT (₹0 No Loss)"
                send_telegram_message(
                    f"🛡️ <b>TRADE #{trade['id']} : TRAILING SL HIT (NO LOSS)!</b> 🛡️\n\n"
                    f"📊 <b>Index:</b> {trade['index']} ({trade['strike']})\n\n"
                    f"📢 <b>प्रो अपडेट:</b> मार्केट वापस पलट गया, लेकिन हमने स्टॉप-लॉस कॉस्ट पर कर लिया था इसलिए <b>आपकी जेब से ₹0 का नुकसान हुआ!</b>\n\n"
                    f"────────────────────\n\n{get_trades_scoreboard()}"
                )
                continue

            elif hit_type == "SL":
                trade["is_open"] = False
                trade["status"] = f"🛑 STOP-LOSS HIT (-₹{trade['max_risk']:.0f} लॉस)"
                send_telegram_message(
                    f"🛑 <b>TRADE #{trade['id']} : STOP-LOSS HIT!</b> 🛑\n\n"
                    f"📊 <b>Index:</b> {trade['index']} ({trade['strike']})\n\n"
                    f"📍 <b>Entry:</b> {trade['entry']:.0f}  ➡️  <b>SL Hit:</b> {trade['sl']:.0f}\n\n"
                    f"⚠️ <b>Result:</b> <b>🛑 -₹{trade['max_risk']:.0f} लॉस</b>\n\n"
                    f"📢 <b>प्रो सलाह:</b> तुरंत बाहर निकल जाएं, स्टॉप-लॉस को कभी बड़ा न करें।\n\n"
                    f"────────────────────\n\n{get_trades_scoreboard()}"
                )
                continue

            # -------------------------------------------------------------
            # 2. प्रो नियम: 1:1 (50% टारगेट) आने पर SL को कॉस्ट (Entry) पर लाना
            # -------------------------------------------------------------
            if not trade["trailed_to_cost"]:
                half_target_reached = False
                if trade["direction"] == "CALL" and current_high >= trade["half_target"]:
                    half_target_reached = True
                elif trade["direction"] == "PUT" and current_low <= trade["half_target"]:
                    half_target_reached = True

                if half_target_reached:
                    trade["trailed_to_cost"] = True
                    trade["sl"] = trade["entry"]  # अब स्टॉप-लॉस एंट्री प्राइस पर आ गया!
                    trade["status"] = "🔥 RUNNING (SL at Cost - Risk Free)"
                    send_telegram_message(
                        f"🛡️ <b>PRO UPDATE (Trade #{trade['id']}) : TRAIL SL TO COST!</b> 🛡️\n\n"
                        f"📊 <b>Index:</b> {trade['index']} ({trade['strike']})\n\n"
                        f"✅ <b>50% टारगेट पूरा हो चुका है! (Spot: {price:.0f})</b>\n\n"
                        f"📢 <b>प्रो एक्शन:</b> अब अपने स्टॉप-लॉस को उठाकर <b>खरीद भाव (Cost-to-Cost: {trade['entry']:.0f})</b> पर लगा लें या आधा प्रॉफिट बुक कर लें। अब इस ट्रेड में नुकसान का रिस्क खत्म!"
                    )

            # -------------------------------------------------------------
            # 3. प्रो नियम: प्रीमियम डीके और साइडवेज मार्केट एग्जिट (30 मिनट रूल)
            # -------------------------------------------------------------
            hold_minutes = (candle_time - trade["entry_candle_time"]).total_seconds() / 60.0
            if hold_minutes >= MAX_HOLD_MINUTES and not trade["trailed_to_cost"]:
                # अगर 30 मिनट हो गए और ट्रेड अभी तक 50% टारगेट भी नहीं छू पाया या मार्केट साइडवेज हो गया
                if adx < ADX_THRESHOLD or abs(price - trade["entry"]) < (trade["sl_points"] * 0.6):
                    trade["is_open"] = False
                    pnl_pts = (price - trade["entry"]) if trade["direction"] == "CALL" else (trade["entry"] - price)
                    exit_tag = "छोटा प्रॉफिट/कॉस्ट" if pnl_pts >= 0 else "छोटा लॉस"
                    trade["status"] = f"⏳ TIME DECAY EXIT ({exit_tag} पर एग्जिट)"
                    
                    send_telegram_message(
                        f"⏳ <b>PRO ALERT (Trade #{trade['id']}) : PREMIUM DECAY EXIT!</b> ⏳\n\n"
                        f"📊 <b>Index:</b> {trade['index']} ({trade['strike']})\n\n"
                        f"📍 <b>Entry:</b> {trade['entry']:.0f}  ➡️  <b>Current Spot:</b> {price:.0f}\n\n"
                        f"⚠️ <b>कारण:</b> ट्रेड लिए 30 मिनट हो चुके हैं और मार्केट बिल्कुल <b>साइडवेज (सुस्त)</b> पड़ गया है।\n\n"
                        f"📢 <b>प्रो सलाह:</b> ऑप्शन बाइंग में साइडवेज मार्केट प्रीमियम गला देता है (Theta Decay)। स्टॉप-लॉस का इंतजार मत कीजिए, <b>अभी तुरंत जो भी भाव मिल रहा है लेकर ट्रेड से बाहर निकल जाएं!</b>\n\n"
                        f"────────────────────\n\n{get_trades_scoreboard()}"
                    )
                    continue

            # -------------------------------------------------------------
            # 4. प्रो नियम: अचानक ट्रेंड पलट जाए (EMA20 + VWAP दोनों टूट जाएं)
            # -------------------------------------------------------------
            if hold_minutes >= 10:
                trend_broken = False
                if trade["direction"] == "CALL" and price < ema20 and price < vwap:
                    trend_broken = True
                elif trade["direction"] == "PUT" and price > ema20 and price > vwap:
                    trend_broken = True

                if trend_broken:
                    trade["is_open"] = False
                    trade["status"] = "🔄 TREND REVERSAL EXIT (जल्दी बचाव)"
                    send_telegram_message(
                        f"🔄 <b>PRO ALERT (Trade #{trade['id']}) : TREND REVERSAL EXIT!</b> 🔄\n\n"
                        f"📊 <b>Index:</b> {trade['index']} ({trade['strike']})\n\n"
                        f"📍 <b>Entry:</b> {trade['entry']:.0f}  ➡️  <b>Current Spot:</b> {price:.0f}\n\n"
                        f"⚠️ <b>कारण:</b> मार्केट का ट्रेंड अचानक पलट गया है (20 EMA और VWAP दोनों टूट गए हैं)।\n\n"
                        f"📢 <b>प्रो सलाह:</b> पूरा स्टॉप-लॉस देने से बेहतर है कि अभी तुरंत ट्रेड से एग्जिट कर लें!\n\n"
                        f"────────────────────\n\n{get_trades_scoreboard()}"
                    )
                    continue

            # -------------------------------------------------------------
            # 5. दोपहर 3:15 पर ऑटो इंट्राडे स्क्वायर-ऑफ
            # -------------------------------------------------------------
            if now_time >= square_off_time:
                trade["is_open"] = False
                trade["status"] = "🕒 3:15 PM AUTO EXIT (मार्केट क्लोजिंग)"
                send_telegram_message(
                    f"🕒 <b>3:15 PM INTRADAY EXIT (Trade #{trade['id']})</b> 🕒\n\n"
                    f"📢 <b>सलाह:</b> मार्केट बंद होने वाला है। अपना `{trade['strike']}` ट्रेड तुरंत बंद कर लें।"
                )

def scan_market():
    global trades_today, current_day, last_candle_traded, daily_trades_log

    if not is_market_open():
        return

    today = get_ist_now().date()
    if today != current_day:
        current_day = today
        trades_today = 0
        daily_trades_log = []
        last_candle_traded = {name: None for name in INDICES}

    for name, info in INDICES.items():
        try:
            df = yf.Ticker(info["symbol"]).history(period="5d", interval="5m")
            if df.empty or len(df) < 50:
                continue

            df = calculate_indicators(df)
            latest = df.iloc[-1]
            candle_time = df.index[-1]

            price  = float(latest['Close'])
            atr    = float(latest['ATR'])
            adx    = float(latest['ADX']) if not np.isnan(latest['ADX']) else 15.0

            # 1. पहले चल रहे ट्रेड को प्रो ट्रेडर की तरह मैनेज करें (SL, Target, Trailing, Time Decay)
            manage_active_trades_like_pro(name, candle_time, latest)

            # 2. अगर इस इंडेक्स में पहले से ट्रेड चल रहा है तो नया न लें
            if any(t["index"] == name and t["is_open"] for t in daily_trades_log):
                continue

            # 3. दिन के 3 ट्रेड पूरे हो गए हों तो रुक जाएं
            if trades_today >= MAX_TRADES_PER_DAY:
                continue

            # 4. एक ही कैंडल पर दोबारा ट्रेड न लें
            if last_candle_traded[name] == candle_time:
                continue

            # 5. प्रो नियम: अगर मार्केट साइडवेज है (ADX < 20) या खराब समय है, तो ऑप्शन बाइंग बिल्कुल न करें!
            if adx < ADX_THRESHOLD or is_no_new_entry_time():
                continue

            signal, direction, strategy_used = evaluate_multi_strategies(df)

            if signal:
                last_candle_traded[name] = candle_time
                trades_today += 1

                sl_points = round(atr * 1.2, 0)
                rr_ratio = 2.2
                target_points = round(sl_points * rr_ratio, 0)

                if direction == "CALL":
                    sl_level = price - sl_points
                    half_target_level = price + sl_points
                    target_level = price + target_points
                else:
                    sl_level = price + sl_points
                    half_target_level = price - sl_points
                    target_level = price - target_points

                step = 100 if name in ["BANK NIFTY", "SENSEX"] else 50
                atm_strike = round(price / step) * step
                opt_type = "CE" if direction == "CALL" else "PE"
                kite_search = f"{info['kite_name']} {atm_strike} {opt_type}"

                max_risk_rs = TOTAL_CAPITAL * (RISK_PER_TRADE_PCT / 100)
                est_opt_sl = max(5, round(sl_points * 0.5, 0))
                risk_per_lot = est_opt_sl * info["lot_size"]
                lots = max(1, int(max_risk_rs // risk_per_lot))

                total_risk = lots * risk_per_lot
                expected_prof = total_risk * rr_ratio

                trade_id = trades_today
                daily_trades_log.append({
                    "id": trade_id,
                    "index": name,
                    "action": signal,
                    "direction": direction,
                    "strategy": strategy_used,
                    "strike": kite_search,
                    "entry": price,
                    "sl": sl_level,
                    "sl_points": sl_points,
                    "half_target": half_target_level,
                    "target": target_level,
                    "max_risk": total_risk,
                    "expected_profit": expected_prof,
                    "entry_candle_time": candle_time,
                    "trailed_to_cost": False,
                    "is_open": True,
                    "status": "⏳ RUNNING (चल रहा है)"
                })

                msg = (
                    f"🚨 <b>PRO TRADE SIGNAL (Trade {trades_today} of {MAX_TRADES_PER_DAY})</b> 🚨\n\n"
                    f"📢 <b>Market Mood:</b> तेज मोमेंटम (Strong Breakout)\n\n"
                    f"📊 <b>Index:</b> {name}\n\n"
                    f"🧠 <b>Setup:</b> {strategy_used}\n\n"
                    f"⚡ <b>Action:</b> <b>{signal}</b>\n\n"
                    f"🔍 <b>Zerodha Search:</b> <code>{kite_search}</code>\n\n"
                    f"📍 <b>Spot Entry:</b> {price:.0f}\n\n"
                    f"🛑 <b>Stop-Loss:</b> {sl_level:.0f} (<i>-{sl_points:.0f} pts</i>)\n\n"
                    f"🛡️ <b>Safe Trail Level (1:1):</b> {half_target_level:.0f}\n\n"
                    f"🎯 <b>Full Target:</b> {target_level:.0f} (<i>+{target_points:.0f} pts</i>)\n\n"
                    f"💼 <b>Lot & Risk:</b> {lots} Lot | Max Risk: ₹{total_risk:.0f}\n\n"
                    f"⏳ <b>Pro Active:</b> अगर 30 मिनट में मूव नहीं आया या ट्रेंड पलटा, तो बॉट खुद पहले एग्जिट करा देगा!\n\n"
                    f"────────────────────\n\n"
                    f"{get_trades_scoreboard()}"
                )
                send_telegram_message(msg)

        except Exception as e:
            print(f"Error scanning {name}: {e}")

if __name__ == "__main__":
    mkt_msg = "🟢 मार्केट अभी खुला है, प्रो स्कैनिंग शुरू है।" if is_market_open() else "🔴 अभी मार्केट बंद है। सोमवार सुबह 9:15 AM से अपने आप ट्रेड आएंगे।"
    
    send_telegram_message(
        "🤖 <b>MyTrader PRO Bot Active!</b>\n\n"
        f"📢 <b>Status:</b> {mkt_msg}\n\n"
        "✅ <b>Pro Rules Active:</b>\n"
        "• फेक ब्रेकआउट फिल्टर (Wick Check)\n"
        "• 30-Min प्रीमियम डीके एग्जिट\n"
        "• 1:1 ऑटो ट्रेलिंग स्टॉप-लॉस\n"
        "• ट्रेंड रिवर्सल अर्ली एग्जिट"
    )
    
    start_run = time.time()
    last_scan_time = 0
    while time.time() - start_run < 19800:
        check_telegram_commands()
        if time.time() - last_scan_time >= 15:
            scan_market()
            last_scan_time = time.time()
        time.sleep(3)
