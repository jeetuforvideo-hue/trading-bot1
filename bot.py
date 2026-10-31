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
MAX_TRADES_PER_DAY = 3        # दिन में अधिकतम 3 ट्रेंडिंग ट्रेड
ADX_THRESHOLD      = 20       # 20 से नीचे होने पर मार्केट साइडवेज माना जाएगा

INDICES = {
    "NIFTY 50": {"symbol": "^NSEI", "lot_size": 75, "kite_name": "NIFTY"},
    "BANK NIFTY": {"symbol": "^NSEBANK", "lot_size": 30, "kite_name": "BANKNIFTY"},
    "SENSEX": {"symbol": "^BSESN", "lot_size": 20, "kite_name": "SENSEX"},
    "MIDCAP NIFTY": {"symbol": "NIFTY_MID_SELECT.NS", "lot_size": 120, "kite_name": "MIDCPNIFTY"}
}

last_alert_sent = {name: None for name in INDICES}
trades_today = 0
last_update_id = 0
daily_trades_log = []

def get_ist_now():
    """क्लाउड सर्वर पर भारतीय समय (IST) निकालने का फंक्शन"""
    return datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)

current_day = get_ist_now().date()

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram Send Error: {e}")

def get_trades_scoreboard():
    if not daily_trades_log:
        return "📋 <b>आज का ट्रेड स्कोरबोर्ड:</b> अभी तक कोई ट्रेड नहीं लिया गया है।"
    board = "📋 <b>आज के ट्रेड्स का लाइव रिजल्ट (Scoreboard):</b>\n"
    for t in daily_trades_log:
        board += f"• <b>Trade #{t['id']} ({t['index']} - {t['strike']}):</b> {t['status']}\n  <i>(Strategy: {t['strategy']})</i>\n"
    return board

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
                if msg_text in ["/status", "status", "/start"]:
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
        return "BUY CALL (CE)", "CALL", "Strategy 1: Institutional Trend + MACD Crossover"
    if price < vwap and price < ema20 and ema20 < ema50 and 28 < rsi < 44 and macd < macd_sig:
        return "BUY PUT (PE)", "PUT", "Strategy 1: Institutional Downtrend + MACD Breakdown"

    if orb_high and orb_low:
        if prev['Close'] <= orb_high and price > orb_high and price > vwap and rsi > 58:
            return "BUY CALL (CE)", "CALL", "Strategy 2: Morning ORB (15-Min High Breakout)"
        if prev['Close'] >= orb_low and price < orb_low and price < vwap and rsi < 42:
            return "BUY PUT (PE)", "PUT", "Strategy 2: Morning ORB (15-Min Low Breakdown)"

    if prev['Close'] <= prev['BB_Upper'] and price > bb_upper and rsi > 60 and macd > macd_sig and price > open_p:
        return "BUY CALL (CE)", "CALL", "Strategy 3: Bollinger Band Blast (Momentum Breakout)"
    if prev['Close'] >= prev['BB_Lower'] and price < bb_lower and rsi < 40 and macd < macd_sig and price < open_p:
        return "BUY PUT (PE)", "PUT", "Strategy 3: Bollinger Band Blast (Selling Breakdown)"

    if ema20 > ema50 and price > vwap and low_p <= ema20 and price > ema20 and price > open_p and 52 < rsi < 68:
        return "BUY CALL (CE)", "CALL", "Strategy 4: 20 EMA + VWAP Pullback Bounce (Sniper Entry)"
    if ema20 < ema50 and price < vwap and high_p >= ema20 and price < ema20 and price < open_p and 32 < rsi < 48:
        return "BUY PUT (PE)", "PUT", "Strategy 4: 20 EMA + VWAP Pullback Rejection (Sniper Entry)"

    return None, None, None

def send_live_market_summary():
    report = "📊 <b>LIVE MULTI-STRATEGY MARKET REPORT</b> 📊\n\n"
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
            mood = "⚠️ साइडवेज (Range-Bound)" if adx < ADX_THRESHOLD else "🔥 ट्रेंडिंग (Strong Move)"
            report += f"🔹 <b>{name}:</b> {price:.2f}\n   • मूड: {mood} (ADX: {adx:.1f} | RSI: {rsi:.1f})\n\n"
        except Exception:
            report += f"🔹 <b>{name}:</b> डेटा लोड हो रहा है...\n\n"
    report += f"🛡️ <b>आज के ट्रेंडिंग ट्रेड:</b> {trades_today}/{MAX_TRADES_PER_DAY}\n\n"
    report += get_trades_scoreboard()
    send_telegram_message(report)

def check_active_trades_sl_target(index_name, current_high, current_low):
    for trade in daily_trades_log:
        if trade["index"] == index_name and trade["is_open"]:
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
                trade["status"] = f"✅ TARGET HIT (+₹{trade['expected_profit']:.0f} Profit)"
                last_alert_sent[index_name] = None
                update_msg = (
                    f"🎯 <b>TRADE #{trade['id']} UPDATE: TARGET HIT!</b> 🎯\n\n"
                    f"📊 <b>Index:</b> {trade['index']}\n"
                    f"🧠 <b>Strategy:</b> {trade['strategy']}\n"
                    f"⚡ <b>Trade:</b> {trade['action']} ({trade['strike']})\n"
                    f"📍 <b>Entry:</b> {trade['entry']:.2f}  ➡️  <b>Target Hit:</b> {trade['target']:.2f}\n"
                    f"💰 <b>Result:</b> <b>✅ TARGET HIT (अनुमानित मुनाफा: +₹{trade['expected_profit']:.0f})</b>\n"
                    f"📢 <b>सलाह:</b> बधाई हो! Zerodha Kite में अपना पूरा प्रॉफिट बुक कर लें।\n\n"
                    f"{get_trades_scoreboard()}"
                )
                send_telegram_message(update_msg)

            elif hit_type == "SL":
                trade["is_open"] = False
                trade["status"] = f"🛑 STOP-LOSS HIT (-₹{trade['max_risk']:.0f} Loss)"
                last_alert_sent[index_name] = None
                update_msg = (
                    f"🛑 <b>TRADE #{trade['id']} UPDATE: STOP-LOSS HIT!</b> 🛑\n\n"
                    f"📊 <b>Index:</b> {trade['index']}\n"
                    f"🧠 <b>Strategy:</b> {trade['strategy']}\n"
                    f"⚡ <b>Trade:</b> {trade['action']} ({trade['strike']})\n"
                    f"📍 <b>Entry:</b> {trade['entry']:.2f}  ➡️  <b>SL Hit:</b> {trade['sl']:.2f}\n"
                    f"⚠️ <b>Result:</b> <b>🛑 STOP-LOSS HIT (अनुमानित लॉस: -₹{trade['max_risk']:.0f})</b>\n"
                    f"📢 <b>सलाह:</b> डिसिप्लिन बनाए रखें और तुरंत ट्रेड से बाहर निकल जाएं।\n\n"
                    f"{get_trades_scoreboard()}"
                )
                send_telegram_message(update_msg)

def scan_market():
    global trades_today, current_day, last_alert_sent, daily_trades_log

    today = get_ist_now().date()
    if today != current_day:
        current_day = today
        trades_today = 0
        daily_trades_log = []
        last_alert_sent = {name: None for name in INDICES}

    for name, info in INDICES.items():
        try:
            df = yf.Ticker(info["symbol"]).history(period="5d", interval="5m")
            if df.empty or len(df) < 50:
                continue

            df = calculate_indicators(df)
            latest = df.iloc[-1]

            price  = float(latest['Close'])
            high_p = float(latest['High'])
            low_p  = float(latest['Low'])
            atr    = float(latest['ATR'])
            adx    = float(latest['ADX']) if not np.isnan(latest['ADX']) else 15.0

            check_active_trades_sl_target(name, high_p, low_p)

            if any(t["index"] == name and t["is_open"] for t in daily_trades_log):
                continue
            if trades_today >= MAX_TRADES_PER_DAY:
                continue

            is_sideways = (adx < ADX_THRESHOLD) or is_lunch_chop_time()
            signal, direction, strategy_used = evaluate_multi_strategies(df)

            if signal and last_alert_sent[name] != signal:
                last_alert_sent[name] = signal

                sl_points = round(atr * 1.2, 2)
                rr_ratio = 2.0 if is_sideways else 2.5
                target_points = round(sl_points * rr_ratio, 2)

                if direction == "CALL":
                    sl_level = round(price - sl_points, 2)
                    target_level = round(price + target_points, 2)
                else:
                    sl_level = round(price + sl_points, 2)
                    target_level = round(price - target_points, 2)

                step = 100 if name in ["BANK NIFTY", "SENSEX"] else 50
                atm_strike = round(price / step) * step
                opt_type = "CE" if direction == "CALL" else "PE"
                kite_search = f"{info['kite_name']} {atm_strike} {opt_type}"

                max_risk_rs = TOTAL_CAPITAL * (RISK_PER_TRADE_PCT / 100)
                est_opt_sl = max(5, round(sl_points * 0.5, 1))
                risk_per_lot = est_opt_sl * info["lot_size"]

                if is_sideways:
                    lots = 1
                    header = "⚠️ <b>SIDEWAYS MARKET SIGNAL (सावधानी रखें)</b> ⚠️"
                    status_note = (
                        "📢 <b>मार्केट स्टेटस:</b> मार्केट अभी साइडवेज (Range-Bound) है!\n"
                        "💡 <b>सलाह:</b> यहाँ ऑप्शन का प्रीमियम गलने (Theta Decay) का खतरा रहता है। "
                        "<b>अगर आप अपनी मर्जी से ट्रेड लेना चाहें तो ले सकते हैं</b>, लेकिन बहुत कम रिस्क या 1 लॉट के साथ ही करें।\n"
                    )
                    trend_tag = f"ADX: {adx:.1f} - साइडवेज/कमजोर ट्रेंड"
                    lot_text = "सिर्फ 1 Lot (साइडवेज सेफ्टी)"
                else:
                    trades_today += 1
                    lots = max(1, int(max_risk_rs // risk_per_lot))
                    header = f"🚨 <b>HIGH PROBABILITY SIGNAL (TRADE #{trades_today})</b> 🚨"
                    status_note = (
                        "📢 <b>मार्केट स्टेटस:</b> मार्केट पूरी तरह ट्रेंडिंग (Strong Momentum) है! "
                        "यह ट्रेड हाई-प्रोबेबिलिटी मल्टी-स्ट्रैटेजी सेटअप में आता है।\n"
                    )
                    trend_tag = f"ADX: {adx:.1f} - मजबूत ट्रेंड"
                    lot_text = f"{lots} Lot(s)"

                total_risk = lots * risk_per_lot
                expected_prof = total_risk * rr_ratio

                trade_id = len(daily_trades_log) + 1
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
                    "is_open": True,
                    "status": "⏳ RUNNING (चल रहा है)"
                })

                msg = (
                    f"{header}\n\n"
                    f"{status_note}\n"
                    f"📊 <b>Index:</b> {name} (<i>{trend_tag}</i>)\n"
                    f"🧠 <b>Matched Strategy:</b> <b>{strategy_used}</b>\n"
                    f"⚡ <b>Action:</b> {signal}\n"
                    f"🔍 <b>Zerodha Kite Search:</b> <code>{kite_search}</code>\n\n"
                    f"📍 <b>Spot Entry:</b> {price:.2f}\n"
                    f"🛑 <b>Spot Stop-Loss:</b> {sl_level:.2f} (<i>-{sl_points} pts</i>)\n"
                    f"✅ <b>Spot Target (1:{rr_ratio}):</b> {target_level:.2f} (<i>+{target_points} pts</i>)\n\n"
                    f"🛡️ <b>DISCIPLINE & RISK MANAGEMENT:</b>\n"
                    f"• <b>Recommended Lots:</b> {lot_text}\n"
                    f"• <b>Option SL (Approx):</b> {est_opt_sl} Points नीचे\n"
                    f"• <b>Max Risk on Trade:</b> ₹{total_risk:.0f}\n"
                    f"• <b>Expected Profit:</b> ₹{expected_prof:.0f}\n"
                    f"• <b>Trending Trade Count:</b> {trades_today}/{MAX_TRADES_PER_DAY}\n\n"
                    f"{get_trades_scoreboard()}"
                )
                send_telegram_message(msg)

        except Exception as e:
            print(f"Error scanning {name}: {e}")

if __name__ == "__main__":
    send_telegram_message(
        "☁️ <b>GitHub Cloud Bot Active!</b>\n\n"
        "✅ आपका बॉट अब क्लाउड सर्वर पर चालू हो चुका है (लैपटॉप बंद कर सकते हैं)।\n"
        "✅ 4-in-1 Multi-Strategy + Live Target/SL Scoreboard ऑन है।\n"
        "💡 कभी भी लाइव हाल जानने के लिए <b>/status</b> लिखकर भेजें।"
    )
    
    start_run = time.time()
    last_scan_time = 0
    # GitHub Actions पर एक सेशन में 5.5 घंटे (पूरे मार्केट टाइम 9:15 AM से 3:30 PM) तक लगातार चलेगा
    while time.time() - start_run < 19800:
        check_telegram_commands()
        if time.time() - last_scan_time >= 15:
            scan_market()
            last_scan_time = time.time()
        time.sleep(3)
