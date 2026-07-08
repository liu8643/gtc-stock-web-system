# GTC 股票專業版看盤分析系統 v5.3.1 Web Edition
# Web shell for v5.3.1 Core Separation FIX.
# Local-only Streamlit app: http://localhost:8501

from __future__ import annotations

from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any
import tempfile
import time
import json

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
APP_VERSION = "v5.3.5-Web-AutoRefresh-BattleCache-FIX"
DEFAULT_SYMBOLS = "2330,2382,3231,2308,3017,4979,AAPL,NVDA,MSFT"
CORE_COLUMNS = [
    "排名", "燈號", "市場", "代號", "名稱", "顯示價", "漲跌幅%",
    "作戰等級", "控制欄", "即時指示", "訊號", "建議", "分數", "等級",
    "目標價", "RR", "主升候選", "技術狀態", "執行狀態", "顯示狀態",
    "進場狀態", "建議倉位%", "資金等級", "配置分", "最終決策", "可下單",
]
ADVANCED_COLUMNS = [
    "排名", "燈號", "市場", "代號", "名稱", "顯示價", "漲跌", "漲跌幅%",
    "作戰等級", "控制欄", "即時指示", "訊號", "建議", "分數", "等級", "目標價", "RR",
    "主升候選", "波浪定位", "費波位置", "禁追提示", "波費判定", "大波", "小波",
    "修正型態", "回撤比例", "反彈性質", "修正完成", "逃命反彈", "推動浪",
    "技術狀態", "執行狀態", "顯示狀態", "波段分", "盤中分", "支撐", "壓力",
    "RSI", "五檔力道", "交易類型", "進場狀態", "進場可執行", "建議倉位%",
    "資金等級", "配置分", "阻擋原因", "下單類型", "最終決策", "可下單", "決策原因", "報價說明",
]


RUNTIME_DIR = Path.cwd() / ".gtc_runtime"
RUNTIME_CACHE_PATH = RUNTIME_DIR / "gtc_web_state_cache.json"


def _load_runtime_cache() -> dict[str, Any]:
    """Load local UI/runtime state so browser meta-refresh does not lose battle-plan symbols."""
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
    """Persist only small UI state; analysis results are intentionally not cached."""
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "symbol_text": st.session_state.get("symbol_text", DEFAULT_SYMBOLS),
            "battle_plan_map": st.session_state.get("battle_plan_map", {}),
            "battle_plan_status": st.session_state.get("battle_plan_status", ""),
            "battle_plan_filename": st.session_state.get("battle_plan_filename", ""),
            "auto_refresh_enabled": bool(st.session_state.get("auto_refresh_enabled", False)),
            "refresh_seconds": int(st.session_state.get("refresh_seconds", 30)),
            "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        with RUNTIME_CACHE_PATH.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        # Cache failure must not block stock analysis.
        pass


def _clear_runtime_cache() -> None:
    try:
        if RUNTIME_CACHE_PATH.exists():
            RUNTIME_CACHE_PATH.unlink()
    except Exception:
        pass

def init_state() -> None:
    """Initialize Streamlit state and restore local cache on every new session.

    Streamlit browser/meta refresh may create a new script session.  The file_uploader
    control cannot retain the uploaded binary after a hard browser refresh, so the
    app must restore the already-parsed battle plan mapping and synchronized symbol
    list from a small local JSON cache.
    """
    cache = _load_runtime_cache()
    defaults = {
        "symbol_text": DEFAULT_SYMBOLS,
        "battle_plan_map": {},
        "battle_plan_status": "作戰表：尚未匯入；即時看盤功能維持原本邏輯",
        "battle_plan_filename": "",
        "results": [],
        "errors": [],
        "last_update_time": None,
        "market_overview": "加權：- ｜ 台積電：- ｜ 上漲/下跌：-/- ｜ 量能：未知\n市場模式：尚無資料 ｜ 今日策略：尚無資料",
        "auto_refresh_enabled": False,
        "refresh_seconds": 30,
        "last_auto_run_ts": 0.0,
    }
    cache_keys = {
        "symbol_text",
        "battle_plan_map",
        "battle_plan_status",
        "battle_plan_filename",
        "auto_refresh_enabled",
        "refresh_seconds",
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            if key in cache_keys and key in cache:
                st.session_state[key] = cache.get(key, value)
            else:
                st.session_state[key] = value

    # If a saved battle plan exists, make the status explicit after browser refresh.
    if st.session_state.get("battle_plan_map") and st.session_state.get("battle_plan_filename"):
        restored_count = len(st.session_state.get("battle_plan_map", {}))
        if "已從本機快取恢復" not in str(st.session_state.get("battle_plan_status", "")):
            st.session_state.battle_plan_status = (
                f"作戰表：{st.session_state.battle_plan_filename}｜已從本機快取恢復 {restored_count} 檔；"
                "可直接執行分析，不必重新上傳"
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
    if code_order:
        st.session_state.symbol_text = ",".join(code_order)
    sync_note = f"｜已同步 {len(code_order)} 檔到股票代號輸入框" if code_order else ""
    st.session_state.battle_plan_status = f"作戰表：{uploaded_file.name}｜Sheet=個股作戰表｜{status}{sync_note}"
    _save_runtime_cache()


def apply_battle_plan_to_result(result: dict[str, Any]) -> dict[str, Any]:
    merged = dict(result)
    fields = core.empty_battle_fields()
    code = core.normalize_stock_code_value(merged.get("input_symbol"))
    item = st.session_state.battle_plan_map.get(code) if code else None
    if item:
        fields.update(item)
        fields["battle_plan_hit"] = True
        ctrl = core.build_control_status(
            merged.get("display_price", merged.get("close")),
            fields.get("battle_support1"),
            fields.get("battle_support2"),
            fields.get("battle_support3"),
            fields.get("battle_resistance1"),
            fields.get("battle_resistance2"),
        )
        fields.update(ctrl)
    merged.update(fields)
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
        f"作戰表命中：{'是' if target.get('battle_plan_hit') else '否'} / 作戰等級：{target.get('battle_strategy_level','-')}",
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
        f"控制欄：{target.get('control_label','-')}",
        f"即時指示：{target.get('control_action','-')}",
        f"完整價位：支撐1={fmt(target.get('battle_support1'))} / 支撐2={fmt(target.get('battle_support2'))} / 支撐3={fmt(target.get('battle_support3'))} / 壓力1={fmt(target.get('battle_resistance1'))} / 壓力2={fmt(target.get('battle_resistance2'))}",
        f"目前距離%：{target.get('control_distance_pct','-')} / 控制狀態={target.get('control_state','-')}",
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
    title = "GTC v5.3.1 Web Edition 即時看盤完整報告" if mode == "full" else "GTC v5.3.1 Web Edition 即時看盤總表摘要"
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


def render_ui() -> None:
    st.set_page_config(page_title=f"{APP_TITLE} {APP_VERSION}", layout="wide")
    init_state()
    st.title(f"{APP_TITLE} {APP_VERSION}")
    st.caption("本機網路版：預設使用 http://localhost:8501，不代表公開上網。")

    with st.sidebar:
        st.header("作戰表與控制")
        uploaded = st.file_uploader("上傳個股作戰表 Excel", type=["xlsx", "xlsm", "xls"])
        if uploaded is not None:
            try:
                load_battle_plan_from_upload(uploaded)
                _save_runtime_cache()
                st.success("作戰表匯入完成，股票代碼已同步。")
            except Exception as exc:
                st.session_state.battle_plan_status = f"作戰表：匯入失敗｜{exc}"
                _save_runtime_cache()
                st.error(str(exc))
        st.info(st.session_state.battle_plan_status)
        if st.session_state.get("battle_plan_map"):
            st.caption(f"快取作戰表：{len(st.session_state.battle_plan_map)} 檔；瀏覽器刷新後仍保留控制價位與股票清單。")
        advanced = st.checkbox("顯示進階欄位", value=False)
        st.session_state.auto_refresh_enabled = st.checkbox("啟用自動刷新", value=bool(st.session_state.auto_refresh_enabled))
        st.session_state.refresh_seconds = int(st.number_input("刷新秒數", min_value=10, max_value=300, value=int(st.session_state.refresh_seconds), step=5))
        _save_runtime_cache()
        if st.session_state.auto_refresh_enabled:
            st.caption(f"自動刷新已啟用：每 {st.session_state.refresh_seconds} 秒重新整理一次。本機 localhost 使用。")
            st.markdown(f"<meta http-equiv='refresh' content='{st.session_state.refresh_seconds}'>", unsafe_allow_html=True)
        st.divider()
        st.write("下載")

    col_input, col_btn1, col_btn2 = st.columns([7, 1.2, 1.2])
    with col_input:
        st.session_state.symbol_text = st.text_input("股票代號（逗號分隔）", value=st.session_state.symbol_text)
        _save_runtime_cache()
    with col_btn1:
        run_clicked = st.button("執行分析", type="primary", use_container_width=True)
    with col_btn2:
        clear_clicked = st.button("清空", use_container_width=True)

    if clear_clicked:
        st.session_state.results = []
        st.session_state.errors = []
        st.session_state.market_overview = "加權：- ｜ 台積電：- ｜ 上漲/下跌：-/- ｜ 量能：未知\n市場模式：尚無資料 ｜ 今日策略：尚無資料"
        st.session_state.last_update_time = None
        _save_runtime_cache()
        st.rerun()

    def execute_analysis_from_current_symbols(trigger: str) -> None:
        symbols = parse_symbols(st.session_state.symbol_text)
        if not symbols:
            st.warning("請輸入至少一個股票代號。")
            return
        results, errors = analyze_symbols(symbols)
        st.session_state.results = results
        st.session_state.errors = errors
        st.session_state.last_update_time = datetime.now().strftime("%H:%M:%S")
        st.session_state.last_auto_run_ts = time.time()
        try:
            st.session_state.market_overview = core.build_market_overview(results)
        except Exception as exc:
            st.session_state.market_overview = f"大盤總覽更新失敗：{exc}"
        _save_runtime_cache()
        if trigger == "auto":
            st.toast("自動刷新完成")

    if run_clicked:
        execute_analysis_from_current_symbols("manual")

    if st.session_state.auto_refresh_enabled:
        elapsed = time.time() - float(st.session_state.last_auto_run_ts or 0.0)
        should_auto_run = bool(parse_symbols(st.session_state.symbol_text)) and elapsed >= float(st.session_state.refresh_seconds)
        if should_auto_run:
            execute_analysis_from_current_symbols("auto")

    st.text(st.session_state.market_overview)
    if st.session_state.last_update_time:
        st.caption(f"最後更新：{st.session_state.last_update_time} ｜ 追蹤檔數：{len(st.session_state.results)} ｜ 版本：{APP_VERSION}")

    if st.session_state.errors:
        with st.expander(f"部分股票失敗：{len(st.session_state.errors)} 檔", expanded=False):
            st.code("\n".join(st.session_state.errors[:30]))

    df = results_dataframe(st.session_state.results, advanced=advanced)
    if not df.empty:
        st.dataframe(df, use_container_width=True, height=430, hide_index=True)
        csv_bytes = df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
        st.download_button("下載 CSV 主表", data=csv_bytes, file_name=f"GTC_v5.3.1_Web_主表_{datetime.now():%Y%m%d_%H%M%S}.csv", mime="text/csv")
        try:
            excel_bytes = make_excel_download(df, st.session_state.results)
            st.download_button("下載 Excel 報告", data=excel_bytes, file_name=f"GTC_v5.3.1_Web_即時看盤報告_{datetime.now():%Y%m%d_%H%M%S}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        except Exception as exc:
            st.warning(f"Excel 下載建立失敗：{exc}")
        all_txt = "\n\n".join("\n".join(build_detail_lines(r) + ["", "="*60, ""] + build_advice_lines(r)) for r in st.session_state.results)
        st.download_button("下載 TXT 完整報告", data=all_txt.encode("utf-8"), file_name=f"GTC_v5.3.1_Web_完整報告_{datetime.now():%Y%m%d_%H%M%S}.txt", mime="text/plain")
        try:
            pdf_summary = make_pdf_download(st.session_state.results, mode="summary")
            st.download_button("下載 PDF 總表摘要", data=pdf_summary, file_name=f"GTC_v5.3.1_Web_總表摘要_{datetime.now():%Y%m%d_%H%M%S}.pdf", mime="application/pdf")
            pdf_full = make_pdf_download(st.session_state.results, mode="full")
            st.download_button("下載 PDF 完整報告", data=pdf_full, file_name=f"GTC_v5.3.1_Web_完整報告_{datetime.now():%Y%m%d_%H%M%S}.pdf", mime="application/pdf")
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
        st.info("請上傳作戰表或輸入股票代號後，按『執行分析』。")


if __name__ == "__main__":
    render_ui()
