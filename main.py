# GTC 股票專業版看盤分析系統 v5.4.1 Dual Deployment Edition
# Based on v5.3.6 AutoRefresh UploadCache FIX + v5.4.0 Streamlit Cloud POC.
# Supports Windows EXE/local BAT and Streamlit Cloud/Render/Railway semi-online deployment.

from __future__ import annotations

from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any
import tempfile
import time
import json
import os
import hmac
import hashlib
import sqlite3
import logging

import pandas as pd
import streamlit as st
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas


try:
    import gtc_core_engine as core
except Exception as exc:  # pragma: no cover
    st.error(f"無法載入核心分析模組 gtc_core_engine.py：{exc}")
    st.stop()

APP_TITLE = "GTC 股票專業版看盤分析系統"
APP_VERSION = "v5.4.3-Dual-MultiUser-SessionSafeAutoRefresh"
AUTO_REFRESH_ENGINE_VERSION = "1.0.0"
AUTO_REFRESH_HEARTBEAT_SECONDS = 1
EVENT_ENGINE_VERSION = "1.4.2"
EVENT_TRUE_CONFIRM_SECONDS = 15 * 60
EVENT_FALSE_CONFIRM_SECONDS = 30 * 60
EVENT_BREAKDOWN_CONFIRM_SECONDS = 30 * 60
EVENT_RECLAIM_CONFIRM_SECONDS = 15 * 60
EVENT_PRICE_TOLERANCE_PCT = 0.002
EVENT_HISTORY_LIMIT = 30
DEFAULT_SYMBOLS = "2330,2382,3231,2308,3017,4979,AAPL,NVDA,MSFT"
CORE_COLUMNS = [
    "排名", "燈號", "市場", "代號", "名稱", "顯示價", "漲跌幅%",
    "作戰等級", "控制欄", "即時指示", "盤中事件", "事件建議", "訊號", "建議", "分數", "等級",
    "目標價", "RR", "主升候選", "技術狀態", "執行狀態", "顯示狀態",
    "進場狀態", "建議倉位%", "資金等級", "配置分", "最終決策", "可下單",
]
ADVANCED_COLUMNS = [
    "排名", "燈號", "市場", "代號", "名稱", "顯示價", "漲跌", "漲跌幅%",
    "作戰等級", "控制欄", "即時指示", "盤中事件", "事件建議", "訊號", "建議", "分數", "等級", "目標價", "RR",
    "主升候選", "波浪定位", "費波位置", "禁追提示", "波費判定", "大波", "小波",
    "修正型態", "回撤比例", "反彈性質", "修正完成", "逃命反彈", "推動浪",
    "技術狀態", "執行狀態", "顯示狀態", "波段分", "盤中分", "支撐", "壓力",
    "RSI", "五檔力道", "交易類型", "進場狀態", "進場可執行", "建議倉位%",
    "資金等級", "配置分", "阻擋原因", "下單類型", "最終決策", "可下單", "決策原因", "報價說明",
]


RUNTIME_DIR = Path.cwd() / ".gtc_runtime"
RUNTIME_CACHE_PATH = RUNTIME_DIR / "gtc_web_state_cache.json"


def _is_local_persistent_mode() -> bool:
    """Return True only for Windows EXE / local BAT mode.

    Cloud deployments must not persist uploaded user battle plans to shared server disk.
    launcher.py and run_gtc_web.bat set GTC_RUNTIME_MODE=local for desktop use.
    Streamlit Cloud / Render / Railway should leave it unset or set it to cloud.
    """
    mode = str(os.environ.get("GTC_RUNTIME_MODE", "")).strip().lower()
    if mode in {"local", "desktop", "exe", "windows", "bat"}:
        return True
    if mode in {"cloud", "streamlit_cloud", "render", "railway"}:
        return False
    # Default is cloud-safe.  Local launchers opt in to disk cache explicitly.
    return False


def _runtime_mode_label() -> str:
    return "Local EXE/BAT" if _is_local_persistent_mode() else "Cloud/Session"


def _load_runtime_cache() -> dict[str, Any]:
    """Load local UI/runtime state only in desktop mode.

    In cloud mode, session_state is the source of truth to avoid cross-user leakage.
    """
    if not _is_local_persistent_mode():
        return {}
    try:
        if RUNTIME_CACHE_PATH.exists():
            with RUNTIME_CACHE_PATH.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _save_runtime_cache() -> None:
    """Persist small UI state only for desktop EXE/BAT mode.

    Analysis results are intentionally not cached.  Cloud mode is no-op.
    """
    if not _is_local_persistent_mode():
        return None
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "symbol_text": st.session_state.get("symbol_text", DEFAULT_SYMBOLS),
            "battle_plan_map": st.session_state.get("battle_plan_map", {}),
            "battle_plan_status": st.session_state.get("battle_plan_status", ""),
            "battle_plan_filename": st.session_state.get("battle_plan_filename", ""),
            "last_uploaded_signature": st.session_state.get("last_uploaded_signature", ""),
            "auto_refresh_enabled": bool(st.session_state.get("auto_refresh_enabled", False)),
            "refresh_seconds": int(st.session_state.get("refresh_seconds", 30)),
            "last_auto_run_ts": float(st.session_state.get("last_auto_run_ts", 0.0) or 0.0),
            "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "runtime_mode": _runtime_mode_label(),
        }
        with RUNTIME_CACHE_PATH.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _clear_runtime_cache() -> None:
    if not _is_local_persistent_mode():
        return None
    try:
        if RUNTIME_CACHE_PATH.exists():
            RUNTIME_CACHE_PATH.unlink()
    except Exception:
        pass


def _uploaded_signature(uploaded_file) -> str:
    """Return stable signature to avoid reparsing the same uploaded file every rerun."""
    try:
        size = getattr(uploaded_file, "size", None)
        if size is None:
            pos = uploaded_file.tell()
            uploaded_file.seek(0, 2)
            size = uploaded_file.tell()
            uploaded_file.seek(pos)
        return f"{uploaded_file.name}|{size}"
    except Exception:
        return str(getattr(uploaded_file, "name", "uploaded-file"))


def init_state() -> None:
    cache = _load_runtime_cache()
    defaults = {
        "symbol_text": DEFAULT_SYMBOLS,
        "battle_plan_map": {},
        "battle_plan_status": "作戰表：尚未匯入；未列入作戰表股票將自動產生控制欄",
        "battle_plan_filename": "",
        "last_uploaded_signature": "",
        "results": [],
        "errors": [],
        "last_update_time": None,
        "market_overview": "加權：- ｜ 台積電：- ｜ 上漲/下跌：-/- ｜ 量能：未知\n市場模式：尚無資料 ｜ 今日策略：尚無資料",
        "auto_refresh_enabled": False,
        "refresh_seconds": 30,
        "last_auto_run_ts": 0.0,
        "intraday_event_store": {},
        "intraday_event_transitions": [],
        "auto_refresh_running": False,
        "auto_refresh_run_count": 0,
        "auto_refresh_last_trigger": "-",
        "auto_refresh_last_error": "",
        "auto_refresh_scheduler_status": "尚未啟用",
    }
    persistent_keys = {
        "symbol_text",
        "battle_plan_map",
        "battle_plan_status",
        "battle_plan_filename",
        "last_uploaded_signature",
        "auto_refresh_enabled",
        "refresh_seconds",
        "last_auto_run_ts",
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = cache.get(key, value) if key in persistent_keys else value

    if st.session_state.get("battle_plan_map") and st.session_state.get("battle_plan_filename"):
        restored_count = len(st.session_state.get("battle_plan_map", {}))
        status = str(st.session_state.get("battle_plan_status", ""))
        restore_marker = "已從本機快取恢復" if _is_local_persistent_mode() else "已從目前工作階段恢復"
        if restore_marker not in status:
            st.session_state.battle_plan_status = (
                f"作戰表：{st.session_state.battle_plan_filename}｜{restore_marker} {restored_count} 檔；"
                "自動刷新可沿用原作戰表，不必重複上傳"
            )

def parse_symbols(text: str) -> list[str]:
    parts = [x.strip() for x in str(text or "").replace("，", ",").split(",")]
    return [p for p in parts if p]


def save_uploaded_file(uploaded_file) -> str:
    suffix = Path(uploaded_file.name).suffix or ".xlsx"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(uploaded_file.getbuffer())
        return tmp.name


def load_battle_plan_from_upload(uploaded_file) -> None:
    tmp_path = save_uploaded_file(uploaded_file)
    mapping, status, code_order = core.load_battle_plan_excel(tmp_path)
    st.session_state.battle_plan_map = mapping
    st.session_state.battle_plan_filename = uploaded_file.name
    st.session_state.last_uploaded_signature = _uploaded_signature(uploaded_file)
    if code_order:
        st.session_state.symbol_text = ",".join(code_order)
    sync_note = f"｜已同步 {len(code_order)} 檔到股票代號輸入框" if code_order else ""
    st.session_state.battle_plan_status = f"作戰表：{uploaded_file.name}｜Sheet=個股作戰表｜{status}{sync_note}"
    _save_runtime_cache()



# =========================
# v5.4.1 Web AUTO Control Overlay
# =========================
# 說明：
# 1) 已匯入作戰表且代號命中：完全沿用 Excel 作戰表支撐/壓力/策略等級。
# 2) 未匯入作戰表、或輸入股票未列入作戰表：由核心分析結果 support/resistance/ma20/ma60 自動產生控制欄。
# 3) AUTO 欄位只作 UI 管控顯示，不覆蓋 core.analyze_symbol() 的 Phase4/Phase5、排序、支撐/壓力與最終決策。


def _web_safe_float(value: Any, default: float | None = None) -> float | None:
    """Web UI 層安全轉 float。避免主程式依賴 gtc_core_engine 內部未匯出的 safe_float。"""
    try:
        if value in (None, "", "-", "--"):
            return default
        return float(value)
    except Exception:
        return default


def _web_safe_battle_float(value: Any) -> float | None:
    """讀取作戰控制價位；空白、非數字、<=0 一律視為無效。"""
    v = _web_safe_float(value)
    if v is None or v <= 0:
        return None
    try:
        return core.round_price(v)
    except Exception:
        return round(float(v), 2)


def _web_format_battle_price(value: Any) -> str:
    """作戰控制價位顯示格式；優先沿用 core.format_battle_price。"""
    try:
        return core.format_battle_price(value)
    except Exception:
        v = _web_safe_battle_float(value)
        if v is None:
            return "-"
        if abs(v - int(v)) < 0.005:
            return str(int(round(v)))
        return f"{v:.2f}".rstrip("0").rstrip(".")


def calc_auto_support_resistance(result: dict[str, Any]) -> dict[str, Any]:
    """
    AUTO 控制欄：未列入作戰表股票才使用。
    由 core.analyze_symbol() 已產出的 support/resistance/ma20/ma60/display_price
    轉為標準作戰控制價位；不改原支撐壓力、不改 Phase4/Phase5。
    """
    close = _web_safe_battle_float(result.get("display_price", result.get("close")))
    s1 = _web_safe_battle_float(result.get("support"))
    r1 = _web_safe_battle_float(result.get("resistance"))
    ma20 = _web_safe_battle_float(result.get("ma20"))
    ma60 = _web_safe_battle_float(result.get("ma60"))

    if close is None or s1 is None or r1 is None:
        return {}
    if s1 <= 0 or r1 <= 0:
        return {}

    # 支撐1為主防線、支撐2為退出/防守線、支撐3為最後風控線。
    s2_candidates = [s1 * 0.98]
    if ma20 is not None and ma20 > 0:
        s2_candidates.append(ma20)
    s2 = min(s2_candidates)

    s3_candidates = [s1 * 0.95]
    if ma60 is not None and ma60 > 0:
        s3_candidates.append(ma60)
    s3 = min(s3_candidates)

    # 保證支撐序列由近到遠，避免 MA 高於支撐造成順序反轉。
    if s2 >= s1:
        s2 = s1 * 0.98
    if s3 >= s2:
        s3 = s2 * 0.97

    # 壓力1為核心壓力；壓力2為上方延伸壓力，只作風控/不追高提示。
    r2 = max(r1 * 1.03, close * 1.03)
    if r2 <= r1:
        r2 = r1 * 1.03

    return {
        "auto_support1": core.round_price(s1),
        "auto_support2": core.round_price(s2),
        "auto_support3": core.round_price(s3),
        "auto_resistance1": core.round_price(r1),
        "auto_resistance2": core.round_price(r2),
    }


def classify_auto_strategy_level(result: dict[str, Any]) -> str:
    """
    AUTO 作戰等級：
    沿用報表策略語義：強者續抱、觀察等突破、風控減碼；不得因 AUTO 直接產生買進升級。
    """
    score = _web_safe_float(result.get("score"), 0) or 0
    state = str(result.get("state_bucket") or "")
    decision = str(result.get("final_decision") or "")
    leader = str(result.get("leader_candidate") or "")
    signal = str(result.get("signal") or "")
    close = _web_safe_battle_float(result.get("display_price", result.get("close")))
    support = _web_safe_battle_float(result.get("support"))

    if decision == "AVOID" or result.get("escape_rally") or score < 45:
        return "D+"
    if close is not None and support is not None and close < support:
        return "D+"
    if score >= 80 and state in ("strong", "bullish") and "修正弱反彈" not in leader:
        return "A-"
    if score >= 65 or any(x in leader for x in ("主升", "整理觀察")):
        return "B-"
    if score >= 55 or state in ("range", "bullish") or "整理" in signal:
        return "C"
    return "C-"


def build_auto_battle_fields(result: dict[str, Any]) -> dict[str, Any]:
    """
    AUTO 控制欄主函式。
    僅在股票未命中匯入作戰表時呼叫，提供 UI 管控欄位。
    """
    fields = core.empty_battle_fields()
    levels = calc_auto_support_resistance(result)
    if not levels:
        fields.update({
            "control_source": "NONE",
            "auto_control_generated": False,
            "battle_plan_hit": False,
            "battle_strategy_level": "-",
            "control_label": "-",
            "control_action": "無法產生作戰控制價位",
            "control_distance_pct": None,
            "control_state": "NO_BATTLE_PLAN",
        })
        return fields

    auto_level = classify_auto_strategy_level(result)
    ctrl = core.build_control_status(
        result.get("display_price", result.get("close")),
        levels.get("auto_support1"),
        levels.get("auto_support2"),
        levels.get("auto_support3"),
        levels.get("auto_resistance1"),
        levels.get("auto_resistance2"),
    )

    code = core.normalize_stock_code_value(result.get("input_symbol"))
    fields.update(levels)
    fields.update({
        "battle_code": code or "-",
        "battle_support1": levels.get("auto_support1"),
        "battle_support2": levels.get("auto_support2"),
        "battle_support3": levels.get("auto_support3"),
        "battle_resistance1": levels.get("auto_resistance1"),
        "battle_resistance2": levels.get("auto_resistance2"),
        "battle_strategy_level": auto_level,
        "auto_strategy_level": auto_level,
        "battle_operation_note": "未列入作戰表，依程式標準自動產生控制價位；僅供管控，不作買進升級。",
        "battle_plan_hit": False,
        "control_source": "AUTO",
        "auto_control_generated": True,
    })
    fields.update(ctrl)
    return fields



EVENT_LABELS = {
    "NONE": "無事件",
    "PENDING_BREAKOUT": "突破待確認",
    "TRUE_BREAKOUT": "真突破",
    "FB_PENDING": "跌回待確認",
    "FALSE_BREAKOUT": "假突破",
    "PULLBACK_TOUCH": "回測支撐",
    "PULLBACK_HOLD": "回測守穩",
    "BD_PENDING": "跌破待確認",
    "RECOVERED_BREAKDOWN": "跌破後收復",
    "BREAKDOWN": "確認跌破",
    "DATA_UNVERIFIED": "資料待確認",
}

EVENT_ACTIONS = {
    "NONE": "依原作戰控制欄執行",
    "PENDING_BREAKOUT": "先觀察，不追價；等待15分鐘站穩",
    "TRUE_BREAKOUT": "列入BUY_READY候選，仍須通過原Phase4/Phase5 Gate",
    "FB_PENDING": "取消追價；等待30分鐘重新站回壓力",
    "FALSE_BREAKOUT": "維持WATCH/REDUCE；重新站回壓力後重新計時",
    "PULLBACK_TOUCH": "觀察支撐是否守穩，不立即加碼",
    "PULLBACK_HOLD": "列入回測承接候選，仍須通過原Gate",
    "BD_PENDING": "暫停新增；等待30分鐘收復支撐2",
    "RECOVERED_BREAKDOWN": "維持WATCH，確認收復後再評估",
    "BREAKDOWN": "AVOID/REDUCE候選；禁止攤平",
    "DATA_UNVERIFIED": "報價品質不足，不確認真突破或跌破",
}


def _event_dt_text(ts: float | None) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(float(ts)).strftime("%H:%M:%S")


def _event_elapsed_text(seconds: float | int | None) -> str:
    sec = max(0, int(seconds or 0))
    return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


def _event_quote_quality(result: dict[str, Any]) -> str:
    explicit = str(result.get("quote_quality") or result.get("data_quality") or "").upper()
    if explicit in {"MID_QUOTE", "STALE", "DELAYED", "INVALID", "UNAVAILABLE"}:
        return "UNVERIFIED"
    price = _web_safe_float(result.get("display_price", result.get("close")))
    return "REALTIME_OR_LATEST" if price and price > 0 else "UNVERIFIED"


class IntradayEventStateEngine:
    """Per-session intraday event state machine.

    The store lives in st.session_state, so Streamlit reruns preserve elapsed time while
    different logged-in browser sessions remain isolated. Events never directly override
    strategy grade or final decision; they provide an independent execution-risk label.
    """

    def __init__(self, store: dict[str, Any]):
        self.store = store

    @staticmethod
    def _new_state(symbol: str, trading_date: str) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "trading_date": trading_date,
            "state": "NONE",
            "started_at": None,
            "last_update_at": None,
            "breakout_at": None,
            "fallback_at": None,
            "reclaim_at": None,
            "support_touch_at": None,
            "breakdown_at": None,
            "reference_price": None,
            "reason": "尚未形成盤中事件",
            "rule_id": "EVT-NONE",
            "history": [],
        }

    def reset_symbol(self, symbol: str) -> None:
        self.store.pop(str(symbol), None)

    def update(self, symbol: str, result: dict[str, Any], levels: dict[str, Any], now_ts: float | None = None) -> dict[str, Any]:
        now_ts = float(now_ts or time.time())
        trading_date = datetime.fromtimestamp(now_ts).strftime("%Y-%m-%d")
        key = str(symbol or "-")
        state = self.store.get(key)
        if not isinstance(state, dict) or state.get("trading_date") != trading_date:
            state = self._new_state(key, trading_date)

        price = _web_safe_float(result.get("display_price", result.get("close")))
        r1 = _web_safe_float(levels.get("battle_resistance1"))
        s1 = _web_safe_float(levels.get("battle_support1"))
        s2 = _web_safe_float(levels.get("battle_support2"))
        quality = _event_quote_quality(result)
        previous = str(state.get("state") or "NONE")
        new_state = previous
        reason = str(state.get("reason") or "")
        rule_id = str(state.get("rule_id") or "EVT-NONE")

        if price is None:
            new_state, reason, rule_id = "DATA_UNVERIFIED", "缺少可用現價", "EVT-DATA-01"
        else:
            tol = EVENT_PRICE_TOLERANCE_PCT
            above_r1 = bool(r1 and price >= r1 * (1 + tol))
            below_r1 = bool(r1 and price <= r1 * (1 - tol))
            below_s2 = bool(s2 and price <= s2 * (1 - tol))
            above_s2 = bool(s2 and price >= s2 * (1 + tol))
            near_s1 = bool(s1 and abs(price - s1) / s1 <= 0.006)

            # Confirmed breakdown has highest risk priority.
            if previous == "BD_PENDING":
                elapsed = now_ts - float(state.get("started_at") or now_ts)
                if above_s2:
                    new_state, reason, rule_id = "RECOVERED_BREAKDOWN", "跌破支撐2後已重新站回", "EVT-BD-RECOVER"
                    state["reclaim_at"] = now_ts
                elif elapsed >= EVENT_BREAKDOWN_CONFIRM_SECONDS:
                    new_state, reason, rule_id = "BREAKDOWN", "跌破支撐2後30分鐘仍未站回", "EVT-BD-30M"
            elif below_s2:
                new_state, reason, rule_id = "BD_PENDING", "現價跌破支撐2，開始30分鐘確認", "EVT-BD-START"
                state["breakdown_at"] = now_ts

            elif previous in {"PENDING_BREAKOUT", "TRUE_BREAKOUT"}:
                if below_r1:
                    new_state, reason, rule_id = "FB_PENDING", "突破壓力後跌回壓力下方，開始30分鐘收復計時", "EVT-FB-START"
                    state["fallback_at"] = now_ts
                elif previous == "PENDING_BREAKOUT":
                    elapsed = now_ts - float(state.get("started_at") or now_ts)
                    if elapsed >= EVENT_TRUE_CONFIRM_SECONDS and quality != "UNVERIFIED":
                        new_state, reason, rule_id = "TRUE_BREAKOUT", "突破壓力後已連續站穩15分鐘", "EVT-TB-15M"
            elif previous in {"FB_PENDING", "FALSE_BREAKOUT"}:
                if above_r1:
                    new_state, reason, rule_id = "PENDING_BREAKOUT", "已重新站回壓力，真突破確認重新計時", "EVT-RECLAIM-R1"
                    state["reclaim_at"] = now_ts
                    state["breakout_at"] = now_ts
                elif previous == "FB_PENDING":
                    elapsed = now_ts - float(state.get("started_at") or now_ts)
                    if elapsed >= EVENT_FALSE_CONFIRM_SECONDS:
                        new_state, reason, rule_id = "FALSE_BREAKOUT", "跌回壓力後30分鐘仍未站回", "EVT-FB-30M"
            elif above_r1:
                new_state, reason, rule_id = "PENDING_BREAKOUT", "現價突破壓力1，開始15分鐘站穩確認", "EVT-TB-START"
                state["breakout_at"] = now_ts
            elif previous == "PULLBACK_TOUCH":
                elapsed = now_ts - float(state.get("started_at") or now_ts)
                if s1 and price >= s1 * 1.01 and elapsed >= EVENT_RECLAIM_CONFIRM_SECONDS:
                    new_state, reason, rule_id = "PULLBACK_HOLD", "觸及支撐1後未破支撐2，且15分鐘後重新走強", "EVT-PB-HOLD"
            elif near_s1:
                new_state, reason, rule_id = "PULLBACK_TOUCH", "現價進入支撐1容忍帶，開始觀察守穩", "EVT-PB-TOUCH"
                state["support_touch_at"] = now_ts
            elif previous in {"RECOVERED_BREAKDOWN", "PULLBACK_HOLD", "DATA_UNVERIFIED"}:
                new_state, reason, rule_id = "NONE", "事件條件解除，回到一般監控", "EVT-CLEAR"

        if new_state != previous:
            state["state"] = new_state
            state["started_at"] = now_ts
            state["reason"] = reason
            state["rule_id"] = rule_id
            transition = {
                "time": _event_dt_text(now_ts), "previous": previous, "new": new_state,
                "price": price, "reason": reason, "rule_id": rule_id,
            }
            history = list(state.get("history") or [])
            history.append(transition)
            state["history"] = history[-EVENT_HISTORY_LIMIT:]
            logging.getLogger("gtc.intraday_event").info(
                "EVENT_TRANSITION symbol=%s prev=%s new=%s price=%s r1=%s s1=%s s2=%s rule=%s reason=%s",
                key, previous, new_state, price, r1, s1, s2, rule_id, reason,
            )
            transitions = list(st.session_state.get("intraday_event_transitions", []))
            transitions.append({"symbol": key, **transition})
            st.session_state.intraday_event_transitions = transitions[-300:]
        else:
            state["reason"] = reason
            state["rule_id"] = rule_id

        state["last_update_at"] = now_ts
        self.store[key] = state
        elapsed = now_ts - float(state.get("started_at") or now_ts)
        hist = state.get("history") or []
        history_text = " → ".join(f"{x.get('time')} {EVENT_LABELS.get(x.get('new'), x.get('new'))}" for x in hist[-6:]) or "尚無狀態轉移"
        confidence = 90 if state["state"] in {"TRUE_BREAKOUT", "FALSE_BREAKOUT", "BREAKDOWN"} else 70 if state["state"] not in {"NONE", "DATA_UNVERIFIED"} else 40
        return {
            "event_engine_version": EVENT_ENGINE_VERSION,
            "event_state": state["state"],
            "event_label": EVENT_LABELS.get(state["state"], state["state"]),
            "event_action": EVENT_ACTIONS.get(state["state"], "依原控制欄執行"),
            "event_started_at": _event_dt_text(state.get("started_at")),
            "event_elapsed_seconds": int(max(0, elapsed)),
            "event_elapsed": _event_elapsed_text(elapsed),
            "event_reason": state.get("reason", "-"),
            "event_rule_id": state.get("rule_id", "-"),
            "event_reference_price": r1 if state["state"] in {"PENDING_BREAKOUT", "TRUE_BREAKOUT", "FB_PENDING", "FALSE_BREAKOUT"} else (s2 if state["state"] in {"BD_PENDING", "RECOVERED_BREAKDOWN", "BREAKDOWN"} else s1),
            "event_breakout_at": _event_dt_text(state.get("breakout_at")),
            "event_fallback_at": _event_dt_text(state.get("fallback_at")),
            "event_reclaim_at": _event_dt_text(state.get("reclaim_at")),
            "event_support_touch_at": _event_dt_text(state.get("support_touch_at")),
            "event_breakdown_at": _event_dt_text(state.get("breakdown_at")),
            "event_quote_quality": quality,
            "event_confidence": confidence,
            "event_history": history_text,
        }


def _event_default_fields() -> dict[str, Any]:
    return {
        "event_engine_version": EVENT_ENGINE_VERSION, "event_state": "NONE", "event_label": "無事件",
        "event_action": "依原作戰控制欄執行", "event_started_at": "-", "event_elapsed_seconds": 0,
        "event_elapsed": "00:00:00", "event_reason": "尚未形成盤中事件", "event_rule_id": "EVT-NONE",
        "event_reference_price": None, "event_breakout_at": "-", "event_fallback_at": "-",
        "event_reclaim_at": "-", "event_support_touch_at": "-", "event_breakdown_at": "-",
        "event_quote_quality": "UNVERIFIED", "event_confidence": 0, "event_history": "尚無狀態轉移",
    }

def apply_battle_plan_to_result(result: dict[str, Any]) -> dict[str, Any]:
    merged = dict(result)
    fields = core.empty_battle_fields()
    code = core.normalize_stock_code_value(merged.get("input_symbol"))
    item = st.session_state.battle_plan_map.get(code) if code else None

    if item:
        # EXCEL 優先：命中匯入作戰表時，完全沿用原作戰表控制欄。
        fields.update(item)
        fields["battle_plan_hit"] = True
        fields["control_source"] = "EXCEL"
        fields["auto_control_generated"] = False
        ctrl = core.build_control_status(
            merged.get("display_price", merged.get("close")),
            fields.get("battle_support1"),
            fields.get("battle_support2"),
            fields.get("battle_support3"),
            fields.get("battle_resistance1"),
            fields.get("battle_resistance2"),
        )
        fields.update(ctrl)
    else:
        # AUTO 補位：未列入作戰表股票，也必須產生標準作戰等級、控制欄、即時指示。
        fields.update(build_auto_battle_fields(merged))

    merged.update(fields)
    merged.update(_event_default_fields())
    try:
        store = st.session_state.get("intraday_event_store", {})
        if not isinstance(store, dict):
            store = {}
        engine = IntradayEventStateEngine(store)
        merged.update(engine.update(code or str(merged.get("input_symbol") or "-"), merged, fields))
        st.session_state.intraday_event_store = store
    except Exception as exc:
        merged["event_state"] = "DATA_UNVERIFIED"
        merged["event_label"] = "事件引擎異常"
        merged["event_action"] = "維持原決策，請查閱事件錯誤"
        merged["event_reason"] = str(exc)
        logging.exception("Intraday event update failed: %s", code)
    return merged


def analyze_symbols(symbols: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    ok_results: list[dict[str, Any]] = []
    errors: list[str] = []
    progress = st.progress(0, text="開始抓取即時股票資料...")
    for idx, sym in enumerate(symbols, start=1):
        try:
            progress.progress((idx - 1) / max(len(symbols), 1), text=f"分析中：{sym}")
            result = core.analyze_symbol(sym)
            result = apply_battle_plan_to_result(result)
            ok_results.append(result)
        except Exception as exc:
            errors.append(f"{sym}: {exc}")
    progress.progress(1.0, text="分析完成")
    time.sleep(0.2)
    progress.empty()
    return sorted(ok_results, key=lambda x: x.get("rank_score", 0), reverse=True), errors


def result_to_row(idx: int, r: dict[str, Any]) -> dict[str, Any]:
    return {
        "排名": idx,
        "燈號": r.get("light", ""),
        "市場": r.get("market", "-"),
        "代號": r.get("input_symbol", "-"),
        "名稱": r.get("name", "-"),
        "顯示價": r.get("display_price", "-"),
        "漲跌": f"{r.get('change', 0):+.2f}" if isinstance(r.get("change"), (int, float)) else r.get("change", "-"),
        "漲跌幅%": f"{r.get('change_pct', 0):+.2f}%" if isinstance(r.get("change_pct"), (int, float)) else r.get("change_pct", "-"),
        "作戰等級": r.get("battle_strategy_level", "-"),
        "控制欄": r.get("control_label", "-"),
        "即時指示": r.get("control_action", "-"),
        "盤中事件": r.get("event_label", "無事件"),
        "事件經過": r.get("event_elapsed", "00:00:00"),
        "事件建議": r.get("event_action", "-"),
        "事件置信度": r.get("event_confidence", 0),
        "訊號": r.get("signal", "-"),
        "建議": r.get("display_advice", r.get("advice", "-")),
        "分數": r.get("score", "-"),
        "等級": r.get("strategy_level", "-"),
        "目標價": r.get("display_target_price", r.get("target_price", "-")),
        "RR": r.get("display_rr", r.get("rr", "-")),
        "主升候選": r.get("leader_candidate", "-"),
        "波浪定位": r.get("wave_stage", "-"),
        "費波位置": r.get("fibo_position", "-"),
        "禁追提示": "是" if (r.get("fibo_risk_flag") or r.get("wave_risk_flag")) else "否",
        "波費判定": r.get("wave_fibo_signal", "-"),
        "大波": r.get("major_wave", "-"),
        "小波": r.get("minor_wave", "-"),
        "修正型態": r.get("correction_type", "-"),
        "回撤比例": r.get("fibo_retrace", "-"),
        "反彈性質": r.get("rebound_type", "-"),
        "修正完成": "是" if r.get("correction_completed") else "否",
        "逃命反彈": "是" if r.get("escape_rally") else "否",
        "推動浪": "是" if r.get("impulsive_wave") else "否",
        "技術狀態": r.get("technical_state", "-"),
        "執行狀態": r.get("execution_state", "-"),
        "顯示狀態": r.get("display_state", "-"),
        "波段分": r.get("trend_score", "-"),
        "盤中分": r.get("intraday_score", "-"),
        "支撐": r.get("support", "-"),
        "壓力": r.get("resistance", "-"),
        "RSI": r.get("rsi", "-"),
        "五檔力道": r.get("orderbook_bias", "-"),
        "交易類型": r.get("display_trade_type", r.get("trade_type", "-")),
        "進場狀態": r.get("entry_zone_status", "-"),
        "進場可執行": "是" if r.get("entry_zone_ready") else "否",
        "建議倉位%": r.get("position_size_pct", 0),
        "資金等級": r.get("allocation_grade", "-"),
        "配置分": r.get("allocation_score", 0),
        "阻擋原因": r.get("final_block_reason", r.get("phase4_block_reason", "-")),
        "下單類型": r.get("order_type_hint", "-"),
        "最終決策": r.get("final_decision", "-"),
        "可下單": "是" if r.get("execution_ready") else "否",
        "決策原因": r.get("decision_reason", "-"),
        "報價說明": r.get("display_note", "-"),
    }


def results_dataframe(results: list[dict[str, Any]], advanced: bool = False) -> pd.DataFrame:
    rows = [result_to_row(idx, r) for idx, r in enumerate(results, start=1)]
    df = pd.DataFrame(rows)
    if df.empty:
        return df

    # v5.4.1 Log Fix: Streamlit/PyArrow requires stable column types.
    # RR is a display column and may contain numeric values from core and "-" for unavailable values.
    # Keep calculation fields untouched in result dict; normalize only the dataframe displayed/downloaded by UI.
    if "RR" in df.columns:
        df["RR"] = df["RR"].apply(lambda x: "-" if pd.isna(x) or x in (None, "") else str(x))

    columns = ADVANCED_COLUMNS if advanced else CORE_COLUMNS
    return df[[c for c in columns if c in df.columns]]


def build_detail_lines(target: dict[str, Any]) -> list[str]:
    fmt = core.format_battle_price
    return [
        f"【{target.get('input_symbol')} {target.get('name')}】個股明細分析",
        f"市場：{target.get('market')}",
        f"資料來源：{target.get('source')}",
        f"報價時間：{target.get('quote_time')}",
        f"顯示價：{target.get('display_price')}",
        f"報價說明：{target.get('display_note')}",
        f"漲跌 / 漲跌幅：{target.get('change'):+.2f} / {target.get('change_pct'):+.2f}%" if isinstance(target.get("change"), (int, float)) else "漲跌 / 漲跌幅：-",
        "",
        str(target.get("summary_block", "")),
        "",
        "【作戰控制價位】",
        f"作戰表命中：{'是' if target.get('battle_plan_hit') else '否'} / 控制來源：{target.get('control_source','-')} / 作戰等級：{target.get('battle_strategy_level','-')}",
        f"支撐1：{fmt(target.get('battle_support1'))}",
        f"支撐2：{fmt(target.get('battle_support2'))}",
        f"支撐3：{fmt(target.get('battle_support3'))}",
        f"壓力1：{fmt(target.get('battle_resistance1'))}",
        f"壓力2：{fmt(target.get('battle_resistance2'))}",
        "",
        "【目前控制狀態】",
        f"現價：{target.get('display_price','-')} / 最近控制點：{target.get('control_label','-')} / 距離%：{target.get('control_distance_pct','-')}",
        f"管控指示：{target.get('control_action','-')}",
        f"作戰操作摘要：{target.get('battle_operation_note','-') or '-'}",
        "",
        "【盤中事件－跌回與重新站回時間歷程】",
        f"事件狀態：{target.get('event_label','無事件')} ({target.get('event_state','NONE')}) / 經過：{target.get('event_elapsed','00:00:00')} / 置信度：{target.get('event_confidence',0)}",
        f"事件建議：{target.get('event_action','-')}",
        f"判斷原因：{target.get('event_reason','-')} / 規則：{target.get('event_rule_id','-')} / 基準價：{target.get('event_reference_price','-')}",
        f"突破：{target.get('event_breakout_at','-')} / 跌回：{target.get('event_fallback_at','-')} / 重新站回：{target.get('event_reclaim_at','-')} / 跌破：{target.get('event_breakdown_at','-')}",
        f"時間歷程：{target.get('event_history','尚無狀態轉移')}",
        "",
        "【Phase4 / Phase5】",
        f"進場狀態：{target.get('entry_zone_status','-')} / 可執行：{'是' if target.get('entry_zone_ready') else '否'}",
        f"資金等級：{target.get('allocation_grade','-')} / 配置分：{target.get('allocation_score','-')} / 建議倉位%：{target.get('position_size_pct','-')}",
        f"最終決策：{target.get('final_decision','-')} / 可下單：{target.get('execution_ready','-')}",
        f"決策原因：{target.get('decision_reason','-')}",
        f"波浪定位：{target.get('wave_stage','-')} / 費波位置：{target.get('fibo_position','-')}",
        f"Phase5定位：{target.get('phase5_wave_label','-')}",
        "",
        "【AI分析】",
        str(target.get("ai_analysis", "-")),
        "",
        "【風險提醒】",
        str(target.get("risk_note", "-")),
    ]


def build_advice_lines(target: dict[str, Any]) -> list[str]:
    fmt = core.format_battle_price
    rr = target.get("rr")
    rr_text = f"1:{rr:.2f}" if isinstance(rr, (int, float)) else "-"
    return [
        f"【{target.get('input_symbol')} {target.get('name')}】交易決策報告",
        "【交易結論】",
        f"建議：{target.get('display_advice', target.get('advice','-'))}",
        f"訊號：{target.get('signal','-')} / 執行狀態：{target.get('final_decision','-')} / 主升：{target.get('leader_candidate','-')}",
        "",
        "【交易計畫】",
        f"建議進場：{target.get('entry_low',0)} ~ {target.get('entry_high',0)}",
        f"停損點：{target.get('stop_loss', 0)}",
        f"策略等級：{target.get('strategy_level','-')} / 作戰等級：{target.get('battle_strategy_level','-')}",
        f"第一目標：{target.get('display_target_price', '-')}",
        f"風險報酬比：{rr_text}",
        "",
        "【作戰控制指示】",
        f"控制來源：{target.get('control_source','-')} / AUTO產生：{'是' if target.get('auto_control_generated') else '否'}",
        f"控制欄：{target.get('control_label','-')}",
        f"即時指示：{target.get('control_action','-')}",
        f"完整價位：支撐1={fmt(target.get('battle_support1'))} / 支撐2={fmt(target.get('battle_support2'))} / 支撐3={fmt(target.get('battle_support3'))} / 壓力1={fmt(target.get('battle_resistance1'))} / 壓力2={fmt(target.get('battle_resistance2'))}",
        f"目前距離%：{target.get('control_distance_pct','-')} / 控制狀態={target.get('control_state','-')}",
        "",
        "【盤中事件執行影響】",
        f"事件：{target.get('event_label','無事件')} / 經過：{target.get('event_elapsed','00:00:00')}",
        f"事件建議：{target.get('event_action','-')}",
        f"注意：盤中事件不覆蓋作戰等級與最終決策，只作執行風險標籤。",
        "",
        "【交易劇本】",
        str(target.get("script_a", "-")),
        str(target.get("script_b", "-")),
        str(target.get("script_c", "-")),
    ]


def make_excel_download(df: pd.DataFrame, results: list[dict[str, Any]]) -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name="即時看盤主表")
        detail_rows = []
        for r in results:
            detail_rows.append({
                "代號": r.get("input_symbol"),
                "名稱": r.get("name"),
                "個股明細": "\n".join(build_detail_lines(r)),
                "操作建議": "\n".join(build_advice_lines(r)),
            })
        detail_df = pd.DataFrame(detail_rows)
        detail_df.to_excel(writer, index=False, sheet_name="個股明細與建議")
        wb = writer.book
        header_fmt = wb.add_format({"bold": True, "bg_color": "#D9EAF7", "border": 1, "align": "center"})
        wrap_fmt = wb.add_format({"text_wrap": True, "valign": "top"})
        for sheet_name, sheet_df in [("即時看盤主表", df), ("個股明細與建議", detail_df)]:
            ws = writer.sheets[sheet_name]
            ws.freeze_panes(1, 0)
            for col_idx, col_name in enumerate(sheet_df.columns):
                ws.write(0, col_idx, col_name, header_fmt)
                max_len = max([len(str(col_name))] + [len(str(v)) for v in sheet_df[col_name].head(30).tolist()])
                width = min(max(max_len + 2, 10), 42)
                ws.set_column(col_idx, col_idx, width, wrap_fmt if sheet_name == "個股明細與建議" else None)
            if sheet_name == "個股明細與建議":
                ws.set_column(2, 3, 70, wrap_fmt)
    return output.getvalue()


def _draw_pdf_lines(c: canvas.Canvas, lines: list[str], font_name: str, page_size) -> None:
    width, height = page_size
    x = 32
    y = height - 36
    line_height = 14
    c.setFont(font_name, 9)
    for raw in lines:
        text = str(raw)
        chunks = [text[i:i+70] for i in range(0, len(text), 70)] or [""]
        for chunk in chunks:
            if y < 36:
                c.showPage()
                c.setFont(font_name, 9)
                y = height - 36
            c.drawString(x, y, chunk)
            y -= line_height


def make_pdf_download(results: list[dict[str, Any]], mode: str = "full") -> bytes:
    output = BytesIO()
    page_size = landscape(A4)
    c = canvas.Canvas(output, pagesize=page_size)
    font_name = core.setup_pdf_font()
    title = "GTC v5.4.3 Dual MultiUser SessionSafeAutoRefresh 即時看盤完整報告" if mode == "full" else "GTC v5.4.3 Dual MultiUser SessionSafeAutoRefresh 即時看盤總表摘要"
    lines = [title, f"產出時間：{datetime.now():%Y-%m-%d %H:%M:%S}", "=" * 90]
    if mode == "summary":
        for idx, r in enumerate(results, start=1):
            lines.append(
                f"{idx}. {r.get('input_symbol')} {r.get('name')}｜價={r.get('display_price')}｜漲跌={r.get('change_pct', 0):+.2f}%｜"
                f"作戰={r.get('battle_strategy_level','-')}｜控制={r.get('control_label','-')}｜指示={r.get('control_action','-')}｜"
                f"訊號={r.get('signal','-')}｜建議={r.get('display_advice', r.get('advice','-'))}"
            )
    else:
        for r in results:
            lines.extend(build_detail_lines(r))
            lines.append("-" * 90)
            lines.extend(build_advice_lines(r))
            lines.append("=" * 90)
    _draw_pdf_lines(c, lines, font_name, page_size)
    c.save()
    return output.getvalue()



# =========================
# v5.4.3 Phase 1 Multi-User Login Manager + Session-Safe Scheduler
# =========================
# 安全原則：
# 1) GitHub Repo 目前可能為 Public，因此帳號/密碼絕不可寫死在 main.py。
# 2) Web Cloud 模式預設啟用登入閘門；未設定 Streamlit Secrets 時直接封鎖主畫面。
# 3) 第一階段新增 SQLite users / login_audit 兩張表；DB 只保存雜湊密碼，不保存明碼。
# 4) 登入成功前不執行 init_state()、作戰表上傳、即時分析與 AUTO 控制欄。
# 5) Windows EXE / 本機 BAT 模式預設不啟用登入，避免影響原本 localhost 操作。
#
# 建議 Streamlit Secrets 格式：
# [auth]
# enabled = true
# title = "GTC 股票系統登入"
# sync_secret_users = true
# disable_users_not_in_secrets = true
# disabled_users = []
#
# [auth.users]
# liu8643 = "請換成你的密碼"
# zhangsan = "張三密碼"
# lisi = "李四密碼"
#
# 停權方式：
# 1) 將帳號自 [auth.users] 移除，且 disable_users_not_in_secrets=true；或
# 2) 在 disabled_users = ["lisi"] 加入帳號。
#
# 進階：可用 sha256:<hex> 或 pbkdf2_sha256$iterations$salt$hash 作為密碼值。
# DB 永遠只保存雜湊，不保存 Secrets 中的明碼。

AUTH_DB_PATH = RUNTIME_DIR / "gtc_auth_users.db"
PBKDF2_ITERATIONS = 260_000


def _now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _auth_db_path() -> Path:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    return AUTH_DB_PATH


def _get_auth_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_auth_db_path()), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _init_auth_db() -> None:
    """Create users and login_audit tables if missing.

    Phase 1 uses SQLite so the Web app can immediately support separate users, disable flags,
    and login audit.  On Streamlit Cloud this database is runtime storage, so production-grade
    persistence should later move the same schema to Supabase/PostgreSQL.
    """
    with _get_auth_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                display_name TEXT,
                role TEXT NOT NULL DEFAULT 'user',
                is_active INTEGER NOT NULL DEFAULT 1,
                failed_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_login_at TEXT,
                password_changed_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS login_audit (
                audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT,
                success INTEGER NOT NULL,
                reason TEXT,
                app_version TEXT,
                runtime_mode TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_login_audit_created ON login_audit(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_login_audit_username ON login_audit(username)")
        conn.commit()


def _secret_to_plain_dict(value: Any) -> dict[str, Any]:
    """Convert Streamlit secrets / AttrDict / dict-like object into a normal dict."""
    if value is None:
        return {}
    try:
        if isinstance(value, dict):
            return dict(value)
        if hasattr(value, "to_dict"):
            data = value.to_dict()
            return dict(data) if isinstance(data, dict) else {}
        return dict(value)
    except Exception:
        return {}


def _get_secret_section(name: str) -> dict[str, Any]:
    """Safely get a section from st.secrets without raising when secrets are absent."""
    try:
        return _secret_to_plain_dict(st.secrets.get(name, {}))
    except Exception:
        return {}


def _bool_from_secret(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "啟用", "是"}


def _list_from_secret(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(x).strip() for x in value if str(x).strip()]
    text = str(value).strip()
    if not text:
        return []
    return [x.strip() for x in text.replace("，", ",").split(",") if x.strip()]


def _cloud_login_required() -> bool:
    """Return whether login is required before showing the GTC app."""
    auth_cfg = _get_secret_section("auth")
    # Local EXE/BAT keeps original behavior unless explicitly enabled in secrets/env.
    if _is_local_persistent_mode():
        env_force = os.environ.get("GTC_AUTH_ENABLED", "").strip()
        if env_force:
            return _bool_from_secret(env_force, False)
        return _bool_from_secret(auth_cfg.get("enabled"), False)
    # Cloud mode is protected by default. It can be explicitly disabled only by secrets/env.
    env_disable = os.environ.get("GTC_AUTH_DISABLED", "").strip()
    if _bool_from_secret(env_disable, False):
        return False
    if "enabled" in auth_cfg:
        return _bool_from_secret(auth_cfg.get("enabled"), True)
    return True


def _hash_password(raw_password: str) -> str:
    """Hash a plain password using PBKDF2-SHA256.  Return a portable encoded hash."""
    raw = str(raw_password or "")
    salt = os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac("sha256", raw.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS).hex()
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest}"


def _normalize_password_for_storage(secret_password: str) -> str:
    """Convert Secrets password into a stored hash.

    - plain text in Secrets -> PBKDF2 hash in DB
    - sha256:<hex> -> stored as sha256 hash marker
    - pbkdf2_sha256$... -> stored as-is
    """
    pwd = str(secret_password or "").strip()
    if not pwd:
        return ""
    if pwd.lower().startswith("sha256:"):
        return "sha256:" + pwd.split(":", 1)[1].strip().lower()
    if pwd.startswith("pbkdf2_sha256$"):
        return pwd
    return _hash_password(pwd)


def _verify_password(input_password: str, stored_password_hash: str) -> bool:
    """Constant-time password check. Supports PBKDF2 and legacy sha256 hashes."""
    raw = str(input_password or "")
    stored = str(stored_password_hash or "")
    if stored.startswith("pbkdf2_sha256$"):
        try:
            _, iter_text, salt_hex, expected = stored.split("$", 3)
            iterations = int(iter_text)
            actual = hashlib.pbkdf2_hmac("sha256", raw.encode("utf-8"), bytes.fromhex(salt_hex), iterations).hex()
            return hmac.compare_digest(actual.lower(), expected.lower())
        except Exception:
            return False
    if stored.lower().startswith("sha256:"):
        expected = stored.split(":", 1)[1].strip().lower()
        actual = hashlib.sha256(raw.encode("utf-8")).hexdigest().lower()
        return hmac.compare_digest(actual, expected)
    # Safety fallback for old in-memory Secrets-only versions. DB rows should never use this path.
    return hmac.compare_digest(raw, stored)


def _record_login_audit(username: str, success: bool, reason: str) -> None:
    try:
        _init_auth_db()
        with _get_auth_conn() as conn:
            conn.execute(
                """
                INSERT INTO login_audit(username, success, reason, app_version, runtime_mode, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (str(username or "").strip(), 1 if success else 0, str(reason or ""), APP_VERSION, _runtime_mode_label(), _now_iso()),
            )
            conn.commit()
    except Exception:
        pass


def _get_secret_auth_users() -> dict[str, str]:
    """Read [auth.users] and compatible [auth] username/password fallback from Secrets."""
    auth_cfg = _get_secret_section("auth")
    users = _secret_to_plain_dict(auth_cfg.get("users"))
    normalized: dict[str, str] = {}
    for user, pwd in users.items():
        user_text = str(user).strip()
        pwd_text = str(pwd).strip()
        if user_text and pwd_text:
            normalized[user_text] = pwd_text

    fallback_user = str(auth_cfg.get("username", "")).strip()
    fallback_pwd = str(auth_cfg.get("password", "")).strip()
    if fallback_user and fallback_pwd:
        normalized[fallback_user] = fallback_pwd
    elif fallback_pwd and not normalized:
        normalized["admin"] = fallback_pwd
    return normalized


def _sync_secret_users_to_db() -> None:
    """Sync Secrets users into SQLite users table.

    This enables immediate multi-user operation while ensuring SQLite stores only hashed passwords.
    A user can be disabled by removing the account from [auth.users] when
    disable_users_not_in_secrets=true, or by adding it to disabled_users.
    """
    _init_auth_db()
    auth_cfg = _get_secret_section("auth")
    if not _bool_from_secret(auth_cfg.get("sync_secret_users"), True):
        return

    secret_users = _get_secret_auth_users()
    disabled_users = set(_list_from_secret(auth_cfg.get("disabled_users")))
    disable_missing = _bool_from_secret(auth_cfg.get("disable_users_not_in_secrets"), True)
    now = _now_iso()

    with _get_auth_conn() as conn:
        for username, pwd in secret_users.items():
            password_hash = _normalize_password_for_storage(pwd)
            if not password_hash:
                continue
            is_active = 0 if username in disabled_users else 1
            row = conn.execute("SELECT user_id FROM users WHERE username=?", (username,)).fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE users
                    SET password_hash=?, is_active=?, updated_at=?, password_changed_at=COALESCE(password_changed_at, ?)
                    WHERE username=?
                    """,
                    (password_hash, is_active, now, now, username),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO users(username, password_hash, display_name, role, is_active, failed_count, created_at, updated_at, password_changed_at)
                    VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)
                    """,
                    (username, password_hash, username, str(auth_cfg.get("default_role", "user") or "user"), is_active, now, now, now),
                )

        if disable_missing:
            active_names = set(secret_users.keys())
            rows = conn.execute("SELECT username FROM users").fetchall()
            for row in rows:
                username = str(row["username"])
                if username not in active_names or username in disabled_users:
                    conn.execute("UPDATE users SET is_active=0, updated_at=? WHERE username=?", (now, username))
        elif disabled_users:
            for username in disabled_users:
                conn.execute("UPDATE users SET is_active=0, updated_at=? WHERE username=?", (now, username))
        conn.commit()


def _get_user_for_login(username: str) -> dict[str, Any] | None:
    _init_auth_db()
    with _get_auth_conn() as conn:
        row = conn.execute(
            "SELECT user_id, username, password_hash, display_name, role, is_active, failed_count, last_login_at FROM users WHERE username=?",
            (str(username or "").strip(),),
        ).fetchone()
    return dict(row) if row else None


def _mark_login_success(username: str) -> None:
    now = _now_iso()
    with _get_auth_conn() as conn:
        conn.execute("UPDATE users SET failed_count=0, last_login_at=?, updated_at=? WHERE username=?", (now, now, username))
        conn.commit()
    _record_login_audit(username, True, "LOGIN_OK")


def _mark_login_failure(username: str, reason: str) -> None:
    try:
        with _get_auth_conn() as conn:
            conn.execute("UPDATE users SET failed_count=failed_count+1, updated_at=? WHERE username=?", (_now_iso(), username))
            conn.commit()
    except Exception:
        pass
    _record_login_audit(username, False, reason)


def _render_login_gate() -> bool:
    """Render login UI. Return True only after successful authentication."""
    if not _cloud_login_required():
        return True
    if bool(st.session_state.get("gtc_auth_ok", False)):
        return True

    try:
        _sync_secret_users_to_db()
    except Exception as exc:
        st.error(f"登入資料庫初始化失敗：{exc}")
        return False

    st.title("GTC 股票系統登入")
    st.caption("此 Web App 已啟用多人登入限制。未驗證前不載入看盤系統、不顯示作戰表功能。")

    auth_cfg = _get_secret_section("auth")
    users = _get_secret_auth_users()
    if not users:
        st.error("尚未設定 Streamlit Secrets 登入帳號密碼，因此系統已封鎖公開進入。")
        st.code(
            """
[auth]
enabled = true
title = "GTC 股票系統登入"
sync_secret_users = true
disable_users_not_in_secrets = true
disabled_users = []

[auth.users]
liu8643 = "請換成你的密碼"
zhangsan = "張三密碼"
lisi = "李四密碼"
            """.strip(),
            language="toml",
        )
        st.info("請到 Streamlit App → Manage app → Settings → Secrets 新增上述設定後重新部署/重啟。")
        return False

    title = str(auth_cfg.get("title", "GTC 股票系統登入"))
    st.subheader(title)
    with st.form("gtc_login_form", clear_on_submit=False):
        username = st.text_input("帳號", value="", autocomplete="username")
        password = st.text_input("密碼", value="", type="password", autocomplete="current-password")
        submitted = st.form_submit_button("登入", type="primary", width="stretch")

    if submitted:
        username_clean = str(username or "").strip()
        user_row = _get_user_for_login(username_clean)
        if not user_row:
            _record_login_audit(username_clean, False, "USER_NOT_FOUND")
            st.error("帳號或密碼錯誤。")
        elif not int(user_row.get("is_active", 0)):
            _record_login_audit(username_clean, False, "USER_DISABLED")
            st.error("帳號已停用，請聯絡系統管理員。")
        elif _verify_password(password, str(user_row.get("password_hash", ""))):
            _mark_login_success(username_clean)
            st.session_state.gtc_auth_ok = True
            st.session_state.gtc_auth_user = username_clean
            st.session_state.gtc_auth_role = str(user_row.get("role", "user") or "user")
            st.rerun()
        else:
            _mark_login_failure(username_clean, "BAD_PASSWORD")
            st.error("帳號或密碼錯誤。")
    return False


def _render_logout_control() -> None:
    """Render logout control in sidebar after the user has passed the login gate."""
    if not _cloud_login_required():
        return
    user = st.session_state.get("gtc_auth_user", "已登入")
    role = st.session_state.get("gtc_auth_role", "user")
    st.caption(f"登入使用者：{user}｜角色：{role}")
    if st.button("登出", width="stretch"):
        for key in ["gtc_auth_ok", "gtc_auth_user", "gtc_auth_role"]:
            if key in st.session_state:
                del st.session_state[key]
        st.rerun()


def _auto_refresh_due(now_ts: float, last_run_ts: float, interval_seconds: int, has_symbols: bool) -> bool:
    """Pure scheduling rule used by the fragment scheduler and unit tests."""
    if not has_symbols:
        return False
    interval = max(10, int(interval_seconds or 30))
    last_run = float(last_run_ts or 0.0)
    return (float(now_ts) - last_run) >= interval


def execute_analysis_from_current_symbols(trigger: str) -> None:
    """Run one analysis cycle without reloading the browser.

    The function updates only the current Streamlit session.  It is shared by the manual
    button and the fragment-based scheduler, so login state, uploaded battle plan and
    intraday event history remain in the same session.
    """
    symbols = parse_symbols(st.session_state.get("symbol_text", ""))
    if not symbols:
        st.warning("請輸入至少一個股票代號。")
        return
    results, errors = analyze_symbols(symbols)
    st.session_state.results = results
    st.session_state.errors = errors
    st.session_state.last_update_time = datetime.now().strftime("%H:%M:%S")
    st.session_state.last_auto_run_ts = time.time()
    st.session_state.auto_refresh_last_trigger = trigger
    if trigger == "auto":
        st.session_state.auto_refresh_run_count = int(st.session_state.get("auto_refresh_run_count", 0)) + 1
    try:
        st.session_state.market_overview = core.build_market_overview(results)
        st.session_state.auto_refresh_last_error = ""
    except Exception as exc:
        st.session_state.market_overview = f"大盤總覽更新失敗：{exc}"
        st.session_state.auto_refresh_last_error = str(exc)
    _save_runtime_cache()
    if trigger == "auto":
        st.toast("自動刷新完成；登入 Session 保持中")


def _auto_refresh_scheduler_body() -> None:
    """Session-safe scheduler body.

    It is invoked by st.fragment, not by HTML meta refresh.  The browser and WebSocket are
    not reloaded.  When an analysis cycle is due, the scheduler updates session_state and
    requests a normal Streamlit app rerun, which preserves the authenticated session.
    """
    enabled = bool(st.session_state.get("auto_refresh_enabled", False))
    if not enabled:
        st.session_state.auto_refresh_scheduler_status = "尚未啟用"
        return
    if _cloud_login_required() and not bool(st.session_state.get("gtc_auth_ok", False)):
        st.session_state.auto_refresh_scheduler_status = "登入狀態不存在，排程停止"
        return

    now_ts = time.time()
    interval = max(10, int(st.session_state.get("refresh_seconds", 30) or 30))
    last_ts = float(st.session_state.get("last_auto_run_ts", 0.0) or 0.0)
    symbols = parse_symbols(st.session_state.get("symbol_text", ""))
    remaining = max(0, int(interval - (now_ts - last_ts))) if last_ts else 0
    st.session_state.auto_refresh_scheduler_status = (
        f"運作中｜每 {interval} 秒｜距下次約 {remaining} 秒｜"
        f"已自動執行 {int(st.session_state.get('auto_refresh_run_count', 0))} 次"
    )
    st.caption(
        f"Session-safe Scheduler：{st.session_state.auto_refresh_scheduler_status}｜"
        f"登入使用者={st.session_state.get('gtc_auth_user', 'local')}"
    )

    due = _auto_refresh_due(now_ts, last_ts, interval, bool(symbols))
    if not due or bool(st.session_state.get("auto_refresh_running", False)):
        return

    st.session_state.auto_refresh_running = True
    try:
        execute_analysis_from_current_symbols("auto")
    except Exception as exc:
        st.session_state.auto_refresh_last_error = str(exc)
        logging.exception("Session-safe auto refresh failed")
    finally:
        st.session_state.auto_refresh_running = False
    # This is a Streamlit script rerun, not a browser reload. Authentication/session data remain.
    st.rerun()


_FRAGMENT_API = getattr(st, "fragment", None) or getattr(st, "experimental_fragment", None)
if _FRAGMENT_API is not None:
    @_FRAGMENT_API(run_every=f"{AUTO_REFRESH_HEARTBEAT_SECONDS}s")
    def render_auto_refresh_scheduler() -> None:
        _auto_refresh_scheduler_body()
else:
    def render_auto_refresh_scheduler() -> None:
        if st.session_state.get("auto_refresh_enabled", False):
            st.error(
                "目前 Streamlit 版本不支援 st.fragment，已停止自動更新以避免整頁重載造成登出。"
                "請將 requirements.txt 的 streamlit 升級到支援 st.fragment 的版本。"
            )


def render_ui() -> None:
    st.set_page_config(page_title=f"{APP_TITLE} {APP_VERSION}", layout="wide")
    if not _render_login_gate():
        return
    init_state()
    st.title(f"{APP_TITLE} {APP_VERSION}")
    st.caption("雙模式版本：Windows EXE / 本機 BAT 使用 localhost；Streamlit Cloud / Render / Railway 使用線上半雲端 POC。")

    with st.sidebar:
        _render_logout_control()
        st.header("作戰表與控制")
        uploaded = st.file_uploader("上傳個股作戰表 Excel", type=["xlsx", "xlsm", "xls"], key="battle_plan_uploader")
        if uploaded is not None:
            sig = _uploaded_signature(uploaded)
            if sig != st.session_state.get("last_uploaded_signature"):
                try:
                    load_battle_plan_from_upload(uploaded)
                    st.success("作戰表匯入完成，股票代碼已同步。")
                except Exception as exc:
                    st.session_state.battle_plan_status = f"作戰表：匯入失敗｜{exc}"
                    _save_runtime_cache()
                    st.error(str(exc))
            else:
                st.caption("作戰表已解析過，本次重新整理不重複匯入。")
        st.info(st.session_state.battle_plan_status)
        if st.session_state.get("battle_plan_map"):
            st.caption(f"作戰表狀態：{len(st.session_state.battle_plan_map)} 檔；模式={_runtime_mode_label()}；自動刷新時保留股票清單與控制價位。")
        advanced = st.checkbox("顯示進階欄位", value=False)
        st.checkbox("啟用自動刷新", key="auto_refresh_enabled")
        st.number_input("刷新秒數", min_value=10, max_value=300, step=5, key="refresh_seconds")
        _save_runtime_cache()
        if st.session_state.auto_refresh_enabled:
            st.caption(
                f"自動刷新已啟用：每 {st.session_state.refresh_seconds} 秒執行分析；"
                f"引擎={AUTO_REFRESH_ENGINE_VERSION}，不重新載入瀏覽器，登入 Session 保持。"
            )
            st.caption(f"排程狀態：{st.session_state.get('auto_refresh_scheduler_status', '等待啟動')}")
            if st.session_state.get("auto_refresh_last_error"):
                st.warning(f"最近一次自動更新錯誤：{st.session_state.auto_refresh_last_error}")
        st.divider()
        st.subheader("盤中事件狀態")
        st.caption(f"事件引擎 Ver {EVENT_ENGINE_VERSION}｜目前記憶 {len(st.session_state.get('intraday_event_store', {}))} 檔")
        if st.button("清除今日盤中事件狀態", width="stretch"):
            st.session_state.intraday_event_store = {}
            st.session_state.intraday_event_transitions = []
            st.success("今日盤中事件狀態已清除。")
        st.write("下載")

    col_input, col_btn1, col_btn2 = st.columns([7, 1.2, 1.2])
    with col_input:
        st.text_input("股票代號（逗號分隔）", key="symbol_text")
        _save_runtime_cache()
    with col_btn1:
        run_clicked = st.button("執行分析", type="primary", width="stretch")
    with col_btn2:
        clear_clicked = st.button("清空", width="stretch")

    if clear_clicked:
        st.session_state.results = []
        st.session_state.errors = []
        st.session_state.market_overview = "加權：- ｜ 台積電：- ｜ 上漲/下跌：-/- ｜ 量能：未知\n市場模式：尚無資料 ｜ 今日策略：尚無資料"
        st.session_state.last_update_time = None
        st.session_state.last_auto_run_ts = 0.0
        _save_runtime_cache()
        st.rerun()

    if run_clicked:
        execute_analysis_from_current_symbols("manual")

    # v5.4.3: fragment scheduler reruns inside the same authenticated Streamlit session.
    render_auto_refresh_scheduler()

    st.text(st.session_state.market_overview)
    if st.session_state.last_update_time:
        st.caption(f"最後更新：{st.session_state.last_update_time} ｜ 追蹤檔數：{len(st.session_state.results)} ｜ 版本：{APP_VERSION}")

    if st.session_state.errors:
        with st.expander(f"部分股票失敗：{len(st.session_state.errors)} 檔", expanded=False):
            st.code("\n".join(st.session_state.errors[:30]))

    df = results_dataframe(st.session_state.results, advanced=advanced)
    if not df.empty:
        st.dataframe(df, width="stretch", height=430, hide_index=True)
        csv_bytes = df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
        st.download_button("下載 CSV 主表", data=csv_bytes, file_name=f"GTC_v5.4.3_SessionSafeAutoRefresh_主表_{datetime.now():%Y%m%d_%H%M%S}.csv", mime="text/csv")
        try:
            excel_bytes = make_excel_download(df, st.session_state.results)
            st.download_button("下載 Excel 報告", data=excel_bytes, file_name=f"GTC_v5.4.3_SessionSafeAutoRefresh_即時看盤報告_{datetime.now():%Y%m%d_%H%M%S}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        except Exception as exc:
            st.warning(f"Excel 下載建立失敗：{exc}")
        all_txt = "\n\n".join("\n".join(build_detail_lines(r) + ["", "="*60, ""] + build_advice_lines(r)) for r in st.session_state.results)
        st.download_button("下載 TXT 完整報告", data=all_txt.encode("utf-8"), file_name=f"GTC_v5.4.3_SessionSafeAutoRefresh_完整報告_{datetime.now():%Y%m%d_%H%M%S}.txt", mime="text/plain")
        try:
            pdf_summary = make_pdf_download(st.session_state.results, mode="summary")
            st.download_button("下載 PDF 總表摘要", data=pdf_summary, file_name=f"GTC_v5.4.3_SessionSafeAutoRefresh_總表摘要_{datetime.now():%Y%m%d_%H%M%S}.pdf", mime="application/pdf")
            pdf_full = make_pdf_download(st.session_state.results, mode="full")
            st.download_button("下載 PDF 完整報告", data=pdf_full, file_name=f"GTC_v5.4.3_SessionSafeAutoRefresh_完整報告_{datetime.now():%Y%m%d_%H%M%S}.pdf", mime="application/pdf")
        except Exception as exc:
            st.warning(f"PDF 下載建立失敗：{exc}")

        symbols = [f"{r.get('input_symbol')} {r.get('name')}" for r in st.session_state.results]
        selected_label = st.selectbox("選取個股查看明細", symbols)
        selected_symbol = selected_label.split()[0]
        target = next((r for r in st.session_state.results if str(r.get("input_symbol")) == selected_symbol), None)
        if target:
            left, right = st.columns(2)
            with left:
                st.subheader("個股明細分析")
                st.text("\n".join(build_detail_lines(target)))
            with right:
                st.subheader("操作建議 / 風險提醒")
                st.text("\n".join(build_advice_lines(target)))
    else:
        st.info("請上傳作戰表或輸入股票代號後，按『執行分析』；未列入作戰表股票會自動產生作戰等級、控制欄與即時指示。")


if __name__ == "__main__":
    render_ui()
