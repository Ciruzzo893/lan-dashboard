#!/usr/bin/env bash

echo "====================================================="
echo " CONFIGURAZIONE AUTOMATICA UPS LEGRAND KEOR SP 600VA "
echo "====================================================="

# 1. Installa Network UPS Tools (NUT) e utilità USB
sudo apt update
sudo apt install -y nut nut-client nut-server usbutils

# 2. Configura il driver USB per Legrand Keor SP 600VA
sudo tee /etc/nut/ups.conf > /dev/null <<EOF
maxretry = 3

[keor]
    driver = usbhid-ups
    port = auto
    desc = "Legrand Keor SP 600VA USB"
EOF

# 3. Imposta NUT in modalità standalone
sudo sed -i 's/MODE=none/MODE=standalone/' /etc/nut/nut.conf

# 4. Riavvia i demoni USB e verifica se il Keor risponde
sudo upsdrvctl stop 2>/dev/null || true
sudo systemctl restart nut-server 2>/dev/null || true
sudo upsdrvctl start || {
    echo "[INFO] Test fallback driver blazer_usb per chipset Legrand..."
    sudo tee /etc/nut/ups.conf > /dev/null <<EOF
maxretry = 3

[keor]
    driver = blazer_usb
    port = auto
    desc = "Legrand Keor SP 600VA USB"
EOF
    sudo upsdrvctl start
}

sudo systemctl enable nut-server
sudo systemctl restart nut-server

echo "-----------------------------------------------------"
echo "Stato lettura UPS:"
upsc keor@localhost 2>/dev/null || echo "Collega il cavo USB dell'UPS al Mini PC e rilancia lo script."
echo "====================================================="