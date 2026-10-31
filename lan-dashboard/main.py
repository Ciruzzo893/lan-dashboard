import asyncio
import json
import os
import platform
import re
import socket
import subprocess
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import List
import concurrent.futures
import ipaddress

import httpx
import psutil
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# =========================================================
# CONFIGURAZIONE PERCORSI E COSTANTI
# =========================================================
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
SHARED_DIR = BASE_DIR / "shared_files"
KNOWN_DEVICES_FILE = BASE_DIR / "known_devices.json"

OLLAMA_BASE_URL = "http://127.0.0.1:11434"
SPEEDTEST_INTERVAL_SEC = 60  # 5 minuti (300 secondi)
LAN_SCAN_INTERVAL_SEC = 30    # Scansione dispositivi LAN ogni 30 secondi

# Assicura che la cartella per la Bacheca Rapida esista
SHARED_DIR.mkdir(exist_ok=True)

# =========================================================
# STATO GLOBALE IN MEMORIA (RETE, SPEEDTEST, RAPL WATT)
# =========================================================
network_state = {
    "devices": [],
    "speedtest": {
        "download_mbps": None,
        "upload_mbps": None,
        "ping_ms": None,
        "last_run": None,
        "is_running": False,
        "cycle_start_ts": time.time()
    }
}

rapl_state = {
    "last_energy_uj": None,
    "last_time": None
}


# =========================================================
# FUNZIONI DI TELEMETRIA HARDWARE (TEMP, WATT, UPS UBUNTU)
# =========================================================
def get_cpu_temperature(cpu_load: float):
    """Legge la temperatura reale della CPU su Linux (o stima su Windows se bloccata dal BIOS)."""
    if hasattr(psutil, "sensors_temperatures"):
        try:
            temps = psutil.sensors_temperatures()
            if temps:
                for name in ("coretemp", "cpu_thermal", "k10temp", "acpitz"):
                    if name in temps and temps[name]:
                        return round(temps[name][0].current, 1), f"Sensore {name}"
                first_sensor = next(iter(temps.values()))
                if first_sensor:
                    return round(first_sensor[0].current, 1), "Sensore Hardware"
        except Exception:
            pass

    # Fallback lettura diretta sysfs su Ubuntu Linux
    thermal_File = Path("/sys/class/thermal/thermal_zone0/temp")
    if thermal_File.exists():
        try:
            val = int(thermal_File.read_text().strip()) / 1000.0
            return round(val, 1), "Thermal Zone Linux"
        except Exception:
            pass

    # Fallback per i test su Windows (dove psutil non ha accesso diretto a coretemp)
    est_temp = round(34.0 + (cpu_load * 0.38), 1)
    return est_temp, "Stima (Test Windows)"


def get_instant_wattage(cpu_load: float, ups_data: dict):
    """
    Calcola il consumo in Watt:
    1. Su Ubuntu Linux legge il contatore energetico Intel RAPL (microjoule al secondo).
    2. Se disponibile il carico dell'UPS Legrand (360W max), usa quello.
    3. Altrimenti stima sulla curva TDP dell'Intel i5-6500T (9W idle - 35W max).
    """
    rapl_path = Path("/sys/class/powercap/intel-rapl:0/energy_uj")
    if rapl_path.exists():
        try:
            current_uj = int(rapl_path.read_text().strip())
            now = time.time()
            if rapl_state["last_energy_uj"] is not None and rapl_state["last_time"] is not None:
                dt = now - rapl_state["last_time"]
                duj = current_uj - rapl_state["last_energy_uj"]
                if dt > 0 and duj >= 0:
                    cpu_watts = (duj / 1_000_000.0) / dt
                    total_watts = round(cpu_watts + 6.5, 1)  # +6.5W per scheda madre/SSD/RAM
                    rapl_state["last_energy_uj"] = current_uj
                    rapl_state["last_time"] = now
                    return total_watts, "Sensore Intel RAPL"
            rapl_state["last_energy_uj"] = current_uj
            rapl_state["last_time"] = now
        except Exception:
            pass

    # Se l'UPS su Ubuntu riporta una percentuale di carico maggiore di 0 (360W nominali per Keor SP 600VA)
    if ups_data.get("available") and ups_data.get("load_percent", 0) > 0:
        ups_watts = round((ups_data["load_percent"] / 100.0) * 360.0, 1)
        return ups_watts, "Sensore Carico UPS"

    # Stima basata sul profilo energetico i5-6500T (TDP 35W)
    est_watt = round(10.0 + (cpu_load / 100.0) * 25.0, 1)
    source = "Stima TDP i5-6500T" if platform.system() == "Linux" else "Stima (Test Windows)"
    return est_watt, source


def get_ups_status_ubuntu():
    """
    Legge lo stato dell'UPS Legrand Keor SP 600VA tramite USB solo su Ubuntu Linux
    usando NUT (comando 'upsc keor@localhost').
    """
    if platform.system() != "Linux":
        return {
            "available": False,
            "reason": "Attivo solo su Ubuntu (USB)"
        }

    try:
        res = subprocess.run(
            ["upsc", "keor@localhost"],
            capture_output=True,
            text=True,
            timeout=2
        )
        if res.returncode != 0 or not res.stdout:
            return {
                "available": False,
                "reason": "UPS USB non rilevato"
            }

        raw = {}
        for line in res.stdout.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                raw[k.strip()] = v.strip()

        status_code = raw.get("ups.status", "OL")
        if "OB" in status_code:
            status_text = "⚡ BATTERIA (Blackout)"
        elif "CHRG" in status_code:
            status_text = "🔌 Rete (In Carica)"
        else:
            status_text = "🟢 Rete Elettrica (OK)"

        battery_pct = int(float(raw.get("battery.charge", 100)))
        load_pct = int(float(raw.get("ups.load", 12)))

        # Calcolo autonomia in minuti (da battery.runtime in secondi o stima su 360W/batteria 12V 7Ah)
        if "battery.runtime" in raw:
            runtime_min = max(1, int(float(raw["battery.runtime"]) / 60))
        else:
            effective_load = max(load_pct, 10)
            runtime_min = max(1, int((battery_pct / 100.0) * (280.0 / effective_load)))

        return {
            "available": True,
            "status": status_text,
            "battery_percent": battery_pct,
            "runtime_min": runtime_min,
            "load_percent": load_pct
        }
    except FileNotFoundError:
        return {
            "available": False,
            "reason": "Servizio NUT non installato"
        }
    except Exception:
        return {
            "available": False,
            "reason": "Lettura USB in attesa..."
        }


# =========================================================
# SCANNER DI SICUREZZA DELLA RETE LOCALE (LAN)
# =========================================================
def get_local_ip_prefix():
    """Rileva automaticamente l'IP locale e i primi 3 blocchi della sottorete (es. 192.168.1)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        parts = local_ip.split(".")
        return local_ip, f"{parts[0]}.{parts[1]}.{parts[2]}"
    except Exception:
        return "127.0.0.1", "192.168.1"


def scan_lan_devices_sync():
    """Esegue un ping rapido della sottorete e legge la tabella ARP per trovare i dispositivi connessi."""
    local_ip, prefix = get_local_ip_prefix()
    is_windows = platform.system() == "Windows"

    # Carica lo storico dei dispositivi già conosciuti
    known_macs = {}
    if KNOWN_DEVICES_FILE.exists():
        try:
            known_macs = json.loads(KNOWN_DEVICES_FILE.read_text(encoding="utf-8"))
        except Exception:
            known_macs = {}

    # Ping leggero per popolare la tabella ARP del sistema operativo
    ping_cmd = ["ping", "-n", "1", "-w", "150"] if is_windows else ["ping", "-c", "1", "-W", "1"]
    procs = []
    for i in range(1, 255):
        target_ip = f"{prefix}.{i}"
        if target_ip == local_ip:
            continue
        try:
            p = subprocess.Popen(
                ping_cmd + [target_ip],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            procs.append(p)
            if len(procs) >= 40:
                for proc in procs:
                    proc.wait()
                procs.clear()
        except Exception:
            pass

    for proc in procs:
        proc.wait()

    # Lettura tabella ARP (Windows e Linux)
    discovered = []
    now_ts = time.time()
    now_str = datetime.now().strftime("%H:%M:%S")

    try:
        arp_res = subprocess.run(["arp", "-a"], capture_output=True, text=True, timeout=5)
        arp_lines = arp_res.stdout.splitlines()
    except Exception:
        arp_lines = []

    mac_regex = re.compile(r"([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}")
    ip_regex = re.compile(r"(\d+\.\d+\.\d+\.\d+)")

    for line in arp_lines:
        ip_match = ip_regex.search(line)
        mac_match = mac_regex.search(line)
        if not ip_match or not mac_match:
            continue

        ip_addr = ip_match.group(1)
        if not ip_addr.startswith(prefix) or ip_addr.endswith(".255"):
            continue

        mac_addr = mac_match.group(0).upper().replace("-", ":")
        if mac_addr == "FF:FF:FF:FF:FF:FF":
            continue

        # Risoluzione nome dispositivo (Hostname)
        hostname = known_macs.get(mac_addr, {}).get("hostname")
        if not hostname or hostname == "Dispositivo LAN":
            try:
                hostname = socket.gethostbyaddr(ip_addr)[0]
            except Exception:
                hostname = "Dispositivo LAN"

        # Verifica se è un dispositivo nuovo (rilevato da meno di 15 minuti)
        if mac_addr not in known_macs:
            known_macs[mac_addr] = {
                "first_seen_ts": now_ts,
                "hostname": hostname
            }

        first_seen_ts = known_macs[mac_addr].get("first_seen_ts", now_ts)
        is_new = (now_ts - first_seen_ts) < 900  # Evidenziato come NUOVO per 15 minuti

        discovered.append({
            "hostname": hostname,
            "ip": ip_addr,
            "mac": mac_addr,
            "last_seen": now_str,
            "is_new": is_new
        })

    # Salva i dispositivi conosciuti su disco
    try:
        KNOWN_DEVICES_FILE.write_text(json.dumps(known_macs, indent=2), encoding="utf-8")
    except Exception:
        pass

    # Ordina mostrando prima i dispositivi NUOVI e poi per IP
    discovered.sort(key=lambda d: (not d["is_new"], [int(x) for x in d["ip"].split(".")]))
    return discovered


# =========================================================
# SPEEDTEST AUTOMATICO OGNI 5 MINUTI
# =========================================================
def run_speedtest_sync():
    """Esegue il test di velocità internet (Download, Upload, Ping) tramite speedtest-cli."""
    import speedtest
    st = speedtest.Speedtest(secure=True)
    st.get_best_server()
    dl_mbps = round(st.download() / 1_000_000, 1)
    ul_mbps = round(st.upload() / 1_000_000, 1)
    ping_ms = round(st.results.ping, 0)
    return dl_mbps, ul_mbps, int(ping_ms)


async def background_lan_scanner_loop():
    while True:
        try:
            devices = await asyncio.to_thread(scan_lan_devices_sync)
            if devices:
                network_state["devices"] = devices
        except Exception:
            pass
        await asyncio.sleep(LAN_SCAN_INTERVAL_SEC)


async def background_speedtest_loop():
    while True:
        network_state["speedtest"]["cycle_start_ts"] = time.time()
        network_state["speedtest"]["is_running"] = True
        try:
            dl, ul, ping = await asyncio.to_thread(run_speedtest_sync)
            network_state["speedtest"]["download_mbps"] = dl
            network_state["speedtest"]["upload_mbps"] = ul
            network_state["speedtest"]["ping_ms"] = ping
            network_state["speedtest"]["last_run"] = datetime.now().strftime("%H:%M")
        except Exception:
            network_state["speedtest"]["last_run"] = "Errore rete"
        finally:
            network_state["speedtest"]["is_running"] = False

        # Attende lo scadere esatto dei 5 minuti (300 secondi)
        elapsed = time.time() - network_state["speedtest"]["cycle_start_ts"]
        sleep_time = max(5.0, SPEEDTEST_INTERVAL_SEC - elapsed)
        await asyncio.sleep(sleep_time)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Avvia in background lo Scanner LAN e il loop di 5 minuti dello Speedtest
    scanner_task = asyncio.create_task(background_lan_scanner_loop())
    speedtest_task = asyncio.create_task(background_speedtest_loop())
    yield
    scanner_task.cancel()
    speedtest_task.cancel()


# =========================================================
# INIZIALIZZAZIONE APP FASTAPI E FILE STATICI
# =========================================================
app = FastAPI(title="LAN Dashboard & AI Locale", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def serve_dashboard():
    return FileResponse(STATIC_DIR / "index.html")


# =========================================================
# ENDPOINT API 1: CHAT-BOT OLLAMA (PORTA 11434)
# =========================================================
class ChatRequest(BaseModel):
    model: str
    messages: list


@app.get("/api/ollama/models")
async def get_ollama_models():
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            res = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
            if res.status_code == 200:
                data = res.json()
                models = [m["name"] for m in data.get("models", [])]
                return {"online": True, "models": models}
    except Exception:
        pass
    return {"online": False, "models": []}


@app.post("/api/ollama/chat")
async def chat_with_ollama(req: ChatRequest):
    payload = {
        "model": req.model,
        "messages": req.messages,
        "stream": False
    }
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            res = await client.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload)
            if res.status_code == 200:
                data = res.json()
                reply_text = data.get("message", {}).get("content", "")
                return {"reply": reply_text}
            raise HTTPException(status_code=res.status_code, detail="Errore restituito da Ollama")
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail="Servizio Ollama non raggiungibile sulla porta 11434.")


# =========================================================
# ENDPOINT API 2: BACHECA RAPIDA (MINI-NAS CONDIVISO)
# =========================================================
def format_size(size_bytes: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"


@app.get("/api/files")
async def list_shared_files():
    files_list = []
    for item in SHARED_DIR.iterdir():
        if item.is_file() and item.name != ".gitkeep":
            stat = item.stat()
            files_list.append({
                "name": item.name,
                "size_human": format_size(stat.st_size),
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%d/%m/%Y %H:%M"),
                "mtime": stat.st_mtime
            })
    files_list.sort(key=lambda x: x["mtime"], reverse=True)
    return {"files": files_list}


@app.post("/api/files/upload")
async def upload_shared_files(files: List[UploadFile] = File(...)):
    saved = []
    for upload in files:
        safe_name = os.path.basename(upload.filename)
        if not safe_name:
            continue
        dest_path = SHARED_DIR / safe_name
        content = await upload.read()
        dest_path.write_bytes(content)
        saved.append(safe_name)
    return {"uploaded": saved}


@app.get("/api/files/download/{filename}")
async def download_shared_file(filename: str):
    safe_name = os.path.basename(filename)
    file_path = SHARED_DIR / safe_name
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File non trovato")
    return FileResponse(file_path, filename=safe_name)


@app.delete("/api/files/{filename}")
async def delete_shared_file(filename: str):
    safe_name = os.path.basename(filename)
    file_path = SHARED_DIR / safe_name
    if file_path.exists():
        file_path.unlink()
        return {"deleted": safe_name}
    raise HTTPException(status_code=404, detail="File non trovato")


# =========================================================
# ENDPOINT API 3: TELEMETRIA HARDWARE E RETE
# =========================================================
@app.get("/api/telemetry/fast")
async def get_fast_telemetry():
    cpu_pct = round(psutil.cpu_percent(interval=None), 1)
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage(str(BASE_DIR))

    ups_info = get_ups_status_ubuntu()
    temp_c, temp_source = get_cpu_temperature(cpu_pct)
    watt, watt_source = get_instant_wattage(cpu_pct, ups_info)

    return {
        "cpu_percent": cpu_pct,
        "ram_percent": round(ram.percent, 1),
        "disk_used_gb": round(disk.used / (1024 ** 3), 1),
        "disk_total_gb": round(disk.total / (1024 ** 3), 0),
        "disk_percent": round(disk.percent, 1),
        "temp_c": temp_c,
        "temp_source": temp_source,
        "watt": watt,
        "watt_source": watt_source,
        "ups": ups_info
    }


@app.get("/api/telemetry/network")
async def get_network_telemetry():
    st = network_state["speedtest"]
    elapsed = time.time() - st["cycle_start_ts"]
    remaining_sec = max(0, int(SPEEDTEST_INTERVAL_SEC - elapsed))

    return {
        "devices": network_state["devices"],
        "speedtest": {
            "download_mbps": st["download_mbps"],
            "upload_mbps": st["upload_mbps"],
            "ping_ms": st["ping_ms"],
            "last_run": st["last_run"],
            "is_running": st["is_running"],
            "next_run_in_sec": remaining_sec
        }
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=False)

def ping_ip(ip):
    try:
        # Ping rapido di 1 solo pacchetto con timeout di 0.5 secondi
        result = subprocess.run(
            ["ping", "-c", "1", "-W", "1", str(ip)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=1
        )
        if result.returncode == 0:
            ip_str = str(ip)
            try:
                hostname = socket.gethostbyaddr(ip_str)[0]
            except socket.herror:
                hostname = "Dispositivo LAN"
            return {"ip": ip_str, "hostname": hostname, "status": "Online"}
    except (subprocess.TimeoutExpired, Exception):
        pass
    return None

@app.get("/api/scan-network")
def scan_network():
    # Rileva la subnet locale (es. 192.168.1.0/24)
    local_ip = socket.gethostbyname(socket.gethostname())
    if local_ip.startswith("127."):
        local_ip = "192.168.1.1" # Fallback sicuro
    
    network_str = ".".join(local_ip.split(".")[:3]) + ".0/24"
    net = ipaddress.ip_network(network_str, strict=False)
    
    active_devices = []
    # Esegue i ping in parallelo per essere velocissimo e non bloccarsi
    with concurrent.futures.ThreadPoolExecutor(max_workers=30) as executor:
        futures = [executor.submit(ping_ip, ip) for ip in net.hosts()]
        for future in concurrent.futures.as_completed(futures):
            res = future.result()
            if res:
                active_devices.append(res)
                
    return {"devices": active_devices}
