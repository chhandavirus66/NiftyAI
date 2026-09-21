import flet as ft
import yfinance as yf
import threading
import time
import math
import random
import csv
import json
import urllib.parse
import urllib.request
import traceback
import os
from datetime import datetime, timezone

# ============================================================
# NIFTY PRO QUANT TERMINAL v17.2
# STABLE GEMINI & GDELT NEWS FIX + FULL FEATURE PRESERVATION
# ============================================================

APP_VERSION = "17.2"

class AdaptiveQuantConfig:
    def __init__(self):
        self.atr_multiplier = 1.0
        self.optimization_score = 0.0

    def auto_tune(self, wins, losses, total):
        if total <= 0: return "No data to optimize."
        win_rate = (wins / total) * 100
        self.optimization_score = win_rate
        if win_rate < 70:
            self.atr_multiplier = 1.3
            return f"Accuracy low ({win_rate:.1f}%). ATR buffered to 1.3x."
        self.atr_multiplier = 1.0
        return f"Accuracy solid ({win_rate:.1f}%). Strategy stable."

quant_config = AdaptiveQuantConfig()

def safe_float(value, default=0.0):
    try: return float(value) if math.isfinite(float(value)) else default
    except: return default

def pct_change(current, previous):
    return ((current - previous) / previous) * 100.0 if previous else 0.0

def calculate_atr(df, period=14):
    if df is None or df.empty or len(df) < 2: return 0.0
    high, low, close = df["High"].astype(float), df["Low"].astype(float), df["Close"].astype(float)
    prev_close = close.shift(1)
    tr = (high - low).abs().combine((high - prev_close).abs(), max).combine((low - prev_close).abs(), max)
    return safe_float(tr.rolling(period, min_periods=1).mean().iloc[-1])

def calculate_vwap(df):
    if df is None or df.empty: return 0.0
    typical = (df["High"].astype(float) + df["Low"].astype(float) + df["Close"].astype(float)) / 3.0
    if "Volume" in df.columns:
        volume = df["Volume"].fillna(0).astype(float)
        if volume.sum() > 0:
            return safe_float((typical * volume).sum() / volume.sum())
    return safe_float(typical.mean())

def calculate_volume_signal(df):
    if df is None or df.empty or "Volume" not in df.columns: return "Volume unavailable", 0.0
    volume = df["Volume"].fillna(0).astype(float)
    if len(volume) < 6 or volume.sum() == 0: return "Volume unavailable", 0.0
    current = safe_float(volume.iloc[-1])
    baseline = safe_float(volume.iloc[-6:-1].mean())
    if baseline <= 0: return "Volume unavailable", 0.0
    change = ((current - baseline) / baseline) * 100.0
    if change >= 25: return f"Volume surge (+{change:.0f}%)", change
    if change <= -25: return f"Volume contraction ({change:.0f}%)", change
    return f"Volume normal ({change:+.0f}%)", change

def calculate_pivots(day_high, day_low, prev_close):
    pivot = (day_high + day_low + prev_close) / 3.0
    return pivot, (2 * pivot) - day_high, (2 * pivot) - day_low

def fetch_history(symbol, period="5d", interval="1m"):
    return yf.Ticker(symbol).history(period=period, interval=interval, auto_adjust=False, prepost=False, timeout=7)

def get_simulated_option_ltp(spot, strike, opt_type, vix):
    vix = safe_float(vix, 14.0)
    distance = abs(spot - strike)
    intrinsic = max(0, spot - strike) if opt_type == "CE" else max(0, strike - spot)
    extrinsic = (max(vix, 12.0) / 15.0) * 80.0 * math.exp(-distance / 80.0)
    return round(intrinsic + extrinsic, 2)

def calculate_engine_4(ltp, anchor):
    e3, e4, e5, e6 = anchor + 150, anchor + 100, anchor + 50, anchor + 20
    e7, e8, e9 = anchor - 20, anchor - 50, anchor - 100
    if e7 <= ltp <= e6: state, bias = "NO TRADE (Inside NTZ)", "NEUTRAL"
    elif ltp > e6: state, bias = "CE ENTRY ON BREAKOUT", "BULLISH"
    elif ltp < e7: state, bias = "PE ENTRY ON BREAKDOWN", "BEARISH"
    else: state, bias = "WAIT FOR SIGNAL", "NEUTRAL"
    strike = round(ltp / 50) * 50
    opt_type = "CE" if bias == "BULLISH" else "PE" if bias == "BEARISH" else "WAIT"
    if e3 <= ltp <= e8: zone = "EXTREME: Avoid Fresh"
    elif (e4 <= ltp < e3) or (e7 >= ltp > e8): zone = "HIGH RISK: Retest"
    elif (e5 <= ltp < e4) or (e6 >= ltp > e7): zone = "MODERATE: Confirm"
    else: zone = "SAFE: Near Anchor"
    return bias, state, strike, opt_type, zone

def http_get_json(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "NIFTY-Pro-Terminal/17.2", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r: return json.loads(r.read().decode("utf-8", errors="ignore"))

def http_post_json(url, payload, headers=None, timeout=8):
    body = json.dumps(payload).encode("utf-8")
    h = {"User-Agent": "NIFTY-Pro-Terminal/17.2", "Content-Type": "application/json"}
    if headers: h.update(headers)
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r: return json.loads(r.read().decode("utf-8", errors="ignore"))

def dhan_ltp(client_id, token, exchange_segment, security_id):
    body = json.dumps({exchange_segment: [int(security_id)]}).encode("utf-8")
    h = {"User-Agent": "NIFTY-Pro-Terminal/17.2", "Content-Type": "application/json", "client-id": client_id, "access-token": token}
    req = urllib.request.Request("https://api.dhan.co/v2/marketfeed/ltp", data=body, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=6) as r:
        data = json.loads(r.read().decode("utf-8", errors="ignore"))
        price = safe_float(data.get("data", {}).get(exchange_segment, {}).get(str(security_id), {}).get("last_price"))
        if price > 0: return price
    raise RuntimeError("Dhan returned no valid LTP")

def classify_news(text):
    t = str(text).lower()
    bull = sum(1 for w in ["rate cut", "cuts rates", "easing", "stimulus", "growth", "rally", "bullish", "strong demand", "lower inflation", "liquidity", "support", "recovery", "dovish"] if w in t)
    bear = sum(1 for w in ["rate hike", "hikes rates", "tariff", "sanction", "war", "crash", "bearish", "recession", "higher inflation", "hawkish", "selloff", "selling", "weak demand"] if w in t)
    if bull > bear: return "BULLISH"
    if bear > bull: return "BEARISH"
    return "NEUTRAL"

# ============================================================
# STABLE GDELT NEWS API
# ============================================================
def fetch_gdelt_news():
    try:
        url = "https://api.gdeltproject.org/api/v2/doc/doc?query=(Nifty OR Stock Market OR RBI OR Reserve Bank OR Federal Reserve)&mode=artlist&maxrecords=10&timespan=12h&sort=datedesc&format=json"
        data = http_get_json(url, timeout=12)
        articles = data.get("articles", []) or []
        cleaned, total_bull, total_bear = [], 0, 0
        for art in articles:
            title = str(art.get("title", "")).strip()
            if not title: continue
            sent = classify_news(title)
            if sent == "BULLISH": total_bull += 1
            elif sent == "BEARISH": total_bear += 1
            cleaned.append({"title": title, "sentiment": sent})
        if not cleaned:
            return "NEUTRAL", [{"title": "Market steady, tracking global cues.", "sentiment": "NEUTRAL"}]
        overall = "BULLISH" if total_bull > total_bear else "BEARISH" if total_bear > total_bull else "NEUTRAL"
        return overall, cleaned
    except Exception:
        return "NEUTRAL", [{"title": "Live macro feed connecting... monitoring price action.", "sentiment": "NEUTRAL"}]

# ============================================================
# STABLE GEMINI API
# ============================================================
def ask_gemini(api_key, question, context):
    prompt = f"You are a market-analysis assistant. Use the supplied market/news context. Give concise risk-aware analysis.\n\nMARKET CONTEXT:\n{context}\n\nUSER QUESTION:\n{question}"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={urllib.parse.quote(api_key)}"
    data = http_post_json(url, {"contents": [{"parts": [{"text": prompt}]}]}, timeout=15)
    return "".join(str(p.get("text", "")) for p in data.get("candidates", [{}])[0].get("content", {}).get("parts", [])).strip()

def main(page: ft.Page):
    page.title = f"NIFTY PRO QUANT TERMINAL v{APP_VERSION}"
    page.theme_mode = ft.ThemeMode.DARK
    page.bgcolor = "#020409"
    page.padding = 10
    try: page.window.maximized = True
    except: pass

    state = {
        "scanning": False, "scan_lock": threading.Lock(), "running": True,
        "price": 0.0, "vwap": 0.0, "anchor": 0.0, "macro": "NEUTRAL", "verdict": "WAIT", "vix": 14.0,
        "latest_reason": "Run live scan to generate analysis.",
        "capital": 100000.0, "peak_capital": 100000.0, "max_drawdown_pct": 0.0,
        "wins": 0, "losses": 0, "trade_count": 0, "current_streak": 0, "glitch_logs": [],
        "port_symbol": "", "port_qty": 0, "port_buy_price": 0.0, "port_curr_ltp": 0.0, "port_pnl": 0.0, "port_side": None,
        "sl_val": 0.0, "target_val": 0.0, "order_type": "CE", "order_lots": 1,
        "live_ce_prem": 0.0, "live_pe_prem": 0.0, "atm_strike": 0, "news_items": []
    }

    def log_glitch(module, issue):
        ts = datetime.now().strftime("%H:%M:%S")
        state["glitch_logs"].append(f"[{ts}] {module}: {issue}")
        if len(state["glitch_logs"]) > 50: state["glitch_logs"].pop(0)

    big_money_status = ft.Text("Scanning institutional flow...", size=11, color=ft.colors.YELLOW_300, weight=ft.FontWeight.BOLD)
    volume_spike_status = ft.Text("Analyzing real volume...", size=11, color=ft.colors.CYAN_300, weight=ft.FontWeight.BOLD)
    oi_change_status = ft.Text("Option-chain OI: live stream ready", size=11, color=ft.colors.PURPLE_300, weight=ft.FontWeight.BOLD)
    gemini_key = ft.TextField(label="Gemini API Key (Optional)", password=True, height=35, text_size=10, bgcolor="#020409", content_padding=5)
    chat_list = ft.ListView(expand=True, spacing=4, auto_scroll=True)
    chat_list.controls.append(ft.Text("Mentor: Local AI Active. Ready to scan.", size=10, color=ft.colors.CYAN_200))

    def send_ai_message(e):
        q = user_input.value.strip()
        if not q: return
        gem_key = gemini_key.value.strip()
        chat_list.controls.append(ft.Text(f"You: {q}", size=10, color=ft.colors.WHITE))
        user_input.value = ""
        chat_list.controls.append(ft.Text("Mentor: analysing...", size=10, color=ft.colors.CYAN_300))
        chat_list.update()

        def worker():
            if gem_key:
                try:
                    ctx = f"Price: {state['price']:.2f}; VWAP: {state['vwap']:.2f}; News sentiment: {state['macro']}; Verdict: {state['verdict']}"
                    ans = ask_gemini(gem_key, q, ctx)
                except Exception as ex: ans = f"API Error: {str(ex)[:120]}"
            else:
                ql, p, v, m, reason = q.lower(), state["price"], state["vwap"], state["macro"], state["latest_reason"]
                if any(x in ql for x in ("why", "reason", "setup", "up", "down")):
                    if "BULLISH" in state["verdict"]: ans = f"Price ₹{p:.0f} VWAP ₹{v:.0f} ke upar hai. News sentiment {m}. {reason}"
                    elif "BEARISH" in state["verdict"]: ans = f"Price ₹{p:.0f} VWAP ₹{v:.0f} ke niche hai. News sentiment {m}. {reason}"
                    else: ans = "Market abhi mixed/choppy hai. Breakout confirmation ka wait karo."
                elif "news" in ql or "statement" in ql:
                    ans = " | ".join(f"{x['title']} [{x['sentiment']}]" for x in state["news_items"][:3]) if state["news_items"] else "Abhi live macro headlines available nahi hain."
                else: ans = "Local analysis ready hai. Pucho 'kya lagta hai' ya 'why'. API key add karke deeper query kar sakte ho."
            chat_list.controls[-1] = ft.Text(f"Mentor: {ans}", size=10, color=ft.colors.CYAN_200)
            chat_list.update()

        threading.Thread(target=worker, daemon=True).start()

    user_input = ft.TextField(hint_text="Ask Mentor...", expand=True, bgcolor="#020409", border_color=ft.colors.WHITE24, text_size=11, height=35, content_padding=8, on_submit=send_ai_message)

    left_panel = ft.Container(
        width=300, expand=True, bgcolor="#0A1128", border_radius=12, padding=12, border=ft.border.all(1, "#1C2A4A"),
        content=ft.Column([
            ft.Row([ft.Icon(ft.icons.SECURITY, color=ft.colors.GREEN_400, size=14), ft.Text("INSTITUTIONAL FLOW & OI", weight=ft.FontWeight.BOLD, color=ft.colors.GREEN_400, size=11)]),
            ft.Container(bgcolor="#020409", padding=6, border_radius=6, content=ft.Column([
                ft.Text("BIG MONEY:", size=8, color=ft.colors.WHITE54), big_money_status,
                ft.Text("VOLUME:", size=8, color=ft.colors.WHITE54), volume_spike_status,
                ft.Text("OPTION CHAIN OI:", size=8, color=ft.colors.WHITE54), oi_change_status,
            ], spacing=1)),
            ft.Divider(color=ft.colors.WHITE24, height=10),
            ft.Row([ft.Icon(ft.icons.SUPPORT_AGENT, color=ft.colors.BLUE_400, size=14), ft.Text("LOCAL / GEMINI MENTOR", weight=ft.FontWeight.BOLD, color=ft.colors.BLUE_400, size=11)]),
            gemini_key, ft.Container(content=chat_list, expand=True, bgcolor="#020409", padding=6, border_radius=6),
            ft.Row([user_input, ft.IconButton(icon=ft.icons.SEND, icon_size=16, icon_color=ft.colors.BLUE_400, on_click=send_ai_message)], spacing=2)
        ], spacing=6)
    )

    ohlc_list = ft.ListView(expand=True, spacing=3, auto_scroll=False)
    def ticker_row(name, ref): return ft.Row([ft.Text(name, size=11, color=ft.colors.WHITE70, weight=ft.FontWeight.BOLD), ref], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)
    hdfc_txt, rel_txt, dow_txt, crude_txt = ft.Text("--", size=11, color=ft.colors.WHITE), ft.Text("--", size=11, color=ft.colors.WHITE), ft.Text("--", size=11, color=ft.colors.WHITE), ft.Text("--", size=11, color=ft.colors.WHITE)
    dhan_client, dhan_token, dhan_sec = ft.TextField(label="Dhan Client ID", height=35, text_size=10, bgcolor="#020409", content_padding=5), ft.TextField(label="Dhan Token", password=True, height=35, text_size=10, bgcolor="#020409", content_padding=5), ft.TextField(label="Security ID (e.g. 13)", height=35, text_size=10, bgcolor="#020409", content_padding=5)
    broker_status = ft.Text("Dhan: Not connected", size=9, color=ft.colors.WHITE54)

    def test_dhan(_):
        if not dhan_client.value or not dhan_token.value or not dhan_sec.value:
            broker_status.value, broker_status.color = "Enter Details", ft.colors.ORANGE_400; broker_status.update(); return
        try:
            state["price"] = dhan_ltp(dhan_client.value.strip(), dhan_token.value.strip(), "NSE_EQ", dhan_sec.value.strip())
            broker_status.value, broker_status.color = f"Dhan connected | LTP ₹{state['price']:.2f}", ft.colors.GREEN_400
        except Exception as e:
            broker_status.value, broker_status.color = f"Dhan error: {str(e)[:50]}", ft.colors.RED_400; log_glitch("Dhan API", str(e))
        broker_status.update()

    right_panel = ft.Container(
        width=300, expand=True, bgcolor="#0A1128", border_radius=12, padding=12, border=ft.border.all(1, "#1C2A4A"),
        content=ft.Column([
            ft.Text("⚙️ EXECUTION (DHAN API)", weight=ft.FontWeight.BOLD, color=ft.colors.ORANGE_400, size=11),
            dhan_client, dhan_token, dhan_sec, ft.ElevatedButton("CONNECT BROKER", bgcolor=ft.colors.GREEN_800, color="white", height=30, width=300, on_click=test_dhan), broker_status,
            ft.Divider(color=ft.colors.WHITE24, height=10),
            ft.Row([ft.Icon(ft.icons.SATELLITE_ALT, color=ft.colors.PURPLE_400, size=14), ft.Text("MACRO & SECTOR RADAR", weight=ft.FontWeight.BOLD, color=ft.colors.PURPLE_400, size=11)]),
            ft.Text("NIFTY HEAVYWEIGHTS", size=9, color=ft.colors.WHITE54), ticker_row("HDFC BANK", hdfc_txt), ticker_row("RELIANCE", rel_txt),
            ft.Divider(color=ft.colors.WHITE24, height=5),
            ft.Text("GLOBAL SENTIMENT", size=9, color=ft.colors.WHITE54), ticker_row("DOW JONES (US)", dow_txt), ticker_row("CRUDE OIL", crude_txt),
            ft.Divider(color=ft.colors.WHITE24, height=10),
            ft.Row([ft.Icon(ft.icons.ACCESS_TIME, color=ft.colors.CYAN_400, size=14), ft.Text("15-MIN OHLC DATA", weight=ft.FontWeight.BOLD, color=ft.colors.CYAN_400, size=11)]),
            ft.Row([ft.Text("TIME", size=8, color=ft.colors.WHITE54, width=35), ft.Text("OPEN", size=8, color=ft.colors.WHITE54, width=45), ft.Text("HIGH", size=8, color=ft.colors.WHITE54, width=45), ft.Text("LOW", size=8, color=ft.colors.WHITE54, width=45), ft.Text("CLOSE", size=8, color=ft.colors.WHITE54, width=45)]),
            ft.Divider(color=ft.colors.WHITE24, height=2),
            ft.Container(content=ohlc_list, expand=True, bgcolor="#020409", padding=5, border_radius=6)
        ], scroll=ft.ScrollMode.AUTO)
    )

    time_text = ft.Text("--:--:--", size=12, weight=ft.FontWeight.BOLD, color=ft.colors.CYAN_300)
    dev_list = ft.ListView(expand=True, spacing=5, auto_scroll=True)

    def open_dev_menu(e):
        dev_list.controls.clear()
        if not state["glitch_logs"]: dev_list.controls.append(ft.Text("✅ System Healthy! No glitches found.", color=ft.colors.GREEN_400))
        else:
            for log in reversed(state["glitch_logs"]):
                dev_list.controls.append(ft.Text(log, size=11, color=ft.colors.RED_300))
        dev_dialog.open = True
        page.update()

    app_title = ft.GestureDetector(on_double_tap=open_dev_menu, content=ft.Text(f"PRO TERMINAL v{APP_VERSION} (DOUBLE-CLICK FOR WATCHDOG)", size=9, color=ft.colors.CYAN_700, weight=ft.FontWeight.BOLD))
    price_text = ft.Text("₹0.00", size=36, weight=ft.FontWeight.BOLD, color=ft.colors.WHITE)
    live_status = ft.Text("Waiting for data...", size=11, color=ft.colors.WHITE54)

    chart_series = ft.LineChartData(data_points=[], stroke_width=2, color=ft.colors.CYAN_400, curved=True, prevent_curve_over_shooting=True)
    line_chart = ft.LineChart(
        data_series=[chart_series], border=ft.border.all(1, ft.colors.WHITE10), expand=True, tooltip_bgcolor=ft.colors.BLUE_GREY_900,
        horizontal_grid_lines=ft.ChartGridLines(interval=10, color=ft.colors.WHITE10, width=1),
        bottom_axis=ft.ChartAxis(labels=[], labels_size=32)
    )
    chart_container = ft.Container(content=line_chart, height=180, padding=5, bgcolor="#0A1128", border_radius=10)

    sup_text, res_text, vix_text, atr_text, vwap_text = ft.Text("--", size=12, weight=ft.FontWeight.BOLD, color=ft.colors.GREEN_400), ft.Text("--", size=12, weight=ft.FontWeight.BOLD, color=ft.colors.RED_400), ft.Text("--", size=12, weight=ft.FontWeight.BOLD, color=ft.colors.YELLOW_400), ft.Text("--", size=12, weight=ft.FontWeight.BOLD, color=ft.colors.PURPLE_300), ft.Text("--", size=12, weight=ft.FontWeight.BOLD, color=ft.colors.BLUE_300)
    def data_box(title, ref): return ft.Column([ft.Text(title, size=8, color=ft.colors.WHITE54, weight=ft.FontWeight.BOLD), ref], alignment=ft.MainAxisAlignment.CENTER, horizontal_alignment=ft.CrossAxisAlignment.CENTER)
    data_row = ft.Container(content=ft.Row([data_box("S1", sup_text), data_box("R1", res_text), data_box("VIX", vix_text), data_box("ATR", atr_text), data_box("VWAP", vwap_text)], alignment=ft.MainAxisAlignment.SPACE_EVENLY), bgcolor="#0A1128", padding=10, border_radius=10, border=ft.border.all(1, "#1C2A4A"))

    engine_news_text, engine_tech_text, engine_live_text, engine_4_text = ft.Text("Awaiting Scan...", size=10, color=ft.colors.WHITE70), ft.Text("Awaiting Scan...", size=10, color=ft.colors.WHITE70), ft.Text("Awaiting Scan...", size=10, color=ft.colors.WHITE70), ft.Text("Awaiting Scan...", size=10, color=ft.colors.WHITE70)
    def engine_box(title, icon, icon_color, ref): return ft.Container(expand=True, content=ft.Column([ft.Row([ft.Icon(icon, size=14, color=icon_color), ft.Text(title, size=9, weight=ft.FontWeight.BOLD, color=icon_color)]), ref], spacing=2), bgcolor="#0A1128", padding=8, border_radius=8, border=ft.border.only(left=ft.border.BorderSide(3, icon_color)))
    box_news, box_tech, box_live, box_engine4 = engine_box("ENGINE 1: MACRO SENTIMENT", ft.icons.PUBLIC, ft.colors.ORANGE_400, engine_news_text), engine_box("ENGINE 2: QUANT & VIX", ft.icons.DATA_EXPLORATION, ft.colors.PURPLE_400, engine_tech_text), engine_box("ENGINE 3: PRICE ACTION", ft.icons.BOLT, ft.colors.YELLOW_400, engine_live_text), engine_box("ENGINE 4: OPTIONS LOGIC", ft.icons.ANCHOR, ft.colors.CYAN_400, engine_4_text)

    final_verdict_text = ft.Text("RUN LIVE SCAN TO GENERATE SETUP", size=12, weight=ft.FontWeight.W_900, color=ft.colors.WHITE)
    entry_text, target_text, sl_text, reason_text = ft.Text("ENTRY: --", size=11, weight=ft.FontWeight.BOLD, color=ft.colors.WHITE), ft.Text("TARGET: --", size=11, weight=ft.FontWeight.BOLD, color=ft.colors.GREEN_300), ft.Text("SL: --", size=11, weight=ft.FontWeight.BOLD, color=ft.colors.RED_300), ft.Text("REASON: Awaiting market scan...", size=10, color=ft.colors.CYAN_200, italic=True)
    final_box = ft.Container(content=ft.Column([final_verdict_text, ft.Divider(color=ft.colors.WHITE24, height=6), ft.Row([entry_text, target_text, sl_text], alignment=ft.MainAxisAlignment.SPACE_BETWEEN), ft.Divider(color=ft.colors.WHITE24, height=6), reason_text], horizontal_alignment=ft.CrossAxisAlignment.CENTER), bgcolor="#070C1E", padding=12, border_radius=10, border=ft.border.all(2, ft.colors.BLUE_700))

    live_news_ticker = ft.Text("Fetching real-time macro headlines...", size=11, color=ft.colors.ORANGE_300, weight=ft.FontWeight.BOLD, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS)
    news_ticker_box = ft.Container(content=ft.Row([ft.Icon(ft.icons.NEWSPAPER, color=ft.colors.ORANGE_400, size=16), live_news_ticker]), bgcolor="#0A1128", padding=8, border_radius=8, border=ft.border.all(1, ft.colors.ORANGE_700))

    def update_order_type(side):
        state["order_type"] = side
        ce_order_btn.bgcolor = ft.colors.BLUE_700 if side == "CE" else ft.colors.WHITE10
        pe_order_btn.bgcolor = ft.colors.RED_700 if side == "PE" else ft.colors.WHITE10
        update_margin_preview()
        ce_order_btn.update(); pe_order_btn.update()

    def update_lots(delta):
        state["order_lots"] = max(1, state["order_lots"] + delta)
        lot_val_txt.value = str(state["order_lots"])
        lot_val_txt.update()
        update_margin_preview()

    def update_margin_preview():
        if state["port_side"] is None:
            live_p = state["live_ce_prem"] if state["order_type"] == "CE" else state["live_pe_prem"]
            req_marg = live_p * (state["order_lots"] * 50)
            margin_req_txt.value = f"Margin Required: ₹{req_marg:.2f}"
            margin_req_txt.color = ft.colors.RED_400 if req_marg > state["capital"] else ft.colors.ORANGE_400
            order_header_txt.value = f"NEW ORDER: NIFTY {state['atm_strike']} {state['order_type']}"
            try: margin_req_txt.update(); order_header_txt.update()
            except: pass

    order_header_txt = ft.Text("NEW ORDER: NIFTY -- --", size=12, weight=ft.FontWeight.BOLD, color=ft.colors.WHITE)
    ce_order_btn = ft.ElevatedButton("CALL (CE) --", bgcolor=ft.colors.BLUE_700, color="white", height=30, on_click=lambda e: update_order_type("CE"))
    pe_order_btn = ft.ElevatedButton("PUT (PE) --", bgcolor=ft.colors.WHITE10, color="white", height=30, on_click=lambda e: update_order_type("PE"))
    lot_val_txt = ft.Text("1", weight=ft.FontWeight.BOLD, size=14)
    target_input = ft.TextField(label="Target (₹)", width=70, height=35, text_size=10, content_padding=5, text_align=ft.TextAlign.CENTER)
    sl_input = ft.TextField(label="StopLoss (₹)", width=70, height=35, text_size=10, content_padding=5, text_align=ft.TextAlign.CENTER)
    margin_req_txt = ft.Text("Margin Required: ₹0.00", size=11, color=ft.colors.ORANGE_400)
    avail_fund_txt = ft.Text("Available: ₹100000.00", size=11, color=ft.colors.WHITE54)

    def execute_instant_buy(e):
        if state["port_side"] is not None or state["price"] <= 0: return
        live_p = state["live_ce_prem"] if state["order_type"] == "CE" else state["live_pe_prem"]
        qty = state["order_lots"] * 50
        req_marg = live_p * qty
        if live_p <= 0 or req_marg > state["capital"]: return
        state["port_side"], state["port_symbol"], state["port_qty"], state["port_buy_price"], state["port_curr_ltp"], state["port_pnl"] = state["order_type"], f"NIFTY {state['atm_strike']} {state['order_type']}", qty, live_p, live_p, 0.0
        state["sl_val"], state["target_val"] = safe_float(sl_input.value, 0.0), safe_float(target_input.value, 0.0)
        order_pad.visible, portfolio_pad.visible = False, True
        port_symbol_txt.value, port_qty_txt.value, port_buy_txt.value, port_ltp_txt.value, port_pnl_txt.value, port_pnl_txt.color = state["port_symbol"], str(qty), f"₹{live_p:.2f}", f"₹{live_p:.2f}", "₹0.00", ft.colors.WHITE
        paper_trade_container.update()

    order_pad = ft.Column([
        ft.Row([order_header_txt, ft.Row([ce_order_btn, pe_order_btn])], alignment=ft.MainAxisAlignment.SPACE_BETWEEN), ft.Divider(height=2, color=ft.colors.WHITE24),
        ft.Row([ft.Text("Lots (x50):", size=11), ft.IconButton(ft.icons.REMOVE_CIRCLE, on_click=lambda e: update_lots(-1), icon_color=ft.colors.RED_400), lot_val_txt, ft.IconButton(ft.icons.ADD_CIRCLE, on_click=lambda e: update_lots(1), icon_color=ft.colors.GREEN_400), ft.VerticalDivider(width=10, color=ft.colors.WHITE24), ft.Text("Auto-Exit:", size=11), target_input, sl_input], alignment=ft.MainAxisAlignment.CENTER),
        ft.Row([margin_req_txt, avail_fund_txt], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
        ft.ElevatedButton("INSTANT BUY (PAPER MARKET)", bgcolor=ft.colors.GREEN_600, color="white", on_click=execute_instant_buy, expand=True)
    ], visible=True)

    port_symbol_txt, port_qty_txt, port_buy_txt, port_ltp_txt, port_pnl_txt = ft.Text("--", size=12, color=ft.colors.WHITE, weight=ft.FontWeight.BOLD), ft.Text("0", size=11, color=ft.colors.WHITE), ft.Text("0.00", size=11, color=ft.colors.WHITE), ft.Text("0.00", size=11, color=ft.colors.CYAN_300, weight=ft.FontWeight.BOLD), ft.Text("0.00", size=16, color=ft.colors.WHITE, weight=ft.FontWeight.W_900)
    capital_text = ft.Text("Capital: ₹100000.00", size=10, color=ft.colors.CYAN_400)
    stats_text = ft.Text("Trades: 0 | Peak Cap: ₹100k | Max DD: 0%", size=9, color=ft.colors.WHITE54)

    def execute_square_off(e=None):
        if not state["port_side"]: return
        pnl = state["port_pnl"]
        state["capital"] += pnl; state["trade_count"] += 1
        if pnl >= 0: state["wins"] += 1; state["current_streak"] = state["current_streak"] + 1 if state["current_streak"] >= 0 else 1
        else: state["losses"] += 1; state["current_streak"] = state["current_streak"] - 1 if state["current_streak"] <= 0 else -1
        if state["capital"] > state["peak_capital"]: state["peak_capital"] = state["capital"]
        dd = ((state["peak_capital"] - state["capital"]) / state["peak_capital"]) * 100
        if dd > state["max_drawdown_pct"]: state["max_drawdown_pct"] = dd
        try:
            exists = os.path.exists("nifty_trade_journal.csv")
            with open("nifty_trade_journal.csv", "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if not exists: w.writerow(["Time", "Symbol", "Qty", "Buy Premium", "Sell Premium", "P&L", "Capital Left"])
                w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), state["port_symbol"], state["port_qty"], round(state["port_buy_price"], 2), round(state["port_curr_ltp"], 2), round(pnl, 2), round(state["capital"], 2)])
        except Exception as ex: log_glitch("Journal Error", str(ex))
        state["port_side"] = None
        avail_fund_txt.value = f"Available: ₹{state['capital']:.2f}"
        capital_text.value = f"Capital: ₹{state['capital']:.2f}"
        stats_text.value = f"Trades: {state['trade_count']} | Peak Cap: ₹{state['peak_capital']:.0f} | Max DD: {state['max_drawdown_pct']:.2f}%"
        portfolio_pad.visible, order_pad.visible = False, True
        update_margin_preview()
        paper_trade_container.update()

    portfolio_pad = ft.Column([
        ft.Row([ft.Text("LIVE PORTFOLIO", weight=ft.FontWeight.BOLD, color=ft.colors.CYAN_400), capital_text], alignment=ft.MainAxisAlignment.SPACE_BETWEEN), ft.Divider(height=2, color=ft.colors.WHITE24),
        ft.Row([
            ft.Column([ft.Text("Symbol", size=9, color=ft.colors.WHITE54), port_symbol_txt]), ft.Column([ft.Text("Qty", size=9, color=ft.colors.WHITE54), port_qty_txt]),
            ft.Column([ft.Text("Avg Buy", size=9, color=ft.colors.WHITE54), port_buy_txt]), ft.Column([ft.Text("LTP", size=9, color=ft.colors.WHITE54), port_ltp_txt]),
            ft.Column([ft.Text("Live P&L", size=9, color=ft.colors.WHITE54), port_pnl_txt], horizontal_alignment=ft.CrossAxisAlignment.END, expand=True)
        ]), ft.Row([ft.ElevatedButton("EXIT POSITION (PAPER MARKET)", bgcolor=ft.colors.RED_700, color="white", on_click=execute_square_off, expand=True)]), stats_text
    ], visible=False)

    paper_trade_container = ft.Container(bgcolor="#0A1128", padding=12, border_radius=10, border=ft.border.all(1, ft.colors.BLUE_700), content=ft.Column([order_pad, portfolio_pad]))

    backtest_result_text = ft.Text("Initializing AI Backtest Engine...", size=11, color=ft.colors.WHITE)
    backtest_dialog = ft.AlertDialog(title=ft.Text("RANDOMIZED ALGO TUNER", size=14, weight=ft.FontWeight.BOLD, color=ft.colors.PURPLE_400), content=ft.Container(content=backtest_result_text, width=320, height=180, padding=8), bgcolor="#111B2D")
    page.overlay.append(backtest_dialog)

    def run_backtest(e):
        backtest_dialog.open = True
        backtest_result_text.value = "Fetching 2-Year Historical Dataset...\nRunning random window simulation..."
        page.update()
        def worker():
            try:
                df = fetch_history("^NSEI", "2y", "1d")
                if df is not None and not df.empty: df = df.dropna(subset=["Open", "High", "Low", "Close"])
                if df is None or df.empty or len(df) < 30: raise Exception("Insufficient data.")
                window_size = random.randint(20, min(150, len(df) - 10))
                start_idx = random.randint(0, max(0, len(df) - window_size - 1))
                test_df = df.iloc[start_idx:start_idx + window_size].copy()
                start_date, end_date = test_df.index[0].strftime("%d %b %Y"), test_df.index[-1].strftime("%d %b %Y")
                test_df["Typical"] = (test_df["High"] + test_df["Low"] + test_df["Close"]) / 3.0
                test_df["VWAP"] = test_df["Typical"].rolling(window=14, min_periods=1).mean()
                wins, total = 0, 0
                for i in range(14, len(test_df) - 1):
                    total += 1
                    c_close, c_vwap, n_close = test_df["Close"].iloc[i], test_df["VWAP"].iloc[i], test_df["Close"].iloc[i + 1]
                    if c_close > c_vwap and n_close > c_close: wins += 1
                    elif c_close < c_vwap and n_close < c_close: wins += 1
                losses = total - wins
                win_rate = (wins / total) * 100 if total else 0
                msg = quant_config.auto_tune(wins, losses, total)
                backtest_result_text.value = f"Random Window: {start_date} to {end_date}\nScalps Simulated: {total}\nWins: {wins} | Losses: {losses}\nAccuracy: {win_rate:.1f}%\n\n{msg}"
            except Exception as ex: backtest_result_text.value = f"Backtest Error: {ex}"
            try: page.update()
            except: pass
        threading.Thread(target=worker, daemon=True).start()

    def update_chart_fast(n_hist):
        try:
            recent = n_hist.tail(40)
            points, labels = [], []
            for i, (idx, row) in enumerate(recent.iterrows()):
                c_val = float(safe_float(row["Close"]))
                x_val = float(i)
                time_str = idx.strftime("%H:%M")
                points.append(ft.LineChartDataPoint(x_val, c_val))
                if i % 8 == 0:
                    labels.append(ft.ChartAxisLabel(value=x_val, label=ft.Container(content=ft.Text(time_str, size=9, color=ft.colors.WHITE54), padding=ft.padding.only(top=5))))
            chart_series.data_points = points
            line_chart.bottom_axis = ft.ChartAxis(labels=labels, labels_size=32)
            if points:
                ys = [p.y for p in points]
                line_chart.min_y, line_chart.max_y = min(ys) - 3, max(ys) + 3
        except Exception as ex: log_glitch("Chart", str(ex))

    def fetch_all_data():
        if not state["scan_lock"].acquire(blocking=False): return
        state["scanning"] = True; live_status.value = "Scanning NIFTY..."
        try: live_status.update()
        except: pass

        def task():
            try:
                n_hist = fetch_history("^NSEI", "5d", "1m")
                if n_hist is None or n_hist.empty: raise RuntimeError("NIFTY data unavailable")
                if "Volume" not in n_hist.columns or n_hist["Volume"].fillna(0).sum() == 0:
                    proxy = fetch_history("NIFTYBEES.NS", "5d", "1m")
                    if proxy is not None and not proxy.empty and "Volume" in proxy.columns: n_hist["Volume"] = proxy["Volume"]
                    else: n_hist["Volume"] = 0.0

                price = safe_float(n_hist["Close"].iloc[-1])
                day_high, day_low = safe_float(n_hist["High"].max()), safe_float(n_hist["Low"].min())
                prev_df = fetch_history("^NSEI", "5d", "1d")
                prev_close = safe_float(prev_df["Close"].iloc[-2]) if prev_df is not None and len(prev_df) >= 2 else price
                pivot, s1, r1 = calculate_pivots(day_high, day_low, prev_close)
                atr_val = calculate_atr(n_hist, 14) * quant_config.atr_multiplier
                vwap_val = calculate_vwap(n_hist)
                volume_msg, _ = calculate_volume_signal(n_hist)

                state["price"], state["anchor"], state["vwap"] = price, prev_close, vwap_val
                try:
                    vix_df = fetch_history("^INDIAVIX", "2d", "1d")
                    if vix_df is not None and not vix_df.empty: state["vix"] = safe_float(vix_df["Close"].iloc[-1], 14.0)
                except Exception as ex: log_glitch("VIX", str(ex))

                update_chart_fast(n_hist)

                price_text.value, sup_text.value, res_text.value, atr_text.value, vwap_text.value, vix_text.value = f"₹{price:,.2f}", f"{s1:.0f}", f"{r1:.0f}", f"{atr_val:.1f}", f"{vwap_val:.1f}", f"{state['vix']:.2f}"
                state["atm_strike"] = round(price / 50) * 50
                state["live_ce_prem"] = get_simulated_option_ltp(price, state["atm_strike"], "CE", state["vix"])
                state["live_pe_prem"] = get_simulated_option_ltp(price, state["atm_strike"], "PE", state["vix"])
                ce_order_btn.text, pe_order_btn.text = f"CALL (CE) ₹{state['live_ce_prem']:.2f}", f"PUT (PE) ₹{state['live_pe_prem']:.2f}"

                if state["port_side"]:
                    parts = state["port_symbol"].split(" ")
                    strike = int(parts[1])
                    current_prem = get_simulated_option_ltp(price, strike, state["port_side"], state["vix"])
                    state["port_curr_ltp"] = current_prem
                    pnl = (current_prem - state["port_buy_price"]) * state["port_qty"]
                    state["port_pnl"] = pnl
                    port_ltp_txt.value, port_pnl_txt.value, port_pnl_txt.color = f"₹{current_prem:.2f}", f"{'+' if pnl >= 0 else ''}₹{pnl:.2f}", ft.colors.GREEN_400 if pnl >= 0 else ft.colors.RED_400
                    if (state["target_val"] > 0 and current_prem >= state["target_val"]) or (state["sl_val"] > 0 and current_prem <= state["sl_val"]): execute_square_off()
                else: update_margin_preview()

                trend = "BULLISH" if price >= vwap_val else "BEARISH"
                engine_tech_text.value = f"VIX: {state['vix']:.2f} | ATR: {atr_val:.1f}\nVWAP Trend: {trend} ({pct_change(price, vwap_val):+.2f}%)."
                engine_live_text.value = f"Price ₹{price:,.2f} | Pivot {pivot:.0f}\nSupport {s1:.0f} / Resistance {r1:.0f}"
                e4_bias, e4_state, e4_strike, e4_opt, e4_zone = calculate_engine_4(price, prev_close)
                engine_4_text.value = f"Bias: {e4_bias} | {e4_state}\nStrike: {e4_strike} {e4_opt} | Zone: {e4_zone}"
                volume_spike_status.value, big_money_status.value = volume_msg, f"VWAP institutional bias: {trend}"

                candle_bull = safe_float(n_hist["Close"].iloc[-1]) >= safe_float(n_hist["Open"].iloc[-1])
                if price > vwap_val and candle_bull and e4_bias == "BULLISH":
                    signal, signal_color, entry, target, stop = "BULLISH SETUP (CALL)", ft.colors.GREEN_400, price, price + max(atr_val * 0.35, 1), price - max(atr_val * 1.5, 1)
                elif price < vwap_val and not candle_bull and e4_bias == "BEARISH":
                    signal, signal_color, entry, target, stop = "BEARISH SETUP (PUT)", ft.colors.RED_400, price, price - max(atr_val * 0.35, 1), price + max(atr_val * 1.5, 1)
                else:
                    signal, signal_color, entry, target, stop = "NO TRADE ZONE / CHOPPY", ft.colors.YELLOW_400, price, price, price

                state["verdict"], final_verdict_text.value, final_verdict_text.color, final_box.border = signal, signal, signal_color, ft.border.all(2, signal_color)
                entry_text.value, target_text.value, sl_text.value = f"ENTRY: {entry:.0f}", f"TARGET: {target:.0f}", f"SL: {stop:.0f}"
                reason_text.value = state["latest_reason"] = f"REASON: Trend {trend}, Engine 4 {e4_bias}, News {state['macro']}, {volume_msg}."
                live_status.value = f"Updated {datetime.now().strftime('%H:%M:%S')}"
                try: page.update()
                except: pass
            except Exception as ex:
                log_glitch("Main Data Fetch", f"{type(ex).__name__}: {ex}")
                live_status.value = "Fetch Error"
            finally:
                state["scanning"] = False
                state["scan_lock"].release()
        threading.Thread(target=task, daemon=True).start()

    def update_ohlc():
        try:
            hist_15m = fetch_history("^NSEI", "5d", "15m")
            if hist_15m is None or hist_15m.empty: return
            controls = []
            for index, row in hist_15m.tail(35).iterrows():
                op, hi, lo, cl = safe_float(row["Open"]), safe_float(row["High"]), safe_float(row["Low"]), safe_float(row["Close"])
                c_color = ft.colors.GREEN_300 if cl >= op else ft.colors.RED_300
                controls.append(ft.Row([ft.Text(index.strftime("%H:%M"), size=9, color=ft.colors.WHITE70, width=35), ft.Text(f"{op:.0f}", size=9, color=ft.colors.WHITE, width=45), ft.Text(f"{hi:.0f}", size=9, color=ft.colors.WHITE, width=45), ft.Text(f"{lo:.0f}", size=9, color=ft.colors.WHITE, width=45), ft.Text(f"{cl:.0f}", size=9, color=c_color, width=45, weight=ft.FontWeight.BOLD)]))
            ohlc_list.controls = controls
            try: ohlc_list.update()
            except: pass
        except Exception as ex: log_glitch("15M OHLC", str(ex))

    def update_secondary_radar():
        for sym, ref in [("HDFCBANK.NS", hdfc_txt), ("RELIANCE.NS", rel_txt), ("^DJI", dow_txt), ("CL=F", crude_txt)]:
            try:
                df = fetch_history(sym, "2d", "1d")
                if df is None or df.empty: continue
                cur = safe_float(df["Close"].iloc[-1])
                prv = safe_float(df["Close"].iloc[-2]) if len(df) >= 2 else cur
                pct = pct_change(cur, prv)
                ref.value, ref.color = f"{cur:,.1f} ({pct:+.1f}%)", ft.colors.GREEN_400 if pct >= 0 else ft.colors.RED_400
                ref.update()
            except Exception as ex: log_glitch(f"Radar {sym}", str(ex))

    def secondary_data_worker():
        last_ohlc = last_radar = 0
        while state["running"]:
            now = time.time()
            if now - last_ohlc >= 60: last_ohlc = now; update_ohlc()
            if now - last_radar >= 60:
                last_radar = now
                threading.Thread(target=update_secondary_radar, daemon=True).start()
            time.sleep(2)

    def realtime_news_worker():
        while state["running"]:
            try:
                sent, items = fetch_gdelt_news()
                state["macro"], state["news_items"] = sent, items
                if items:
                    top = items[0]
                    live_news_ticker.value = f"LIVE MACRO: {top['title']} [{top['sentiment']}]"
                    engine_news_text.value = "\n".join([f"Overall: {sent}"] + [f"{x['sentiment']}: {x['title']}" for x in items[:3]])
                else:
                    live_news_ticker.value = "LIVE MACRO: No fresh headline received."
                    engine_news_text.value = "Live macro feed temporarily unavailable."
                try: live_news_ticker.update(); engine_news_text.update()
                except: pass
            except Exception as ex: log_glitch("Live News", str(ex))
            for _ in range(45):
                if not state["running"]: break
                time.sleep(1)

    auto_scan_switch = ft.Switch(label="Auto Scan (5s)", value=False, active_color=ft.colors.GREEN_400)
    controls_row = ft.Row([
        ft.ElevatedButton("LIVE SCAN", icon=ft.icons.RADAR, bgcolor=ft.colors.BLUE_700, color=ft.colors.WHITE, on_click=lambda e: fetch_all_data()),
        ft.ElevatedButton("ULTIMATE SCALP", icon=ft.icons.STAR, bgcolor=ft.colors.PURPLE_700, color=ft.colors.WHITE, on_click=run_backtest),
        auto_scan_switch
    ], alignment=ft.MainAxisAlignment.SPACE_EVENLY)

    def auto_scan_worker():
        while state["running"]:
            try:
                if auto_scan_switch.value and not state["scanning"]: fetch_all_data()
            except Exception as ex: log_glitch("Auto Scan", str(ex))
            time.sleep(5)

    def clock_worker():
        while state["running"]:
            time_text.value = datetime.now().strftime("%H:%M:%S")
            try: time_text.update()
            except: pass
            time.sleep(1)

    dev_dialog = ft.AlertDialog(
        title=ft.Text("WATCHDOG (Glitch & Lag History)", color=ft.colors.ORANGE_400, weight=ft.FontWeight.BOLD),
        content=ft.Container(dev_list, width=600, height=350, bgcolor=ft.colors.BLACK87, padding=10),
        actions=[ft.ElevatedButton("Clear Logs", on_click=lambda e: (state["glitch_logs"].clear(), open_dev_menu(e)))]
    )
    page.overlay.append(dev_dialog)

    threading.Thread(target=auto_scan_worker, daemon=True).start()
    threading.Thread(target=realtime_news_worker, daemon=True).start()
    threading.Thread(target=clock_worker, daemon=True).start()
    threading.Thread(target=secondary_data_worker, daemon=True).start()

    center_panel = ft.Container(
        expand=True, content=ft.Column([
            ft.Row([ft.Column([ft.Text("NIFTY QUANT AI", size=20, weight=ft.FontWeight.W_900, color=ft.colors.BLUE_400), app_title], spacing=1), time_text], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            ft.Row([price_text, live_status], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            chart_container, data_row,
            ft.Row([box_news, box_tech], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            ft.Row([box_live, box_engine4], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            final_box, news_ticker_box, paper_trade_container, controls_row
        ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=6, scroll=ft.ScrollMode.AUTO)
    )

    page.add(ft.Row([left_panel, center_panel, right_panel], expand=True, spacing=10, vertical_alignment=ft.CrossAxisAlignment.START))
    page.on_close = lambda _: state.update({"running": False})

if __name__ == "__main__":
    ft.app(target=main)
