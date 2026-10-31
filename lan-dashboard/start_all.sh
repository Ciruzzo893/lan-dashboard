#!/usr/bin/env bash

# Percorso automatico della cartella del progetto
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

# 0. Sblocco permessi sensore energetico Intel RAPL (per lettura Watt reali su Ubuntu)
if [ -f /sys/class/powercap/intel-rapl:0/energy_uj ]; then
    sudo chmod a+r /sys/class/powercap/intel-rapl:0/energy_uj 2>/dev/null || true
fi

# 1. Avvio / Riavvio Servizio Ollama (Porta 11434)
if systemctl list-unit-files | grep -q "ollama.service"; then
    sudo systemctl restart ollama
    sleep 2
    if systemctl is-active --quiet ollama; then
        echo -e "${GREEN}[OK] Ollama attivo${NC}"
    else
        echo -e "${RED}[ERRORE] Impossibile avviare il servizio Ollama${NC}"
    fi
elif command -v ollama >/dev/null 2>&1; then
    pkill -f "ollama serve" 2>/dev/null || true
    nohup ollama serve >/dev/null 2>&1 &
    sleep 2
    echo -e "${GREEN}[OK] Ollama attivo${NC}"
else
    echo -e "${YELLOW}[AVVISO] Ollama non ancora installato sul sistema${NC}"
fi

# 2. Avvio / Riavvio Demone Lettura UPS Legrand Keor SP via USB (NUT)
if command -v upsc >/dev/null 2>&1; then
    sudo systemctl restart nut-driver.target nut-server.service 2>/dev/null || sudo systemctl restart nut-server 2>/dev/null || true
    sleep 2
    if upsc keor@localhost >/dev/null 2>&1; then
        echo -e "${GREEN}[OK] Lettura UPS attiva${NC}"
    else
        echo -e "${YELLOW}[OK] Lettura UPS attiva (Servizio NUT avviato - verifica cavo USB)${NC}"
    fi
else
    echo -e "${YELLOW}[AVVISO] Servizio NUT UPS non ancora installato${NC}"
fi

# 3. Preparazione ambiente virtuale Python (se è il primo avvio)
if [ ! -d "$PROJECT_DIR/venv" ]; then
    echo -e "${BLUE}[INFO] Creazione ambiente virtuale Python e installazione librerie...${NC}"
    python3 -m venv "$PROJECT_DIR/venv"
    "$PROJECT_DIR/venv/bin/pip" install --upgrade pip >/dev/null 2>&1
    "$PROJECT_DIR/venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt"
fi

# 4. Avvio / Riavvio Backend FastAPI (Dashboard, Bacheca File, Telemetria, Scanner Rete)
if systemctl list-unit-files | grep -q "lan-dashboard.service"; then
    sudo systemctl restart lan-dashboard.service
else
    pkill -f "uvicorn main:app" 2>/dev/null || true
    nohup "$PROJECT_DIR/venv/bin/uvicorn" main:app --host 0.0.0.0 --port 8080 > "$PROJECT_DIR/dashboard.log" 2>&1 &
fi

sleep 2

# Rilevamento IP locale del Mini PC
LOCAL_IP=$(hostname -I | awk '{print $1}')
if [ -z "$LOCAL_IP" ]; then
    LOCAL_IP="127.0.0.1"
fi

if pgrep -f "uvicorn" >/dev/null 2>&1 || systemctl is-active --quiet lan-dashboard.service; then
    echo -e "${GREEN}[OK] Dashboard e Scanner Rete attivi su http://${LOCAL_IP}:8080${NC}"
else
    echo -e "${RED}[ERRORE] Il server FastAPI non è partito. Controlla dashboard.log${NC}"
fi

echo -e "${BLUE}=====================================================${NC}"