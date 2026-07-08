@echo off
chcp 65001 >nul
cd /d %~dp0
echo 啟動 GTC v5.3.1 Web Core Separation，本機網址：http://localhost:8501
echo 若缺少套件，請先執行：pip install -r requirements.txt
streamlit run main.py --server.address localhost --server.port 8501
pause
