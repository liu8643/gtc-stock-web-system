# GTC 股票專業版看盤分析系統 v5.3.1 Core Engine
# Extracted from v5.2.5 BattlePlan ControlOverlay AutoCodes.
# Pure analysis/core module for desktop/web reuse.
# No Tkinter / Streamlit UI dependency.

# v5.2.0 Phase4 Takeover 機構級交易引擎接管版
# 已整合：
# 1. 波浪理論結構化欄位 wave_stage / wave_score / wave_risk_flag
# 2. 費波南西位置欄位 fibo_position / fibo_score / fibo_risk_flag
# 3. Decision Layer：final_decision / execution_ready / decision_reason
# 4. UI / CSV / PDF / TXT 同步顯示波費與最終決策
# 5. RR 閘門與禁追風險優先級
# 6. Phase4：進場區判斷 / 倉位管理 / 波浪RR風險資金配置 / 驗收規則

from datetime import datetime
from functools import lru_cache
import pandas as pd
import yfinance as yf
import requests
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
import os
import csv
import logging

APP_TITLE = "GTC 股票專業版看盤分析系統"
APP_VERSION = "v5.3.1-Core-Engine"
DECISION_MODEL_VERSION = "EXEC-P5-WAVE-POSITION-20260509"
AUTO_REFRESH_MS = 30000
DEFAULT_ACCOUNT_CAPITAL = 1000000
DEFAULT_RISK_PCT = 1.0
MIN_BUY_RR = 1.5
MIN_BUY_ALLOCATION_SCORE = 70

LOG_FILE = "gtc_phase4_decision.log"
MIS_BASE_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
MIS_REFERER = "https://mis.twse.com.tw/stock/index?lang=zhHant"
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    encoding="utf-8"
)


def setup_pdf_font():
    candidates = [
        r"C:\Windows\Fonts\msjh.ttc",
        r"C:\Windows\Fonts\msjh.ttf",
        r"C:\Windows\Fonts\mingliu.ttc",
        r"C:\Windows\Fonts\kaiu.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                pdfmetrics.registerFont(TTFont("CH_FONT", path))
                return "CH_FONT"
            except Exception:
                pass
    return "Helvetica"

def normalize_symbol(symbol: str) -> list[str]:
    s = symbol.strip().upper()
    if not s:
        return []
    if "." in s:
        return [s]
    if s.isdigit():
        if len(s) == 4:
            # P1-01：依官方交易所名單決定上市/上櫃，避免上市股先打 .TWO 造成資料錯誤。
            exchange = get_tw_exchange_map().get(s)
            if exchange == "上市":
                return [f"{s}.TW"]
            if exchange == "上櫃":
                return [f"{s}.TWO"]
            # 無法判斷時採上市優先，再嘗試上櫃，不再先 .TWO。
            return [f"{s}.TW", f"{s}.TWO"]
        return [s]
    return [s]

@lru_cache(maxsize=1)
def get_tw_name_map():
    mapping = {}
    sources = [
        "https://openapi.twse.com.tw/v1/opendata/t187ap03_L",
        "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O",
    ]
    for url in sources:
        try:
            df = pd.read_json(url)
            code_col = None
            name_col = None
            for c in df.columns:
                c_str = str(c).strip()
                if code_col is None and ("代號" in c_str or "Code" in c_str):
                    code_col = c
                if name_col is None and ("簡稱" in c_str or "名稱" in c_str or "Name" in c_str):
                    name_col = c
            if code_col is None or name_col is None:
                continue
            for _, row in df.iterrows():
                code = str(row[code_col]).strip()
                name = str(row[name_col]).strip()
                if code.isdigit() and len(code) == 4 and name:
                    mapping[code] = name
        except Exception:
            continue
    return mapping


@lru_cache(maxsize=1)
def get_tw_exchange_map():
    """P1-01：建立台股代號交易所對照表，上市回 .TW、上櫃回 .TWO。"""
    mapping = {}
    sources = [
        ("上市", "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"),
        ("上櫃", "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"),
    ]
    for exchange, url in sources:
        try:
            df = pd.read_json(url)
            code_col = None
            for c in df.columns:
                c_str = str(c).strip()
                if code_col is None and ("代號" in c_str or "Code" in c_str):
                    code_col = c
            if code_col is None:
                continue
            for _, row in df.iterrows():
                code = str(row[code_col]).strip()
                if code.isdigit() and len(code) == 4:
                    mapping[code] = exchange
        except Exception as e:
            logging.warning("TW_EXCHANGE_MAP_FAILED url=%s error=%s", url, e)
            continue
    return mapping

def get_stock_name(input_symbol: str, yf_symbol: str) -> str:
    if input_symbol.isdigit() and len(input_symbol) == 4:
        tw_map = get_tw_name_map()
        if input_symbol in tw_map:
            return tw_map[input_symbol]
    try:
        ticker = yf.Ticker(yf_symbol)
        info = ticker.info
        name = info.get("shortName") or info.get("longName")
        if name:
            return str(name)
    except Exception:
        pass
    return yf_symbol

def download_symbol_data(symbol: str, period: str = "12mo") -> tuple[str, pd.DataFrame]:
    candidates = normalize_symbol(symbol)
    last_error = None
    for yf_symbol in candidates:
        try:
            df = yf.download(
                yf_symbol,
                period=period,
                interval="1d",
                auto_adjust=False,
                progress=False,
                threads=False,
            )
            if df is None or df.empty:
                logging.info("YFINANCE_CANDIDATE_EMPTY input=%s candidate=%s", symbol, yf_symbol)
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0] for c in df.columns]
            needed = ["Open", "High", "Low", "Close", "Volume"]
            if not all(c in df.columns for c in needed):
                logging.info("YFINANCE_CANDIDATE_MISSING_COLUMNS input=%s candidate=%s columns=%s", symbol, yf_symbol, list(df.columns))
                continue
            df = df.dropna(subset=["Close"]).copy()
            if df.empty:
                logging.info("YFINANCE_CANDIDATE_AFTER_DROP_EMPTY input=%s candidate=%s", symbol, yf_symbol)
                continue
            return yf_symbol, df
        except Exception as e:
            last_error = e
    if last_error:
        raise ValueError(f"查無資料：{symbol} / {last_error}")
    raise ValueError(f"查無資料：{symbol}")

def round_price(v: float) -> float:
    return round(float(v), 2)

def safe_float(v, default=None):
    try:
        if v in (None, "", "-", "--"):
            return default
        return float(v)
    except Exception:
        return default

def safe_int(v, default=None):
    try:
        if v in (None, "", "-", "--"):
            return default
        return int(float(v))
    except Exception:
        return default

def split_prices(text):
    if not text:
        return []
    vals = []
    for x in str(text).split("_"):
        v = safe_float(x)
        if v is not None and v > 0:
            vals.append(round_price(v))
    return vals

def split_ints(text):
    if not text:
        return []
    vals = []
    for x in str(text).split("_"):
        v = safe_int(x)
        if v is not None and v >= 0:
            vals.append(v)
    return vals

def get_orderbook_bias(bid_vols, ask_vols):
    buy_qty = sum(bid_vols[:5]) if bid_vols else 0
    sell_qty = sum(ask_vols[:5]) if ask_vols else 0
    if buy_qty == 0 and sell_qty == 0:
        return {"buy_qty": 0, "sell_qty": 0, "ratio": "-", "bias": "無有效五檔"}
    if sell_qty == 0:
        return {"buy_qty": buy_qty, "sell_qty": sell_qty, "ratio": "∞", "bias": "買盤明顯偏強"}
    ratio = buy_qty / sell_qty
    if ratio >= 1.5:
        bias = "買盤偏強"
    elif ratio <= 0.67:
        bias = "賣盤偏強"
    else:
        bias = "多空均衡"
    return {"buy_qty": buy_qty, "sell_qty": sell_qty, "ratio": f"{ratio:.2f}", "bias": bias}

def detect_market(input_symbol: str, yf_symbol: str) -> str:
    if yf_symbol.endswith(".TW"):
        return "台股上市"
    if yf_symbol.endswith(".TWO"):
        return "台股上櫃"
    if input_symbol.isalpha():
        return "美股/海外"
    return "其他"

def fetch_mis_msg_array(ex_ch: str, retries: int = 2, timeout: int = 8) -> list:
    """P1-02：TWSE MIS 共用請求 helper，統一 Referer、retry 與 log。"""
    params = {"ex_ch": ex_ch, "json": "1", "delay": "0", "_": str(int(datetime.now().timestamp() * 1000))}
    headers = {"User-Agent": "Mozilla/5.0", "Referer": MIS_REFERER}
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(MIS_BASE_URL, params=params, headers=headers, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            msg_array = data.get("msgArray", []) or []
            logging.info("MIS_REQUEST ex_ch=%s attempt=%s msg_count=%s", ex_ch, attempt, len(msg_array))
            if msg_array:
                return msg_array
        except Exception as e:
            last_error = e
            logging.warning("MIS_REQUEST_FAILED ex_ch=%s attempt=%s error=%s", ex_ch, attempt, e)
    if last_error:
        logging.warning("MIS_REQUEST_EMPTY ex_ch=%s retries=%s last_error=%s", ex_ch, retries, last_error)
    return []

def get_tw_realtime_quote(symbol: str, market: str) -> dict | None:
    if market not in ("台股上市", "台股上櫃"):
        return None
    ex_prefix = "tse" if market == "台股上市" else "otc"
    ex_ch = f"{ex_prefix}_{symbol}.tw"
    try:
        msg_array = fetch_mis_msg_array(ex_ch, retries=2, timeout=8)
        if not msg_array:
            return None
        item = msg_array[0]
        last_trade = safe_float(item.get("z"))
        open_price = safe_float(item.get("o"))
        high_price = safe_float(item.get("h"))
        low_price = safe_float(item.get("l"))
        prev_close = safe_float(item.get("y"))
        ask_prices = split_prices(item.get("a"))
        bid_prices = split_prices(item.get("b"))
        ask_vols = split_ints(item.get("f"))
        bid_vols = split_ints(item.get("g"))
        indicative_price = None
        if bid_prices and ask_prices:
            indicative_price = round_price((bid_prices[0] + ask_prices[0]) / 2)
        elif bid_prices:
            indicative_price = bid_prices[0]
        elif ask_prices:
            indicative_price = ask_prices[0]
        if last_trade is not None:
            display_price = round_price(last_trade)
            display_note = "即時成交價"
            quote_quality = "REALTIME"
        elif indicative_price is not None:
            display_price = round_price(indicative_price)
            display_note = "當下無成交，改用買一/賣一中間價"
            quote_quality = "MID_QUOTE"
        elif prev_close is not None:
            display_price = round_price(prev_close)
            display_note = "當下無成交且無五檔，暫以昨收顯示"
            quote_quality = "PREV_CLOSE_FALLBACK"
        else:
            return None
        ob = get_orderbook_bias(bid_vols, ask_vols)
        return {
            "close": display_price,
            "display_price": display_price,
            "display_note": display_note,
            "quote_quality": quote_quality,
            "analysis_price_valid": True,
            "execution_price_valid": bool(last_trade is not None),
            "last_trade": round_price(last_trade) if last_trade is not None else None,
            "indicative_price": round_price(indicative_price) if indicative_price is not None else None,
            "prev_close": round_price(prev_close if prev_close is not None else display_price),
            "open": round_price(open_price if open_price is not None else display_price),
            "high": round_price(high_price if high_price is not None else display_price),
            "low": round_price(low_price if low_price is not None else display_price),
            "bid_prices": bid_prices,
            "ask_prices": ask_prices,
            "bid_vols": bid_vols,
            "ask_vols": ask_vols,
            "buy_qty": ob["buy_qty"],
            "sell_qty": ob["sell_qty"],
            "orderbook_ratio": ob["ratio"],
            "orderbook_bias": ob["bias"],
            "quote_time": item.get("t") or item.get("tt") or "",
            "source": "TWSE MIS 即時",
        }
    except Exception:
        return None

def get_us_yahoo_quote(yf_symbol: str, fallback_close: float, fallback_prev_close: float, fallback_open: float, fallback_high: float, fallback_low: float) -> dict:
    live_price = fallback_close
    prev_close = fallback_prev_close
    open_price = fallback_open
    high_price = fallback_high
    low_price = fallback_low
    try:
        ticker = yf.Ticker(yf_symbol)
        try:
            fi = ticker.fast_info
            if fi:
                lp = fi.get("lastPrice")
                pc = fi.get("previousClose")
                day_high = fi.get("dayHigh")
                day_low = fi.get("dayLow")
                day_open = fi.get("open")
                if lp is not None:
                    live_price = round(float(lp), 2)
                if pc is not None:
                    prev_close = round(float(pc), 2)
                if day_high is not None:
                    high_price = round(float(day_high), 2)
                if day_low is not None:
                    low_price = round(float(day_low), 2)
                if day_open is not None:
                    open_price = round(float(day_open), 2)
        except Exception:
            pass
        try:
            info = ticker.info
            rp = info.get("regularMarketPrice")
            pcp = info.get("regularMarketPreviousClose")
            day_high = info.get("regularMarketDayHigh")
            day_low = info.get("regularMarketDayLow")
            day_open = info.get("regularMarketOpen")
            if rp is not None:
                live_price = round(float(rp), 2)
            if pcp is not None:
                prev_close = round(float(pcp), 2)
            if day_high is not None:
                high_price = round(float(day_high), 2)
            if day_low is not None:
                low_price = round(float(day_low), 2)
            if day_open is not None:
                open_price = round(float(day_open), 2)
        except Exception:
            pass
    except Exception:
        pass
    return {"close": live_price, "prev_close": prev_close, "open": open_price, "high": high_price, "low": low_price, "source": "Yahoo Finance"}

def calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["MA5"] = df["Close"].rolling(5).mean()
    df["MA10"] = df["Close"].rolling(10).mean()
    df["MA20"] = df["Close"].rolling(20).mean()
    df["MA60"] = df["Close"].rolling(60).mean()
    delta = df["Close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    df["RSI"] = 100 - (100 / (1 + rs))
    df["RSI"] = df["RSI"].fillna(50)
    ema12 = df["Close"].ewm(span=12, adjust=False).mean()
    ema26 = df["Close"].ewm(span=26, adjust=False).mean()
    df["MACD"] = ema12 - ema26
    df["MACD_SIGNAL"] = df["MACD"].ewm(span=9, adjust=False).mean()
    low9 = df["Low"].rolling(9).min()
    high9 = df["High"].rolling(9).max()
    rsv = (df["Close"] - low9) / (high9 - low9) * 100
    df["K"] = rsv.ewm(com=2).mean()
    df["D"] = df["K"].ewm(com=2).mean()
    return df

def calc_professional_sr(df: pd.DataFrame) -> dict:
    recent20 = df.tail(20)
    recent40 = df.tail(40)
    close = float(df["Close"].iloc[-1])
    support_20 = float(recent20["Low"].min())
    resistance_20 = float(recent20["High"].max())
    swing_low = float(recent40["Low"].min())
    swing_high = float(recent40["High"].max())
    last_bar = df.iloc[-1]
    pivot = (float(last_bar["High"]) + float(last_bar["Low"]) + float(last_bar["Close"])) / 3
    r1 = pivot * 2 - float(last_bar["Low"])
    s1 = pivot * 2 - float(last_bar["High"])
    support_candidates = [support_20, swing_low, s1]
    resistance_candidates = [resistance_20, swing_high, r1]
    supports_below = [x for x in support_candidates if x <= close]
    main_support = max(supports_below) if supports_below else min(support_candidates)
    resistances_above = [x for x in resistance_candidates if x >= close]
    main_resistance = min(resistances_above) if resistances_above else max(resistance_candidates)
    return {
        "support": round_price(main_support),
        "resistance": round_price(main_resistance),
        "support20": round_price(support_20),
        "resistance20": round_price(resistance_20),
        "swing_low": round_price(swing_low),
        "swing_high": round_price(swing_high),
        "pivot": round_price(pivot),
        "s1": round_price(s1),
        "r1": round_price(r1),
    }

def build_trade_advice(close, ma20, ma60, score, rsi, support, resistance, change_pct, intraday_score=0, open_price=None, prev_close=None, trend_score=0, orderbook_bias="無"):
    if change_pct <= -9.0:
        return "觀望為主"
    if close < support:
        return "減碼/防守"
    if close > resistance and trend_score >= 82 and intraday_score >= 78 and orderbook_bias in ("買盤偏強", "買盤明顯偏強"):
        return "突破追價"
    if score >= 95 and trend_score >= 90 and intraday_score >= 85:
        return "拉回加碼"
    if score >= 82 and trend_score >= 80 and intraday_score >= 70 and change_pct > 0.5:
        return "低接布局"
    if score >= 45 and support <= close <= resistance:
        return "區間操作"
    if rsi > 70:
        return "減碼/防守"
    if close < ma20 and close < ma60 and change_pct < 0:
        return "減碼/防守"
    return "觀望為主"


def classify_trade_type(state_bucket: str, signal: str, advice: str) -> str:
    if "第3浪" in signal:
        return "主升觀察池"
    if "修正反彈" in signal:
        return "高風險反彈池"
    if "末升" in signal:
        return "高風險末升池"
    if "整理偏多" in signal:
        return "中線整理觀察池"
    if signal == "突破強勢" or "突破可追" in advice:
        return "主升確認池"
    if state_bucket == "strong":
        return "主升確認池"
    if state_bucket == "bullish":
        return "主升觀察池"
    if state_bucket == "range":
        return "突破前觀察池"
    return "觀察池"



def normalize_quote_quality(note: str, source: str = "", last_trade=None, indicative_price=None) -> str:
    """V6：報價品質分層。可分析價與可下單成交價分離。"""
    if last_trade is not None:
        return "REALTIME"
    note = str(note or "")
    source = str(source or "")
    if "中間價" in note or indicative_price is not None:
        return "MID_QUOTE"
    if "昨收" in note:
        return "PREV_CLOSE_FALLBACK"
    if "日線回退" in note or "日線" in source:
        return "DAILY_FALLBACK"
    return "UNKNOWN"


def display_quote_note(quote_quality: str) -> str:
    mapping = {
        "REALTIME": "即時成交，可作執行參考",
        "MID_QUOTE": "無即時成交，僅供分析，不可直接下單",
        "PREV_CLOSE_FALLBACK": "昨收/回退參考價，僅供分析，不可直接下單",
        "DAILY_FALLBACK": "日線回退參考價，僅供分析，不可直接下單",
    }
    return mapping.get(quote_quality, "報價品質待確認，僅供分析")


def semantic_signal_from_structure(signal: str, wave_stage: str, fibo_position: str, quote_quality: str = "REALTIME", phase5: dict | None = None) -> str:
    """Phase5：外顯訊號語義。修正反彈不得再單獨顯示，必須帶波段定位。"""
    suffix = "（成交待確認）" if quote_quality != "REALTIME" else ""
    phase5 = phase5 or {}
    label = phase5.get("phase5_wave_label", "")
    rebound_type = phase5.get("rebound_type", "")
    minor_wave = phase5.get("minor_wave", "")
    major_wave = phase5.get("major_wave", "")
    if phase5.get("escape_rally"):
        return f"{label or '主跌反彈'}（逃命反彈風控）" + suffix
    if wave_stage == "A/C修正浪" or "修正反彈" in str(signal):
        if label:
            return label + suffix
        if rebound_type:
            return f"{major_wave}/{minor_wave}/{rebound_type}" + suffix
        return "修正反彈待定位" + suffix
    if wave_stage == "第3浪" and fibo_position == "挑戰1.0前":
        return "第3浪突破前觀察" + suffix
    if phase5.get("impulsive_wave"):
        return "第3浪推動啟動" + suffix
    if wave_stage == "第3浪":
        return "第3浪主升觀察" + suffix
    if wave_stage == "第5浪":
        return "末升風險" + suffix
    if wave_stage == "整理偏多":
        return "整理偏多觀察" + suffix
    if wave_stage in ("整理/待確認", "第2浪/回測浪"):
        return "整理待確認" + suffix
    if signal == "資料待確認":
        return "等待成交確認" + suffix
    return signal


def semantic_advice_from_state(result: dict) -> str:
    quote_quality = result.get("quote_quality", "REALTIME")
    wave_stage = result.get("wave_stage", "-")
    entry_status = result.get("entry_zone_status", "-")
    decision = result.get("final_decision", "WAIT")
    if result.get("escape_rally"):
        return "逃命反彈風控，不追價"
    if result.get("rebound_type") == "跌深技術反彈":
        return "跌深技術反彈，等待修正完成"
    if result.get("rebound_type") == "主升拉回" and not result.get("correction_completed"):
        return "主升拉回觀察，等待止穩完成"
    if decision == "BUY":
        return result.get("advice", "可依策略執行")
    if quote_quality != "REALTIME":
        if wave_stage == "第3浪":
            return "等待成交確認；突破可觀察"
        return "等待成交確認"
    if entry_status == "ABOVE_ENTRY":
        return "等待回測，不追價"
    if entry_status in ("NO_CHASE", "BROKEN"):
        return "風控優先"
    if wave_stage == "A/C修正浪":
        return "修正反彈待確認"
    return result.get("advice", "觀望")


def classify_candidate_pool(result: dict) -> str:
    wave_stage = result.get("wave_stage", "-")
    fibo_position = result.get("fibo_position", "-")
    second_wave_score = safe_float(result.get("second_wave_score"), 0) or 0
    if result.get("escape_rally"):
        return "禁追風控池"
    if result.get("rebound_type") == "跌深技術反彈":
        return "高風險反彈池"
    if result.get("rebound_type") == "主升拉回" and result.get("correction_completed"):
        return "中線低接池"
    if result.get("rebound_type") == "主升拉回":
        return "中線整理觀察池"
    if result.get("impulsive_wave"):
        return "主升確認池"
    if wave_stage == "第3浪" and (fibo_position == "挑戰1.0前" or second_wave_score >= 70):
        return "主升觀察池"
    if wave_stage == "第3浪":
        return "主升確認池"
    if wave_stage == "第5浪":
        return "高風險末升池"
    if wave_stage == "A/C修正浪":
        return "修正觀察池"
    if wave_stage == "整理偏多":
        return "中線整理觀察池"
    return "突破前觀察池"


def semantic_leader_candidate(result: dict) -> str:
    wave_stage = result.get("wave_stage", "-")
    fibo_position = result.get("fibo_position", "-")
    if result.get("escape_rally"):
        return "非主升/逃命反彈"
    if result.get("impulsive_wave"):
        return "主升確認"
    if result.get("rebound_type") == "主升拉回":
        return "主升拉回觀察" if not result.get("correction_completed") else "主升拉回完成"
    if result.get("fibo_risk_flag") or result.get("wave_risk_flag"):
        if wave_stage == "A/C修正浪":
            return "修正觀察"
        return "末升風險"
    if wave_stage == "第3浪" and fibo_position == "挑戰1.0前":
        return "主升觀察"
    if wave_stage == "第3浪":
        return "主升確認"
    if wave_stage == "整理偏多":
        return "整理觀察"
    if wave_stage == "A/C修正浪":
        return "修正觀察"
    return "非主升"


def detect_wave_n_breakout(df: pd.DataFrame, wave: dict, fibo_pos: dict, close: float) -> dict:
    """V6：第N波/第二波突破偵測器。用於保留彩晶類主升前候選，不直接代表可下單。"""
    try:
        lookback = df.tail(80).copy()
        if len(lookback) < 30:
            return {"breakout_attempt_count": 0, "second_wave_score": 0, "wave_n_breakout": False, "volume_breakout_confirm": False}
        rolling_high = lookback["High"].rolling(20).max().shift(1)
        attempts = ((lookback["Close"] > rolling_high) & rolling_high.notna()).sum()
        vol20 = lookback["Volume"].tail(20).mean()
        vol5 = lookback["Volume"].tail(5).mean()
        volume_ratio = float(vol5 / vol20) if vol20 and vol20 > 0 else 0.0
        volume_confirm = volume_ratio >= 1.25
        base = 0
        if wave.get("wave_stage") == "第3浪":
            base += 45
        if fibo_pos.get("fibo_position") in ("挑戰1.0前", "站上/測試1.0", "1.0~1.382主升區"):
            base += 20
        if attempts >= 2:
            base += 20
        elif attempts == 1:
            base += 10
        if volume_confirm:
            base += 15
        score = max(0, min(100, int(base)))
        return {
            "breakout_attempt_count": int(attempts),
            "second_wave_score": score,
            "wave_n_breakout": bool(score >= 70),
            "volume_breakout_confirm": bool(volume_confirm),
            "volume_ratio": round(volume_ratio, 2),
        }
    except Exception:
        return {"breakout_attempt_count": 0, "second_wave_score": 0, "wave_n_breakout": False, "volume_breakout_confirm": False}


def build_rank_scores(result: dict) -> dict:
    """V6：結構分與執行分分離。不可下單只影響 execution，不抹殺 structure。"""
    risk_penalty = 18 if (result.get("fibo_risk_flag") or result.get("wave_risk_flag")) else 0
    leader = result.get("leader_candidate", "")
    leader_bonus = 15 if leader in ("主升確認", "主升觀察") else (6 if leader in ("整理觀察", "修正觀察") else 0)
    structure_score = (
        result.get("score", 0) * 0.40 +
        result.get("trend_score", 0) * 0.30 +
        result.get("intraday_score", 0) * 0.10 +
        result.get("wave_score", 0) * 1.0 +
        result.get("fibo_score", 0) * 1.0 +
        (safe_float(result.get("second_wave_score"), 0) or 0) * 0.25 +
        result.get("change_pct", 0) * 0.8 +
        leader_bonus -
        risk_penalty
    )
    execution_penalty = 0
    if result.get("allocation_grade") == "BLOCK":
        execution_penalty += 15
    if result.get("final_decision") == "WAIT":
        execution_penalty += 6
    if result.get("final_decision") == "AVOID":
        execution_penalty += 20
    execution_score = (
        result.get("allocation_score", 0) * 0.50 +
        (20 if result.get("execution_ready") else 0) +
        (12 if result.get("entry_zone_ready") else 0) +
        (10 if result.get("position_size_pct", 0) > 0 else 0) -
        execution_penalty
    )
    structure_score = round(max(0, min(100, structure_score)), 2)
    execution_score = round(max(0, min(100, execution_score)), 2)
    final_rank_score = round(structure_score * 0.75 + execution_score * 0.25, 2)
    return {
        "structure_rank_score": structure_score,
        "execution_rank_score": execution_score,
        "final_rank_score": final_rank_score,
        "rank_score": final_rank_score,
    }


def build_risk_note(close, support, resistance, rsi, score, change_pct=None,
                    wave_stage="-", fibo_position="-", fibo_risk_flag=False,
                    wave_risk_flag=False, rr_valid=True, price_valid=True):
    notes = []
    if not price_valid:
        notes.append("報價非即時成交或為回退資料，不可直接下單")
    if fibo_risk_flag:
        notes.append(f"費波位置為「{fibo_position}」，觸發禁追風險")
    if wave_risk_flag:
        notes.append(f"波浪定位為「{wave_stage}」，需防末升或修正風險")
    if not rr_valid:
        notes.append("RR未達有效門檻，買進條件不足")
    if change_pct is not None and change_pct <= -7:
        notes.append("當日跌幅偏大，短線波動風險升高")
    if change_pct is not None and change_pct <= -9:
        notes.append("接近或達跌停級別，避免把急跌誤判為強勢買點")
    if close <= support * 1.01:
        notes.append("接近支撐，觀察是否守穩")
    if close < support:
        notes.append("已跌破支撐，需提高風險控管")
    if close >= resistance * 0.99:
        notes.append("逼近壓力，留意獲利了結賣壓")
    if close > resistance:
        notes.append("已突破壓力，觀察是否假突破")
    if rsi >= 70:
        notes.append("RSI 偏高，短線過熱風險上升")
    if rsi <= 30:
        notes.append("RSI 偏低，可能進入超跌區")
    if score < 30:
        notes.append("綜合評分偏弱，不宜積極追價")
    if not notes:
        notes.append("目前技術面無明顯異常，但仍須控管部位")
    return "；".join(notes)

def build_ai_analysis(data: dict) -> str:
    close = data["close"]
    ma20 = data["ma20"]
    ma60 = data["ma60"]
    rsi = data["rsi"]
    score = data["score"]
    trend_score = data.get("trend_score", score)
    intraday_score = data.get("intraday_score", score)
    support = data["support"]
    resistance = data["resistance"]
    signal = data["signal"]
    advice = data["advice"]
    orderbook_bias = data.get("orderbook_bias", "無")
    orderbook_ratio = data.get("orderbook_ratio", "-")
    change_pct = data.get("change_pct", 0.0)
    if close >= ma20 and close >= ma60:
        trend_text = "目前股價位於20日線與60日線之上，中期趨勢偏強。"
        trend = "偏多"
    elif close >= ma20 and close < ma60:
        trend_text = "目前股價站上20日線，但仍在60日線下方，屬短強中性結構。"
        trend = "盤整偏多"
    elif close < ma20 and close >= ma60:
        trend_text = "目前股價跌破20日線但仍守住60日線，短線轉弱、中期待觀察。"
        trend = "盤整偏弱"
    else:
        trend_text = "目前股價位於20日線與60日線下方，技術面偏弱。"
        trend = "偏空"
    if close < support:
        pos_text = f"目前股價 {close} 已跌破支撐 {support}，位置偏弱。"
    elif close > resistance:
        pos_text = f"目前股價 {close} 已突破壓力 {resistance}，位置轉強。"
    else:
        pos_text = f"目前股價位於支撐 {support} 與壓力 {resistance} 之間，仍屬區間內。"
    if rsi >= 70:
        rsi_text = f"RSI為 {rsi}，已接近或進入過熱區，短線需留意震盪與拉回。"
    elif rsi <= 30:
        rsi_text = f"RSI為 {rsi}，已進入相對低檔區，若量價配合有機會出現反彈。"
    elif rsi >= 55:
        rsi_text = f"RSI為 {rsi}，動能偏強，但仍需觀察是否能持續放大。"
    elif rsi >= 40:
        rsi_text = f"RSI為 {rsi}，動能中性偏弱，屬整理觀察區。"
    else:
        rsi_text = f"RSI為 {rsi}，動能偏弱，短線仍需保守。"
    ob_text = f"五檔力道為「{orderbook_bias}」，委買/委賣比為 {orderbook_ratio}。"
    if score >= 80:
        score_text = "綜合評分屬高分區，結構偏強。"
    elif score >= 65:
        score_text = "綜合評分中上，偏多但仍需確認續航力。"
    elif score >= 45:
        score_text = "綜合評分中性，屬區間整理型。"
    else:
        score_text = "綜合評分偏弱，先以風險控制優先。"
    if change_pct <= -9:
        drop_text = f"當日跌幅 {change_pct:+.2f}% 已屬高風險急跌，不宜僅因均線與歷史分數誤判為強勢買點。"
    elif change_pct <= -5:
        drop_text = f"當日跌幅 {change_pct:+.2f}% 偏大，需提高風險意識。"
    elif change_pct >= 5:
        drop_text = f"當日漲幅 {change_pct:+.2f}% 偏強，需觀察是否放量續攻。"
    else:
        drop_text = f"當日漲跌幅 {change_pct:+.2f}% 屬正常波動區間。"
    final_text = f"AI綜合判斷：趨勢偏向「{trend}」，訊號為「{signal}」，建議採取「{advice}」策略。"
    return "\n".join([
        "【AI個股分析】",
        f"1. 趨勢判讀：{trend_text}",
        f"2. 位置判讀：{pos_text}",
        f"3. 動能狀態：{rsi_text}",
        f"4. 五檔力道：{ob_text}",
        f"5. 當日強弱：{drop_text}",
        f"6. 分數解讀：{score_text}（波段分={trend_score} / 盤中分={intraday_score} / 總分={score}）",
        f"7. AI結論：{final_text}",
    ])

def detect_local_pivots(series: pd.Series, left: int = 2, right: int = 2):
    pivots = []
    values = series.tolist()
    for i in range(left, len(values) - right):
        window = values[i - left:i + right + 1]
        center = values[i]
        if center == max(window):
            pivots.append((i, "H", float(center)))
        elif center == min(window):
            pivots.append((i, "L", float(center)))
    return pivots

def summarize_wave(df: pd.DataFrame, period: int, label: str) -> str:
    part = df.tail(period).copy()
    if len(part) < 15:
        return f"{label}：資料不足，暫無法判讀。"
    close_start = float(part["Close"].iloc[0])
    close_end = float(part["Close"].iloc[-1])
    highest = float(part["High"].max())
    lowest = float(part["Low"].min())
    amplitude_pct = ((highest - lowest) / lowest * 100) if lowest != 0 else 0
    ma20_last = float(part["Close"].rolling(20).mean().iloc[-1]) if len(part) >= 20 else close_end
    ma60_last = float(part["Close"].rolling(60).mean().iloc[-1]) if len(part) >= 60 else close_end
    pivots = detect_local_pivots(part["Close"], left=2, right=2)
    recent_pivots = pivots[-6:] if len(pivots) >= 6 else pivots
    if close_end > close_start and close_end >= ma20_last:
        if len(recent_pivots) >= 5:
            wave_hint = "較偏推動浪結構，可能處於第3浪或第5浪延伸區。"
        else:
            wave_hint = "偏多推升結構，可能處於推動浪初升段。"
    elif close_end < close_start and close_end < ma20_last:
        if len(recent_pivots) >= 4:
            wave_hint = "較偏修正浪結構，可能位於 A / C 浪下修階段。"
        else:
            wave_hint = "偏弱修正結構，較像回檔整理波。"
    else:
        wave_hint = "目前較像整理浪或轉折確認階段，尚未形成明確單邊波段。"
    if close_end >= ma20_last and close_end >= ma60_last:
        trend_hint = "均線結構偏多。"
    elif close_end >= ma20_last and close_end < ma60_last:
        trend_hint = "短線偏強，但中期壓力仍在。"
    elif close_end < ma20_last and close_end >= ma60_last:
        trend_hint = "短線轉弱，中期尚未完全破壞。"
    else:
        trend_hint = "短中期均線結構偏弱。"
    return f"{label}：區間波動約 {amplitude_pct:.2f}% ，{wave_hint}{trend_hint}"

def build_wave_analysis(df: pd.DataFrame) -> str:
    return "\n".join([
        "【波浪理論分析】",
        f"1. {summarize_wave(df, 20, '短期')}",
        f"2. {summarize_wave(df, 60, '中期')}",
        f"3. {summarize_wave(df, 120, '長期')}",
    ])

def calc_fibonacci_targets(df: pd.DataFrame) -> dict:
    lookback = df.tail(120).copy()
    if len(lookback) < 30:
        close_now = float(df["Close"].iloc[-1])
        return {
            "direction": "資料不足",
            "base_low": round_price(close_now),
            "base_high": round_price(close_now),
            "range": 0.0,
            "target_1_0": round_price(close_now),
            "target_1_382": round_price(close_now),
            "target_1_618": round_price(close_now),
            "next_target": round_price(close_now),
            "bullish_next_target": round_price(close_now),
            "bearish_next_target": round_price(close_now),
            "summary": "資料不足，暫無法估算費波南西目標位。",
        }
    close_now = float(lookback["Close"].iloc[-1])
    low_val = float(lookback["Low"].min())
    high_val = float(lookback["High"].max())
    price_range = high_val - low_val
    low_idx = lookback["Low"].idxmin()
    high_idx = lookback["High"].idxmax()
    upward = low_idx < high_idx
    if price_range <= 0:
        return {
            "direction": "整理",
            "base_low": round_price(low_val),
            "base_high": round_price(high_val),
            "range": round_price(price_range),
            "target_1_0": round_price(close_now),
            "target_1_382": round_price(close_now),
            "target_1_618": round_price(close_now),
            "next_target": round_price(close_now),
            "bullish_next_target": round_price(close_now),
            "bearish_next_target": round_price(close_now),
            "summary": "區間過小，暫不適合估算費波南西延伸目標。",
        }
    if upward:
        direction = "上升波"
        target_1_0 = high_val
        target_1_382 = low_val + price_range * 1.382
        target_1_618 = low_val + price_range * 1.618
        if close_now < target_1_0:
            next_target = target_1_0
        elif close_now < target_1_382:
            next_target = target_1_382
        else:
            next_target = target_1_618
        bullish_next_target = next_target
        bearish_next_target = low_val
        summary = f"目前較偏上升波段，近波段低點 {round_price(low_val)} 至高點 {round_price(high_val)}。若續強，下一觀察目標依序為 1.0={round_price(target_1_0)}、1.382={round_price(target_1_382)}、1.618={round_price(target_1_618)}。"
    else:
        direction = "下降波"
        target_1_0 = low_val
        target_1_382 = high_val - price_range * 1.382
        target_1_618 = high_val - price_range * 1.618
        if close_now > target_1_0:
            next_target = target_1_0
        elif close_now > target_1_382:
            next_target = target_1_382
        else:
            next_target = target_1_618
        bullish_next_target = high_val
        bearish_next_target = next_target
        summary = f"目前較偏下降修正波，近波段高點 {round_price(high_val)} 至低點 {round_price(low_val)}。若續弱，下一觀察目標依序為 1.0={round_price(target_1_0)}、1.382={round_price(target_1_382)}、1.618={round_price(target_1_618)}。"
    return {
        "direction": direction,
        "base_low": round_price(low_val),
        "base_high": round_price(high_val),
        "range": round_price(price_range),
        "target_1_0": round_price(target_1_0),
        "target_1_382": round_price(target_1_382),
        "target_1_618": round_price(target_1_618),
        "next_target": round_price(next_target),
        "bullish_next_target": round_price(bullish_next_target),
        "bearish_next_target": round_price(bearish_next_target),
        "summary": summary,
    }

def build_fibonacci_analysis(fibo: dict) -> str:
    return "\n".join([
        "【費波南西目標位】",
        f"1. 波段方向：{fibo['direction']}",
        f"2. 波段低點：{fibo['base_low']} / 波段高點：{fibo['base_high']}",
        f"3. 1.0 目標位：{fibo['target_1_0']}",
        f"4. 1.382 目標位：{fibo['target_1_382']}",
        f"5. 1.618 目標位：{fibo['target_1_618']}",
        f"6. 多方下一觀察目標：{fibo.get('bullish_next_target', fibo['next_target'])}",
        f"7. 空方風險目標：{fibo.get('bearish_next_target', fibo['next_target'])}",
        f"8. 判讀：{fibo['summary']}",
    ])

def build_bull_bear_path(data: dict) -> str:
    support = data["support"]
    resistance = data["resistance"]
    bullish_target = data.get("bullish_target", data["fibo"].get("bullish_next_target", data["fibo"].get("next_target", resistance)))
    bearish_target = data.get("bearish_target", data["fibo"].get("bearish_next_target", support))
    signal = data["signal"]
    advice = data.get("display_advice", data.get("advice", ""))
    return "\n".join([
        "【多空路徑圖示】",
        "◎ 多方路徑：",
        f"→ 多方路徑①：守住支撐 {support}",
        f"→ 多方路徑②：重新挑戰壓力 {resistance}",
        f"→ 多方路徑③：若有效突破壓力，下一目標看 {bullish_target}",
        "",
        "◎ 空方路徑：",
        f"→ 空方路徑①：若跌破支撐 {support}",
        f"→ 空方路徑②：短線結構轉弱，空方風險目標看 {bearish_target}",
        f"→ 空方路徑③：若反彈無法站回壓力 {resistance}，弱勢格局延續",
        "",
        f"【路徑結論】當前訊號為「{signal}」，操作建議為「{advice}」。",
    ])


def get_bullish_target(fibo: dict, resistance: float = 0.0) -> float:
    """V6.2：多方目標唯一來源。不得用 bearish/next_target 混入買進RR。"""
    return round_price(safe_float(fibo.get("bullish_next_target"), resistance) or resistance)


def get_bearish_target(fibo: dict, support: float = 0.0) -> float:
    """V6.2：空方風險目標唯一來源，只供風險與空方路徑。"""
    return round_price(safe_float(fibo.get("bearish_next_target"), support) or support)


def classify_technical_state(result: dict) -> str:
    """V6.2：技術結構狀態，不受報價是否可下單影響。"""
    wave_stage = result.get("wave_stage", "-")
    fibo_position = result.get("fibo_position", "-")
    signal = result.get("signal", "")
    score = safe_float(result.get("score"), 0) or 0
    trend = safe_float(result.get("trend_score"), 0) or 0
    intra = safe_float(result.get("intraday_score"), 0) or 0
    close = safe_float(result.get("close"), 0) or 0
    ma20 = safe_float(result.get("ma20"), 0) or 0
    ma60 = safe_float(result.get("ma60"), 0) or 0
    if result.get("escape_rally"):
        return "escape_rally"
    if result.get("rebound_type") == "跌深技術反彈":
        return "correction_rebound"
    if signal in ("急跌風險", "跌破支撐"):
        return "risk_broken"
    if wave_stage == "第3浪" and fibo_position == "挑戰1.0前":
        return "wave3_prebreakout"
    if wave_stage == "第3浪":
        return "wave3_active"
    if wave_stage == "第5浪" or result.get("fibo_risk_flag") or result.get("wave_risk_flag"):
        return "late_risk"
    if wave_stage == "A/C修正浪":
        return "correction_rebound"
    if wave_stage == "整理偏多" or (trend >= 72 and score >= 65 and close >= ma20 and ma20 >= ma60):
        return "neutral_bullish"
    if signal in ("偏多觀察", "整理偏多", "整理偏多觀察"):
        return "neutral_bullish"
    if score >= 45 or intra >= 45:
        return "range_watch"
    return "weak_watch"


def classify_execution_state(result: dict) -> str:
    """V6.2：執行狀態，只負責下單Gate，不覆蓋技術結構。"""
    if not bool(result.get("price_valid", False)):
        return "BLOCK_DATA"
    if result.get("final_decision") == "BUY" and result.get("execution_ready"):
        return "EXECUTION_READY"
    if result.get("final_decision") == "AVOID":
        return "BLOCK_RISK"
    if result.get("allocation_grade") == "BLOCK":
        return "BLOCK_ALLOCATION"
    if result.get("entry_zone_status") in ("ABOVE_ENTRY", "NO_CHASE", "BROKEN", "NO_PLAN"):
        return "BLOCK_ENTRY"
    if result.get("rr_valid") is False:
        return "WAIT_RR"
    return "WAIT_REALTIME"


def classify_display_state(result: dict) -> str:
    """V6.2：UI顯示狀態 = 技術狀態 + 執行狀態。"""
    tech = result.get("technical_state") or classify_technical_state(result)
    exe = result.get("execution_state") or classify_execution_state(result)
    if exe == "EXECUTION_READY":
        return "可執行"
    if exe == "BLOCK_DATA":
        if tech in ("wave3_prebreakout", "wave3_active", "neutral_bullish"):
            return "等待成交確認"
        return "資料待確認"
    if exe.startswith("BLOCK"):
        return "風控阻擋"
    if tech == "wave3_prebreakout":
        return "第3浪突破前觀察"
    if tech == "neutral_bullish":
        return "整理偏多觀察"
    if tech == "correction_rebound":
        if result.get("phase5_wave_label"):
            return result.get("phase5_wave_label")
        return "修正反彈觀察"
    return "等待條件改善"







# =========================
# Phase5 Wave Position Engine
# =========================
def _phase5_series_high_low(series_high, series_low, lookback=120):
    """Return recent swing high/low safely for Phase5 calculations."""
    try:
        high_part = series_high.tail(lookback)
        low_part = series_low.tail(lookback)
        swing_high = float(high_part.max())
        swing_low = float(low_part.min())
        return swing_high, swing_low
    except Exception:
        return 0.0, 0.0


def calculate_major_wave(df: pd.DataFrame, close: float, ma20: float, ma60: float) -> str:
    """Phase5：判定大波方向。"""
    part = df.tail(120).copy()
    if len(part) < 60:
        return "資料不足"
    try:
        high_recent = float(part["High"].tail(30).max())
        high_prev = float(part["High"].head(60).max())
        low_recent = float(part["Low"].tail(30).min())
        low_prev = float(part["Low"].head(60).min())
        highs_up = high_recent >= high_prev * 0.995
        lows_up = low_recent >= low_prev * 0.995
        highs_down = high_recent <= high_prev * 0.985
        lows_down = low_recent <= low_prev * 0.985
        if close >= ma20 >= ma60 and highs_up and lows_up:
            return "主升推動浪"
        if (close < ma20 and ma20 < ma60) or (highs_down and lows_down):
            return "主跌修正浪"
        return "大箱型整理"
    except Exception:
        if close >= ma20 >= ma60:
            return "主升推動浪"
        if close < ma20 and ma20 < ma60:
            return "主跌修正浪"
        return "大箱型整理"


def calc_fibo_retrace(df: pd.DataFrame, close: float, fibo: dict) -> dict:
    """Phase5：量化回撤/反彈比例。"""
    swing_high, swing_low = _phase5_series_high_low(df["High"], df["Low"], 120)
    rng = swing_high - swing_low
    if rng <= 0:
        return {"fibo_retrace": 0.0, "fibo_retrace_zone": "資料不足", "swing_high": round_price(close), "swing_low": round_price(close)}
    direction = fibo.get("direction", "整理")
    if direction == "下降波":
        retrace = (close - swing_low) / rng
    else:
        retrace = (swing_high - close) / rng
    retrace = max(0.0, min(1.5, float(retrace)))
    if retrace < 0.382:
        zone = "0.382以下淺回/弱反彈"
    elif retrace < 0.5:
        zone = "0.382回撤區"
    elif retrace < 0.618:
        zone = "0.5健康回撤區"
    elif retrace < 0.786:
        zone = "0.618深回撤區"
    else:
        zone = "0.786以上結構破壞區"
    return {
        "fibo_retrace": round(retrace, 3),
        "fibo_retrace_zone": zone,
        "swing_high": round_price(swing_high),
        "swing_low": round_price(swing_low),
    }


def calculate_minor_wave(major_wave: str, wave_stage: str, fibo_retrace: float, close: float, ma20: float, ma60: float, support: float, resistance: float) -> str:
    """Phase5：判定小波位置。"""
    if major_wave == "主升推動浪":
        if 0.382 <= fibo_retrace <= 0.618:
            return "第2浪/第4浪拉回"
        if wave_stage == "第3浪":
            return "第3浪推動"
        if fibo_retrace > 0.618:
            return "深回撤待確認"
        return "主升整理小波"
    if major_wave == "主跌修正浪":
        if close < support:
            return "C浪延伸"
        if close < ma20 or close < ma60:
            if fibo_retrace < 0.5:
                return "B浪弱反彈"
            return "C浪末端反彈"
        if close >= ma20 and close < ma60:
            return "B浪反彈"
        return "C浪反彈待確認"
    if wave_stage == "A/C修正浪":
        return "A/C修正待確認"
    if wave_stage == "整理偏多":
        return "箱型整理波"
    return "小波待確認"


def classify_correction_type(df: pd.DataFrame, major_wave: str, minor_wave: str, close: float, ma60: float) -> str:
    """Phase5：修正型態粗分。"""
    try:
        part = df.tail(80).copy()
        pivots = detect_local_pivots(part["Close"], left=2, right=2)
        high_20 = float(part["High"].tail(20).max())
        high_60 = float(part["High"].tail(60).max())
        low_20 = float(part["Low"].tail(20).min())
        low_60 = float(part["Low"].tail(60).min())
        box_pct = (high_60 - low_60) / low_60 * 100 if low_60 else 0
        if major_wave == "主跌修正浪" and len(pivots) >= 5 and close < ma60:
            return "ABC修正"
        if major_wave == "主跌修正浪" and high_20 < high_60 * 0.985:
            return "Zigzag修正"
        if box_pct <= 12:
            return "Flat平台修正"
        if major_wave == "主跌修正浪" and close >= high_60 * 0.995 and close < ma60:
            return "Expanded Flat擴大型修正"
        if "拉回" in minor_wave:
            return "主升回撤修正"
        return "修正型態待確認"
    except Exception:
        if major_wave == "主跌修正浪":
            return "ABC修正"
        if "拉回" in minor_wave:
            return "主升回撤修正"
        return "修正型態待確認"


def classify_rebound_type(major_wave: str, minor_wave: str, fibo_retrace: float, close: float, ma20: float, ma60: float, rsi: float, volume_ratio: float, support: float) -> str:
    """Phase5：判斷反彈性質。"""
    if close < support:
        return "反彈失敗"
    if major_wave == "主升推動浪" and 0.382 <= fibo_retrace <= 0.618 and close >= ma20 and ma20 >= ma60:
        return "主升拉回"
    if major_wave == "主跌修正浪":
        if close < ma60 and (volume_ratio < 1.0 or rsi < 50) and fibo_retrace < 0.5:
            return "逃命反彈"
        if close < ma60:
            return "跌深技術反彈"
        return "修正反彈待確認"
    if "拉回" in minor_wave:
        return "主升拉回"
    return "一般反彈"


def is_correction_completed(df: pd.DataFrame, close: float, ma20: float, rsi: float, volume_ratio: float, resistance: float) -> bool:
    """Phase5：判斷修正是否完成。"""
    try:
        ma20_prev = float(df["MA20"].dropna().iloc[-5]) if "MA20" in df.columns and len(df["MA20"].dropna()) >= 5 else ma20
        ma20_flat_up = ma20 >= ma20_prev * 0.995
        pressure_20 = float(df["High"].tail(20).max())
        breakout_pressure = close >= min(pressure_20, resistance if resistance else pressure_20) * 0.995
        return bool(close >= ma20 and ma20_flat_up and breakout_pressure and volume_ratio >= 1.2 and rsi > 45)
    except Exception:
        return bool(close >= ma20 and volume_ratio >= 1.2 and rsi > 45)


def detect_escape_rally(major_wave: str, close: float, ma60: float, fibo_retrace: float, rsi: float, volume_ratio: float, resistance: float) -> bool:
    """Phase5：逃命反彈偵測。"""
    fail_before_resistance = close < resistance * 0.99 if resistance else True
    return bool(
        major_wave == "主跌修正浪" and
        close < ma60 and
        fail_before_resistance and
        (volume_ratio < 1.0 or rsi < 50) and
        fibo_retrace < 0.5
    )


def detect_impulsive_wave(wave_stage: str, close: float, resistance: float, ma20: float, ma60: float, volume_ratio: float, rsi: float, fibo_position: str) -> bool:
    """Phase5：真正第3浪推動啟動判斷。"""
    return bool(
        wave_stage == "第3浪" and
        (resistance <= 0 or close > resistance) and
        ma20 >= ma60 and
        volume_ratio >= 1.2 and
        45 <= rsi <= 72 and
        fibo_position in ("站上/測試1.0", "1.0~1.382主升區", "挑戰1.382")
    )


def build_phase5_wave_label(phase5: dict) -> str:
    """Phase5：人能看懂的波浪定位語義。"""
    major = phase5.get("major_wave", "-")
    minor = phase5.get("minor_wave", "-")
    rebound = phase5.get("rebound_type", "-")
    ctype = phase5.get("correction_type", "-")
    retrace = phase5.get("fibo_retrace", "-")
    if phase5.get("escape_rally"):
        return f"{major} / {minor} / {rebound}風控"
    if rebound == "跌深技術反彈":
        return f"{major} / {minor} / 跌深技術反彈"
    if rebound == "主升拉回":
        return f"{major} / {minor} / 主升拉回"
    if ctype not in (None, "-", "修正型態待確認"):
        return f"{major} / {minor} / {ctype} / 回撤{retrace}"
    return f"{major} / {minor} / {rebound}"


def calculate_phase5_wave_position(df: pd.DataFrame, wave: dict, fibo: dict, fibo_pos: dict, sr: dict, close: float, ma20: float, ma60: float, rsi: float, volume_ratio: float) -> dict:
    """Phase5：波浪定位主入口。"""
    support = safe_float(sr.get("support"), 0.0) or 0.0
    resistance = safe_float(sr.get("resistance"), 0.0) or 0.0
    major_wave = calculate_major_wave(df, close, ma20, ma60)
    retrace_info = calc_fibo_retrace(df, close, fibo)
    fibo_retrace = retrace_info.get("fibo_retrace", 0.0)
    minor_wave = calculate_minor_wave(major_wave, wave.get("wave_stage", "-"), fibo_retrace, close, ma20, ma60, support, resistance)
    correction_type = classify_correction_type(df, major_wave, minor_wave, close, ma60)
    rebound_type = classify_rebound_type(major_wave, minor_wave, fibo_retrace, close, ma20, ma60, rsi, volume_ratio, support)
    correction_completed = is_correction_completed(df, close, ma20, rsi, volume_ratio, resistance)
    escape_rally = detect_escape_rally(major_wave, close, ma60, fibo_retrace, rsi, volume_ratio, resistance) or rebound_type == "逃命反彈"
    impulsive_wave = detect_impulsive_wave(wave.get("wave_stage", "-"), close, resistance, ma20, ma60, volume_ratio, rsi, fibo_pos.get("fibo_position", "-"))
    phase5 = {
        "major_wave": major_wave,
        "minor_wave": minor_wave,
        "correction_type": correction_type,
        "fibo_retrace": fibo_retrace,
        "fibo_retrace_zone": retrace_info.get("fibo_retrace_zone", "-"),
        "rebound_type": rebound_type,
        "correction_completed": bool(correction_completed),
        "escape_rally": bool(escape_rally),
        "impulsive_wave": bool(impulsive_wave),
        "phase5_wave_label": "",
        "phase5_block_reason": "",
    }
    phase5["phase5_wave_label"] = build_phase5_wave_label(phase5)
    if phase5["escape_rally"]:
        phase5["phase5_block_reason"] = "Phase5判定為逃命反彈/主跌弱反彈，禁止追價或主動布局。"
    elif not phase5["correction_completed"] and phase5["rebound_type"] in ("跌深技術反彈", "修正反彈待確認", "一般反彈"):
        phase5["phase5_block_reason"] = "Phase5修正尚未完成，僅可觀察，等待站回均線與放量確認。"
    return phase5

def structured_wave_analysis(df: pd.DataFrame) -> dict:
    part = df.tail(120).copy()
    if len(part) < 30:
        return {
            "wave_stage": "資料不足",
            "wave_score": 0,
            "wave_reason": "日線資料少於30筆，暫不納入波浪判定。",
            "wave_risk_flag": False,
        }

    close_now = float(part["Close"].iloc[-1])
    close_start_20 = float(part["Close"].tail(20).iloc[0]) if len(part) >= 20 else float(part["Close"].iloc[0])
    ma20 = float(part["Close"].rolling(20).mean().iloc[-1]) if len(part) >= 20 else close_now
    ma60 = float(part["Close"].rolling(60).mean().iloc[-1]) if len(part) >= 60 else ma20
    rsi = float(df["RSI"].iloc[-1]) if "RSI" in df.columns and pd.notna(df["RSI"].iloc[-1]) else 50.0
    pivots = detect_local_pivots(part["Close"], left=2, right=2)
    recent_pivots = pivots[-6:]
    high_120 = float(part["High"].max())
    low_120 = float(part["Low"].min())
    range_pct = ((high_120 - low_120) / low_120 * 100) if low_120 else 0.0
    above_ma = close_now >= ma20 >= ma60
    below_ma20 = close_now < ma20
    momentum_up = close_now > close_start_20
    near_high = close_now >= high_120 * 0.94 if high_120 else False
    near_low = close_now <= low_120 * 1.08 if low_120 else False

    if above_ma and momentum_up and len(recent_pivots) >= 5 and 45 <= rsi <= 72:
        stage = "第3浪"
        score = 15
        reason = "站上MA20/MA60、20日動能向上且轉折點足夠，偏主升推動浪。"
        risk = False
    elif above_ma and momentum_up and (near_high or rsi > 72):
        stage = "第5浪"
        score = -8
        reason = "價格接近120日高點或RSI偏熱，偏末升延伸區。"
        risk = True
    elif below_ma20 and not momentum_up:
        stage = "A/C修正浪"
        score = -12
        reason = "跌破MA20且20日動能轉弱，偏修正浪。"
        risk = True
    elif near_low and rsi <= 40:
        stage = "第2浪/回測浪"
        score = 6
        reason = "接近波段低位且RSI偏低，偏回測觀察區。"
        risk = False
    elif close_now >= ma20:
        stage = "整理偏多"
        score = 5
        reason = "價格站上MA20但主升條件未完全成立，屬整理偏多。"
        risk = False
    else:
        stage = "整理/待確認"
        score = 0
        reason = f"波段振幅約{range_pct:.2f}%，尚未形成明確推動或修正結構。"
        risk = False

    return {
        "wave_stage": stage,
        "wave_score": int(score),
        "wave_reason": reason,
        "wave_risk_flag": bool(risk),
    }


def classify_fibo_position(close: float, fibo: dict) -> dict:
    direction = fibo.get("direction", "資料不足")
    t10 = safe_float(fibo.get("target_1_0"))
    t1382 = safe_float(fibo.get("target_1_382"))
    t1618 = safe_float(fibo.get("target_1_618"))

    if direction == "資料不足" or t10 is None or t1382 is None or t1618 is None:
        return {
            "fibo_position": "資料不足",
            "fibo_score": 0,
            "fibo_risk_flag": False,
            "fibo_reason": "費波南西資料不足，僅保留價格參考，不做交易升級。",
        }

    if direction == "下降波":
        if close <= t1382:
            return {
                "fibo_position": "下降延伸/破位",
                "fibo_score": -12,
                "fibo_risk_flag": True,
                "fibo_reason": "價格落在下降延伸區，優先風控。",
            }
        if close <= t10:
            return {
                "fibo_position": "跌破1.0",
                "fibo_score": -8,
                "fibo_risk_flag": True,
                "fibo_reason": "價格跌破下降波1.0目標，偏弱勢延續。",
            }
        return {
            "fibo_position": "下降波反彈區",
            "fibo_score": -2,
            "fibo_risk_flag": False,
            "fibo_reason": "下降波中反彈，需等待轉強確認。",
        }

    if close < t10 * 0.985:
        pos = "挑戰1.0前"
        score = 4
        risk = False
        reason = "尚未站上1.0目標，屬低接或突破前觀察區。"
    elif close < t10 * 1.015:
        pos = "站上/測試1.0"
        score = 10
        risk = False
        reason = "價格位於1.0目標附近，若量價配合可視為轉強確認。"
    elif close < t1382 * 0.985:
        pos = "1.0~1.382主升區"
        score = 12
        risk = False
        reason = "價格位於1.0與1.382之間，屬主升延伸有效區。"
    elif close < t1618 * 0.97:
        pos = "挑戰1.382"
        score = 8
        risk = False
        reason = "價格挑戰1.382延伸，仍可追蹤但需控管追價風險。"
    elif close <= t1618 * 1.02:
        pos = "接近1.618禁追區"
        score = -10
        risk = True
        reason = "價格接近1.618延伸目標，屬高檔禁追區。"
    else:
        pos = "突破1.618過熱區"
        score = -15
        risk = True
        reason = "價格已超過1.618延伸目標，追價風險過高。"

    return {
        "fibo_position": pos,
        "fibo_score": int(score),
        "fibo_risk_flag": bool(risk),
        "fibo_reason": reason,
    }


def build_wave_fibo_decision_note(result: dict) -> str:
    wave_stage = result.get("wave_stage", "-")
    fibo_position = result.get("fibo_position", "-")
    rr_valid = result.get("rr_valid", False)
    fibo_risk = result.get("fibo_risk_flag", False)
    wave_risk = result.get("wave_risk_flag", False)
    phase5_label = result.get("phase5_wave_label", "")
    retrace = result.get("fibo_retrace", "-")
    rebound_type = result.get("rebound_type", "-")
    correction_type = result.get("correction_type", "-")
    if result.get("escape_rally"):
        return f"逃命反彈風控：{phase5_label}，回撤={retrace}，禁止追價。"
    if rebound_type == "跌深技術反彈":
        return f"主跌弱反彈：{phase5_label}，{correction_type}，回撤={retrace}，等待修正完成。"
    if rebound_type == "主升拉回":
        done = "修正完成" if result.get("correction_completed") else "修正未完成"
        return f"主升拉回：{phase5_label}，回撤={retrace}，{done}。"
    if result.get("impulsive_wave"):
        return f"主升推動確認：{phase5_label}，第3浪推動啟動。"
    if fibo_risk or wave_risk:
        return f"禁追風控：{wave_stage} + {fibo_position}，避免高檔追價。"
    if wave_stage == "第3浪" and fibo_position == "挑戰1.0前":
        return "主升前觀察：第3浪結構，等待突破確認。"
    if wave_stage == "第3浪" and fibo_position in ("站上/測試1.0", "1.0~1.382主升區", "挑戰1.382") and rr_valid:
        return "主升確認：第3浪 + 費波主升區 + RR有效，可拉回加碼。"
    if wave_stage in ("第2浪/回測浪", "整理偏多") and fibo_position == "下降波反彈區":
        return f"修正反彈觀察：{phase5_label or '整理偏多反彈'}，仍需支撐止穩與放量轉強。"
    if wave_stage in ("第2浪/回測浪", "整理偏多"):
        return f"低接觀察：{wave_stage} + {fibo_position}，等待支撐止穩。"
    if wave_stage == "A/C修正浪":
        return f"修正反彈：{phase5_label or 'A/C修正浪'}，僅觀察止穩，不追高。"
    return f"波費待確認：{wave_stage} + {fibo_position}，以原技術訊號為主。"


def classify_entry_zone(result: dict) -> dict:
    close = safe_float(result.get("close"), 0.0) or 0.0
    entry_low = safe_float(result.get("entry_low"), 0.0) or 0.0
    entry_high = safe_float(result.get("entry_high"), 0.0) or 0.0
    support = safe_float(result.get("support"), 0.0) or 0.0
    resistance = safe_float(result.get("resistance"), 0.0) or 0.0
    stop_loss = safe_float(result.get("stop_loss"), 0.0) or 0.0
    signal = result.get("signal", "")
    rr_valid = bool(result.get("rr_valid", False))
    fibo_risk = bool(result.get("fibo_risk_flag", False) or result.get("wave_risk_flag", False))
    state = result.get("state_bucket", "range")

    if close <= 0 or not result.get("trade_plan_valid", False):
        status = "NO_PLAN"
        ready = False
        reason = "交易計畫無效或價格不足，禁止下單。"
    elif fibo_risk:
        status = "NO_CHASE"
        ready = False
        reason = "波浪/費波觸發禁追，禁止追價。"
    elif (support > 0 and close < support) or (stop_loss > 0 and close < stop_loss):
        status = "BROKEN"
        ready = False
        reason = "跌破支撐或停損線，交易條件失效。"
    elif entry_low <= close <= entry_high:
        status = "IN_ZONE"
        ready = True
        reason = "目前價格位於建議進場區間內。"
    elif close < entry_low:
        status = "WAIT_PULLBACK"
        ready = False
        reason = "價格低於進場區，等待止穩或回到有效區間。"
    elif resistance > 0 and close > resistance and signal in ("主升突破", "突破強勢") and rr_valid and state == "strong":
        status = "BREAKOUT_CONFIRM"
        ready = True
        reason = "價格突破壓力且主升/突破訊號成立，允許小倉突破確認。"
    elif close > entry_high:
        status = "ABOVE_ENTRY"
        ready = False
        reason = "價格高於建議進場區，不追價，等待回測。"
    else:
        status = "WAIT_CONFIRM"
        ready = False
        reason = "尚未符合進場條件，等待確認。"

    if entry_low > 0 and close > 0:
        if close < entry_low:
            distance = (entry_low - close) / entry_low * 100
        elif close > entry_high and entry_high > 0:
            distance = (close - entry_high) / entry_high * 100
        else:
            distance = 0.0
    else:
        distance = 0.0

    chase_risk = bool(status in ("ABOVE_ENTRY", "NO_CHASE") or (resistance > 0 and close >= resistance * 0.99))
    if status in ("NO_CHASE", "BROKEN", "NO_PLAN"):
        order_type = "禁止"
    elif status == "IN_ZONE":
        order_type = "低接限價"
    elif status == "BREAKOUT_CONFIRM":
        order_type = "突破小倉"
    else:
        order_type = "等待"

    return {
        "entry_zone_status": status,
        "entry_zone_ready": bool(ready),
        "entry_zone_reason": reason,
        "distance_to_entry_pct": round(distance, 2),
        "chase_risk_flag": chase_risk,
        "order_type_hint": order_type,
    }


def calc_wave_rr_risk_allocation(result: dict) -> dict:
    rr = safe_float(result.get("rr"), 0.0) or 0.0
    wave_stage = result.get("wave_stage", "-")
    state = result.get("state_bucket", "range")
    entry_zone_status = result.get("entry_zone_status", "NO_PLAN")
    fibo_risk = bool(result.get("fibo_risk_flag", False) or result.get("wave_risk_flag", False))
    price_valid = bool(result.get("price_valid", False))
    signal = result.get("signal", "")

    if not price_valid:
        return {
            "allocation_score": 0,
            "allocation_grade": "BLOCK",
            "allocation_multiplier": 0.0,
            "phase4_block_reason": "報價非即時有效，資金配置歸零，但不代表技術轉弱。",
        }
    if result.get("escape_rally"):
        return {
            "allocation_score": 0,
            "allocation_grade": "BLOCK",
            "allocation_multiplier": 0.0,
            "phase4_block_reason": result.get("phase5_block_reason") or "Phase5判定為逃命反彈，資金配置歸零。",
        }
    if result.get("rebound_type") == "跌深技術反彈" and not result.get("correction_completed"):
        return {
            "allocation_score": 0,
            "allocation_grade": "BLOCK",
            "allocation_multiplier": 0.0,
            "phase4_block_reason": result.get("phase5_block_reason") or "Phase5跌深技術反彈尚未完成，只能觀察。",
        }
    if fibo_risk or wave_stage in ("第5浪", "A/C修正浪") or entry_zone_status in ("NO_CHASE", "BROKEN", "NO_PLAN"):
        return {
            "allocation_score": 0,
            "allocation_grade": "BLOCK",
            "allocation_multiplier": 0.0,
            "phase4_block_reason": f"波浪/費波或進場區觸發阻擋：{wave_stage}/{entry_zone_status}。",
        }
    if rr < 1.0:
        return {
            "allocation_score": 0,
            "allocation_grade": "BLOCK",
            "allocation_multiplier": 0.0,
            "phase4_block_reason": "RR小於1，資金配置歸零。",
        }

    score = 50
    if result.get("impulsive_wave"):
        score += 30
    elif wave_stage == "第3浪":
        score += 25
    elif result.get("rebound_type") == "主升拉回" and result.get("correction_completed"):
        score += 15
    elif wave_stage in ("整理偏多", "第2浪/回測浪"):
        score += 10
    else:
        score += 0

    if rr >= 2.0:
        score += 20
    elif rr >= MIN_BUY_RR:
        score += 12
    elif rr >= 1.0:
        score += 3

    if entry_zone_status == "IN_ZONE":
        score += 15
    elif entry_zone_status == "BREAKOUT_CONFIRM":
        score += 5
    elif entry_zone_status == "ABOVE_ENTRY":
        score -= 20

    if state == "strong" or signal == "主升突破":
        score += 8
    elif state == "bullish":
        score += 3
    elif state in ("weak", "range"):
        score -= 8

    score = max(0, min(100, int(score)))
    if score >= 85:
        grade = "A"
        multiplier = 1.0
        block = ""
    elif score >= 70:
        grade = "B"
        multiplier = 0.65
        block = ""
    elif score >= 55:
        grade = "C"
        multiplier = 0.35
        block = "配置分未達主攻，僅允許小倉觀察。"
    else:
        grade = "D"
        multiplier = 0.0
        block = "配置分低於55，禁止下單。"

    return {
        "allocation_score": score,
        "allocation_grade": grade,
        "allocation_multiplier": round(multiplier, 2),
        "phase4_block_reason": block,
    }


def calc_position_sizing(result: dict, account_capital=DEFAULT_ACCOUNT_CAPITAL, risk_pct=DEFAULT_RISK_PCT) -> dict:
    close = safe_float(result.get("close"), 0.0) or 0.0
    entry_high = safe_float(result.get("entry_high"), 0.0) or 0.0
    stop_loss = safe_float(result.get("stop_loss"), 0.0) or 0.0
    rr = safe_float(result.get("rr"), 0.0) or 0.0
    wave_stage = result.get("wave_stage", "-")
    entry_zone_status = result.get("entry_zone_status", "NO_PLAN")
    allocation_multiplier = safe_float(result.get("allocation_multiplier"), 0.0) or 0.0
    allocation_grade = result.get("allocation_grade", "BLOCK")

    base_pct = 0.0
    if wave_stage == "第3浪" and rr >= 2.0 and entry_zone_status == "IN_ZONE":
        base_pct = 10.0
    elif wave_stage == "第3浪" and entry_zone_status == "BREAKOUT_CONFIRM":
        base_pct = 5.0
    elif wave_stage == "整理偏多" and rr >= 1.5 and entry_zone_status == "IN_ZONE":
        base_pct = 4.0
    elif rr >= 1.5 and entry_zone_status == "IN_ZONE":
        base_pct = 3.0

    if allocation_grade in ("BLOCK", "D") or allocation_multiplier <= 0:
        position_pct = 0.0
    else:
        position_pct = round(base_pct * allocation_multiplier, 2)

    # 限制最大單檔曝險與風險預算
    max_loss_pct = 0.0
    if entry_high > 0 and stop_loss > 0 and entry_high > stop_loss:
        max_loss_pct = round((entry_high - stop_loss) / entry_high * 100, 2)

    risk_budget_pct = round(min(float(risk_pct), position_pct * max_loss_pct / 100), 2) if position_pct > 0 else 0.0
    suggested_capital = account_capital * position_pct / 100
    suggested_shares = int(suggested_capital // close) if close > 0 and position_pct > 0 else 0
    if suggested_shares > 0:
        suggested_shares = (suggested_shares // 1000) * 1000 if close < 500 else (suggested_shares // 100) * 100

    return {
        "position_size_pct": position_pct,
        "risk_budget_pct": risk_budget_pct,
        "suggested_shares": suggested_shares,
        "max_loss_pct": max_loss_pct,
    }


def ensure_phase4_fields(result: dict) -> dict:
    """Phase4欄位完整性防呆：避免UI/CSV/PDF因缺欄造成KeyError或錯誤下單。"""
    defaults = {
        "entry_zone_status": "NO_PLAN",
        "entry_zone_ready": False,
        "entry_zone_reason": "Phase4欄位未完整產生，系統降級等待。",
        "distance_to_entry_pct": 0.0,
        "chase_risk_flag": False,
        "allocation_score": 0,
        "allocation_grade": "BLOCK",
        "allocation_multiplier": 0.0,
        "position_size_pct": 0.0,
        "risk_budget_pct": 0.0,
        "suggested_shares": 0,
        "max_loss_pct": 0.0,
        "phase4_block_reason": "Phase4欄位未完整產生，禁止下單。",
        "order_type_hint": "等待",
        "final_decision": "WAIT",
        "execution_ready": False,
        "decision_reason": "Phase4欄位未完整產生，系統降級等待。",
        "final_block_reason": "Phase4欄位未完整產生，系統降級等待。",
        "display_advice": result.get("advice", "觀望為主"),
        "display_trade_type": result.get("candidate_pool", result.get("trade_type", "觀望")),
        "candidate_pool": result.get("candidate_pool", result.get("display_trade_type", result.get("trade_type", "觀望"))),
        "technical_state": result.get("technical_state", "-"),
        "execution_state": result.get("execution_state", "-"),
        "display_state": result.get("display_state", "-"),
        "bullish_target": result.get("bullish_target", 0.0),
        "bearish_target": result.get("bearish_target", 0.0),
        "execution_target": result.get("execution_target", 0.0),
        "watch_target": result.get("watch_target", 0.0),
        "observation_rr": result.get("observation_rr", result.get("rr")),
        "major_wave": result.get("major_wave", "-"),
        "minor_wave": result.get("minor_wave", "-"),
        "correction_type": result.get("correction_type", "-"),
        "fibo_retrace": result.get("fibo_retrace", 0.0),
        "fibo_retrace_zone": result.get("fibo_retrace_zone", "-"),
        "rebound_type": result.get("rebound_type", "-"),
        "correction_completed": result.get("correction_completed", False),
        "escape_rally": result.get("escape_rally", False),
        "impulsive_wave": result.get("impulsive_wave", False),
        "phase5_wave_label": result.get("phase5_wave_label", "-"),
        "phase5_block_reason": result.get("phase5_block_reason", ""),
    }
    missing = []
    for key, value in defaults.items():
        if key not in result or result.get(key) is None:
            result[key] = value
            missing.append(key)
    if missing:
        logging.warning("PHASE4_MISSING_FIELDS symbol=%s missing=%s", result.get("input_symbol", "-"), ",".join(missing))
    return result


def sync_display_semantics(result: dict) -> dict:
    """V6.2：統一 UI/CSV/PDF 外顯語義。只在核心計算完成後統一輸出，不反覆覆蓋內部欄位。"""
    quote_quality = result.get("quote_quality") or normalize_quote_quality(
        result.get("display_note", ""), result.get("source", ""),
        result.get("last_trade"), result.get("indicative_price")
    )
    result["quote_quality"] = quote_quality
    result["display_note"] = display_quote_note(quote_quality)

    # 內部 signal 保留規則命中結果，外顯 signal 統一由波浪/費波/報價品質轉換。
    phase5_info = {
        "major_wave": result.get("major_wave"),
        "minor_wave": result.get("minor_wave"),
        "correction_type": result.get("correction_type"),
        "fibo_retrace": result.get("fibo_retrace"),
        "rebound_type": result.get("rebound_type"),
        "correction_completed": result.get("correction_completed"),
        "escape_rally": result.get("escape_rally"),
        "impulsive_wave": result.get("impulsive_wave"),
        "phase5_wave_label": result.get("phase5_wave_label"),
    }
    result["signal"] = semantic_signal_from_structure(
        result.get("signal", ""),
        result.get("wave_stage", "-"),
        result.get("fibo_position", "-"),
        quote_quality,
        phase5_info
    )
    result["technical_state"] = classify_technical_state(result)
    result["execution_state"] = classify_execution_state(result)
    result["display_state"] = classify_display_state(result)
    result["display_advice"] = semantic_advice_from_state(result)

    # 外顯交易類型唯一來源：candidate_pool / classify_candidate_pool。
    pool = result.get("candidate_pool") or classify_candidate_pool(result)
    result["candidate_pool"] = pool
    result["display_trade_type"] = pool
    result["trade_type"] = pool

    # 主升候選統一來源：classify_leader_stage，避免 semantic 與 leader 函式重複覆蓋。
    result["leader_candidate"] = classify_leader_stage(result)
    result["leader_stage"] = result["leader_candidate"]

    if result.get("final_decision") != "BUY":
        reason = result.get("decision_reason", "")
        if quote_quality != "REALTIME":
            result["decision_reason"] = "等待即時成交確認；僅限制下單，不覆蓋技術結構。"
            result["final_block_reason"] = "報價未形成即時成交，暫不下單，但不代表技術轉弱。"
        elif "價格高於建議進場區" in reason or result.get("entry_zone_status") == "ABOVE_ENTRY":
            result["decision_reason"] = "價格高於進場區，等待回測或突破確認。"
    return result


def log_decision_trace(result: dict) -> None:
    """記錄每檔股票Phase4 Gate，方便EXE問題追蹤。"""
    try:
        logging.info(
            "DECISION symbol=%s price=%s source=%s entry=%s ready_entry=%s rr=%s rr_valid=%s alloc=%s grade=%s position=%s decision=%s execution_ready=%s order=%s reason=%s",
            result.get("input_symbol"), result.get("close"), result.get("source"),
            result.get("entry_zone_status"), result.get("entry_zone_ready"),
            result.get("rr"), result.get("rr_valid"), result.get("allocation_score"),
            result.get("allocation_grade"), result.get("position_size_pct"),
            result.get("final_decision"), result.get("execution_ready"),
            result.get("order_type_hint"), result.get("decision_reason")
        )
    except Exception as e:
        logging.warning("DECISION_LOG_FAILED symbol=%s error=%s", result.get("input_symbol", "-"), e)

def build_final_decision(result: dict) -> dict:
    price_valid = bool(result.get("price_valid", False))
    signal = result.get("signal", "")
    advice = result.get("advice", "")
    state = result.get("state_bucket", "range")
    rr = safe_float(result.get("rr"), None)
    rr_valid = bool(result.get("rr_valid", False))
    fibo_risk = bool(result.get("fibo_risk_flag", False))
    wave_risk = bool(result.get("wave_risk_flag", False))
    entry_zone_ready = bool(result.get("entry_zone_ready", False))
    entry_zone_status = result.get("entry_zone_status", "NO_PLAN")
    position_size_pct = safe_float(result.get("position_size_pct"), 0.0) or 0.0
    allocation_score = safe_float(result.get("allocation_score"), 0.0) or 0.0
    allocation_grade = result.get("allocation_grade", "BLOCK")
    phase4_block_reason = result.get("phase4_block_reason", "")
    entry_zone_reason = result.get("entry_zone_reason", "不符合進場區")
    order_type_hint = result.get("order_type_hint", "等待")
    support_broken = signal in ("急跌風險", "跌破支撐", "轉弱警戒")
    phase5_escape = bool(result.get("escape_rally", False))
    phase5_rebound = result.get("rebound_type", "")
    phase5_done = bool(result.get("correction_completed", False))
    phase5_impulsive = bool(result.get("impulsive_wave", False))

    if phase5_escape:
        decision = "AVOID"
        ready = False
        reason = result.get("phase5_block_reason") or "Phase5判定為逃命反彈/主跌弱反彈，禁止追價或主動布局。"
        order_type_hint = "禁止"
    elif phase5_rebound == "跌深技術反彈" and not phase5_done:
        decision = "WAIT"
        ready = False
        reason = result.get("phase5_block_reason") or "Phase5跌深技術反彈尚未確認修正完成，等待站回均線與放量。"
        order_type_hint = "等待"
    elif wave_stage := result.get("wave_stage", "-"):
        # keep normal Gate logic below; assignment expression used only to avoid extra variable scope.
        pass

    if phase5_escape or (phase5_rebound == "跌深技術反彈" and not phase5_done):
        pass
    elif not price_valid:
        decision = "WAIT"
        ready = False
        reason = "等待即時成交確認；僅限制下單，不覆蓋技術結構。"
        order_type_hint = "等待"
    elif support_broken:
        decision = "AVOID"
        ready = False
        reason = f"命中風險訊號：{signal}。"
        order_type_hint = "禁止"
    elif fibo_risk or wave_risk:
        decision = "AVOID"
        ready = False
        reason = "波浪/費波觸發禁追或末升風險，不允許追價。"
        order_type_hint = "禁止"
    elif allocation_grade == "BLOCK":
        # Phase4硬Gate：資金等級BLOCK必須直接阻擋，避免被後續WAIT覆蓋成可觀察語義。
        decision = "AVOID"
        ready = False
        reason = phase4_block_reason or "Phase4資金等級為BLOCK，禁止下單。"
        order_type_hint = "禁止"
    elif entry_zone_status in ("ABOVE_ENTRY", "NO_CHASE", "BROKEN", "NO_PLAN"):
        decision = "AVOID" if entry_zone_status in ("NO_CHASE", "BROKEN") else "WAIT"
        ready = False
        reason = f"Phase4進場區阻擋：{entry_zone_reason}。"
        if decision == "AVOID":
            order_type_hint = "禁止"
        else:
            order_type_hint = "等待"
    elif not entry_zone_ready:
        decision = "WAIT"
        ready = False
        reason = f"尚未進入可執行進場區：{entry_zone_reason}。"
        order_type_hint = "等待"
    elif rr is None or rr < 1:
        decision = "WAIT"
        ready = False
        reason = "RR小於1或無法計算，禁止買進。"
        order_type_hint = "等待"
    elif rr < MIN_BUY_RR:
        decision = "WAIT"
        ready = False
        reason = f"RR介於1.0~{MIN_BUY_RR}，僅觀察或等待更佳風險報酬。"
        order_type_hint = "等待"
    elif position_size_pct <= 0:
        decision = "WAIT"
        ready = False
        reason = phase4_block_reason or "倉位計算為0，禁止下單。"
        order_type_hint = "等待"
    elif allocation_score < MIN_BUY_ALLOCATION_SCORE:
        decision = "WAIT"
        ready = False
        reason = phase4_block_reason or f"配置分低於{MIN_BUY_ALLOCATION_SCORE}，等待更佳進場條件。"
        order_type_hint = "等待"
    elif state == "strong" and rr_valid and entry_zone_ready and advice in ("突破可追", "拉回加碼"):
        decision = "BUY"
        ready = True
        reason = f"Phase4通過：進場區有效、RR有效、配置分={allocation_score}、建議倉位={position_size_pct}%。"
    elif state == "bullish" and rr_valid:
        decision = "WAIT"
        ready = False
        reason = "偏多但未達強勢買進，等待拉回或突破確認。"
        order_type_hint = "等待"
    else:
        decision = "WAIT"
        ready = False
        reason = "未符合可下單條件。"
        order_type_hint = "等待"

    # 後置Gate保護：未來維護時即使上方誤判BUY，也不能繞過Phase4硬條件。
    gate_ready = (
        decision == "BUY" and price_valid and entry_zone_ready and
        position_size_pct > 0 and allocation_score >= MIN_BUY_ALLOCATION_SCORE and
        rr_valid and rr is not None and rr >= MIN_BUY_RR and
        allocation_grade not in ("BLOCK", "D") and
        entry_zone_status in ("IN_ZONE", "BREAKOUT_CONFIRM") and
        not (fibo_risk or wave_risk or support_broken or result.get("escape_rally")) and
        not (result.get("rebound_type") == "跌深技術反彈" and not result.get("correction_completed"))
    )
    if decision == "BUY" and not gate_ready:
        decision = "WAIT"
        ready = False
        reason = "Phase4後置Gate未通過，降級等待。"
        order_type_hint = "等待"
    else:
        ready = bool(gate_ready)

    if decision != "BUY":
        position_size_pct = 0.0

    final_block_reason = "" if decision == "BUY" else (phase4_block_reason or reason)

    return {
        "final_decision": decision,
        "execution_ready": ready,
        "decision_reason": reason,
        "order_type_hint": order_type_hint,
        "position_size_pct": position_size_pct,
        "final_block_reason": final_block_reason,
    }

def get_light(signal, score, change_pct, intraday_score=None, fibo_risk_flag=False, wave_risk_flag=False, final_decision=None):
    intraday_score = intraday_score or 0
    if final_decision == "BUY":
        return "🔵"
    if signal == "急跌風險" or change_pct <= -9.0:
        return "🔴"
    if "逃命反彈" in str(signal) or signal in ("跌破支撐", "轉弱警戒") or final_decision == "AVOID":
        return "🟠"
    if fibo_risk_flag or wave_risk_flag:
        return "🟠"
    if signal == "突破強勢":
        return "🔵"
    if signal in ("偏多觀察", "強勢追蹤", "主升突破", "第3浪突破前觀察", "第3浪主升觀察", "第3浪突破前觀察（成交待確認）", "第3浪主升觀察（成交待確認）"):
        return "🟢"
    if signal in ("區間整理", "整理偏多觀察", "整理偏多觀察（成交待確認）", "修正反彈觀察", "修正反彈觀察（成交待確認）") or "技術反彈" in str(signal) or "C浪" in str(signal) or "B浪" in str(signal):
        return "🟡"
    if score >= 45 or intraday_score >= 45:
        return "🟡"
    return "🟠"


def evaluate_trade_state(close, prev_close, open_price, support, resistance, change_pct,
                         trend_score, intraday_score, score, orderbook_bias, ma20=0, ma60=0, rsi=50,
                         wave_stage="-", fibo_position="-", fibo_risk_flag=False,
                         wave_risk_flag=False, rr_valid=False, price_valid=True):
    near_resistance = close >= resistance * 0.988 if resistance else False
    at_breakout = close >= resistance * 0.998 if resistance else False
    above_open = close >= open_price
    above_prev = close >= prev_close
    bullish_orderbook = orderbook_bias in ("買盤偏強", "買盤明顯偏強")
    structure_bullish = (close >= ma20 and close >= ma60 and ma20 >= ma60) if ma20 and ma60 else False

    if not price_valid:
        if wave_stage == "第3浪" and fibo_position == "挑戰1.0前":
            return "第3浪突破前觀察", "等待成交確認；突破可觀察", "bullish", "D00", "成交尚未確認，執行層等待；技術結構需獨立判讀"
        if wave_stage == "第3浪":
            return "第3浪主升觀察", "等待成交確認；突破可觀察", "bullish", "D00", "成交尚未確認，執行層等待；技術結構需獨立判讀"
        if wave_stage == "整理偏多":
            return "整理偏多觀察", "等待成交確認", "bullish", "D00", "成交尚未確認，執行層等待；技術結構需獨立判讀"
        if wave_stage == "A/C修正浪":
            return "修正反彈觀察", "等待成交確認", "range", "D00", "成交尚未確認，僅限制下單，不代表技術轉弱"
        return "資料待確認", "等待成交確認", "range", "D00", "成交尚未確認，僅限制下單，不代表技術轉弱"

    if change_pct <= -9.0 or intraday_score <= 15:
        return "急跌風險", "觀望為主", "weak", "R01", "當日急跌或盤中分過低，先處理風險"

    if close < support * 0.997 or (close < support and intraday_score < 42):
        return "跌破支撐", "減碼/防守", "weak", "R02", "跌破主支撐且盤中力道不足"

    if fibo_risk_flag or wave_risk_flag:
        return "末升/禁追風險", "不追高", "weak", "WF_RISK", "波浪或費波觸發末升/禁追條件"

    if (
        wave_stage == "第3浪" and
        fibo_position in ("站上/測試1.0", "1.0~1.382主升區", "挑戰1.382") and
        rr_valid and trend_score >= 80 and intraday_score >= 70 and score >= 78
    ):
        return "主升突破", "拉回加碼", "strong", "WF_BUY", "第3浪+費波主升區+RR有效"

    if score >= 95 and trend_score >= 90 and intraday_score >= 85 and rr_valid:
        if at_breakout and bullish_orderbook:
            return "突破強勢", "突破可追", "strong", "S01", "高分突破且五檔買盤偏強"
        return "強勢追蹤", "拉回加碼", "strong", "S02", "高分強勢但未完成有效突破"

    if (
        close > resistance and trend_score >= 82 and intraday_score >= 78 and score >= 86 and
        change_pct >= 1.8 and above_open and above_prev and bullish_orderbook and rr_valid
    ):
        return "突破強勢", "突破可追", "strong", "S03", "突破壓力、量價轉強且RR有效"

    if (
        trend_score >= 82 and intraday_score >= 70 and score >= 82 and
        change_pct >= 0.8 and above_open and above_prev and bullish_orderbook and rr_valid
    ):
        return "強勢追蹤", "拉回加碼", "strong", "S04", "波段與盤中分數同步偏強"

    if (
        trend_score >= 80 and intraday_score >= 70 and score >= 75 and structure_bullish
    ):
        return "整理偏多", "低接布局", "bullish", "B01", "均線結構偏多但未達可追條件"

    if (
        trend_score >= 82 and intraday_score >= 68 and score >= 78 and
        structure_bullish and change_pct >= 1.5 and
        orderbook_bias in ("買盤偏強", "買盤明顯偏強") and 35 <= rsi <= 68 and
        (not resistance or close <= resistance * 1.03)
    ):
        return "整理偏多", "低接布局", "bullish", "B02", "站穩中期均線且五檔偏多，但未完成有效突破"

    if (
        trend_score >= 72 and intraday_score >= 58 and score >= 70 and
        change_pct >= 0.3 and (above_open or structure_bullish)
    ):
        return "偏多觀察", "低接布局", "bullish", "B03", "偏多觀察，但仍需等待確認"

    if score >= 45 and support <= close <= resistance:
        return "區間整理", "區間操作", "range", "N01", "價格位於支撐與壓力間，屬區間操作"

    if score >= 30:
        return "轉弱警戒", "減碼/防守", "weak", "W01", "分數不足且結構轉弱"

    return "轉弱警戒", "減碼/防守", "weak", "W02", "綜合條件偏弱"


def is_main_trend_candidate(data: dict) -> bool:
    close = data.get("close", 0)
    open_price = data.get("open", 0)
    prev_close = data.get("prev_close", 0)
    resistance = data.get("resistance", 0)
    trend = data.get("trend_score", 0)
    intra = data.get("intraday_score", 0)
    score = data.get("score", 0)
    rsi = data.get("rsi", 0)
    ma20 = data.get("ma20", 0)
    ma60 = data.get("ma60", 0)
    signal = data.get("signal", "")
    orderbook = data.get("orderbook_bias", "無")
    change_pct = data.get("change_pct", 0)
    wave_stage = data.get("wave_stage", "-")
    fibo_position = data.get("fibo_position", "-")
    rr_valid = bool(data.get("rr_valid", False))
    fibo_risk = bool(data.get("fibo_risk_flag", False) or data.get("wave_risk_flag", False))

    bullish_orderbook = orderbook in ("買盤偏強", "買盤明顯偏強", "多空均衡")
    not_too_far_from_resistance = close <= resistance * 1.01 if resistance else True
    healthy_strength = signal in ("強勢追蹤", "突破強勢", "偏多觀察", "主升突破")
    wave_fibo_ok = (
        wave_stage == "第3浪" and
        fibo_position in ("站上/測試1.0", "1.0~1.382主升區", "挑戰1.382")
    )

    return (
        not fibo_risk and rr_valid and
        score >= 85 and trend >= 80 and intra >= 70 and
        45 <= rsi <= 72 and close > ma20 >= ma60 and
        close >= open_price and close >= prev_close and
        change_pct >= 0.5 and bullish_orderbook and healthy_strength and
        not_too_far_from_resistance and wave_fibo_ok
    )


def classify_leader_stage(data: dict) -> str:
    """Phase5：外顯主升候選改為生命週期 + 波浪定位分類。"""
    if data.get("escape_rally"):
        return "非主升/逃命反彈"
    if data.get("rebound_type") == "跌深技術反彈":
        return "修正弱反彈"
    if data.get("impulsive_wave"):
        return "主升確認"
    if data.get("rebound_type") == "主升拉回":
        return "主升拉回完成" if data.get("correction_completed") else "主升拉回觀察"
    if bool(data.get("fibo_risk_flag", False) or data.get("wave_risk_flag", False)):
        if data.get("wave_stage") == "A/C修正浪":
            return "修正觀察"
        return "末升風險"

    wave_stage = data.get("wave_stage", "-")
    fibo_position = data.get("fibo_position", "-")
    if is_main_trend_candidate(data):
        return "主升確認"
    if wave_stage == "第3浪" and fibo_position == "挑戰1.0前":
        return "主升觀察"
    if wave_stage == "第3浪":
        return "主升確認"
    if wave_stage == "整理偏多":
        return "整理觀察"
    if wave_stage == "A/C修正浪":
        return "修正觀察"
    return "非主升"

def get_strategy_level(score: int) -> str:
    if score >= 85:
        return "A"
    if score >= 70:
        return "B"
    if score >= 55:
        return "C"
    return "D"

def get_strategy_level_score(level: str) -> int:
    mapping = {"A": 4, "B": 3, "C": 2, "D": 1}
    return mapping.get(str(level).strip().upper(), 0)


def normalize_rr_display(rr):
    return "-" if rr is None else rr


def get_display_target(target, signal: str, state_bucket: str):
    if target in (None, "-", ""):
        if "第3浪" in signal:
            return "待突破確認"
        if "修正反彈" in signal:
            return "反彈觀察"
        return "-"
    if signal in ("急跌風險", "跌破支撐"):
        return "-"
    if "末升" in signal:
        return "不追價"
    return target



# =========================
# v5.2.5 Battle Plan Control Overlay
# =========================
def normalize_stock_code_value(value) -> str:
    """將 Excel/輸入股票代碼正規化為 4 碼字串；非股票代碼回傳空字串。"""
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return ""
    if "." in text:
        text = text.split(".")[0]
    text = text.replace("'", "").strip()
    if text.isdigit() and len(text) <= 4:
        text = text.zfill(4)
    if text.isdigit() and len(text) == 4:
        return text
    return ""


def safe_battle_float(value):
    """讀取作戰表價位；空白、非數字、<=0 一律視為無效。"""
    v = safe_float(value)
    if v is None or v <= 0:
        return None
    return round_price(v)


def format_battle_price(value) -> str:
    v = safe_battle_float(value)
    if v is None:
        return "-"
    if abs(v - int(v)) < 0.005:
        return str(int(round(v)))
    return f"{v:.2f}".rstrip("0").rstrip(".")


def build_control_status(price, s1=None, s2=None, s3=None, r1=None, r2=None, proximity_pct=0.01) -> dict:
    """
    依即時價與 Excel 作戰控制價位，產生 UI 主表的「控制欄」與「即時指示」。
    僅提供管控顯示，不覆蓋原本支撐/壓力、訊號、排序、Phase4/Phase5。
    """
    price = safe_battle_float(price)
    levels = {
        "s1": safe_battle_float(s1),
        "s2": safe_battle_float(s2),
        "s3": safe_battle_float(s3),
        "r1": safe_battle_float(r1),
        "r2": safe_battle_float(r2),
    }
    if price is None or not any(v is not None for v in levels.values()):
        return {
            "control_label": "-",
            "control_action": "未載入作戰控制價位",
            "control_distance_pct": None,
            "control_state": "NO_BATTLE_PLAN",
        }

    def pct_to(level):
        if level is None or level <= 0:
            return None
        return round((price - level) / level * 100, 2)

    # 先處理破位與突破，因為這些是風控優先訊號。
    if levels["s2"] is not None and price < levels["s2"]:
        return {
            "control_label": f"跌破支撐2 {format_battle_price(levels['s2'])}",
            "control_action": "防守優先",
            "control_distance_pct": pct_to(levels["s2"]),
            "control_state": "BREAK_S2",
        }
    if levels["s1"] is not None and price < levels["s1"]:
        return {
            "control_label": f"跌破支撐1 {format_battle_price(levels['s1'])}",
            "control_action": "停止加碼，降級觀察",
            "control_distance_pct": pct_to(levels["s1"]),
            "control_state": "BREAK_S1",
        }
    if levels["r1"] is not None and price > levels["r1"]:
        # 若已接近壓力2，優先提示高檔風險；否則顯示突破壓力1。
        if levels["r2"] is not None and abs(price - levels["r2"]) / levels["r2"] <= proximity_pct:
            return {
                "control_label": f"壓力2 {format_battle_price(levels['r2'])}",
                "control_action": "高檔區，留意賣壓",
                "control_distance_pct": pct_to(levels["r2"]),
                "control_state": "NEAR_R2",
            }
        return {
            "control_label": f"突破壓力1 {format_battle_price(levels['r1'])}",
            "control_action": "確認站穩才升級",
            "control_distance_pct": pct_to(levels["r1"]),
            "control_state": "BREAK_R1",
        }

    candidates = []
    names = {
        "s1": ("支撐1", "接近支撐，觀察承接", "NEAR_S1"),
        "s2": ("支撐2", "防守區，嚴格風控", "NEAR_S2"),
        "s3": ("支撐3", "最後防線，禁止攤平", "NEAR_S3"),
        "r1": ("壓力1", "等突破，不追價", "NEAR_R1"),
        "r2": ("壓力2", "高檔區，留意賣壓", "NEAR_R2"),
    }
    for key, level in levels.items():
        if level is None:
            continue
        distance_abs_pct = abs(price - level) / level
        label, action, state = names[key]
        candidates.append((distance_abs_pct, key, level, label, action, state))

    if not candidates:
        return {
            "control_label": "-",
            "control_action": "未載入作戰控制價位",
            "control_distance_pct": None,
            "control_state": "NO_BATTLE_PLAN",
        }
    candidates.sort(key=lambda x: x[0])
    distance_abs_pct, key, level, label, action, state = candidates[0]

    if distance_abs_pct <= proximity_pct:
        return {
            "control_label": f"{label} {format_battle_price(level)}",
            "control_action": action,
            "control_distance_pct": pct_to(level),
            "control_state": state,
        }

    # 未接近任一價位時，仍顯示最近控制點，讓主表具備管制參考。
    return {
        "control_label": f"{label} {format_battle_price(level)}",
        "control_action": "區間內，等待接近控制點",
        "control_distance_pct": pct_to(level),
        "control_state": "BETWEEN_LEVELS",
    }


def load_battle_plan_excel(file_path: str) -> tuple[dict, str, list[str]]:
    """讀取 Excel「個股作戰表」，回傳 code -> battle plan mapping、狀態訊息與股票代碼順序。"""
    try:
        raw = pd.read_excel(file_path, sheet_name="個股作戰表", header=None)
    except Exception as e:
        raise ValueError(f"無法讀取 Sheet『個股作戰表』：{e}")

    header_row = None
    for idx in range(min(len(raw), 30)):
        row_values = [str(x).strip() for x in raw.iloc[idx].tolist() if str(x).strip() and str(x).strip().lower() != "nan"]
        if "代碼" in row_values and "支撐1" in row_values and "壓力1" in row_values:
            header_row = idx
            break
    if header_row is None:
        raise ValueError("找不到必要欄位列：代碼、支撐1、壓力1")

    df = pd.read_excel(file_path, sheet_name="個股作戰表", header=header_row)
    df.columns = [str(c).strip() for c in df.columns]
    required = ["代碼", "支撐1", "支撐2", "支撐3", "壓力1", "壓力2", "策略等級"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"缺少必要欄位：{', '.join(missing)}")

    optional_operation = "操作" if "操作" in df.columns else None
    mapping = {}
    code_order = []
    skipped = 0
    for _, row in df.iterrows():
        code = normalize_stock_code_value(row.get("代碼"))
        if not code:
            skipped += 1
            continue
        if code not in code_order:
            code_order.append(code)
        item = {
            "battle_code": code,
            "battle_support1": safe_battle_float(row.get("支撐1")),
            "battle_support2": safe_battle_float(row.get("支撐2")),
            "battle_support3": safe_battle_float(row.get("支撐3")),
            "battle_resistance1": safe_battle_float(row.get("壓力1")),
            "battle_resistance2": safe_battle_float(row.get("壓力2")),
            "battle_strategy_level": str(row.get("策略等級") or "-").strip(),
            "battle_operation_note": str(row.get(optional_operation) or "").strip() if optional_operation else "",
        }
        ctrl = build_control_status(
            0, item["battle_support1"], item["battle_support2"], item["battle_support3"],
            item["battle_resistance1"], item["battle_resistance2"]
        )
        item.update({
            "control_label": ctrl["control_label"],
            "control_action": ctrl["control_action"],
            "control_distance_pct": ctrl["control_distance_pct"],
            "control_state": ctrl["control_state"],
        })
        mapping[code] = item
    if not mapping:
        raise ValueError("個股作戰表沒有任何有效 4 碼股票代碼")
    status = f"成功讀取 {len(mapping)} 檔；略過 {skipped} 列非股票資料"
    return mapping, status, code_order


def empty_battle_fields() -> dict:
    return {
        "battle_code": "-",
        "battle_support1": None,
        "battle_support2": None,
        "battle_support3": None,
        "battle_resistance1": None,
        "battle_resistance2": None,
        "battle_strategy_level": "-",
        "battle_operation_note": "",
        "battle_plan_hit": False,
        "control_label": "-",
        "control_action": "未載入作戰控制價位",
        "control_distance_pct": None,
        "control_state": "NO_BATTLE_PLAN",
    }



def calc_trade_plan(data: dict) -> dict:
    support = float(data.get("support", 0) or 0)
    resistance = float(data.get("resistance", 0) or 0)
    fibo = data.get("fibo", {}) or {}
    bullish_target = get_bullish_target(fibo, resistance)
    bearish_target = get_bearish_target(fibo, support)
    # P0：買進交易計畫與RR只能使用多方目標；空方目標只供風險顯示。
    fibo_target = float(bullish_target or resistance)
    state = data.get("state_bucket", "range")
    technical_state = data.get("technical_state", "")
    fibo_risk = bool(data.get("fibo_risk_flag", False) or data.get("wave_risk_flag", False))

    if state == "strong" and not fibo_risk:
        entry_low = support * 1.002
        entry_high = min(support * 1.012, resistance * 0.995) if resistance > 0 else support * 1.012
        stop = support * 0.982
    elif state == "bullish" and not fibo_risk:
        entry_low = support * 1.000
        entry_high = min(support * 1.010, resistance * 0.992) if resistance > 0 else support * 1.010
        stop = support * 0.978
    elif state == "range" and not fibo_risk:
        entry_low = support * 0.998
        entry_high = min(support * 1.006, resistance * 0.988) if resistance > 0 else support * 1.006
        stop = support * 0.972
    else:
        entry_low = 0.0
        entry_high = 0.0
        stop = support * 0.968 if support else 0.0

    target = max(resistance, fibo_target) if state in ("strong", "bullish") else resistance
    target_source = "Fibo/壓力擇高" if state in ("strong", "bullish") else "壓力"

    entry_mid = (entry_low + entry_high) / 2 if entry_low > 0 and entry_high > 0 else 0.0
    entry_band_width_pct = ((entry_high - entry_low) / entry_low * 100) if entry_low > 0 and entry_high >= entry_low else 0.0
    stop_distance_pct = ((entry_high - stop) / entry_high * 100) if entry_high > 0 and stop > 0 and entry_high > stop else 0.0
    reward_to_target_pct = ((target - entry_high) / entry_high * 100) if entry_high > 0 and target > entry_high else 0.0
    trade_plan_valid = bool(entry_low > 0 and entry_high > 0 and stop > 0 and target > entry_high and entry_high > stop)

    risk = entry_high - stop
    reward = target - entry_high
    rr = round(reward / risk, 2) if trade_plan_valid and risk > 0 and reward > 0 else None
    if rr is None:
        rr_valid = False
        rr_level = "無效"
    elif rr >= MIN_BUY_RR:
        rr_valid = True
        rr_level = "有效"
    elif rr >= 1.0:
        rr_valid = False
        rr_level = "觀察"
    else:
        rr_valid = False
        rr_level = "不足"

    return {
        "entry_low": round_price(entry_low) if entry_low else 0.0,
        "entry_high": round_price(entry_high) if entry_high else 0.0,
        "entry_mid": round_price(entry_mid) if entry_mid else 0.0,
        "entry_band_width_pct": round(entry_band_width_pct, 2),
        "stop_loss": round_price(stop) if stop else 0.0,
        "stop_distance_pct": round(stop_distance_pct, 2),
        "target_price": round_price(target) if target else 0.0,
        "reward_to_target_pct": round(reward_to_target_pct, 2),
        "trade_plan_valid": trade_plan_valid,
        "rr": rr,
        "rr_valid": rr_valid,
        "rr_level": rr_level,
        "trade_target": round_price(target) if target else 0.0,
        "execution_target": round_price(target) if target else 0.0,
        "watch_target": round_price(bullish_target) if bullish_target else 0.0,
        "bullish_target": round_price(bullish_target) if bullish_target else 0.0,
        "bearish_target": round_price(bearish_target) if bearish_target else 0.0,
        "observation_rr": rr,
        "fibo_target": round_price(fibo_target) if fibo_target else 0.0,
        "resistance_target": round_price(resistance) if resistance else 0.0,
        "target_source": target_source,
    }

def build_trade_scripts(data: dict) -> dict:
    support = data["support"]
    resistance = data["resistance"]
    next_target = data.get("bullish_target", data["fibo"].get("bullish_next_target", data["fibo"].get("next_target", resistance)))
    bearish_target = data.get("bearish_target", data["fibo"].get("bearish_next_target", support))
    bucket = data.get("state_bucket", "range")

    if bucket == "strong":
        return {
            "script_a": f"劇本A（強勢突破）: 若站穩 {resistance} 之上且量能續強，可順勢追蹤，下一目標看 {next_target}",
            "script_b": f"劇本B（拉回承接）: 若回測 {support} 附近不破，可分批承接；失守則降級為偏多/整理",
            "script_c": f"劇本C（壓力震盪）: 若接近 {resistance} 但量能不足，先等縮量整理後再攻，不宜盲目追高",
        }
    if bucket == "bullish":
        return {
            "script_a": f"劇本A（偏多延續）: 守住 {support} 可維持偏多觀察，等待再次挑戰 {resistance}",
            "script_b": f"劇本B（回測確認）: 若回測 {support} 但止穩，可偏向低接；跌破則先退場觀望",
            "script_c": f"劇本C（轉強升級）: 若有效突破 {resistance} 並量價配合，可由偏多觀察升級為強勢追蹤",
        }
    if bucket == "weak":
        return {
            "script_a": f"劇本A（弱勢反彈）: 若反彈至 {resistance} 下方仍無法突破，先視為弱勢反彈，不宜追價",
            "script_b": f"劇本B（跌破續弱）: 若失守 {support}，空方風險目標看 {bearish_target}，優先控管部位，避免逆勢攤平",
            "script_c": f"劇本C（止穩觀察）: 只有重新站回 {support} 並伴隨量價轉強，才考慮恢復偏多",
        }
    return {
        "script_a": f"劇本A（區間低接）: 靠近 {support} 可觀察承接力道，未見止穩前不急著進場",
        "script_b": f"劇本B（跌破下緣）: 若跌破 {support}，區間整理失效，先轉為保守觀察",
        "script_c": f"劇本C（突破上緣）: 若有效突破 {resistance} 並量能配合，可由整理升級為偏多追蹤",
    }


def calc_intraday_score(close, prev_close, open_price, high_price, low_price, support, resistance, orderbook_bias, change_pct):
    score = 50
    comments = []

    if change_pct >= 3:
        score += 20; comments.append("當日漲幅偏強")
    elif change_pct >= 1:
        score += 10; comments.append("當日漲幅為正")
    elif change_pct <= -9:
        score -= 35; comments.append("急跌風險")
    elif change_pct <= -5:
        score -= 20; comments.append("當日跌幅偏大")
    elif change_pct < 0:
        score -= 8; comments.append("當日走弱")

    if close >= open_price:
        score += 8; comments.append("站上開盤")
    else:
        score -= 8; comments.append("跌破開盤")

    if close >= prev_close:
        score += 8; comments.append("站上昨收")
    else:
        score -= 8; comments.append("跌破昨收")

    day_range = max(high_price - low_price, 0.01)
    pos = (close - low_price) / day_range
    if pos >= 0.8:
        score += 12; comments.append("接近日高")
    elif pos <= 0.2:
        score -= 12; comments.append("接近日低")

    if close > resistance:
        score += 18; comments.append("突破壓力")
    elif close >= resistance * 0.995:
        score += 6; comments.append("逼近壓力")
    elif close < support:
        score -= 18; comments.append("跌破支撐")

    if change_pct >= 1.5 and close >= open_price and close >= prev_close:
        score += 10; comments.append("盤中續強")

    if orderbook_bias == "買盤明顯偏強":
        score += 12; comments.append("五檔買盤明顯偏強")
    elif orderbook_bias == "買盤偏強":
        score += 7; comments.append("五檔買盤偏強")
    elif orderbook_bias == "賣盤偏強":
        score -= 8; comments.append("五檔賣盤偏強")

    score = max(0, min(100, int(score)))
    return score, "；".join(comments)


def analyze_symbol(symbol: str) -> dict:
    yf_symbol, df = download_symbol_data(symbol)
    market = detect_market(symbol, yf_symbol)
    stock_name = get_stock_name(symbol, yf_symbol)
    df = calc_indicators(df)
    last = df.iloc[-1]

    fallback_close = round_price(last["Close"])
    fallback_prev_close = round_price(df.iloc[-2]["Close"]) if len(df) >= 2 else fallback_close
    fallback_open = round_price(last["Open"])
    fallback_high = round_price(last["High"])
    fallback_low = round_price(last["Low"])

    if market in ("台股上市", "台股上櫃"):
        rt = get_tw_realtime_quote(symbol, market)
        if rt is None:
            rt = {
                "close": fallback_close, "display_price": fallback_close, "display_note": "日線回退",
                "quote_quality": "DAILY_FALLBACK", "analysis_price_valid": True, "execution_price_valid": False,
                "last_trade": None, "indicative_price": None, "prev_close": fallback_prev_close,
                "open": fallback_open, "high": fallback_high, "low": fallback_low,
                "bid_prices": [], "ask_prices": [], "bid_vols": [], "ask_vols": [],
                "buy_qty": 0, "sell_qty": 0, "orderbook_ratio": "-", "orderbook_bias": "無有效五檔",
                "quote_time": "", "source": "日線回退",
            }
    else:
        rt = get_us_yahoo_quote(
            yf_symbol=yf_symbol,
            fallback_close=fallback_close,
            fallback_prev_close=fallback_prev_close,
            fallback_open=fallback_open,
            fallback_high=fallback_high,
            fallback_low=fallback_low,
        )
        rt["display_price"] = rt["close"]
        rt["display_note"] = "即時/近即時成交價"
        rt["quote_quality"] = "REALTIME"
        rt["analysis_price_valid"] = True
        rt["execution_price_valid"] = True
        rt["last_trade"] = rt["close"]
        rt["indicative_price"] = rt["close"]
        rt["bid_prices"] = []
        rt["ask_prices"] = []
        rt["bid_vols"] = []
        rt["ask_vols"] = []
        rt["buy_qty"] = 0
        rt["sell_qty"] = 0
        rt["orderbook_ratio"] = "-"
        rt["orderbook_bias"] = "不適用"
        rt["quote_time"] = ""

    close = rt["close"]
    prev_close = rt["prev_close"]
    open_price = rt["open"]
    high_price = rt["high"]
    low_price = rt["low"]

    change = round_price(close - prev_close)
    change_pct = round((change / prev_close) * 100, 2) if prev_close != 0 else 0.0

    ma5 = round_price(last["MA5"]) if pd.notna(last["MA5"]) else close
    ma10 = round_price(last["MA10"]) if pd.notna(last["MA10"]) else close
    ma20 = round_price(last["MA20"]) if pd.notna(last["MA20"]) else close
    ma60 = round_price(last["MA60"]) if pd.notna(last["MA60"]) else close
    rsi = round(float(last["RSI"]), 2) if pd.notna(last["RSI"]) else 50.0

    sr = calc_professional_sr(df)
    support = sr["support"]
    resistance = sr["resistance"]

    trend_score = 50
    comments = []

    if close >= ma5:
        trend_score += 4; comments.append("站上5日線")
    else:
        trend_score -= 4; comments.append("跌破5日線")
    if close >= ma10:
        trend_score += 6; comments.append("站上10日線")
    else:
        trend_score -= 5; comments.append("跌破10日線")
    if close >= ma20:
        trend_score += 10; comments.append("站上20日線")
    else:
        trend_score -= 10; comments.append("跌破20日線")
    if close >= ma60:
        trend_score += 15; comments.append("站上60日線")
    else:
        trend_score -= 12; comments.append("跌破60日線")
    if float(last["MACD"]) >= float(last["MACD_SIGNAL"]):
        trend_score += 8; comments.append("MACD偏多")
    else:
        trend_score -= 6; comments.append("MACD偏弱")
    if pd.notna(last["K"]) and pd.notna(last["D"]):
        if float(last["K"]) >= float(last["D"]):
            trend_score += 6; comments.append("KD偏多")
        else:
            trend_score -= 4; comments.append("KD偏空")
    if rsi < 30:
        trend_score += 8; comments.append("RSI超跌")
    elif rsi > 70:
        trend_score -= 8; comments.append("RSI過熱")
    if len(df) >= 20:
        vol5 = df["Volume"].tail(5).mean()
        vol20 = df["Volume"].tail(20).mean()
        if pd.notna(vol5) and pd.notna(vol20) and vol5 > vol20:
            trend_score += 4; comments.append("量能放大")

    trend_score = max(0, min(100, int(trend_score)))
    intraday_score, intraday_comment = calc_intraday_score(
        close, prev_close, open_price, high_price, low_price, support, resistance,
        rt.get("orderbook_bias", "無"), change_pct
    )
    score = max(0, min(100, int(round(trend_score * 0.6 + intraday_score * 0.4))))

    extra_comment = (
        f"{'；'.join(comments)}"
        f"；盤中={intraday_comment}"
        f"；20日支撐={sr['support20']}"
        f"；20日壓力={sr['resistance20']}"
        f"；波段低點={sr['swing_low']}"
        f"；波段高點={sr['swing_high']}"
        f"；Pivot={sr['pivot']}"
        f"；來源={rt['source']}"
    )

    fibo = calc_fibonacci_targets(df)
    wave = structured_wave_analysis(df)
    fibo_pos = classify_fibo_position(close, fibo)
    quote_quality = rt.get("quote_quality") or normalize_quote_quality(rt.get("display_note", ""), rt.get("source", ""), rt.get("last_trade"), rt.get("indicative_price"))
    analysis_price_valid = bool(rt.get("analysis_price_valid", True))
    price_valid = bool(rt.get("execution_price_valid", quote_quality == "REALTIME"))

    breakout = detect_wave_n_breakout(df, wave, fibo_pos, close)
    phase5 = calculate_phase5_wave_position(
        df=df, wave=wave, fibo=fibo, fibo_pos=fibo_pos, sr=sr,
        close=close, ma20=ma20, ma60=ma60, rsi=rsi,
        volume_ratio=safe_float(breakout.get("volume_ratio"), 0.0) or 0.0
    )

    signal, advice, state_bucket, rule_id, signal_reason = evaluate_trade_state(
        close, prev_close, open_price, support, resistance, change_pct,
        trend_score, intraday_score, score, rt.get("orderbook_bias", "無"),
        ma20=ma20, ma60=ma60, rsi=rsi,
        wave_stage=wave["wave_stage"],
        fibo_position=fibo_pos["fibo_position"],
        fibo_risk_flag=fibo_pos["fibo_risk_flag"],
        wave_risk_flag=wave["wave_risk_flag"],
        rr_valid=True,
        price_valid=price_valid
    )

    result = {
        "input_symbol": symbol, "name": stock_name, "yf_symbol": yf_symbol, "market": market,
        "close": close, "display_price": rt.get("display_price", close), "display_note": rt.get("display_note", ""),
        "last_trade": rt.get("last_trade"), "indicative_price": rt.get("indicative_price"),
        "prev_close": prev_close, "open": open_price, "high": high_price, "low": low_price,
        "change": change, "change_pct": change_pct, "signal": signal, "advice": advice, "score": score,
        "trend_score": trend_score, "intraday_score": intraday_score,
        "support": support, "resistance": resistance, "rsi": rsi, "ma5": ma5, "ma10": ma10,
        "ma20": ma20, "ma60": ma60, "comment": extra_comment,
        "source": rt["source"], "fibo": fibo, "bid_prices": rt.get("bid_prices", []),
        "ask_prices": rt.get("ask_prices", []), "bid_vols": rt.get("bid_vols", []),
        "ask_vols": rt.get("ask_vols", []), "buy_qty": rt.get("buy_qty", 0),
        "sell_qty": rt.get("sell_qty", 0), "orderbook_ratio": rt.get("orderbook_ratio", "-"),
        "orderbook_bias": rt.get("orderbook_bias", "無"), "quote_time": rt.get("quote_time", ""),
        "state_bucket": state_bucket,
        "strategy_level": get_strategy_level(score),
        "strategy_level_score": get_strategy_level_score(get_strategy_level(score)),
        "target_price": get_bullish_target(fibo, resistance),
        "bullish_target": get_bullish_target(fibo, resistance),
        "bearish_target": get_bearish_target(fibo, support),
        "watch_target": get_bullish_target(fibo, resistance),
        "price_valid": price_valid,
        "execution_price_valid": price_valid,
        "analysis_price_valid": analysis_price_valid,
        "rule_id": rule_id,
        "signal_reason": signal_reason,
        "wave_stage": wave["wave_stage"],
        "wave_score": wave["wave_score"],
        "wave_reason": wave["wave_reason"],
        "wave_risk_flag": wave["wave_risk_flag"],
        "fibo_position": fibo_pos["fibo_position"],
        "fibo_score": fibo_pos["fibo_score"],
        "fibo_risk_flag": fibo_pos["fibo_risk_flag"],
        "fibo_reason": fibo_pos["fibo_reason"],
        "trend_score_detail": "；".join(comments),
        "intraday_score_detail": intraday_comment,
        "wave_score_detail": wave["wave_reason"],
        "fibo_score_detail": fibo_pos["fibo_reason"],
        "decision_model_version": DECISION_MODEL_VERSION,
        "breakout_attempt_count": breakout.get("breakout_attempt_count", 0),
        "second_wave_score": breakout.get("second_wave_score", 0),
        "wave_n_breakout": breakout.get("wave_n_breakout", False),
        "volume_breakout_confirm": breakout.get("volume_breakout_confirm", False),
        "volume_ratio": breakout.get("volume_ratio", 0),
        "major_wave": phase5.get("major_wave", "-"),
        "minor_wave": phase5.get("minor_wave", "-"),
        "correction_type": phase5.get("correction_type", "-"),
        "fibo_retrace": phase5.get("fibo_retrace", 0.0),
        "fibo_retrace_zone": phase5.get("fibo_retrace_zone", "-"),
        "rebound_type": phase5.get("rebound_type", "-"),
        "correction_completed": phase5.get("correction_completed", False),
        "escape_rally": phase5.get("escape_rally", False),
        "impulsive_wave": phase5.get("impulsive_wave", False),
        "phase5_wave_label": phase5.get("phase5_wave_label", "-"),
        "phase5_block_reason": phase5.get("phase5_block_reason", ""),
    }
    result.update(calc_trade_plan(result))

    signal, advice, state_bucket, rule_id, signal_reason = evaluate_trade_state(
        close, prev_close, open_price, support, resistance, change_pct,
        trend_score, intraday_score, score, rt.get("orderbook_bias", "無"),
        ma20=ma20, ma60=ma60, rsi=rsi,
        wave_stage=result["wave_stage"],
        fibo_position=result["fibo_position"],
        fibo_risk_flag=result["fibo_risk_flag"],
        wave_risk_flag=result["wave_risk_flag"],
        rr_valid=result["rr_valid"],
        price_valid=price_valid
    )
    result.update({
        "signal": signal,
        "advice": advice,
        "state_bucket": state_bucket,
        "rule_id": rule_id,
        "signal_reason": signal_reason,
    })
    result.update(calc_trade_plan(result))
    result.update(classify_entry_zone(result))
    result.update(calc_wave_rr_risk_allocation(result))
    result.update(calc_position_sizing(result, account_capital=DEFAULT_ACCOUNT_CAPITAL, risk_pct=DEFAULT_RISK_PCT))
    result["wave_fibo_signal"] = build_wave_fibo_decision_note(result)
    result.update(build_final_decision(result))
    result = ensure_phase4_fields(result)
    result["risk_note"] = build_risk_note(
        close, support, resistance, rsi, score, change_pct,
        wave_stage=result["wave_stage"],
        fibo_position=result["fibo_position"],
        fibo_risk_flag=result["fibo_risk_flag"],
        wave_risk_flag=result["wave_risk_flag"],
        rr_valid=result["rr_valid"],
        price_valid=price_valid
    )
    result["candidate_pool"] = classify_candidate_pool(result)
    result["display_trade_type"] = result["candidate_pool"]
    result["trade_type"] = result["candidate_pool"]
    result["leader_candidate"] = classify_leader_stage(result)
    result["leader_stage"] = result["leader_candidate"]
    result["technical_state"] = classify_technical_state(result)
    result["execution_state"] = classify_execution_state(result)
    result["display_state"] = classify_display_state(result)
    result.update(build_rank_scores(result))
    result["light"] = get_light(
        result["signal"], result["score"], result["change_pct"],
        intraday_score=result["intraday_score"],
        fibo_risk_flag=result["fibo_risk_flag"],
        wave_risk_flag=result["wave_risk_flag"],
        final_decision=result.get("final_decision")
    )
    result = sync_display_semantics(result)
    log_decision_trace(result)
    result["display_target_price"] = get_display_target(result.get("target_price"), result["signal"], result["state_bucket"])
    result["display_rr"] = normalize_rr_display(result.get("rr"))
    result["summary_block"] = "\n".join([
        "【速讀摘要】",
        f"現價 / 漲跌幅 / 報價：{result['display_price']} / {result['change_pct']:+.2f}% / {result['display_note']}",
        f"總分 / 波段 / 盤中：{result['score']} / {result['trend_score']} / {result['intraday_score']}",
        f"波浪 / 費波 / 禁追：{result['wave_stage']} / {result['fibo_position']} / {'是' if (result['fibo_risk_flag'] or result['wave_risk_flag']) else '否'}",
        f"Phase5：{result.get('phase5_wave_label','-')} / 修正型態={result.get('correction_type','-')} / 回撤={result.get('fibo_retrace','-')} / 完成={'是' if result.get('correction_completed') else '否'} / 逃命={'是' if result.get('escape_rally') else '否'}",
        f"波費判定：{result['wave_fibo_signal']}",
        f"支撐 / 壓力 / 五檔：{result['support']} / {result['resistance']} / {result['orderbook_bias']}",
        f"燈號 / 訊號 / 建議 / 主升狀態：{result['light']} / {result['signal']} / {result.get('display_advice', result['advice'])} / {result['leader_candidate']}",
        f"交易類型 / 等級：{result.get('display_trade_type', result['trade_type'])} / {result['strategy_level']}",
        f"目標價 / RR / RR等級：{result['display_target_price']} / {result['display_rr']} / {result['rr_level']}",
        f"Phase4：進場狀態={result.get('entry_zone_status','-')} / 倉位={result.get('position_size_pct',0)}% / 資金等級={result.get('allocation_grade','-')} / 配置分={result.get('allocation_score','-')}",
        f"最終決策 / 可下單 / 下單類型：{result['final_decision']} / {result['execution_ready']} / {result.get('order_type_hint','-')} / {result['decision_reason']}",
        f"策略定位：技術狀態={result.get('technical_state','-')} / 執行狀態={result.get('execution_state','-')} / 顯示狀態={result.get('display_state','-')} / 量價比={result['orderbook_ratio']} / RSI={result['rsi']}",
    ])
    result["ai_analysis"] = build_ai_analysis(result)
    result["wave_analysis"] = build_wave_analysis(df)
    result["fibo_analysis"] = build_fibonacci_analysis(fibo)
    result["path_analysis"] = build_bull_bear_path(result)
    result.update(build_trade_scripts(result))
    return result






def get_yahoo_market_index_fallback(symbol: str) -> dict | None:
    """P0-03：Yahoo 只作明確 fallback，不再當正式加權來源。"""
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="5d", interval="1d", auto_adjust=False)
        if hist is None or hist.empty:
            return None
        last = hist.iloc[-1]
        if len(hist) >= 2:
            prev_close = float(hist.iloc[-2]["Close"])
        else:
            prev_close = float(last["Close"])
        close = float(last["Close"])
        change = close - prev_close
        pct = (change / prev_close * 100) if prev_close else 0.0
        snapshot = {
            "close": round(close, 2),
            "prev_close": round(prev_close, 2),
            "change": round(change, 2),
            "pct": round(pct, 2),
            "source": "Yahoo_FALLBACK",
            "quality": "YAHOO_FALLBACK",
            "market_data_valid": False,
            "update_time": "",
            "ex_ch": symbol,
            "raw_z": round(close, 2),
            "raw_y": round(prev_close, 2),
        }
        return snapshot
    except Exception as e:
        logging.warning("YAHOO_MARKET_FALLBACK_FAILED symbol=%s error=%s", symbol, e)
        return None


# 相容舊呼叫名稱；正式程式不得直接用作主來源。
def get_market_index_quote(symbol: str) -> dict | None:
    return get_yahoo_market_index_fallback(symbol)


def validate_taiex_snapshot(snapshot: dict | None) -> dict | None:
    """P2-02：加權指數快照合理性與時效檢查。"""
    if not snapshot:
        return None
    close = safe_float(snapshot.get("close"))
    prev_close = safe_float(snapshot.get("prev_close"))
    pct = safe_float(snapshot.get("pct"))
    update_time = str(snapshot.get("update_time") or "").strip()
    if close is None or prev_close is None or pct is None:
        snapshot["quality"] = "INVALID"
        snapshot["market_data_valid"] = False
        return None
    if close <= 0 or prev_close <= 0 or abs(pct) > 10 or not update_time:
        snapshot["quality"] = "INVALID"
        snapshot["market_data_valid"] = False
        return None
    return snapshot


def fetch_twse_mis_market_snapshot() -> dict | None:
    """P0-01：以 TWSE MIS tse_t00.tw 作加權指數單一優先來源。"""
    ex_ch = "tse_t00.tw"
    try:
        msg_array = fetch_mis_msg_array(ex_ch, retries=2, timeout=8)
        if not msg_array:
            return None
        item = msg_array[0]
        close = safe_float(item.get("z"))
        prev = safe_float(item.get("y"))
        if close is None or prev is None or prev <= 0:
            logging.warning(
                "TAIEX_MIS_INVALID_RAW ex_ch=%s raw_z=%s raw_y=%s time=%s msg_count=%s",
                ex_ch, item.get("z"), item.get("y"), item.get("t") or item.get("tt"), len(msg_array)
            )
            return None
        change = round_price(close - prev)
        pct = round(change / prev * 100, 2)
        snapshot = {
            "close": round_price(close),
            "prev_close": round_price(prev),
            "change": change,
            "pct": pct,
            "source": "TWSE_MIS",
            "quality": "TWSE_MIS_REALTIME",
            "market_data_valid": True,
            "update_time": item.get("t") or item.get("tt") or "",
            "ex_ch": ex_ch,
            "raw_z": item.get("z"),
            "raw_y": item.get("y"),
        }
        return validate_taiex_snapshot(snapshot)
    except Exception as e:
        logging.warning("TAIEX_MIS_FETCH_FAILED error=%s", e)
        return None


def log_market_snapshot(market: dict) -> None:
    """P0-02：記錄市場總覽原始來源，未來可從 log 追真因。"""
    try:
        twse = market.get("twse", {}) or {}
        logging.info(
            "MARKET_SNAPSHOT source=%s quality=%s valid=%s taiex=%s change=%s pct=%s time=%s ex_ch=%s raw_z=%s raw_y=%s up=%s down=%s breadth_source=%s breadth_quality=%s",
            twse.get("source"), twse.get("quality"), twse.get("market_data_valid"),
            twse.get("close"), twse.get("change"), twse.get("pct"), twse.get("update_time"),
            twse.get("ex_ch"), twse.get("raw_z"), twse.get("raw_y"),
            market.get("up"), market.get("down"), market.get("breadth_source"), market.get("breadth_quality")
        )
    except Exception as e:
        logging.warning("MARKET_SNAPSHOT_LOG_FAILED error=%s", e)


def infer_volume_status(results: list[dict]) -> str:
    if not results:
        return "未知"
    trend_up = sum(1 for r in results if r.get("trend_score", 0) >= 75)
    weak = sum(1 for r in results if r.get("trend_score", 0) < 40)
    if trend_up >= max(2, len(results) * 0.35):
        return "放量"
    if weak >= max(3, len(results) * 0.45):
        return "量縮"
    return "正常"

def _count_change_sign(v) -> int:
    if v in (None, "", "--", "---"):
        return 0
    s = str(v).strip().replace(",", "")
    if any(x in s for x in ["跌", "▼", "-"]):
        try:
            return -1 if float(s.replace("跌", "").replace("▼", "")) != 0 else 0
        except Exception:
            return -1
    if any(x in s for x in ["漲", "+", "▲"]):
        try:
            return 1 if float(s.replace("漲", "").replace("+", "").replace("▲", "")) != 0 else 0
        except Exception:
            return 1
    try:
        f = float(s)
        return 1 if f > 0 else (-1 if f < 0 else 0)
    except Exception:
        return 0

def fetch_twse_breadth() -> tuple[int, int, str]:
    urls = [
        "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
        "https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL?response=json",
    ]
    for url in urls:
        try:
            records = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"}).json()
            if isinstance(records, dict):
                records = records.get("data") or records.get("records") or []
            if not isinstance(records, list) or not records:
                continue
            up = down = 0
            for rec in records:
                if not isinstance(rec, dict):
                    continue
                code = str(rec.get("Code") or rec.get("證券代號") or rec.get("股票代號") or "").strip()
                if not (code.isdigit() and len(code) == 4):
                    continue
                sign_val = None
                for key in ("Change", "漲跌價差", "漲跌(+/-)", "漲跌"):
                    if key in rec:
                        sign_val = rec.get(key)
                        break
                sign = _count_change_sign(sign_val)
                if sign > 0:
                    up += 1
                elif sign < 0:
                    down += 1
            if up + down > 0:
                return up, down, "TWSE 官方"
        except Exception:
            continue
    return 0, 0, ""

def fetch_tpex_breadth() -> tuple[int, int, str]:
    candidate_urls = [
        "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes",
        "https://www.tpex.org.tw/openapi/v1/tpex_daily_market_value",
    ]
    for url in candidate_urls:
        try:
            records = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"}).json()
            if not isinstance(records, list) or not records:
                continue
            up = down = 0
            for rec in records:
                if not isinstance(rec, dict):
                    continue
                code = str(rec.get("SecuritiesCompanyCode") or rec.get("股票代號") or rec.get("證券代號") or rec.get("Code") or "").strip()
                if code and not (code.isdigit() and len(code) == 4):
                    continue
                sign_val = None
                for key in ("漲跌", "Change", "漲跌價差", "UpDown", "漲跌(+/-)"):
                    if key in rec:
                        sign_val = rec.get(key)
                        break
                sign = _count_change_sign(sign_val)
                if sign > 0:
                    up += 1
                elif sign < 0:
                    down += 1
            if up + down > 0:
                return up, down, "TPEX 官方"
        except Exception:
            continue
    return 0, 0, ""

def get_tsmc_market_quote() -> dict:
    rt = get_tw_realtime_quote("2330", "台股上市")
    if rt:
        change = round_price(rt["close"] - rt["prev_close"])
        pct = round((change / rt["prev_close"] * 100), 2) if rt["prev_close"] else 0.0
        return {"close": rt["close"], "change": change, "pct": pct, "source": rt.get("source", "TWSE MIS")}
    yq = get_us_yahoo_quote("2330.TW", 0.0, 0.0, 0.0, 0.0, 0.0)
    change = round_price(yq["close"] - yq["prev_close"])
    pct = round((change / yq["prev_close"] * 100), 2) if yq["prev_close"] else 0.0
    return {"close": yq["close"], "change": change, "pct": pct, "source": yq.get("source", "Yahoo Finance")}

def get_market_data(results: list[dict]) -> dict:
    twse = fetch_twse_mis_market_snapshot()
    if not twse:
        twse = get_yahoo_market_index_fallback("^TWII")
    if not twse:
        twse = {
            "close": 0.0, "prev_close": 0.0, "change": 0.0, "pct": 0.0,
            "source": "NONE", "quality": "INVALID", "market_data_valid": False,
            "update_time": "", "ex_ch": "tse_t00.tw", "raw_z": None, "raw_y": None,
        }
    tsmc = get_tsmc_market_quote()

    listed_up, listed_down, src1 = fetch_twse_breadth()
    otc_up, otc_down, src2 = fetch_tpex_breadth()
    up = listed_up + otc_up
    down = listed_down + otc_down
    breadth_source = " / ".join([s for s in (src1, src2) if s]).strip()
    breadth_quality = "OFFICIAL" if breadth_source else "PROXY"

    if up + down == 0:
        up = sum(1 for r in results if r.get("change", 0) > 0)
        down = sum(1 for r in results if r.get("change", 0) < 0)
        breadth_source = "觀察池代理"
        breadth_quality = "PROXY"

    market = {
        "twse": twse,
        "tsmc": tsmc,
        "up": up,
        "down": down,
        "volume_status": infer_volume_status(results),
        "breadth_source": breadth_source,
        "breadth_quality": breadth_quality,
        "market_data_valid": bool(twse.get("market_data_valid", False)),
        "market_execution_allowed": bool(twse.get("market_data_valid", False)),
        "market_risk_level": "NORMAL" if twse.get("market_data_valid", False) else "DATA_UNVERIFIED",
        "source_note": (
            f"加權={twse.get('source','-')} / quality={twse.get('quality','-')} / "
            f"time={twse.get('update_time','-')} / 台積電={tsmc.get('source','TWSE MIS')} / "
            f"家數={breadth_source}({breadth_quality})"
        ),
    }
    log_market_snapshot(market)
    return market

def get_market_mode(market: dict) -> str:
    twse_quality = market.get("twse", {}).get("quality", "")
    if not market.get("market_data_valid", False) or twse_quality != "TWSE_MIS_REALTIME":
        return "資料待確認"
    twse_pct = market.get("twse", {}).get("pct", 0.0)
    tsmc_pct = market.get("tsmc", {}).get("pct", 0.0)
    up = market.get("up", 0)
    down = market.get("down", 0)
    if twse_pct >= 0.6 and tsmc_pct >= 0.8 and up > down:
        return "偏多震盪"
    if twse_pct <= -0.6 and tsmc_pct <= -0.5 and down > up:
        return "偏弱震盪"
    if twse_pct >= 0 and tsmc_pct >= 0 and up >= down * 0.9:
        return "震盪偏多"
    if twse_pct < 0 and tsmc_pct < 0 and down > up:
        return "震盪偏弱"
    return "區間震盪"

def get_today_strategy(market: dict, mode: str) -> str:
    if mode == "資料待確認" or not market.get("market_data_valid", False):
        return "大盤資料未通過 TWSE MIS 即時驗證，總體策略降級為人工觀察；個股技術分析保留，但不得用大盤資料升級下單判斷"
    twse_pct = market.get("twse", {}).get("pct", 0.0)
    tsmc_pct = market.get("tsmc", {}).get("pct", 0.0)
    breadth_balance = market.get("up", 0) - market.get("down", 0)
    if mode == "偏多震盪":
        if tsmc_pct >= 1.0:
            return "大盤與台積電同步偏強，只做主升與整理偏多，避免追高末升段"
        return "指數偏強但台積電未全面發動，以拉回承接為主，不追爆量長紅"
    if mode == "震盪偏多":
        return "大盤偏多但結構未全面擴散，以整理偏多與低接型主升股為主"
    if mode == "偏弱震盪":
        return "大盤與台積電偏弱，優先防守，不抄底弱勢股，只看支撐是否止穩"
    if mode == "震盪偏弱":
        return "盤面偏弱且家數落後，降低持股水位，反彈先看壓力不追價"
    if twse_pct > 0 or breadth_balance > 0:
        return "市場無明確主流但略有撐盤，只做型態完整個股"
    return "市場無明確優勢，觀望為主，等待大盤與台積電同步轉強"

def build_market_overview(results: list[dict]) -> str:
    if not results:
        return "加權：- ｜ 台積電：- ｜ 上漲/下跌：-/- ｜ 量能：未知\n市場模式：尚無資料 ｜ 今日策略：尚無資料"
    market = get_market_data(results)
    mode = get_market_mode(market)
    strategy = get_today_strategy(market, mode)

    twse = market["twse"]
    tsmc = market["tsmc"]
    twse_arrow = "▲" if twse["change"] >= 0 else "▼"
    tsmc_arrow = "▲" if tsmc["change"] >= 0 else "▼"
    line1 = (
        f"加權：{twse['close']} {twse_arrow}{abs(twse['change'])} ({twse['pct']:+.2f}%) ｜ "
        f"台積電：{tsmc['close']} {tsmc_arrow}{abs(tsmc['change'])} ({tsmc['pct']:+.2f}%) ｜ "
        f"上漲/下跌：{market['up']}/{market['down']} ｜ 量能：{market['volume_status']}"
    )
    line2 = f"市場模式：{mode} ｜ 今日策略：{strategy}"
    line3 = (
        f"加權來源：{twse.get('source','-')} / quality={twse.get('quality','-')} / "
        f"time={twse.get('update_time','-')} ｜ 家數來源：{market.get('breadth_source','-')} / {market.get('breadth_quality','-')}"
    )
    if not market.get("market_data_valid", False):
        line3 += " ｜ 警告：非 TWSE MIS 即時有效大盤，不作交易決策升級"
    return line1 + "\n" + line2 + "\n" + line3




def validate_phase4_decision_rules():
    """Phase4 A01-A12 防回歸驗收。"""
    cases = [
        {
            "id": "A01",
            "name": "BUY 必須進場區有效",
            "data": {"price_valid": True, "signal": "主升突破", "advice": "拉回加碼", "state_bucket": "strong", "rr": 2.1, "rr_valid": True, "entry_zone_ready": True, "entry_zone_status": "IN_ZONE", "position_size_pct": 8, "allocation_score": 85, "allocation_grade": "A", "fibo_risk_flag": False, "wave_risk_flag": False},
            "expected": "BUY",
        },
        {
            "id": "A02",
            "name": "ABOVE_ENTRY不可下單",
            "data": {"price_valid": True, "signal": "強勢追蹤", "advice": "拉回加碼", "state_bucket": "strong", "rr": 2.2, "rr_valid": True, "entry_zone_ready": False, "entry_zone_status": "ABOVE_ENTRY", "position_size_pct": 0, "allocation_score": 82, "allocation_grade": "B", "fibo_risk_flag": False, "wave_risk_flag": False},
            "expected": "WAIT",
        },
        {
            "id": "A03",
            "name": "BUY必須倉位大於0",
            "data": {"price_valid": True, "signal": "主升突破", "advice": "拉回加碼", "state_bucket": "strong", "rr": 2.3, "rr_valid": True, "entry_zone_ready": True, "entry_zone_status": "IN_ZONE", "position_size_pct": 0, "allocation_score": 90, "allocation_grade": "A", "fibo_risk_flag": False, "wave_risk_flag": False},
            "expected": "WAIT",
        },
        {
            "id": "A04",
            "name": "禁追不可BUY",
            "data": {"price_valid": True, "signal": "末升/禁追風險", "advice": "不追高", "state_bucket": "weak", "rr": 3.0, "rr_valid": True, "entry_zone_ready": True, "entry_zone_status": "NO_CHASE", "position_size_pct": 8, "allocation_score": 90, "allocation_grade": "A", "fibo_risk_flag": True, "wave_risk_flag": False},
            "expected": "AVOID",
        },
        {
            "id": "A05",
            "name": "RR不足不可下單",
            "data": {"price_valid": True, "signal": "強勢追蹤", "advice": "拉回加碼", "state_bucket": "strong", "rr": 1.2, "rr_valid": False, "entry_zone_ready": True, "entry_zone_status": "IN_ZONE", "position_size_pct": 3, "allocation_score": 80, "allocation_grade": "B", "fibo_risk_flag": False, "wave_risk_flag": False},
            "expected": "WAIT",
        },
        {
            "id": "A06",
            "name": "非即時報價不可下單",
            "data": {"price_valid": False, "signal": "主升突破", "advice": "拉回加碼", "state_bucket": "strong", "rr": 3.0, "rr_valid": True, "entry_zone_ready": True, "entry_zone_status": "IN_ZONE", "position_size_pct": 8, "allocation_score": 90, "allocation_grade": "A", "fibo_risk_flag": False, "wave_risk_flag": False},
            "expected": "WAIT",
        },
        {
            "id": "A07",
            "name": "Allocation低於70不可BUY",
            "data": {"price_valid": True, "signal": "主升突破", "advice": "拉回加碼", "state_bucket": "strong", "rr": 2.0, "rr_valid": True, "entry_zone_ready": True, "entry_zone_status": "IN_ZONE", "position_size_pct": 4, "allocation_score": 60, "allocation_grade": "C", "fibo_risk_flag": False, "wave_risk_flag": False},
            "expected": "WAIT",
        },
        {
            "id": "A08",
            "name": "BLOCK必須AVOID",
            "data": {"price_valid": True, "signal": "強勢追蹤", "advice": "拉回加碼", "state_bucket": "strong", "rr": 2.5, "rr_valid": True, "entry_zone_ready": True, "entry_zone_status": "IN_ZONE", "position_size_pct": 8, "allocation_score": 0, "allocation_grade": "BLOCK", "phase4_block_reason": "測試BLOCK", "fibo_risk_flag": False, "wave_risk_flag": False},
            "expected": "AVOID",
        },
        {
            "id": "A09",
            "name": "BROKEN必須AVOID",
            "data": {"price_valid": True, "signal": "跌破支撐", "advice": "減碼/防守", "state_bucket": "weak", "rr": 2.0, "rr_valid": True, "entry_zone_ready": False, "entry_zone_status": "BROKEN", "position_size_pct": 0, "allocation_score": 0, "allocation_grade": "BLOCK", "fibo_risk_flag": False, "wave_risk_flag": False},
            "expected": "AVOID",
        },
        {
            "id": "A10",
            "name": "BREAKOUT_CONFIRM小倉可BUY",
            "data": {"price_valid": True, "signal": "突破強勢", "advice": "突破可追", "state_bucket": "strong", "rr": 2.0, "rr_valid": True, "entry_zone_ready": True, "entry_zone_status": "BREAKOUT_CONFIRM", "position_size_pct": 3, "allocation_score": 75, "allocation_grade": "B", "fibo_risk_flag": False, "wave_risk_flag": False},
            "expected": "BUY",
        },
    ]
    for case in cases:
        decision = build_final_decision(case["data"])
        assert decision["final_decision"] == case["expected"], f"Phase4驗收失敗：{case['id']} {case['name']}，得到 {decision['final_decision']}"
        if decision["final_decision"] == "BUY":
            assert decision["execution_ready"] is True, "BUY 必須 execution_ready=True"
            assert case["data"].get("entry_zone_ready") is True, "BUY 必須 entry_zone_ready=True"
            assert case["data"].get("position_size_pct", 0) > 0, "BUY 必須 position_size_pct>0"
            assert case["data"].get("allocation_score", 0) >= MIN_BUY_ALLOCATION_SCORE, "BUY 必須 allocation_score達標"
        else:
            assert decision["execution_ready"] is False, "非BUY不可 execution_ready=True"
            assert decision.get("position_size_pct", 0) == 0, "非BUY倉位必須歸零"
    # V6 semantic regression checks: price invalid must not become weak if structure is valid.
    sig = evaluate_trade_state(
        close=9.32, prev_close=9.4, open_price=9.32, support=8.93, resistance=9.68, change_pct=-0.85,
        trend_score=100, intraday_score=18, score=67, orderbook_bias="賣盤偏強",
        ma20=9.0, ma60=8.8, rsi=56.18,
        wave_stage="第3浪", fibo_position="挑戰1.0前",
        fibo_risk_flag=False, wave_risk_flag=False, rr_valid=False, price_valid=False
    )
    assert sig[2] != "weak", "V6驗收失敗：彩晶類非即時報價不得覆蓋為 weak"
    assert "第3浪" in sig[0], "V6驗收失敗：彩晶類應保留第3浪觀察語義"

    fibo_test = {
        "next_target": 120,
        "bullish_next_target": 150,
        "bearish_next_target": 100,
        "target_1_0": 120,
        "target_1_382": 100,
        "target_1_618": 87,
        "direction": "下降波",
        "base_low": 120,
        "base_high": 167,
        "summary": "test",
    }
    path = build_bull_bear_path({"support": 140, "resistance": 147, "fibo": fibo_test, "signal": "整理偏多觀察", "display_advice": "等待成交確認"})
    assert "下一目標看 150" in path, "V6驗收失敗：多方路徑不得引用空方 next_target"
    assert "空方風險目標看 100" in path, "V6驗收失敗：空方路徑需讀 bearish target"

    # V6.2 consistency regression: RR/target must use bullish_next_target, not bearish/next_target.
    plan = calc_trade_plan({
        "support": 140,
        "resistance": 147,
        "state_bucket": "bullish",
        "fibo_risk_flag": False,
        "wave_risk_flag": False,
        "fibo": {
            "next_target": 120,
            "bullish_next_target": 160,
            "bearish_next_target": 100,
        }
    })
    assert plan["bullish_target"] == 160, "V6.2驗收失敗：bullish_target需讀bullish_next_target"
    assert plan["bearish_target"] == 100, "V6.2驗收失敗：bearish_target需讀bearish_next_target"
    assert plan["fibo_target"] == 160, "V6.2驗收失敗：交易RR不得讀空方next_target"
    assert plan["target_price"] >= 147, "V6.2驗收失敗：多方target_price不得低於壓力"

    # V6.2 consistency regression: candidate_pool/display_trade_type/trade_type需一致。
    test_result = {
        "wave_stage": "第3浪", "fibo_position": "挑戰1.0前",
        "second_wave_score": 75, "signal": "第3浪突破前觀察",
        "quote_quality": "REALTIME", "final_decision": "WAIT",
        "entry_zone_status": "WAIT_CONFIRM", "rr_valid": False,
        "score": 70, "trend_score": 75, "intraday_score": 60,
        "close": 10, "ma20": 9, "ma60": 8,
        "fibo_risk_flag": False, "wave_risk_flag": False,
        "price_valid": False
    }
    test_result = sync_display_semantics(test_result)
    assert test_result["trade_type"] == test_result["display_trade_type"] == test_result["candidate_pool"], "V6.2驗收失敗：交易類型外顯必須一致"

    return True


def validate_phase5_wave_rules():
    """Phase5 P5-01~P5-20 防回歸驗收。"""
    import pandas as _pd

    # TC-P5-01：4763類主跌反彈/逃命反彈，不得BUY。
    n = 130
    df_down = _pd.DataFrame({
        "Open": [100 - i * 0.3 for i in range(n)],
        "High": [101 - i * 0.3 for i in range(n)],
        "Low": [99 - i * 0.3 for i in range(n)],
        "Close": [100 - i * 0.3 for i in range(n)],
        "Volume": [1000 for _ in range(n)],
    })
    df_down = calc_indicators(df_down)
    close = float(df_down["Close"].iloc[-1])
    ma20 = float(df_down["MA20"].iloc[-1])
    ma60 = float(df_down["MA60"].iloc[-1])
    rsi = float(df_down["RSI"].iloc[-1])
    sr = calc_professional_sr(df_down)
    wave = structured_wave_analysis(df_down)
    fibo = calc_fibonacci_targets(df_down)
    fibo_pos = classify_fibo_position(close, fibo)
    phase5 = calculate_phase5_wave_position(df_down, wave, fibo, fibo_pos, sr, close, ma20, ma60, rsi, 0.8)
    assert phase5["major_wave"] == "主跌修正浪", "Phase5驗收失敗：主跌反彈應判為主跌修正浪"
    assert phase5["escape_rally"] is True or phase5["rebound_type"] in ("跌深技術反彈", "逃命反彈"), "Phase5驗收失敗：主跌弱反彈需標示逃命/跌深反彈"
    decision = build_final_decision({
        "price_valid": True, "signal": "修正反彈觀察", "advice": "低接布局", "state_bucket": "strong",
        "rr": 2.0, "rr_valid": True, "entry_zone_ready": True, "entry_zone_status": "IN_ZONE",
        "position_size_pct": 6, "allocation_score": 80, "allocation_grade": "B",
        "fibo_risk_flag": False, "wave_risk_flag": False, **phase5
    })
    assert decision["final_decision"] != "BUY" and decision["execution_ready"] is False, "Phase5驗收失敗：逃命/跌深技術反彈不可BUY"

    # TC-P5-02：主升2/4浪拉回未完成，只能WAIT。
    phase5_pullback = {
        "major_wave": "主升推動浪", "minor_wave": "第2浪/第4浪拉回", "correction_type": "主升回撤修正",
        "fibo_retrace": 0.5, "rebound_type": "主升拉回", "correction_completed": False,
        "escape_rally": False, "impulsive_wave": False, "phase5_wave_label": "主升推動浪 / 第2浪/第4浪拉回 / 主升拉回"
    }
    dec2 = build_final_decision({
        "price_valid": True, "signal": "主升拉回觀察", "advice": "低接布局", "state_bucket": "bullish",
        "rr": 2.0, "rr_valid": True, "entry_zone_ready": True, "entry_zone_status": "IN_ZONE",
        "position_size_pct": 3, "allocation_score": 75, "allocation_grade": "B",
        "fibo_risk_flag": False, "wave_risk_flag": False, **phase5_pullback
    })
    assert dec2["final_decision"] != "BUY", "Phase5驗收失敗：主升拉回未完成不可直接BUY"

    # TC-P5-04：真正第3浪推動仍可保留BUY通道。
    phase5_impulse = {"escape_rally": False, "rebound_type": "一般反彈", "correction_completed": True, "impulsive_wave": True}
    dec3 = build_final_decision({
        "price_valid": True, "signal": "突破強勢", "advice": "突破可追", "state_bucket": "strong",
        "rr": 2.0, "rr_valid": True, "entry_zone_ready": True, "entry_zone_status": "BREAKOUT_CONFIRM",
        "position_size_pct": 3, "allocation_score": 80, "allocation_grade": "B",
        "fibo_risk_flag": False, "wave_risk_flag": False, **phase5_impulse
    })
    assert dec3["final_decision"] == "BUY" and dec3["execution_ready"] is True, "Phase5驗收失敗：真正第3浪推動應可通過後置Gate"
    return True

def validate_market_snapshot_rules():
    """P2-01：大盤資料來源防回歸驗收，不連網，以 mock snapshot 驗證 Gate。"""
    ok = validate_taiex_snapshot({
        "close": 23000.0,
        "prev_close": 22800.0,
        "change": 200.0,
        "pct": 0.88,
        "source": "TWSE_MIS",
        "quality": "TWSE_MIS_REALTIME",
        "market_data_valid": True,
        "update_time": "09:30:00",
        "ex_ch": "tse_t00.tw",
        "raw_z": "23000",
        "raw_y": "22800",
    })
    assert ok is not None and ok["quality"] == "TWSE_MIS_REALTIME", "A-M01驗收失敗：TWSE MIS成功快照未通過"
    bad = validate_taiex_snapshot({
        "close": 0.0,
        "prev_close": 22800.0,
        "change": 0.0,
        "pct": 0.0,
        "source": "TWSE_MIS",
        "quality": "TWSE_MIS_REALTIME",
        "market_data_valid": True,
        "update_time": "09:30:00",
    })
    assert bad is None, "A-M02/A-M04驗收失敗：無效大盤值不可通過"
    fallback_market = {
        "twse": {"close": 23000.0, "prev_close": 22800.0, "change": 200.0, "pct": 0.88, "source": "Yahoo_FALLBACK", "quality": "YAHOO_FALLBACK", "market_data_valid": False},
        "tsmc": {"pct": 1.0},
        "up": 1000,
        "down": 200,
        "market_data_valid": False,
    }
    assert get_market_mode(fallback_market) == "資料待確認", "A-M03/A-M09驗收失敗：Yahoo fallback必須降級"
    assert "人工觀察" in get_today_strategy(fallback_market, "資料待確認"), "A-M08驗收失敗：資料待確認需提示人工觀察"
    assert normalize_symbol("2330")[0] in ("2330.TW", "2330.TWO"), "A-M05/A-M06驗收失敗：代碼候選格式錯誤"
    return True



if __name__ == "__main__":
    main()

# Core module intentionally has no __main__ Tkinter startup.
