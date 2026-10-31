#!/usr/bin/env bash

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR" || exit 1

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;36m'
NC='\033[0m'

echo -e "${BLUE}=====================================================${NC}"
echo -e "${BLUE}   AVVIO E RIPRISTINO SISTEMA LAN DASHBOARD (24/7)   ${NC}"
echo -e "${BLUE}=====================================================${NC}"

if [ -f /sys/class/powercap/intel-rapl:0/energy_uj ]; then
    sudo chmod a+r /sys/class/powercap/intel-rapl:0/energy_uj 2>/dev/null || true
fi

# Ollama
if command -v ollama >/dev/null 2>&1; then
    pkill -f "ollama serve" 2>/dev/null || true
    ollama serve >/dev/null 2>&1 &
    sleep 2
    echo -e "${GREEN}[OK] Ollama attivo${NC}"
else
    echo -e "${YELLOW}[AVVISO] Ollama non trovato${NC}"
fi

# UPS (NUT)
if command -v upsc >/dev/null 2>&1; then
    sudo systemctl restart nut-server 2>/dev/null || true
    echo -e "${GREEN}[OK] Lettura UPS attiva${NC}"
else
    echo -e "${YELLOW}[AVVISO] UPS NUT non installato${NC}"
fi

# Venv Python locale al progetto
if [ ! -d "$PROJECT_DIR/venv" ]; then
    echo -e "${BLUE}[INFO] Creazione ambiente virtuale Python...${NC}"
    python3 -m venv "$PROJECT_DIR/venv"
    "$PROJECT_DIR/venv/bin/pip" install --upgrade pip
    "$PROJECT_DIR/venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt"
fi

# FastAPI Uvicorn
pkill -f "uvicorn main:app" 2>/dev/null || true
nohup "$PROJECT_DIR/venv/bin/uvicorn" main:app --host 0.0.0.0 --port 8080 > "$PROJECT_DIR/dashboard.log" 2>&1 &
sleep 2

LOCAL_IP=$(hostname -I | awk '{print $1}')
if [ -z "$LOCAL_IP" ]; then
    LOCAL_IP="127.0.0.1"
fi

if pgrep -f "uvicorn" >/dev/null 2>&1; then
    echo -e "${GREEN}[OK] Dashboard e Scanner Rete attivi su http://${LOCAL_IP}:8080${NC}"
else
    echo -e "${RED}[ERRORE] Il server FastAPI non è partito. Controlla dashboard.log${NC}"
fi
echo -e "${BLUE}=====================================================${NC}"
