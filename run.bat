@echo off
REM Start NyayaSathi. Needs Ollama running with the qwen3:8b model (ollama pull qwen3:8b).
cd /d "%~dp0"
pip install -q -r requirements.txt
start "" http://localhost:8000
python app.py
