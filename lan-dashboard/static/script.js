/* =========================================================
   STATO GLOBALE DELL'APPLICAZIONE
   ========================================================= */
let chatHistory = [];
let currentAttachment = null; // { name, type: 'text'|'image', content }
let speedtestRemainingSeconds = 60; // 5 minuti (300 secondi)
let isGeneratingResponse = false;

/* =========================================================
   INIZIALIZZAZIONE AL CARICAMENTO PAGINA
   ========================================================= */
document.addEventListener("DOMContentLoaded", () => {
    initMarkdownConfig();
    initChatEvents();
    initNasModalEvents();

    // Avvio caricamento modelli Ollama
    loadOllamaModels();

    // Primo caricamento immediato della telemetria
    fetchFastTelemetry();
    fetchNetworkTelemetry();

    // Loop telemetria hardware e UPS ogni 2 secondi
    setInterval(fetchFastTelemetry, 2000);

    // Loop scanner LAN e stato Speedtest ogni 5 secondi
    setInterval(fetchNetworkTelemetry, 5000);

    // Timer locale al secondo per il conto alla rovescia MM:SS
    setInterval(tickSpeedtestCountdown, 1000);
});

/* =========================================================
   CONFIGURAZIONE MARKDOWN E FALLBACK OFFLINE
   ========================================================= */
function initMarkdownConfig() {
    if (typeof marked !== "undefined") {
        marked.setOptions({
            breaks: true,
            gfm: true
        });
    }
}

function renderFormattedText(rawText) {
    // Se la libreria marked è caricata, formatta Markdown e codice
    if (typeof marked !== "undefined") {
        const html = marked.parse(rawText);
        return html;
    }
    // Fallback offline sicuro se non c'è internet
    const escaped = rawText
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/\n/g, "<br>");
    return escaped;
}

function highlightCodeBlocks(container) {
    if (typeof hljs !== "undefined") {
        container.querySelectorAll("pre code").forEach((block) => {
            hljs.highlightElement(block);
        });
    }
}

/* =========================================================
   SEZIONE 1: CHAT-BOT OLLAMA E ALLEGATI
   ========================================================= */
async function loadOllamaModels() {
    const select = document.getElementById("model-select");
    const statusDot = document.getElementById("ollama-status-dot");

    try {
        const res = await fetch("/api/ollama/models");
        const data = await res.json();

        select.innerHTML = "";

        if (data.online && data.models && data.models.length > 0) {
            statusDot.classList.add("online");
            statusDot.title = "Ollama Online (Porta 11434)";

            data.models.forEach((modelName) => {
                const opt = document.createElement("option");
                opt.value = modelName;
                opt.textContent = modelName;
                select.appendChild(opt);
            });
        } else {
            statusDot.classList.remove("online");
            statusDot.title = "Ollama Offline o nessun modello installato";
            const opt = document.createElement("option");
            opt.value = "";
            opt.textContent = "Nessun modello trovato";
            select.appendChild(opt);
        }
    } catch (err) {
        statusDot.classList.remove("online");
        select.innerHTML = '<option value="">Ollama non raggiungibile</option>';
    }
}

function initChatEvents() {
    const chatInput = document.getElementById("chat-input");
    const sendBtn = document.getElementById("send-btn");
    const attachBtn = document.getElementById("attach-file-btn");
    const fileInput = document.getElementById("chat-file-input");
    const removeAttachmentBtn = document.getElementById("remove-attachment-btn");

    // Invio con tasto Invio
    chatInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            sendChatMessage();
        }
    });

    // Invio con bottone freccia
    sendBtn.addEventListener("click", sendChatMessage);

    // Apertura selettore file da allegare alla chat
    attachBtn.addEventListener("click", () => fileInput.click());

    // Lettura file allegato (testo/codice oppure immagine)
    fileInput.addEventListener("change", handleChatFileSelect);

    // Rimozione allegato attivo
    removeAttachmentBtn.addEventListener("click", clearChatAttachment);
}

function handleChatFileSelect(event) {
    const file = event.target.files[0];
    if (!file) return;

    const isImage = file.type.startsWith("image/");
    const reader = new FileReader();

    if (isImage) {
        reader.onload = (e) => {
            // Estrae solo la stringa Base64 rimuovendo l'intestazione data:image/...;base64,
            const base64Data = e.target.result.split(",")[1];
            currentAttachment = {
                name: file.name,
                type: "image",
                content: base64Data
            };
            showAttachmentBadge(file.name, "Immagine");
        };
        reader.readAsDataURL(file);
    } else {
        reader.onload = (e) => {
            currentAttachment = {
                name: file.name,
                type: "text",
                content: e.target.result
            };
            showAttachmentBadge(file.name, "Testo/Codice");
        };
        reader.readAsText(file);
    }
}

function showAttachmentBadge(fileName, labelType) {
    const bar = document.getElementById("attached-file-bar");
    const nameSpan = document.getElementById("attached-file-name");
    nameSpan.textContent = `📎 Allegato pronto (${labelType}): ${fileName}`;
    bar.classList.remove("hidden");
}

function clearChatAttachment() {
    currentAttachment = null;
    document.getElementById("chat-file-input").value = "";
    document.getElementById("attached-file-bar").classList.add("hidden");
}

function appendMessageToUI(role, text) {
    const container = document.getElementById("chat-messages");
    const msgDiv = document.createElement("div");
    msgDiv.className = `message ${role === "user" ? "user-message" : "assistant-message"}`;

    const contentDiv = document.createElement("div");
    contentDiv.className = "message-content";
    contentDiv.innerHTML = renderFormattedText(text);

    msgDiv.appendChild(contentDiv);
    container.appendChild(msgDiv);

    highlightCodeBlocks(contentDiv);
    container.scrollTop = container.scrollHeight;
    return contentDiv;
}

async function sendChatMessage() {
    if (isGeneratingResponse) return;

    const chatInput = document.getElementById("chat-input");
    const modelSelect = document.getElementById("model-select");
    const rawUserText = chatInput.value.trim();
    const selectedModel = modelSelect.value;

    if (!rawUserText && !currentAttachment) return;

    if (!selectedModel) {
        appendMessageToUI("assistant", "⚠️ Seleziona prima un modello Ollama valido dal menu in alto a destra.");
        return;
    }

    // Costruzione del messaggio utente e gestione eventuale allegato
    let finalPrompt = rawUserText;
    let imagesPayload = [];
    let uiDisplayMessage = rawUserText;

    if (currentAttachment) {
        if (currentAttachment.type === "text") {
            finalPrompt = `${rawUserText}\n\n--- INIZIO FILE ALLEGATO (${currentAttachment.name}) ---\n${currentAttachment.content}\n--- FINE FILE ALLEGATO ---`;
            uiDisplayMessage = `${rawUserText || "Analizza questo file:"}\n\n📄 *[File allegato: ${currentAttachment.name}]*`;
        } else if (currentAttachment.type === "image") {
            imagesPayload.push(currentAttachment.content);
            uiDisplayMessage = `${rawUserText || "Descrivi questa immagine:"}\n\n🖼️ *[Immagine allegata: ${currentAttachment.name}]*`;
        }
    }

    // Mostra il messaggio dell'utente nella chat
    appendMessageToUI("user", uiDisplayMessage);
    chatInput.value = "";
    clearChatAttachment();

    // Aggiorna lo storico conversazione per Ollama
    const userMessageObj = { role: "user", content: finalPrompt };
    if (imagesPayload.length > 0) {
        userMessageObj.images = imagesPayload;
    }
    chatHistory.push(userMessageObj);

    // Messaggio temporaneo di caricamento
    isGeneratingResponse = true;
    const assistantContentDiv = appendMessageToUI("assistant", "⏳ *Elaborazione risposta in corso...*");

    try {
        const res = await fetch("/api/ollama/chat", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                model: selectedModel,
                messages: chatHistory
            })
        });

        const data = await res.json();

        if (res.ok && data.reply) {
            chatHistory.push({ role: "assistant", content: data.reply });
            assistantContentDiv.innerHTML = renderFormattedText(data.reply);
            highlightCodeBlocks(assistantContentDiv);
        } else {
            assistantContentDiv.innerHTML = `❌ **Errore Ollama:** ${data.detail || "Impossibile ottenere risposta dal modello."}`;
        }
    } catch (err) {
        assistantContentDiv.innerHTML = "❌ **Errore di connessione:** Verifica che il backend e Ollama siano attivi.";
    } finally {
        isGeneratingResponse = false;
        const container = document.getElementById("chat-messages");
        container.scrollTop = container.scrollHeight;
    }
}

/* =========================================================
   SEZIONE 2: BACHECA RAPIDA (MINI-NAS CONDIVISIONE FILE LAN)
   ========================================================= */
function initNasModalEvents() {
    const openBtn = document.getElementById("open-nas-btn");
    const closeBtn = document.getElementById("close-nas-btn");
    const modal = document.getElementById("nas-modal");
    const uploadBtn = document.getElementById("nas-upload-btn");

    openBtn.addEventListener("click", () => {
        modal.classList.remove("hidden");
        loadSharedFilesList();
    });

    closeBtn.addEventListener("click", () => {
        modal.classList.add("hidden");
    });

    // Chiudi cliccando sullo sfondo scuro esterno
    modal.addEventListener("click", (e) => {
        if (e.target === modal) {
            modal.classList.add("hidden");
        }
    });

    uploadBtn.addEventListener("click", uploadFilesToNas);
}

async function loadSharedFilesList() {
    const tbody = document.getElementById("nas-files-tbody");

    try {
        const res = await fetch("/api/files");
        const data = await res.json();

        tbody.innerHTML = "";

        if (!data.files || data.files.length === 0) {
            tbody.innerHTML = '<tr><td colspan="4" class="empty-row">Nessun file presente nella bacheca condivisa.</td></tr>';
            return;
        }

        data.files.forEach((f) => {
            const tr = document.createElement("tr");
            const encodedName = encodeURIComponent(f.name);

            tr.innerHTML = `
                <td><strong>${escapeHtml(f.name)}</strong></td>
                <td>${f.size_human}</td>
                <td>${f.modified}</td>
                <td class="text-right">
                    <a href="/api/files/download/${encodedName}" class="nas-btn-download" download>⬇ Scarica</a>
                    <button type="button" class="nas-btn-delete" data-filename="${escapeHtml(f.name)}">🗑 Elimina</button>
                </td>
            `;

            tr.querySelector(".nas-btn-delete").addEventListener("click", () => {
                deleteSharedFile(f.name);
            });

            tbody.appendChild(tr);
        });
    } catch (err) {
        tbody.innerHTML = '<tr><td colspan="4" class="empty-row">Errore nel caricamento della lista file.</td></tr>';
    }
}

async function uploadFilesToNas() {
    const fileInput = document.getElementById("nas-file-input");
    const statusMsg = document.getElementById("nas-upload-status");
    const files = fileInput.files;

    if (!files || files.length === 0) {
        statusMsg.style.color = "#FBBF24";
        statusMsg.textContent = "Seleziona almeno un file prima di cliccare su Carica.";
        return;
    }

    const formData = new FormData();
    for (let i = 0; i < files.length; i++) {
        formData.append("files", files[i]);
    }

    statusMsg.style.color = "#38BDF8";
    statusMsg.textContent = "Caricamento sull'SSD in corso...";

    try {
        const res = await fetch("/api/files/upload", {
            method: "POST",
            body: formData
        });

        if (res.ok) {
            fileInput.value = "";
            statusMsg.style.color = "#34D399";
            statusMsg.textContent = "✅ File salvati con successo nella Bacheca Rapida!";
            loadSharedFilesList();
        } else {
            statusMsg.style.color = "#F87171";
            statusMsg.textContent = "❌ Errore durante il caricamento dei file.";
        }
    } catch (err) {
        statusMsg.style.color = "#F87171";
        statusMsg.textContent = "❌ Errore di rete durante l'upload.";
    }
}

async function deleteSharedFile(filename) {
    if (!confirm(`Vuoi davvero eliminare definitivamente "${filename}" dall'SSD?`)) {
        return;
    }

    try {
        const res = await fetch(`/api/files/${encodeURIComponent(filename)}`, {
            method: "DELETE"
        });
        if (res.ok) {
            loadSharedFilesList();
        }
    } catch (err) {
        alert("Errore durante l'eliminazione del file.");
    }
}

/* =========================================================
   SEZIONE 3: TELEMETRIA HARDWARE & UPS (OGNI 2 SECONDI)
   ========================================================= */
async function fetchFastTelemetry() {
    try {
        const res = await fetch("/api/telemetry/fast");
        if (!res.ok) return;
        const data = await res.json();

        // RIGA 1 - Riquadro 1: CPU, RAM, SPAZIO SSD
        document.getElementById("val-cpu").textContent = `${data.cpu_percent}%`;
        document.getElementById("bar-cpu").style.width = `${Math.min(data.cpu_percent, 100)}%`;

        document.getElementById("val-ram").textContent = `${data.ram_percent}%`;
        document.getElementById("bar-ram").style.width = `${Math.min(data.ram_percent, 100)}%`;

        document.getElementById("val-disk").textContent = `${data.disk_used_gb} / ${data.disk_total_gb} GB`;
        document.getElementById("bar-disk").style.width = `${Math.min(data.disk_percent, 100)}%`;

        // RIGA 1 - Riquadro 2: TEMPERATURA E WATT
        document.getElementById("val-temp").textContent = data.temp_c !== null ? `${data.temp_c}°C` : "N/D";
        document.getElementById("sub-temp").textContent = data.temp_source || "Sensore CPU";

        document.getElementById("val-watt").textContent = data.watt !== null ? `${data.watt} W` : "N/D";
        document.getElementById("sub-watt").textContent = data.watt_source || "Consumo istantaneo";

        // RIGA 3 - Riquadro 1: STATO UPS (Solo su Ubuntu via USB)
        const ups = data.ups;
        const upsBadge = document.getElementById("ups-mode-badge");

        if (ups && ups.available) {
            upsBadge.textContent = "USB Online (Ubuntu)";
            upsBadge.style.color = "#34D399";
            document.getElementById("val-ups-status").textContent = ups.status;
            document.getElementById("val-ups-battery").textContent = `${ups.battery_percent}%`;
            document.getElementById("val-ups-runtime").textContent = `${ups.runtime_min} min`;
            document.getElementById("val-ups-load").textContent = `${ups.load_percent}%`;
        } else {
            upsBadge.textContent = ups ? ups.reason : "Solo su Ubuntu USB";
            upsBadge.style.color = "#94A3B8";
            document.getElementById("val-ups-status").textContent = "In attesa (Ubuntu)";
            document.getElementById("val-ups-battery").textContent = "--%";
            document.getElementById("val-ups-runtime").textContent = "-- min";
            document.getElementById("val-ups-load").textContent = "--%";
        }
    } catch (err) {
        // Silenzioso in caso di riavvio temporaneo del server
    }
}

/* =========================================================
   SEZIONE 4: SCANNER LAN, SPEEDTEST E TIMER 5 MINUTI
   ========================================================= */
async function fetchNetworkTelemetry() {
    try {
        const res = await fetch("/api/telemetry/network");
        if (!res.ok) return;
        const data = await res.json();

        // Aggiorna tabella Scanner di Sicurezza LAN
        const tbody = document.getElementById("scanner-tbody");
        const countBadge = document.getElementById("device-count");

        if (data.devices && data.devices.length > 0) {
            countBadge.textContent = `${data.devices.length} dispositivi`;
            tbody.innerHTML = "";

            data.devices.forEach((dev) => {
                const tr = document.createElement("tr");
                if (dev.is_new) {
                    tr.classList.add("new-device");
                }

                const newBadgeHtml = dev.is_new ? '<span class="new-badge">NUOVO</span>' : '';

                tr.innerHTML = `
                    <td><strong>${escapeHtml(dev.hostname)}</strong>${newBadgeHtml}</td>
                    <td>${escapeHtml(dev.ip)}</td>
                    <td><code>${escapeHtml(dev.mac)}</code></td>
                    <td>${escapeHtml(dev.last_seen)}</td>
                `;
                tbody.appendChild(tr);
            });
        }

        // Aggiorna valori Speedtest (Scarica, Carica, Ping)
        if (data.speedtest) {
            const st = data.speedtest;
            document.getElementById("val-download").textContent = st.download_mbps !== null ? st.download_mbps : "--";
            document.getElementById("val-upload").textContent = st.upload_mbps !== null ? st.upload_mbps : "--";
            document.getElementById("val-ping").textContent = st.ping_ms !== null ? st.ping_ms : "--";
            document.getElementById("net-test-status").textContent = st.is_running ? "Test in corso..." : `Ultimo: ${st.last_run || "In avvio"}`;

            // Sincronizza il conto alla rovescia coi secondi reali del backend
            if (typeof st.next_run_in_sec === "number") {
                speedtestRemainingSeconds = st.next_run_in_sec;
            }
        }
    } catch (err) {
        // Ignora errori transitori di rete
    }
}

function tickSpeedtestCountdown() {
    if (speedtestRemainingSeconds > 0) {
        speedtestRemainingSeconds--;
    }

    const minutes = Math.floor(speedtestRemainingSeconds / 60);
    const seconds = speedtestRemainingSeconds % 60;

    const formatted = `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
    document.getElementById("speedtest-countdown").textContent = formatted;
}

/* =========================================================
   UTILITÀ DI SICUREZZA TESTO HTML
   ========================================================= */
function escapeHtml(str) {
    if (!str) return "";
    return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}