import uuid
from datetime import datetime
from typing import Dict, List, Set
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse

app = FastAPI(title="Smartwatch Realtime Chat Engine")

# ----------------------------------------------------
# IN-MEMORY DATABASE (Sadece RAM'de Tutulur)
# ----------------------------------------------------
class InMemoryStore:
    def __init__(self):
        self.messages: List[Dict] = []
        self.devices: Dict[str, Dict] = {}
        self.banned_devices: Set[str] = set()
        self.system_logs: List[str] = []

    def log(self, message: str):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{timestamp}] {message}"
        self.system_logs.append(entry)

db = InMemoryStore()

# ----------------------------------------------------
# WEBSOCKET MANAGER
# ----------------------------------------------------
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, device_id: str):
        await websocket.accept()
        self.active_connections[device_id] = websocket

    def disconnect(self, device_id: str):
        if device_id in self.active_connections:
            del self.active_connections[device_id]

    async def broadcast(self, message: dict):
        for connection in self.active_connections.values():
            await connection.send_json(message)

    async def kick_device(self, device_id: str):
        if device_id in self.active_connections:
            ws = self.active_connections[device_id]
            await ws.send_json({"type": "BANNED", "reason": "Cihazınız yönetici tarafından engellendi."})
            await ws.close()
            self.disconnect(device_id)

manager = ConnectionManager()

# ----------------------------------------------------
# HTML ARAYÜZÜ (GÖMÜLÜ)
# ----------------------------------------------------
HTML_CONTENT = """
<!DOCTYPE html>
<html lang="tr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>Smartwatch & Web Chat Engine</title>
  <style>
    :root {
      --bg: #090a0f;
      --card-bg: rgba(255, 255, 255, 0.05);
      --accent: #6c5ce7;
      --accent-glow: rgba(108, 92, 231, 0.4);
      --text: #e2e8f0;
      --danger: #ff7675;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    html, body { background: var(--bg); color: var(--text); height: 100dvh; width: 100vw; overflow: hidden; display: flex; justify-content: center; align-items: center; }

    .app-wrapper {
      width: 100%; max-width: 420px; height: 100dvh;
      background: rgba(18, 18, 26, 0.95); backdrop-filter: blur(20px);
      display: flex; flex-direction: column; overflow: hidden; position: relative;
    }

    .header {
      padding: 14px 16px; background: rgba(0,0,0,0.4); border-bottom: 1px solid rgba(255,255,255,0.08);
      display: flex; justify-content: space-between; align-items: center; flex-shrink: 0;
    }
    .header h2 { font-size: 0.95rem; font-weight: 600; letter-spacing: 0.5px; }
    .admin-btn {
      background: var(--card-bg); border: 1px solid rgba(255,255,255,0.2);
      color: var(--text); padding: 5px 12px; border-radius: 12px; cursor: pointer;
      font-size: 0.75rem; transition: all 0.3s ease;
    }
    .admin-btn:hover { background: var(--accent); border-color: var(--accent); box-shadow: 0 0 10px var(--accent-glow); }

    .chat-body { flex: 1; padding: 16px; overflow-y: auto; display: flex; flex-direction: column; gap: 12px; }
    .chat-body::-webkit-scrollbar { width: 4px; }
    .chat-body::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 4px; }

    .msg-bubble {
      max-width: 80%; padding: 10px 14px; border-radius: 16px; font-size: 0.85rem;
      line-height: 1.4; word-break: break-word; animation: popIn 0.3s ease-out forwards;
      position: relative;
    }
    @keyframes popIn {
      from { opacity: 0; transform: translateY(10px) scale(0.95); }
      to { opacity: 1; transform: translateY(0) scale(1); }
    }

    .msg-incoming { background: rgba(255,255,255,0.08); align-self: flex-start; border-bottom-left-radius: 4px; }
    .msg-outgoing { background: var(--accent); color: #fff; align-self: flex-end; border-bottom-right-radius: 4px; box-shadow: 0 4px 12px var(--accent-glow); }

    .msg-meta { font-size: 0.65rem; opacity: 0.6; margin-top: 4px; text-align: right; }
    .msg-sender { font-size: 0.7rem; font-weight: bold; color: #a29bfe; margin-bottom: 2px; }

    .input-area { padding: 12px; background: rgba(0,0,0,0.5); border-top: 1px solid rgba(255,255,255,0.05); display: flex; gap: 8px; flex-shrink: 0; }
    .input-area input {
      flex: 1; background: var(--card-bg); border: 1px solid rgba(255,255,255,0.1);
      padding: 12px 16px; border-radius: 20px; color: var(--text); outline: none; font-size: 0.85rem;
    }
    .input-area button {
      background: var(--accent); border: none; color: white; padding: 0 18px;
      border-radius: 20px; font-weight: bold; cursor: pointer; transition: 0.2s;
    }
    .input-area button:active { transform: scale(0.95); }

    .modal {
      position: absolute; top: 0; left: 0; width: 100%; height: 100%;
      background: rgba(0,0,0,0.9); backdrop-filter: blur(10px);
      display: none; flex-direction: column; padding: 20px; z-index: 100;
    }

    .modal-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
    .close-btn { background: none; border: none; color: var(--text); font-size: 1.2rem; cursor: pointer; }

    .device-list { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 8px; margin-bottom: 15px; }
    .device-card {
      background: var(--card-bg); padding: 10px 14px; border-radius: 12px;
      display: flex; justify-content: space-between; align-items: center; font-size: 0.8rem;
    }
    .ban-btn { background: var(--danger); border: none; color: white; padding: 4px 8px; border-radius: 6px; cursor: pointer; font-size: 0.7rem; }
    
    .admin-controls { display: flex; gap: 8px; }
    .admin-controls button { flex: 1; padding: 10px; border-radius: 8px; border: none; cursor: pointer; font-weight: bold; font-size: 0.75rem; }

    .ban-screen {
      position: absolute; top: 0; left: 0; width: 100%; height: 100%;
      background: #090a0f; color: var(--danger); display: none;
      flex-direction: column; justify-content: center; align-items: center;
      text-align: center; padding: 20px; z-index: 200;
    }
  </style>
</head>
<body>

<div class="app-wrapper">
  <div class="header">
    <h2>⚡ Watch Live Chat</h2>
    <button class="admin-btn" onclick="openAdminPrompt()">Admin</button>
  </div>

  <div class="chat-body" id="chatBody"></div>

  <div class="input-area">
    <input type="text" id="msgInput" placeholder="Mesaj yazın..." onkeypress="handleKeyPress(event)">
    <button onclick="sendMessage()">Gönder</button>
  </div>

  <div class="modal" id="adminModal">
    <div class="modal-header">
      <h3>Yönetici Paneli</h3>
      <button class="close-btn" onclick="closeAdminModal()">✕</button>
    </div>
    <div class="device-list" id="deviceList"></div>
    <div class="admin-controls">
      <button style="background: #00b894; color: white;" onclick="downloadLogs('txt')">TXT Log İndir</button>
      <button style="background: #0984e3; color: white;" onclick="downloadLogs('json')">JSON Log İndir</button>
    </div>
  </div>

  <div class="ban-screen" id="banScreen">
    <h1>🚫 CİHAZ YASAKLANDI</h1>
    <p style="color: #aaa; margin-top: 10px; font-size: 0.85rem;" id="banReason">Bu cihaza Donanım Banı uygulanmıştır.</p>
  </div>
</div>

<script>
  let deviceId = "";
  let ws = null;
  let adminPassword = "";

  function generateFingerprint() {
    let fp = localStorage.getItem("dev_fp");
    if (!fp) {
      const raw = navigator.userAgent + navigator.hardwareConcurrency + screen.width + "x" + screen.height;
      fp = 'DEV-' + btoa(raw).replace(/=/g, '').substring(0, 16);
      localStorage.setItem("dev_fp", fp);
    }
    return fp;
  }

  async function initApp() {
    deviceId = generateFingerprint();
    
    try {
      const res = await fetch("/api/init", {
        headers: { "X-Device-Fingerprint": deviceId }
      });
      const data = await res.json();

      if (data.banned) {
        showBanScreen(data.reason);
        return;
      }

      const chatBody = document.getElementById("chatBody");
      chatBody.innerHTML = "";
      if (data.history) data.history.forEach(appendMessage);

      connectWebSocket();

    } catch (err) {
      console.error("Başlatma hatası:", err);
    }
  }

  function connectWebSocket() {
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${protocol}//${location.host}/ws/chat/${deviceId}`);

    ws.onmessage = (event) => {
      const payload = JSON.parse(event.data);
      if (payload.type === "NEW_MESSAGE") {
        appendMessage(payload.data);
      } else if (payload.type === "BANNED") {
        showBanScreen(payload.reason);
      }
    };

    ws.onclose = () => {
      setTimeout(connectWebSocket, 3000);
    };
  }

  function appendMessage(msg) {
    const chatBody = document.getElementById("chatBody");
    const isOut = msg.deviceId === deviceId;
    
    const bubble = document.createElement("div");
    bubble.className = `msg-bubble ${isOut ? 'msg-outgoing' : 'msg-incoming'}`;
    
    let content = "";
    if (!isOut) {
      content += `<div class="msg-sender">${msg.nickname}</div>`;
    }
    content += `<div>${escapeHtml(msg.text)}</div><div class="msg-meta">${msg.timestamp}</div>`;
    
    bubble.innerHTML = content;
    chatBody.appendChild(bubble);
    chatBody.scrollTop = chatBody.scrollHeight;
  }

  function sendMessage() {
    const input = document.getElementById("msgInput");
    const text = input.value.trim();
    if (text && ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ text }));
      input.value = "";
    }
  }

  function handleKeyPress(e) {
    if (e.key === "Enter") sendMessage();
  }

  async function openAdminPrompt() {
    const pwd = prompt("Admin Şifresini Girin:");
    if (!pwd) return;

    const res = await fetch("/api/admin/auth", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: pwd })
    });

    if (res.ok) {
      adminPassword = pwd;
      document.getElementById("adminModal").style.display = "flex";
      loadAdminDevices();
    } else {
      alert("Hatalı Şifre!");
    }
  }

  async function loadAdminDevices() {
    const res = await fetch(`/api/admin/devices?auth=${adminPassword}`);
    const data = await res.json();
    const list = document.getElementById("deviceList");
    list.innerHTML = "";

    data.devices.forEach(dev => {
      const card = document.createElement("div");
      card.className = "device-card";
      card.innerHTML = `
        <div>
          <strong>${dev.nickname}</strong> (${dev.ip})<br>
          <small style="color:#aaa">${dev.deviceId}</small>
        </div>
        ${dev.isBanned ? 
          '<span style="color:var(--danger)">BANLI</span>' : 
          `<button class="ban-btn" onclick="banDevice('${dev.deviceId}')">BANLA</button>`
        }
      `;
      list.appendChild(card);
    });
  }

  async function banDevice(targetId) {
    if (!confirm("Bu cihazı banlamak istediğinizden emin misiniz?")) return;

    await fetch("/api/admin/ban", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: adminPassword, targetDeviceId: targetId })
    });

    loadAdminDevices();
  }

  function downloadLogs(type) {
    window.open(`/api/admin/logs/${type}?password=${adminPassword}`, '_blank');
  }

  function closeAdminModal() {
    document.getElementById("adminModal").style.display = "none";
  }

  function showBanScreen(reason) {
    if (ws) ws.close();
    document.getElementById("banReason").innerText = reason || "Erişiminiz engellendi.";
    document.getElementById("banScreen").style.display = "flex";
  }

  function escapeHtml(text) {
    return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  window.onload = initApp;
</script>
</body>
</html>
"""

# ----------------------------------------------------
# ENDPOINTS
# ----------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def get_index():
    return HTML_CONTENT

@app.get("/api/init")
async def init_device(request: Request):
    client_ip = request.client.host if request.client else "127.0.0.1"
    device_id = request.headers.get("X-Device-Fingerprint", "DEV-DEFAULT")

    if device_id in db.banned_devices:
        return JSONResponse({"banned": True, "reason": "Cihazınız sistemden yasaklanmıştır."}, status_code=403)

    if device_id not in db.devices:
        nickname = f"User_{device_id[:5]}"
        db.devices[device_id] = {
            "nickname": nickname,
            "joined_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "ip": client_ip
        }
        db.log(f"Yeni cihaz kaydoldu: {device_id} ({nickname}) - IP: {client_ip}")

    return {
        "banned": False,
        "deviceId": device_id,
        "nickname": db.devices[device_id]["nickname"],
        "history": db.messages
    }

@app.post("/api/admin/auth")
async def admin_auth(payload: dict):
    password = payload.get("password")
    if password == "456456456":
        db.log("Başarılı Admin girişi yapıldı.")
        return {"success": True}
    db.log("Hatalı Admin giriş denemesi!")
    return JSONResponse({"success": False, "error": "Geçersiz Parola"}, status_code=401)

@app.get("/api/admin/devices")
async def get_admin_devices(auth: str = ""):
    if auth != "456456456":
        raise HTTPException(status_code=403, detail="Yetkisiz erişim")
    
    device_list = []
    for dev_id, info in db.devices.items():
        device_list.append({
            "deviceId": dev_id,
            "nickname": info["nickname"],
            "joinedAt": info["joined_at"],
            "ip": info["ip"],
            "isBanned": dev_id in db.banned_devices,
            "isOnline": dev_id in manager.active_connections
        })
    return {"devices": device_list}

@app.post("/api/admin/ban")
async def ban_device(payload: dict):
    if payload.get("password") != "456456456":
        raise HTTPException(status_code=403, detail="Yetkisiz işlem")
    
    target_id = payload.get("targetDeviceId")
    if target_id:
        db.banned_devices.add(target_id)
        db.log(f"ADMIN BAN: {target_id} cihazı yasaklandı.")
        await manager.kick_device(target_id)
        return {"success": True, "message": f"{target_id} başarıyla banlandı."}
    return {"success": False, "error": "Hedef ID bulunamadı."}

@app.get("/api/admin/logs/txt", response_class=PlainTextResponse)
async def download_logs_txt(password: str):
    if password != "456456456":
        raise HTTPException(status_code=403, detail="Yetkisiz işlem")
    return "\n".join(db.system_logs)

@app.get("/api/admin/logs/json")
async def download_logs_json(password: str):
    if password != "456456456":
        raise HTTPException(status_code=403, detail="Yetkisiz işlem")
    return {
        "logs": db.system_logs,
        "total_messages": len(db.messages),
        "messages": db.messages,
        "devices": db.devices,
        "banned_devices": list(db.banned_devices)
    }

# ----------------------------------------------------
# WEBSOCKET CHAT ENDPOINT
# ----------------------------------------------------
@app.websocket("/ws/chat/{device_id}")
async def websocket_chat(websocket: WebSocket, device_id: str):
    if device_id in db.banned_devices:
        await websocket.close(code=4003)
        return

    await manager.connect(websocket, device_id)
    db.log(f"WebSocket bağlantısı sağlandı: {device_id}")

    try:
        while True:
            data = await websocket.receive_json()
            
            if device_id in db.banned_devices:
                await manager.kick_device(device_id)
                break

            msg_text = data.get("text", "").strip()
            if msg_text:
                msg_payload = {
                    "id": str(uuid.uuid4()),
                    "deviceId": device_id,
                    "nickname": db.devices.get(device_id, {}).get("nickname", "Bilinmeyen"),
                    "text": msg_text,
                    "timestamp": datetime.now().strftime("%H:%M:%S")
                }
                
                db.messages.append(msg_payload)
                db.log(f"CHAT [{msg_payload['nickname']}]: {msg_text}")
                await manager.broadcast({"type": "NEW_MESSAGE", "data": msg_payload})

    except WebSocketDisconnect:
        manager.disconnect(device_id)
        db.log(f"WebSocket ayrıldı: {device_id}")
