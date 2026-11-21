(() => {
    "use strict";

    const byId = (id) => document.getElementById(id);
    const defaults = {
        robotName: "Lenze / Roboter 01",
        endpoint: "",
        apiUrl: "http://192.168.5.204:8000/command",
        forwardApi: true,
        limitX: 500,
        limitY: 500,
        limitZ: 400,
        language: "de-DE",
        forwardTranscript: false,
        showGrid: true,
        showActual: true,
        actualOpacity: 30,
        units: "mm",
        sendInterim: true,
        autoRestart: true
    };
    const storageKey = "lenze-motion-settings-v2";
    let settings = { ...defaults };
    let target = { x: 240, y: 125, z: 180 };
    let telemetry = null;
    let gripper = "open";
    let targetLocked = false;
    let apiQueue = [];
    let apiRequest = null;
    let apiGeneration = 0;
    let apiDelivered = 0;
    let apiRequestNumber = 0;
    let manualPending = false;
    const apiLogEntries = [];
    let socket = null;
    let connectionTimer = null;
    let streamSequence = 0;
    let messageCount = 0;
    let confirmedText = "";
    let visibleText = "";
    let session = null;
    let phase = "idle";
    let wantsRecording = false;
    let restartTimer = null;
    let endReason = "ended";
    let speechError = false;
    const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    const recognition = Recognition ? new Recognition() : null;

    function notice(text) {
        byId("appNotice").textContent = text;
    }

    function validateSetting(key, value) {
        if (typeof defaults[key] === "boolean") return typeof value === "boolean" ? value : defaults[key];
        if (["limitX", "limitY", "limitZ"].includes(key)) {
            return Number.isFinite(Number(value)) && Number(value) >= 1 && Number(value) <= 10000 ? Number(value) : defaults[key];
        }
        if (key === "actualOpacity") return Math.max(10, Math.min(80, Number(value) || 30));
        if (key === "language") return ["de-DE", "en-US"].includes(value) ? value : defaults[key];
        if (key === "units") return ["mm", "cm"].includes(value) ? value : defaults[key];
        return typeof value === "string" ? value.slice(0, key === "robotName" ? 60 : 2000) : defaults[key];
    }

    function loadSettings() {
        try {
            const stored = JSON.parse(localStorage.getItem(storageKey) || "{}");
            for (const key of Object.keys(defaults)) {
                if (stored && Object.hasOwn(stored, key)) settings[key] = validateSetting(key, stored[key]);
            }
            if (!stored || stored.commandApiVersion !== 1) {
                settings.apiUrl = defaults.apiUrl;
                settings.forwardApi = true;
            }
        } catch {
            notice("Gespeicherte Einstellungen konnten nicht geladen werden. Standardwerte sind aktiv.");
        }
        for (const key of Object.keys(defaults)) {
            if (typeof defaults[key] === "boolean") byId(key).checked = settings[key];
            else byId(key).value = settings[key];
        }
        applySettings();
    }

    function persistSettings() {
        try {
            localStorage.setItem(storageKey, JSON.stringify({ ...settings, commandApiVersion: 1 }));
            notice("Einstellungen auf diesem Gerät gespeichert.");
        } catch {
            notice("Einstellungen gelten für diese Sitzung. Lokales Speichern ist nicht verfügbar.");
        }
    }

    function applySettings() {
        byId("workspaceName").textContent = settings.robotName || defaults.robotName;
        document.querySelector(".grid-layer").style.display = settings.showGrid ? "" : "none";
        byId("actualArm").style.opacity = settings.actualOpacity / 100;
        target.x = Math.max(0, Math.min(settings.limitX, target.x));
        target.y = Math.max(0, Math.min(settings.limitY, target.y));
        target.z = Math.max(0, Math.min(settings.limitZ, target.z));
        byId("heightControl").max = settings.limitZ;
        byId("heightControl").value = target.z;
        renderRobot();
        updateStreamStatus();
    }

    for (const key of Object.keys(defaults)) {
        const input = byId(key);
        input.addEventListener("change", () => {
            if (input.type === "number" && !input.checkValidity()) {
                input.reportValidity();
                input.value = settings[key];
                return;
            }
            settings[key] = validateSetting(key, input.type === "checkbox" ? input.checked : input.value);
            if (key === "apiUrl" || key === "forwardApi") {
                cancelApiRequests();
                if (settings.forwardApi && !validApiUrl()) {
                    settings.forwardApi = false;
                    byId("forwardApi").checked = false;
                    notice("Bitte zuerst eine gültige HTTP(S)-API-Adresse eintragen.");
                }
            }
            persistSettings();
            applySettings();
        });
    }
    byId("actualOpacity").addEventListener("input", () => {
        byId("actualArm").style.opacity = Number(byId("actualOpacity").value) / 100;
    });

    document.querySelectorAll(".nav-button").forEach((button) => {
        button.addEventListener("click", () => {
            document.querySelectorAll(".nav-button").forEach((item) => {
                item.classList.toggle("active", item === button);
                if (item === button) item.setAttribute("aria-current", "page");
                else item.removeAttribute("aria-current");
            });
            document.querySelectorAll(".page").forEach((page) => {
                page.hidden = page.id !== `page-${button.dataset.page}`;
            });
        });
    });

    function toggleFocus(force) {
        const active = typeof force === "boolean" ? force : !document.body.classList.contains("focus-mode");
        document.body.classList.toggle("focus-mode", active);
        byId("focusButton").setAttribute("aria-pressed", String(active));
        byId("focusButton").textContent = active ? "Ansicht schließen ⛶" : "Ansicht vergrößern ⛶";
    }
    byId("focusButton").addEventListener("click", () => toggleFocus());
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") toggleFocus(false);
    });

    function armGeometry(position) {
        const x = 90 + position.x / settings.limitX * 620;
        const y = 390 - position.y / settings.limitY * 280;
        const dx = x - 170;
        const dy = y - 420;
        const distance = Math.max(31, Math.min(629, Math.hypot(dx, dy)));
        const angle = Math.atan2(dy, dx) - Math.acos(Math.max(-1, Math.min(1, (330 ** 2 + distance ** 2 - 300 ** 2) / (2 * 330 * distance))));
        const jointX = 170 + Math.cos(angle) * 330;
        const jointY = 420 + Math.sin(angle) * 330;
        return { x, y, jointX, jointY, path: `M170 420 L${jointX} ${jointY} L${x} ${y}` };
    }

    function renderRobot() {
        const arm = armGeometry(target);
        byId("armPath").setAttribute("d", arm.path);
        byId("armShadow").setAttribute("d", arm.path);
        for (const [id, x, y] of [["armJoint", arm.jointX, arm.jointY], ["armTip", arm.x, arm.y]]) {
            byId(id).setAttribute("cx", x);
            byId(id).setAttribute("cy", y);
        }
        byId("targetMarker").setAttribute("transform", `translate(${arm.x} ${arm.y})`);
        byId("gripperDrawing").setAttribute("d", gripper === "open" ? "M-10-8V9L-5 15M10-8V9L5 15" : "M-5-8V9L0 15M5-8V9L0 15");
        const factor = settings.units === "cm" ? 10 : 1;
        for (const axis of ["x", "y", "z"]) byId(`coord${axis.toUpperCase()}`).textContent = String(Math.round(target[axis] / factor * 10) / 10);
        byId("unitLabel").textContent = settings.units;
        byId("gripperState").textContent = gripper === "open" ? "offen" : "geschlossen";
        byId("gripperButton").setAttribute("aria-pressed", String(gripper === "closed"));
        const showTelemetry = settings.showActual && telemetry !== null;
        byId("actualArm").setAttribute("visibility", showTelemetry ? "visible" : "hidden");
        if (showTelemetry) {
            const actual = armGeometry(telemetry.position);
            byId("actualPath").setAttribute("d", actual.path);
            byId("actualJoint").setAttribute("cx", actual.jointX);
            byId("actualJoint").setAttribute("cy", actual.jointY);
            byId("actualTip").setAttribute("cx", actual.x);
            byId("actualTip").setAttribute("cy", actual.y);
        }
    }

    function setTargetLocked(locked) {
        targetLocked = locked;
        byId("targetMode").textContent = locked ? "ZIEL FIXIERT" : "ZIEL WÄHLEN";
        byId("stageHint").textContent = locked ? "Phantomarm folgt den Roboterkoordinaten" : "Maus bewegen · Klick oder Tippen fixiert";
        byId("robotStage").classList.toggle("target-locked", locked);
        byId("unlockTargetButton").disabled = !locked;
        byId("heightControl").disabled = locked;
        byId("gripperButton").disabled = locked;
    }

    function updateTelemetry(message) {
        if (!message || message.type !== "telemetry" || message.units !== "mm") return false;
        const position = message.position;
        if (!position || !["x", "y", "z"].every((key) => typeof position[key] === "number" && Number.isFinite(position[key]) && position[key] >= 0 && position[key] <= settings[`limit${key.toUpperCase()}`])) return false;
        telemetry = { position: { x: position.x, y: position.y, z: position.z } };
        byId("telemetryLabel").textContent = `· ${new Date().toLocaleTimeString("de-DE")} / Z ${position.z} mm`;
        renderRobot();
        return true;
    }

    function moveTarget(event) {
        if (targetLocked) return;
        if (event.pointerType !== "mouse" && event.type !== "pointerdown" && !event.buttons) return;
        const rect = byId("robotDrawing").getBoundingClientRect();
        const scale = Math.min(rect.width / 800, rect.height / 500);
        const offsetX = rect.left + (rect.width - 800 * scale) / 2;
        const offsetY = rect.top + (rect.height - 500 * scale) / 2;
        target.x = Math.round(Math.max(0, Math.min(1, ((event.clientX - offsetX) / scale - 90) / 620)) * settings.limitX);
        target.y = Math.round(Math.max(0, Math.min(1, (390 - (event.clientY - offsetY) / scale) / 280)) * settings.limitY);
        renderRobot();
    }
    byId("robotStage").addEventListener("pointermove", moveTarget);
    byId("robotStage").addEventListener("pointerdown", (event) => {
        if (event.isPrimary === false || (event.button !== undefined && event.button !== 0) || targetLocked) return;
        moveTarget(event);
        setTargetLocked(true);
    });
    byId("unlockTargetButton").addEventListener("click", () => setTargetLocked(false));
    byId("robotStage").addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            setTargetLocked(true);
            return;
        }
        if (targetLocked) return;
        const moves = { ArrowLeft: [-10, 0], ArrowRight: [10, 0], ArrowUp: [0, 10], ArrowDown: [0, -10] };
        if (!moves[event.key]) return;
        event.preventDefault();
        target.x = Math.max(0, Math.min(settings.limitX, target.x + moves[event.key][0]));
        target.y = Math.max(0, Math.min(settings.limitY, target.y + moves[event.key][1]));
        renderRobot();
    });
    byId("heightControl").addEventListener("input", () => {
        if (targetLocked) return;
        target.z = Number(byId("heightControl").value);
        renderRobot();
    });
    byId("homeButton").addEventListener("click", () => {
        setTargetLocked(false);
        target = { x: settings.limitX / 2, y: settings.limitY / 2, z: settings.limitZ / 2 };
        gripper = "open";
        applySettings();
    });
    byId("gripperButton").addEventListener("click", () => {
        if (targetLocked) return;
        gripper = gripper === "open" ? "closed" : "open";
        renderRobot();
    });

    function isConnected() {
        return socket && socket.readyState === WebSocket.OPEN;
    }
    function sendMessage(message) {
        if (!isConnected()) return false;
        if (socket.bufferedAmount > 256 * 1024) {
            notice("Backend verarbeitet Daten zu langsam. Nachricht wurde nicht gesendet.");
            return false;
        }
        try {
            socket.send(JSON.stringify(message));
            return true;
        } catch {
            notice("Nachricht konnte nicht gesendet werden.");
            return false;
        }
    }
    function updateStreamStatus() {
        const destinations = ["Browser-Events"];
        if (settings.forwardApi && validApiUrl()) destinations.push("Command-API · endgültiger Text");
        if (isConnected() && settings.forwardTranscript) destinations.push("WebSocket");
        byId("streamStatus").textContent = destinations.join(" + ");
        byId("apiStatus").textContent = !settings.apiUrl ? "API noch nicht konfiguriert." : settings.forwardApi && validApiUrl() ? `API aktiv · ${apiDelivered} bestätigt · ${apiQueue.length} warten` : "API-Weiterleitung pausiert.";
    }
    function updateConnection(text) {
        const connected = Boolean(isConnected());
        byId("headerConnection").textContent = connected ? "Backend verbunden" : "Lokal · nicht verbunden";
        byId("headerConnection").classList.toggle("connected", connected);
        byId("connectionStatus").textContent = text;
        byId("disconnectButton").disabled = !socket;
        byId("connectButton").disabled = Boolean(socket);
        byId("endpoint").disabled = Boolean(socket);
        byId("sendTargetButton").disabled = !connected;
        byId("targetStatus").textContent = connected ? "Ziele werden nur über den Button ans Backend gesendet." : "Nur lokale Vorschau. Keine Roboterverbindung.";
        updateStreamStatus();
    }
    function clearTelemetry() {
        telemetry = null;
        byId("telemetryLabel").textContent = "· keine Daten";
        renderRobot();
    }
    function disconnect() {
        clearTimeout(connectionTimer);
        const previous = socket;
        socket = null;
        if (previous) previous.close();
        clearTelemetry();
        updateConnection("Verbindung getrennt. Textstream bleibt lokal verfügbar.");
    }
    byId("connectButton").addEventListener("click", () => {
        if (socket) return;
        let url;
        try {
            url = new URL(byId("endpoint").value.trim());
            if (!["ws:", "wss:"].includes(url.protocol) || url.username || url.password) throw new Error("URL");
            if (location.protocol === "https:" && url.protocol !== "wss:") throw new Error("TLS");
        } catch {
            byId("connectionStatus").textContent = "Gültige ws://- oder wss://-Adresse eingeben. Auf HTTPS ist wss:// erforderlich. Zugangsdaten nicht in die URL schreiben.";
            return;
        }
        try {
            settings.endpoint = url.href;
            persistSettings();
            const activeSocket = new WebSocket(url.href);
            socket = activeSocket;
            clearTelemetry();
            updateConnection("Verbindung wird aufgebaut …");
            connectionTimer = setTimeout(() => {
                if (socket !== activeSocket || isConnected()) return;
                disconnect();
                updateConnection("Zeitüberschreitung. Adresse und Backend prüfen.");
            }, 8000);
            activeSocket.addEventListener("open", () => {
                if (socket !== activeSocket) return;
                clearTimeout(connectionTimer);
                updateConnection("WebSocket verbunden. Nachrichtenformat muss zum Backend passen.");
            });
            activeSocket.addEventListener("message", (event) => {
                if (socket !== activeSocket) return;
                try {
                    if (!updateTelemetry(JSON.parse(event.data))) notice("Backend-Nachricht nicht übernommen: gültige Telemetrie in mm erwartet.");
                } catch {
                    notice("Backend-Nachricht ist kein gültiges JSON.");
                }
            });
            activeSocket.addEventListener("error", () => {
                if (socket === activeSocket) updateConnection("Verbindungsfehler. Backend-Adresse und Erreichbarkeit prüfen.");
            });
            activeSocket.addEventListener("close", () => {
                if (socket !== activeSocket) return;
                clearTimeout(connectionTimer);
                socket = null;
                clearTelemetry();
                updateConnection("Verbindung geschlossen. Keine Nachrichten werden zwischengespeichert.");
            });
        } catch {
            socket = null;
            updateConnection("WebSocket konnte nicht gestartet werden.");
        }
    });
    byId("disconnectButton").addEventListener("click", disconnect);
    byId("sendTargetButton").addEventListener("click", () => {
        const message = { type: "target", position: { ...target }, gripper, units: "mm" };
        if (sendMessage(message)) {
            window.dispatchEvent(new CustomEvent("robot:target", { detail: message }));
            byId("targetStatus").textContent = "Ziel ans Backend gesendet. Ausführung nicht bestätigt.";
        } else byId("targetStatus").textContent = "Ziel nicht gesendet. Backend-Verbindung prüfen.";
    });

    function validApiUrl() {
        try {
            const url = new URL(settings.apiUrl);
            if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) return null;
            if (location.protocol === "https:" && url.protocol !== "https:") return null;
            return url;
        } catch {
            return null;
        }
    }

    function addApiLog(level, event, details = {}, requestKey = "") {
        const secrets = [byId("apiKey").value.trim(), requestKey].filter(Boolean);
        let text = JSON.stringify(details, (key, value) => {
            return /^(authorization|password|key|.*token|.*secret|.*api.?key)$/i.test(key) ? "[ausgeblendet]" : value;
        }, 2);
        for (const secret of secrets) text = text.split(secret).join("[ausgeblendet]");
        text = text.replace(/Bearer\s+[^\s"\\]+/gi, "Bearer [ausgeblendet]");
        const entry = `[${new Date().toLocaleTimeString("de-DE")}] ${level} · ${event}\n${text.slice(0, 12000)}`;
        apiLogEntries.push(entry);
        if (apiLogEntries.length > 100) apiLogEntries.shift();
        byId("apiLog").value = apiLogEntries.join("\n\n");
        byId("apiLog").scrollTop = byId("apiLog").scrollHeight;
        byId("apiLogSummary").textContent = `${apiLogEntries.length} Einträge · zuletzt: ${event}`;
        if (level === "FEHLER") byId("apiLogPanel").open = true;
    }

    function logUrl(address) {
        try {
            const url = new URL(address);
            url.username = "";
            url.password = "";
            for (const key of [...url.searchParams.keys()]) url.searchParams.set(key, "[ausgeblendet]");
            return url.href;
        } catch {
            return "Ungültige Adresse";
        }
    }

    function browserRequestContext(address) {
        const origin = location.origin || (location.protocol === "file:" ? "null" : "unbekannt");
        const crossOrigin = new URL(address).origin !== origin;
        return {
            frontendOrigin: origin,
            zielOrigin: new URL(address).origin,
            browserVorpruefung: crossOrigin
                ? "Browser prüft vor dem JSON-POST per OPTIONS (CORS). Bei Ablehnung wird der POST nicht gesendet."
                : "Gleiche Herkunft: keine CORS-Vorabprüfung erforderlich.",
            erwarteteFreigaben: crossOrigin ? {
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Methods": "POST",
                "Access-Control-Allow-Headers": byId("apiKey").value.trim() ? "content-type, authorization" : "content-type"
            } : null
        };
    }

    function cancelApiRequests() {
        if (apiRequest || apiQueue.length) {
            addApiLog("HINWEIS", "Versand abgebrochen", { offen: apiQueue.length, hinweis: "Bereits abgeschickte Befehle können vom Server trotzdem verarbeitet worden sein. Keine automatische Wiederholung." });
        }
        apiGeneration++;
        apiQueue = [];
        if (apiRequest) apiRequest.abort();
        apiRequest = null;
        manualPending = false;
        updateRecordingUI();
    }

    function pauseApi(message) {
        cancelApiRequests();
        settings.forwardApi = false;
        byId("forwardApi").checked = false;
        persistSettings();
        updateStreamStatus();
        byId("apiStatus").textContent = message;
        notice(message);
    }

    async function pumpApi() {
        if (apiRequest || !settings.forwardApi || !apiQueue.length) return;
        const item = apiQueue.shift();
        const generation = apiGeneration;
        const controller = new AbortController();
        apiRequest = controller;
        let timedOut = false;
        const started = Date.now();
        const timeout = setTimeout(() => { timedOut = true; controller.abort(); }, 30000);
        updateStreamStatus();
        const headers = { "Content-Type": "application/json" };
        if (item.key) headers.Authorization = `Bearer ${item.key}`;
        addApiLog("FETCH", `POST #${item.id} angefordert`, {
            url: logUrl(item.url), quelle: item.manual ? "Texteingabe" : "Spracherkennung",
            ...browserRequestContext(item.url), headers, body: JSON.parse(item.body),
            hinweis: "Dies protokolliert den fetch-Aufruf, nicht den bestätigten Eingang des POST am Backend."
        }, item.key);
        try {
            const response = await fetch(item.url, {
                method: "POST", headers, body: item.body, signal: controller.signal,
                credentials: "omit", redirect: "error"
            });
            if (generation !== apiGeneration) return;
            const body = await response.text();
            if (generation !== apiGeneration) return;
            let data;
            try { data = JSON.parse(body); } catch { data = null; }
            addApiLog(response.ok ? "ANTWORT" : "FEHLER", `HTTP ${response.status} · #${item.id}`, {
                url: logUrl(item.url), status: response.status, dauerMs: Date.now() - started,
                contentType: response.headers.get("content-type"), antwort: data || body.slice(0, 6000)
            }, item.key);
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            apiDelivered++;
            if (data?.type === "telemetry" && !updateTelemetry(data)) {
                addApiLog("HINWEIS", "Ungültige Telemetrie", { request: item.id });
            }
            notice(`Befehl #${item.id} von der API bestätigt (HTTP ${response.status}).`);
        } catch (error) {
            if (generation !== apiGeneration) return;
            const detail = error.message?.startsWith("HTTP ") ? error.message : timedOut
                ? "Zeitüberschreitung nach 30 Sekunden; Verarbeitung am Server unbekannt"
                : "Netzwerkfehler: Erreichbarkeit, CORS, Mixed Content oder Browser-Zugriff aufs lokale Netzwerk prüfen. Der Browser nennt keine eindeutige Ursache.";
            addApiLog("FEHLER", `Request #${item.id} fehlgeschlagen`, {
                url: logUrl(item.url), fehler: detail, browserMeldung: String(error.message || error.name),
                dauerMs: Date.now() - started,
                diagnose: error.message?.startsWith("HTTP ") ? "HTTP-Antwort im Log darüber prüfen."
                    : "Server-Log prüfen: OPTIONS /command 400 deutet auf abgelehnte CORS-Freigaben hin. POST /command 400 erfordert den Antworttext des Endpunkts. Ohne lesbare Antwort kann JavaScript dies nicht unterscheiden."
            }, item.key);
            pauseApi(`API pausiert: ${detail}. Keine automatische Wiederholung.`);
        } finally {
            clearTimeout(timeout);
            if (generation === apiGeneration) {
                apiRequest = null;
                if (item.manual) manualPending = false;
                updateRecordingUI();
                updateStreamStatus();
                void pumpApi();
            }
        }
    }

    function enqueueApi(message, manual = false) {
        if (message.type !== "transcript" || !message.isFinal || !message.text.trim()) return false;
        if (!settings.forwardApi) {
            addApiLog("HINWEIS", "Nicht gesendet: API pausiert", { quelle: manual ? "Texteingabe" : "Spracherkennung" });
            return false;
        }
        const url = validApiUrl();
        if (!url) {
            const reason = location.protocol === "https:" && settings.apiUrl.startsWith("http:")
                ? "Mixed Content: HTTPS-Seite darf die HTTP-API nicht aufrufen. Website über localhost öffnen oder API per HTTPS-Proxy bereitstellen."
                : "Gültige HTTP(S)-API-Adresse ohne Zugangsdaten erforderlich.";
            addApiLog("FEHLER", "Versand blockiert", { url: logUrl(settings.apiUrl), grund: reason });
            pauseApi(reason);
            return false;
        }
        if (apiQueue.length >= 100) {
            addApiLog("FEHLER", "Warteschlange voll", { offen: apiQueue.length });
            pauseApi("API pausiert: Zu viele offene Nachrichten.");
            return false;
        }
        const item = {
            id: ++apiRequestNumber, url: url.href, key: byId("apiKey").value.trim(), manual,
            body: JSON.stringify({ inputType: "text", data: message.text.trim() })
        };
        apiQueue.push(item);
        addApiLog("QUEUE", `Befehl #${item.id} vorgemerkt`, { url: logUrl(item.url), body: JSON.parse(item.body), offen: apiQueue.length });
        void pumpApi();
        return true;
    }

    byId("apiKey").addEventListener("input", () => {
        cancelApiRequests();
        updateStreamStatus();
    });

    function publish(message) {
        message.sequence = ++streamSequence;
        message.timestamp = new Date().toISOString();
        messageCount++;
        byId("streamCount").textContent = String(messageCount).padStart(3, "0");
        window.dispatchEvent(new CustomEvent(message.type === "transcript" ? "transcript:update" : message.type, { detail: message }));
        if (settings.forwardTranscript) sendMessage(message);
        enqueueApi(message);
    }

    function transcriptText(finalOnly = false) {
        const segments = session ? [...session.segments.values()].filter((item) => !finalOnly || item.isFinal).map((item) => item.text) : [];
        return [confirmedText, ...segments].filter(Boolean).join(" ").trim();
    }
    function displayTranscript(text) {
        visibleText = text;
        byId("log").value = text;
        byId("log").scrollTop = byId("log").scrollHeight;
        byId("transcriptCount").textContent = `${text.length} Zeichen`;
        updateRecordingUI();
    }
    function updateRecordingUI(status) {
        const busy = phase !== "idle" || wantsRecording;
        byId("startButton").disabled = !recognition || busy;
        byId("stopButton").disabled = !busy || phase === "stopping";
        byId("clearButton").disabled = busy;
        byId("log").readOnly = busy;
        byId("manualSendButton").disabled = busy || manualPending || !visibleText.trim();
        byId("manualSendButton").textContent = manualPending ? "Wird gesendet …" : "Text an API senden";
        byId("language").disabled = busy;
        document.body.classList.toggle("recording", phase === "recording");
        byId("recordingBadge").textContent = phase === "recording" ? "AUFNAHME" : busy ? "WARTE" : "BEREIT";
        if (status) byId("recordingStatus").textContent = status;
    }
    function finishSession() {
        if (!session) return;
        const id = session.id;
        confirmedText = transcriptText(true);
        session = null;
        displayTranscript(confirmedText);
        publish({ type: "transcript.end", sessionId: id, reason: endReason, fullText: confirmedText });
    }
    function beginRecognition() {
        if (!recognition || !wantsRecording || phase !== "idle") return;
        session = { id: window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`, segments: new Map() };
        endReason = "ended";
        speechError = false;
        phase = "starting";
        recognition.lang = settings.language;
        updateRecordingUI("Mikrofon wird gestartet …");
        try {
            recognition.start();
        } catch {
            wantsRecording = false;
            phase = "idle";
            endReason = "error";
            finishSession();
            updateRecordingUI("Aufnahme konnte nicht gestartet werden. Mikrofonzugriff und Browser prüfen.");
        }
    }
    function stopRecording(reason = "stopped") {
        wantsRecording = false;
        clearTimeout(restartTimer);
        endReason = reason;
        if (phase === "idle") {
            updateRecordingUI("Aufnahme beendet.");
            return;
        }
        phase = "stopping";
        updateRecordingUI("Aufnahme wird beendet …");
        try {
            recognition.stop();
        } catch {
            phase = "idle";
            finishSession();
            updateRecordingUI("Aufnahme beendet.");
        }
    }
    byId("startButton").addEventListener("click", () => {
        if (!recognition || wantsRecording || phase !== "idle") return;
        wantsRecording = true;
        beginRecognition();
    });
    byId("stopButton").addEventListener("click", () => stopRecording());

    if (recognition) {
        recognition.continuous = true;
        recognition.interimResults = true;
        recognition.maxAlternatives = 1;
        recognition.onstart = () => {
            if (!wantsRecording) {
                stopRecording();
                return;
            }
            phase = "recording";
            updateRecordingUI("Aufnahme läuft. Dein Text wird fortlaufend transkribiert.");
        };
        recognition.onresult = (event) => {
            if (!session) return;
            const changed = [];
            for (let index = event.resultIndex; index < event.results.length; index++) {
                const result = event.results[index];
                const next = { text: result[0].transcript.trim(), isFinal: result.isFinal };
                const previous = session.segments.get(index);
                session.segments.set(index, next);
                if (!previous || previous.text !== next.text || previous.isFinal !== next.isFinal) changed.push({ index, ...next });
            }
            displayTranscript(transcriptText());
            for (const item of changed) {
                if (!item.isFinal && !settings.sendInterim) continue;
                publish({
                    type: "transcript",
                    sessionId: session.id,
                    segmentId: `${session.id}:${item.index}`,
                    text: item.text,
                    isFinal: item.isFinal,
                    fullText: visibleText,
                    language: recognition.lang
                });
            }
        };
        recognition.onerror = (event) => {
            speechError = true;
            wantsRecording = false;
            endReason = "error";
            const messages = {
                "not-allowed": "Mikrofonzugriff verweigert. Berechtigung im Browser prüfen.",
                "service-not-allowed": "Spracherkennungsdienst ist in diesem Browser nicht erlaubt.",
                "audio-capture": "Kein nutzbares Mikrofon gefunden.",
                "no-speech": "Keine Sprache erkannt. Starte eine neue Aufnahme.",
                "network": "Verbindungsproblem beim Spracherkennungsdienst.",
                "aborted": "Aufnahme abgebrochen.",
                "language-not-supported": "Die ausgewählte Sprache wird nicht unterstützt."
            };
            updateRecordingUI(messages[event.error] || "Spracherkennung fehlgeschlagen. Bitte erneut starten.");
        };
        recognition.onend = () => {
            phase = "idle";
            finishSession();
            if (wantsRecording && settings.autoRestart && !document.hidden && !speechError) {
                updateRecordingUI("Browser hat die Aufnahme pausiert. Fortsetzung …");
                restartTimer = setTimeout(beginRecognition, 350);
            } else {
                wantsRecording = false;
                updateRecordingUI(speechError ? undefined : "Aufnahme beendet. Endgültiger Text bleibt erhalten.");
            }
        };
    }

    byId("log").addEventListener("input", () => {
        if (phase !== "idle" || wantsRecording) return;
        confirmedText = byId("log").value;
        visibleText = confirmedText;
        byId("transcriptCount").textContent = `${visibleText.length} Zeichen`;
        updateRecordingUI();
    });
    function sendManualCommand() {
        if (phase !== "idle" || wantsRecording || manualPending || !visibleText.trim()) return;
        if (!settings.forwardApi) {
            settings.forwardApi = true;
            byId("forwardApi").checked = true;
            persistSettings();
            updateStreamStatus();
            addApiLog("HINWEIS", "API für manuellen Versand aktiviert");
        }
        manualPending = true;
        if (!enqueueApi({ type: "transcript", text: visibleText, isFinal: true }, true)) manualPending = false;
        updateRecordingUI();
    }
    byId("manualSendButton").addEventListener("click", sendManualCommand);
    byId("log").addEventListener("keydown", (event) => {
        if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
            event.preventDefault();
            sendManualCommand();
        }
    });
    byId("clearApiLog").addEventListener("click", () => {
        apiLogEntries.length = 0;
        byId("apiLog").value = "";
        byId("apiLogSummary").textContent = "Log geleert.";
    });
    byId("copyApiLog").addEventListener("click", async () => {
        try {
            await navigator.clipboard.writeText(byId("apiLog").value);
            notice("API-Log kopiert.");
        } catch {
            byId("apiLog").focus();
            byId("apiLog").select();
            notice("Log ausgewählt. Mit Strg+C kopieren.");
        }
    });

    byId("clearButton").addEventListener("click", () => {
        if (phase !== "idle" || wantsRecording) return;
        confirmedText = "";
        displayTranscript("");
        publish({ type: "transcript.reset", fullText: "" });
        notice("Transkript geleert.");
    });
    byId("copyButton").addEventListener("click", async () => {
        if (!visibleText) return notice("Noch kein Text zum Kopieren vorhanden.");
        try {
            await navigator.clipboard.writeText(visibleText);
            notice("Transkript kopiert.");
        } catch {
            byId("log").focus();
            byId("log").select();
            notice("Text ausgewählt. Mit Strg+C oder der Kopierfunktion deines Geräts kopieren.");
        }
    });
    byId("downloadButton").addEventListener("click", () => {
        if (!visibleText) return notice("Noch kein Text zum Herunterladen vorhanden.");
        const url = URL.createObjectURL(new Blob([visibleText], { type: "text/plain;charset=utf-8" }));
        const link = document.createElement("a");
        link.href = url;
        link.download = "lenze-transkript.txt";
        link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
        notice("Textdatei zum Download bereitgestellt.");
    });
    byId("resetSettings").addEventListener("click", () => {
        stopRecording();
        disconnect();
        cancelApiRequests();
        byId("apiKey").value = "";
        settings = { ...defaults };
        persistSettings();
        loadSettings();
        notice("Standardwerte wiederhergestellt.");
    });
    document.addEventListener("visibilitychange", () => {
        if (document.hidden && (wantsRecording || phase !== "idle")) stopRecording("hidden");
    });
    window.addEventListener("pagehide", () => {
        stopRecording("closed");
        disconnect();
        cancelApiRequests();
        byId("apiKey").value = "";
    });

    window.motionInterface = {
        updateTelemetry,
        getTranscript: () => visibleText,
        getTarget: () => ({ type: "target", position: { ...target }, gripper, units: "mm" })
    };
    loadSettings();
    addApiLog("BEREIT", "Command-API initialisiert", { url: logUrl(settings.apiUrl), aktiv: settings.forwardApi, format: { inputType: "text", data: "…" } });
    setTargetLocked(false);
    updateConnection("Nicht verbunden. Text bleibt lokal verfügbar.");
    updateRecordingUI(recognition ? "Bereit für deine Aufnahme." : "Dieser Browser unterstützt keine Spracherkennung. Bitte Chrome oder Edge ausprobieren.");
})();
