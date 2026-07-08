# GTC 股票專業版看盤分析系統 v5.3.1

## 版本定位

`v5.3.1 = Web Core Separation 基準版`

本專案是乾淨的新 GitHub Repo 部署版，只保留正式 Web 執行所需檔案：

- `main.py`：Streamlit Web 主程式
- `gtc_core_engine.py`：純核心分析引擎，無 Tkinter / Streamlit UI 依賴
- `requirements.txt`：執行套件
- `run_gtc_web.bat`：Windows 本機啟動檔
- `test_v53_core_equivalence.py`：核心分離與 Web 引用結構驗證
- `.github/workflows/build-windows-exe.yml`：GitHub Actions Windows EXE 編譯流程

## 不納入正式 Repo 的舊檔

以下檔案僅作備查，不放入正式執行專案：

- `gtc_v525_core_legacy_do_not_use_as_core.py`
- `main_desktop_legacy.py`
- `excel1`
- `excel2`
- 舊版 zip

## 本機啟動方式

```bash
pip install -r requirements.txt
streamlit run main.py --server.address localhost --server.port 8501
```

或 Windows 直接雙擊：

```text
run_gtc_web.bat
```

瀏覽器開啟：

```text
http://localhost:8501
```

## Web 功能驗收項目

啟動後請確認：

1. 可上傳作戰表 Excel
2. 股票代碼可自動同步
3. 可執行即時分析
4. 主表顯示控制欄與即時指示
5. 可下載 CSV / Excel / TXT / PDF
6. 可啟用自動刷新
7. 大盤總覽可顯示資料來源與策略文字

## 結構驗證

```bash
python test_v53_core_equivalence.py
```

驗證重點：

- Core 不包含 Tkinter UI class
- Core 不包含桌面版啟動 main()
- Core 不依賴 tkinter / messagebox / filedialog
- Web 主程式只引用 `gtc_core_engine`
- Web 主程式不引用 legacy core
- Web 主程式保留 PDF 匯出與自動刷新控制

## GitHub Actions 編譯 EXE

上傳到 GitHub 後：

```text
Actions → build-windows-exe → Run workflow
```

完成後下載：

```text
Artifacts / GTC-v5.3.1-Windows-EXE
```

## 建議版本控管

- `v5.3.1`：Web Core Separation 基準版
- `v5.3.2`：GitHub EXE 修正版
- `v5.4.0`：正式 Web 版穩定版
