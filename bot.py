import os
import time
import requests
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone

# =====================================================================
# 1. अपनी टेलीग्राम डिटेल्स यहाँ डालें (Chat ID पहले से डली हुई है)
# =====================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "9166955310:AAFQzHViDc6nwjC1QAgKDEOzPeDfq5PK2_U")
CHAT_ID   = "5610520408"

TOTAL_CAPITAL      = 100000   # आपकी कुल पूंजी (₹1,00,000)
RISK_PER_TRADE_PCT = 1.0      # एक ट्रेड में सिर्फ 1% रिस्क (₹1,000)
MAX_TRADES_PER_DAY = 3        # पूरे दिन में अधिकतम 3 ट्रेड
ADX_THRESHOLD      = 20       # 20 से नीचे मतलब साइडवेज मार्केट

INDICES = {
    "NIFTY 50": {"symbol": "^NSEI", "lot_size": 75, "kite_name": "NIFTY"},
    "BANK NIFTY": {"symbol": "^NSEBANK", "lot_size": 30, "kite_name": "BANKNIFTY"},
    "SENSEX": {"symbol": "^BSESN", "lot_size": 20, "kite_name": "SENSEX"},
    "MIDCAP NIFTY": {"symbol": "NIFTY_MID_SELECT.NS", "lot_size": 120, "kite_name": "MIDCPNIFTY"}
}

# सिस्टम मेमोरी
last_candle_traded = {name: None for name in INDICES}
trades_today = 0
last_update_id = 0
daily_trades_log = []

def get_ist_now():
    """भारतीय समय (IST) निकालने का फंक्शन"""
    return datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)

current_day = get_ist_now().date()

def is_market_open():
    """चेक करेगा कि क्या अभी असली मार्केट खुला है (सोम-शुक्र, 9:15 AM से 3:30 PM)"""
    now = get_ist_now()
    # 5 = Saturday, 6 = Sunday
    if now.weekday() >= 5:
        return False
    market_start = now.replace(hour=9, minute=15, second=0, microsecond=0)
    market_end   = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return market_start <= now <= market_end

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram Send Error: {e}")

def get_trades_scoreboard():
    """तीनों ट्रेड्स का साफ और खुला-खुला स्कोरबोर्ड"""
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
                msg_text = result.get("message", {}).get("text", "").strip().lower()
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

def is_lunch_chop_time():
    now = get_ist_now().time()
    start_chop = datetime.strptime("12:00", "%H:%M").time()
    end_chop = datetime.strptime("13:15", "%H:%M").time()
    return start_chop <= now <= end_chop

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

    today_date = df.index[-1].date()
    today_df = df[df.index.date == today_date]
    orb_high, orb_low = None, None
    if len(today_df) >= 4:
        first_15m = today_df.iloc[:3]
        orb_high = float(first_15m['High'].max())
        orb_low  = float(first_15m['Low'].min())

    if price > vwap and price > ema20 and ema20 > ema50 and 56 < rsi < 72 and macd > macd_sig:
        return "BUY CALL (CE)", "CALL", "Trend + MACD Breakout"
    if price < vwap and price < ema20 and ema20 < ema50 and 28 < rsi < 44 and macd < macd_sig:
        return "BUY PUT (PE)", "PUT", "Downtrend + MACD Breakdown"

    if orb_high and orb_low:
        if prev['Close'] <= orb_high and price > orb_high and price > vwap and rsi > 58:
            return "BUY CALL (CE)", "CALL", "Morning 15-Min High Breakout"
        if prev['Close'] >= orb_low and price < orb_low and price < vwap and rsi < 42:
            return "BUY PUT (PE)", "PUT", "Morning 15-Min Low Breakdown"

    if prev['Close'] <= prev['BB_Upper'] and price > bb_upper and rsi > 60 and macd > macd_sig and price > open_p:
        return "BUY CALL (CE)", "CALL", "Bollinger Band Fast Breakout"
    if prev['Close'] >= prev['BB_Lower'] and price < bb_lower and rsi < 40 and macd < macd_sig and price < open_p:
        return "BUY PUT (PE)", "PUT", "Bollinger Band Fast Breakdown"

    if ema20 > ema50 and price > vwap and low_p <= ema20 and price > ema20 and price > open_p and 52 < rsi < 68:
        return "BUY CALL (CE)", "CALL", "20 EMA + VWAP Support Bounce"
    if ema20 < ema50 and price < vwap and high_p >= ema20 and price < ema20 and price < open_p and 32 < rsi < 48:
        return "BUY PUT (PE)", "PUT", "20 EMA + VWAP Resistance Drop"

    return None, None, None

def send_live_market_summary():
    market_state = "🟢 मार्केट अभी खुला है (LIVE)" if is_market_open() else "🔴 मार्केट अभी बंद है (CLOSED)"
    report = f"📊 <b>MARKET STATUS REPORT</b> 📊\n\n📢 <b>Status:</b> {market_state}\n\n"
    
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
            mood = "⚠️ साइडवेज (Range)" if adx < ADX_THRESHOLD else "🔥 ट्रेंडिंग (Fast Move)"
            
            report += (
                f"🔹 <b>{name}:</b> {price:.0f}\n"
                f"   • मूड: {mood} (RSI: {rsi:.0f})\n\n"
            )
        except Exception:
            report += f"🔹 <b>{name}:</b> लोड हो रहा है...\n\n"
            
    report += f"🛡️ <b>आज के कुल ट्रेड:</b> {trades_today} / {MAX_TRADES_PER_DAY}\n\n"
    report += get_trades_scoreboard()
    send_telegram_message(report)

def check_active_trades_sl_target(index_name, candle_time, current_high, current_low):
    """सिर्फ नई कैंडल आने पर ही चेक करेगा कि चल रहे ट्रेड का टारगेट या स्टॉप-लॉस हिट हुआ या नहीं"""
    for trade in daily_trades_log:
        if trade["index"] == index_name and trade["is_open"]:
            # जिस कैंडल पर एंट्री हुई है, उसी कैंडल पर तुरंत SL/Target हिट न माने (अगली कैंडल से चेक करे)
            if candle_time <= trade["entry_candle_time"]:
                continue

            hit_type = None
            if trade["direction"] == "CALL":
                if current_high >= trade["target"]:
                    hit_type = "TARGET"
                elif current_low <= trade["sl"]:
                    hit_type = "SL"
            elif trade["direction"] == "PUT":
                if current_low <= trade["target"]:
                    hit_type = "TARGET"
                elif current_high >= trade["sl"]:
                    hit_type = "SL"

            if hit_type == "TARGET":
                trade["is_open"] = False
                trade["status"] = f"✅ TARGET HIT (+₹{trade['expected_profit']:.0f} प्रॉफिट)"
                
                update_msg = (
                    f"🎯 <b>TRADE #{trade['id']} : TARGET HIT!</b> 🎯\n\n"
                    f"📊 <b>Index:</b> {trade['index']}\n\n"
                    f"⚡ <b>Trade:</b> {trade['strike']}\n\n"
                    f"📍 <b>Entry:</b> {trade['entry']:.0f}  ➡️  <b>Target Hit:</b> {trade['target']:.0f}\n\n"
                    f"💰 <b>Result:</b> <b>✅ TARGET HIT (+₹{trade['expected_profit']:.0f} प्रॉफिट)</b>\n\n"
                    f"📢 <b>सलाह:</b> बहुत बढ़िया! अपना प्रॉफिट बुक कर लें।\n\n"
                    f"────────────────────\n\n"
                    f"{get_trades_scoreboard()}"
                )
                send_telegram_message(update_msg)

            elif hit_type == "SL":
                trade["is_open"] = False
                trade["status"] = f"🛑 STOP-LOSS HIT (-₹{trade['max_risk']:.0f} लॉस)"
                
                update_msg = (
                    f"🛑 <b>TRADE #{trade['id']} : STOP-LOSS HIT!</b> 🛑\n\n"
                    f"📊 <b>Index:</b> {trade['index']}\n\n"
                    f"⚡ <b>Trade:</b> {trade['strike']}\n\n"
                    f"📍 <b>Entry:</b> {trade['entry']:.0f}  ➡️  <b>SL Hit:</b> {trade['sl']:.0f}\n\n"
                    f"⚠️ <b>Result:</b> <b>🛑 STOP-LOSS HIT (-₹{trade['max_risk']:.0f} लॉस)</b>\n\n"
                    f"📢 <b>सलाह:</b> स्टॉप-लॉस हिट हो गया है, तुरंत ट्रेड से बाहर निकल जाएं।\n\n"
                    f"────────────────────\n\n"
                    f"{get_trades_scoreboard()}"
                )
                send_telegram_message(update_msg)

def scan_market():
    global trades_today, current_day, last_candle_traded, daily_trades_log

    # अगर मार्केट बंद है (शनिवार/रविवार या 9:15 से पहले / 3:30 के बाद), तो कोई ट्रेड न ले
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
            high_p = float(latest['High'])
            low_p  = float(latest['Low'])
            atr    = float(latest['ATR'])
            adx    = float(latest['ADX']) if not np.isnan(latest['ADX']) else 15.0

            # 1. पहले चल रहे ट्रेड्स का Target या Stop-Loss चेक करें
            check_active_trades_sl_target(name, candle_time, high_p, low_p)

            # 2. अगर इस इंडेक्स में पहले से कोई ट्रेड चल रहा है, तो नया न दें
            if any(t["index"] == name and t["is_open"] for t in daily_trades_log):
                continue

            # 3. अगर दिन के कुल 3 ट्रेड पूरे हो चुके हैं, तो नया ट्रेड बिल्कुल न दें
            if trades_today >= MAX_TRADES_PER_DAY:
                continue

            # 4. एक ही कैंडल पर बार-बार ट्रेड लेने से रोकें
            if last_candle_traded[name] == candle_time:
                continue

            is_sideways = (adx < ADX_THRESHOLD) or is_lunch_chop_time()
            signal, direction, strategy_used = evaluate_multi_strategies(df)

            if signal:
                last_candle_traded[name] = candle_time
                trades_today += 1

                sl_points = round(atr * 1.2, 0)
                rr_ratio = 2.0 if is_sideways else 2.5
                target_points = round(sl_points * rr_ratio, 0)

                if direction == "CALL":
                    sl_level = price - sl_points
                    target_level = price + target_points
                else:
                    sl_level = price + sl_points
                    target_level = price - target_points

                step = 100 if name in ["BANK NIFTY", "SENSEX"] else 50
                atm_strike = round(price / step) * step
                opt_type = "CE" if direction == "CALL" else "PE"
                kite_search = f"{info['kite_name']} {atm_strike} {opt_type}"

                max_risk_rs = TOTAL_CAPITAL * (RISK_PER_TRADE_PCT / 100)
                est_opt_sl = max(5, round(sl_points * 0.5, 0))
                risk_per_lot = est_opt_sl * info["lot_size"]

                if is_sideways:
                    lots = 1
                    header = f"⚠️ <b>SIDEWAYS MARKET SIGNAL (Trade {trades_today} of {MAX_TRADES_PER_DAY})</b> ⚠️"
                    status_note = (
                        "📢 <b>Market Mood:</b> मार्केट अभी साइडवेज (धीमा) है!\n\n"
                        "💡 <b>सलाह:</b> मार्केट साइडवेज है, आप अपनी मर्जी से ट्रेड लेना चाहें तो ले सकते हैं (सिर्फ 1 लॉट से करें)।"
                    )
                    lot_text = "1 Lot (साइडवेज सेफ्टी)"
                else:
                    lots = max(1, int(max_risk_rs // risk_per_lot))
                    header = f"🚨 <b>NEW TRADE SIGNAL (Trade {trades_today} of {MAX_TRADES_PER_DAY})</b> 🚨"
                    status_note = "📢 <b>Market Mood:</b> मार्केट अभी तेज ट्रेंड में है (Strong Trend)!"
                    lot_text = f"{lots} Lot"

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
                    "target": target_level,
                    "max_risk": total_risk,
                    "expected_profit": expected_prof,
                    "entry_candle_time": candle_time,
                    "is_open": True,
                    "status": "⏳ RUNNING (चल रहा है)"
                })

                msg = (
                    f"{header}\n\n"
                    f"{status_note}\n\n"
                    f"📊 <b>Index:</b> {name}\n\n"
                    f"🧠 <b>Setup:</b> {strategy_used}\n\n"
                    f"⚡ <b>Action:</b> <b>{signal}</b>\n\n"
                    f"🔍 <b>Zerodha Search:</b> <code>{kite_search}</code>\n\n"
                    f"📍 <b>Spot Entry:</b> {price:.0f}\n\n"
                    f"🛑 <b>Stop-Loss:</b> {sl_level:.0f} (<i>-{sl_points:.0f} pts</i>)\n\n"
                    f"🎯 <b>Target:</b> {target_level:.0f} (<i>+{target_points:.0f} pts</i>)\n\n"
                    f"💼 <b>Risk & Lot Guide:</b>\n\n"
                    f"• <b>कितने लॉट लें:</b> {lot_text}\n\n"
                    f"• <b>ऑप्शन का SL:</b> प्रीमियम से {est_opt_sl:.0f} पॉइंट नीचे\n\n"
                    f"• <b>अधिकतम रिस्क:</b> ₹{total_risk:.0f}\n\n"
                    f"• <b>अनुमानित प्रॉफिट:</b> ₹{expected_prof:.0f}\n\n"
                    f"────────────────────\n\n"
                    f"{get_trades_scoreboard()}"
                )
                send_telegram_message(msg)

        except Exception as e:
            print(f"Error scanning {name}: {e}")

if __name__ == "__main__":
    mkt_msg = "🟢 मार्केट अभी खुला है, लाइव स्कैनिंग शुरू है।" if is_market_open() else "🔴 अभी मार्केट बंद है। सोमवार सुबह 9:15 AM से अपने आप ट्रेड आएंगे।"
    
    send_telegram_message(
        "🤖 <b>MyTrader Bot Active!</b>\n\n"
        f"📢 <b>Status:</b> {mkt_msg}\n\n"
        "✅ दिन में सिर्फ 3 बेस्ट ट्रेड आएंगे।\n\n"
        "💡 मार्केट का हाल देखने के लिए कभी भी <b>/status</b> लिखकर भेजें।"
    )
    
    start_run = time.time()
    last_scan_time = 0
    while time.time() - start_run < 19800:
        check_telegram_commands()
        if time.time() - last_scan_time >= 15:
            scan_market()
            last_scan_time = time.time()
        time.sleep(3)
