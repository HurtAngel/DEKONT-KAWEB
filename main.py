import os
import uuid
import asyncio
from datetime import datetime
from typing import Dict, List, Set
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse
from google import genai

app = FastAPI(title="Watch DM & Gemini Engine")

# ----------------------------------------------------
# GEMINI CLIENT KURULUMU
# ----------------------------------------------------
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
gemini_client = genai.Client(api_key=GEMINI_KEY) if GEMINI_KEY else None

# ----------------------------------------------------
# IN-MEMORY DATABASE (RAM)
# ----------------------------------------------------
class InMemoryStore:
    def __init__(self):
        self.messages: List[Dict] = []
        self.devices: Dict[str, Dict] = {}
        self.banned_devices: Set[str] = set()
        self.system_logs: List[str] = []

        # Bot için sabit Gemini hesabı
        self.devices["DEV-GEMINI"] = {
            "nickname": "🤖 Gemini AI",
            "last_seen": "Her zaman aktif",
            "ip": "SYSTEM"
        }

    def log(self, message: str):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.system_logs.append(f"[{timestamp}] {message}")

db = InMemoryStore()

# ----------------------------------------------------
# GÖMÜLÜ TEK PARÇA ARAYÜZ (HTML + CSS + JS)
# ----------------------------------------------------
HTML_CONTENT = """
<!DOCTYPE html>
<html lang="tr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>Watch DM Engine</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, sans-serif; }
    body { background: #090a0f; color: #e2e8f0; height: 100dvh; display: flex; justify-content: center; align-items: center; }
    
    .app { width: 100%; max-width: 420px; height: 100dvh; background: #12121a; display: flex; flex-direction: column; position: relative; }
    
    .header { padding: 12px; background: #000; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #222; }
    .header h3 { font-size: 0.9rem; color: #a29bfe; }
    
    .user-bar { display: flex; gap: 8px; padding: 10px; background: #1a1a24; overflow-x: auto; border-bottom: 1px solid #222; }
    .user-chip { padding: 6px 12px; background: #252538; border-radius: 12px; font-size: 0.75rem; cursor: pointer; white-space: nowrap; border: 1px solid transparent; }
    .user-chip.active { background: #6c5ce7; color: white; border-color: #a29bfe; }
    
    .chat-area { flex: 1; padding: 12px; overflow-y: auto; display: flex; flex-direction: column; gap: 8px; }
    
    .msg { max-width: 80%; padding: 8px 12px; border-radius: 12px; font-size: 0.85rem; line-height: 1.3; }
    .msg.in { background: #252538; align-self: flex-start; }
    .msg.out { background: #6c5ce7; align-self: flex-end; }
    .msg.bot { background: #00b894; color: white; align-self: flex-start; border: 1px solid #55efc4; }
    .msg .sender-name { font-size: 0.65rem; font-weight: bold; color: #a29bfe; margin-bottom: 2px; }
    .msg.bot .sender-name { color: #ffeaa7; }
    .msg .time { font-size: 0.6rem; opacity: 0.6; text-align: right; margin-top: 3px; }
    
    .input-box { padding: 10px; background: #000; display: flex; gap: 8px; }
    .input-box input { flex: 1; padding: 10px; border-radius: 8px; border: 1px solid #333; background: #111; color: white; outline: none; }
    .input-box button { padding: 10px 16px; border-radius: 8px; border: none; background: #6c5ce7; color: white; font-weight: bold; cursor: pointer; }
    
    .ban-overlay { position: absolute; inset: 0; background: #000; color: #ff7675; display: none; flex-direction: column; justify-content: center; align-items: center; text-align: center; padding: 20px; z-index: 99; }
    
    .admin-modal { position: absolute; inset: 0; background: rgba(0,0,0,0.95); display: none; flex-direction: column; padding: 15px; z-index: 50; }
    .dev-item { display: flex; justify-content: space-between; align-items: center; padding: 8px; background: #222; margin-bottom: 5px; border-radius: 6px; font-size: 0.75rem; }
    .dev-btn { padding: 3px 8px; border-radius: 4px; border: none; color: white; cursor: pointer; font-size: 0.7rem; }
  </style>
</head>
<body>

<div class="app">
  <div class="header">
    <h3 id="myTitle">Giriş yapılıyor...</h3>
    <button style="background:#333; color:white; border:none; padding:4px 8px; border-radius:6px; cursor:pointer;" onclick="openAdmin()">Admin</button>
  </div>

  <div class="user-bar" id="userBar">
    <div class="user-chip active" onclick="selectUser('GLOBAL')">🌐 Genel</div>
  </div>

  <div class="chat-area" id="chatArea"></div>

  <div class="input-area input-box">
    <input type="text" id="msgInput" placeholder="@gemini sor veya mesaj yaz..." onkeypress="if(event.key==='Enter') sendMsg()">
    <button onclick="sendMsg()">Gönder</button>
  </div>

  <div class="ban-overlay" id="banOverlay">
    <h2>🚫 BANLANDINIZ</h2>
    <p style="margin-top:10px; color:#aaa;">Cihazınız yönetici tarafından engellendi.</p>
  </div>

  <div class="admin-modal" id="adminModal">
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
      <h4>Admin Paneli</h4>
      <button onclick="document.getElementById('adminModal').style.display='none'" style="background:none; color:white; border:none; cursor:pointer;">✕</button>
    </div>
    <div id="adminDevList" style="flex:1; overflow-y:auto;"></div>
    <div style="display:flex; gap:5px; margin-top:10px;">
      <button style="flex:1; padding:8px; background:#00b894; color:white; border:none; border-radius:6px;" onclick="downloadLog('txt')">TXT Log</button>
      <button style="flex:1; padding:8px; background:#0984e3; color:white; border:none; border-radius:6px;" onclick="downloadLog('json')">JSON Log</button>
    </div>
  </div>
</div>

<script>
  let myDevId = localStorage.getItem("dev_id") || "DEV-" + Math.random().toString(36).substring(2, 9);
  localStorage.setItem("dev_id", myDevId);

  let activeTarget = "GLOBAL";
  let adminPass = "";

  async function apiCall(url, method="GET", body=null) {
    const headers = { "Content-Type": "application/json", "X-Device-Id": myDevId };
    const opts = { method, headers };
    if(body) opts.body = JSON.stringify(body);
    const res = await fetch(url, opts);
    if(res.status === 403) {
      document.getElementById("banOverlay").style.display = "flex";
      throw new Error("Banned");
    }
    return res.json();
  }

  async function init() {
    try {
      const data = await apiCall("/api/init");
      document.getElementById("myTitle").innerText = "Ben: " + data.nickname;
      setInterval(fetchData, 1500);
      fetchData();
    } catch(e) {}
  }

  async function fetchData() {
    try {
      const data = await apiCall("/api/sync?target=" + activeTarget);
      
      // Kendi ismimiz güncellendiyse başlığı güncelle
      if(data.myNickname) {
        document.getElementById("myTitle").innerText = "Ben: " + data.myNickname;
      }

      const uBar = document.getElementById("userBar");
      let html = `<div class="user-chip ${activeTarget==='GLOBAL'?'active':''}" onclick="selectUser('GLOBAL')">🌐 Genel</div>`;
      
      data.users.forEach(u => {
        if(u.id !== myDevId) {
          html += `<div class="user-chip ${activeTarget===u.id?'active':''}" onclick="selectUser('${u.id}')">💬 ${u.nickname}</div>`;
        }
      });
      uBar.innerHTML = html;

      const cArea = document.getElementById("chatArea");
      cArea.innerHTML = "";
      data.messages.forEach(m => {
        const isMe = m.sender === myDevId;
        const isBot = m.sender === "DEV-GEMINI";
        
        const div = document.createElement("div");
        div.className = `msg ${isBot ? 'bot' : (isMe ? 'out' : 'in')}`;
        
        let senderHeader = !isMe ? `<div class="sender-name">${escapeHtml(m.senderName)}</div>` : '';
        div.innerHTML = `${senderHeader}<div>${escapeHtml(m.text)}</div><div class="time">${m.timestamp}</div>`;
        cArea.appendChild(div);
      });
      cArea.scrollTop = cArea.scrollHeight;

    } catch(e) {}
  }

  function selectUser(targetId) {
    activeTarget = targetId;
    fetchData();
  }

  async function sendMsg() {
    const inp = document.getElementById("msgInput");
    const txt = inp.value.trim();
    if(!txt) return;

    inp.value = "";
    await apiCall("/api/send", "POST", { target: activeTarget, text: txt });
    fetchData();
  }

  async function openAdmin() {
    const pwd = prompt("Admin Şifresi:");
    if(!pwd) return;
    adminPass = pwd;
    loadAdminData();
  }

  async function loadAdminData() {
    try {
      const data = await fetch(`/api/admin/devices?auth=${adminPass}`).then(r => r.json());
      document.getElementById("adminModal").style.display = "flex";
      
      const list = document.getElementById("adminDevList");
      list.innerHTML = "";
      data.devices.forEach(d => {
        if(d.id === "DEV-GEMINI") return;
        list.innerHTML += `
          <div class="dev-item">
            <div><b>${escapeHtml(d.nickname)}</b><br><small style="color:#aaa">${d.id}</small></div>
            <div style="display:flex; gap:4px;">
              <button class="dev-btn" style="background:#0984e3;" onclick="setNick('${d.id}')">İsim Değiş</button>
              ${d.banned ? '<span style="color:red">BANLI</span>' : `<button class="dev-btn" style="background:red;" onclick="ban('${d.id}')">BANLA</button>`}
            </div>
          </div>
        `;
      });
    } catch(e) { alert("Hatalı Şifre!"); }
  }

  async function setNick(id) {
    const newNick = prompt("Yeni Kullanıcı Adı:");
    if(!newNick) return;
    await fetch("/api/admin/set-nickname", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ password: adminPass, targetId: id, nickname: newNick })
    });
    loadAdminData();
  }

  async function ban(id) {
    await fetch("/api/admin/ban", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ password: adminPass, targetId: id })
    });
    loadAdminData();
  }

  function downloadLog(type) {
    window.open(`/api/admin/logs/${type}?password=${adminPass}`, '_blank');
  }

  function escapeHtml(t) { return (t||'').replace(/</g, "&lt;").replace(/>/g, "&gt;"); }

  window.onload = init;
</script>
</body>
</html>
"""

# ----------------------------------------------------
# HELPER: GEMINI YANIT ÜRETİCİSİ
# ----------------------------------------------------
async def process_gemini_reply(user_prompt: str, target_room: str):
    """Arka planda Gemini'den cevap alıp ilgili odaya yazar."""
    if not gemini_client:
        reply_text = "⚠️ Gemini API Key henüz ayarlanmamış (GEMINI_API_KEY eksik)."
    else:
        try:
            # Komut alanını temizle
            clean_prompt = user_prompt.replace("@gemini", "").strip()
            if not clean_prompt:
                clean_prompt = "Merhaba"
            
            response = gemini_client.models.generate_content(
                model="gemini-2.5-flash",
                contents=clean_prompt,
            )
            reply_text = response.text
        except Exception as e:
            reply_text = f"⚠️ Gemini Hatanın Detayı: {str(e)}"

    # Bot cevabını sohbet havuzuna ekle
    msg = {
        "id": str(uuid.uuid4()),
        "sender": "DEV-GEMINI",
        "receiver": target_room,
        "text": reply_text,
        "timestamp": datetime.now().strftime("%H:%M:%S")
    }
    db.messages.append(msg)
    db.log(f"GEMINI BOT RESPONDED [{target_room}]: {reply_text[:50]}...")

# ----------------------------------------------------
# API ENDPOINTS
# ----------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def get_index():
    return HTML_CONTENT

@app.get("/api/init")
async def init_device(request: Request):
    dev_id = request.headers.get("X-Device-Id", "DEV-UNKNOWN")
    client_ip = request.client.host if request.client else "127.0.0.1"

    if dev_id in db.banned_devices:
        raise HTTPException(status_code=403, detail="Banned")

    if dev_id not in db.devices:
        db.devices[dev_id] = {
            "nickname": f"User_{dev_id[-4:]}",
            "last_seen": datetime.now().strftime("%H:%M:%S"),
            "ip": client_ip
        }
        db.log(f"Yeni cihaz kaydoldu: {dev_id} ({db.devices[dev_id]['nickname']})")

    return {"nickname": db.devices[dev_id]["nickname"]}

@app.get("/api/sync")
async def sync_data(request: Request, target: str = "GLOBAL"):
    dev_id = request.headers.get("X-Device-Id")
    if dev_id in db.banned_devices:
        raise HTTPException(status_code=403, detail="Banned")

    if dev_id in db.devices:
        db.devices[dev_id]["last_seen"] = datetime.now().strftime("%H:%M:%S")

    # Kullanıcı Listesi
    users = [{"id": k, "nickname": v["nickname"]} for k, v in db.devices.items()]

    # Mesajları Filtrele & Gönderen İsmini Ekle
    filtered_msgs = []
    for m in db.messages:
        show = False
        if target == "GLOBAL" and m["receiver"] == "GLOBAL":
            show = True
        elif target != "GLOBAL":
            if (m["sender"] == dev_id and m["receiver"] == target) or \
               (m["sender"] == target and m["receiver"] == dev_id) or \
               (m["sender"] == "DEV-GEMINI" and m["receiver"] == target):
                show = True

        if show:
            msg_copy = dict(m)
            sender_info = db.devices.get(m["sender"], {})
            msg_copy["senderName"] = sender_info.get("nickname", "Bilinmeyen")
            filtered_msgs.append(msg_copy)

    my_nick = db.devices.get(dev_id, {}).get("nickname", "")
    return {"users": users, "messages": filtered_msgs, "myNickname": my_nick}

@app.post("/api/send")
async def send_message(request: Request, payload: dict):
    dev_id = request.headers.get("X-Device-Id")
    if dev_id in db.banned_devices:
        raise HTTPException(status_code=403, detail="Banned")

    text = payload.get("text", "").strip()
    target = payload.get("target", "GLOBAL")

    if text:
        msg = {
            "id": str(uuid.uuid4()),
            "sender": dev_id,
            "receiver": target,
            "text": text,
            "timestamp": datetime.now().strftime("%H:%M:%S")
        }
        db.messages.append(msg)
        db.log(f"MESAJ [{dev_id} -> {target}]: {text}")

        # @gemini Komutu Kontrolü
        if "@gemini" in text.lower():
            # Yanıtı kilitlenmeden asenkron arka planda üret
            asyncio.create_task(process_gemini_reply(text, target))

    return {"status": "ok"}

# ----------------------------------------------------
# ADMIN ENDPOINTS
# ----------------------------------------------------

@app.get("/api/admin/devices")
async def get_admin_devices(auth: str):
    if auth != "456456456":
        raise HTTPException(status_code=403, detail="Yetkisiz")
    
    devs = []
    for k, v in db.devices.items():
        devs.append({
            "id": k,
            "nickname": v["nickname"],
            "ip": v["ip"],
            "banned": k in db.banned_devices
        })
    return {"devices": devs}

@app.post("/api/admin/set-nickname")
async def set_nickname(payload: dict):
    if payload.get("password") != "456456456":
        raise HTTPException(status_code=403, detail="Yetkisiz")
    
    target_id = payload.get("targetId")
    new_nick = payload.get("nickname", "").strip()

    if target_id in db.devices and new_nick:
        old_nick = db.devices[target_id]["nickname"]
        db.devices[target_id]["nickname"] = new_nick
        db.log(f"ADMIN RENAMED: {target_id} ({old_nick} -> {new_nick})")
        return {"status": "ok"}
    return {"status": "error"}

@app.post("/api/admin/ban")
async def ban_device(payload: dict):
    if payload.get("password") != "456456456":
        raise HTTPException(status_code=403, detail="Yetkisiz")
    
    target_id = payload.get("targetId")
    if target_id:
        db.banned_devices.add(target_id)
        db.log(f"ADMIN BAN: {target_id}")
        return {"status": "ok"}

@app.get("/api/admin/logs/txt", response_class=PlainTextResponse)
async def get_txt_logs(password: str):
    if password != "456456456":
        raise HTTPException(status_code=403, detail="Yetkisiz")
    return "\n".join(db.system_logs)

@app.get("/api/admin/logs/json")
async def get_json_logs(password: str):
    if password != "456456456":
        raise HTTPException(status_code=403, detail="Yetkisiz")
    return {
        "logs": db.system_logs,
        "messages": db.messages,
        "devices": db.devices,
        "banned": list(db.banned_devices)
    }
