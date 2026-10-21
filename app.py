import os
import json
import random
import string
import io
import math
from datetime import datetime

import requests
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from flask import Flask, render_template, request, jsonify, session
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dekontika_gizli_anahtar_123")

DB_FILE = "bank_db.json"
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

# --- VERİTABANI YÖNETİMİ ---
def load_db():
    if not os.path.exists(DB_FILE):
        return {
            "users": {
                "admin": {
                    "ad_soyad": "Sistem Yöneticisi",
                    "password": "123",
                    "bakiye": 0.0,
                    "iban": "TR000000000000000000000000",
                    "kart_no": "4444 0000 0000 0000",
                    "skt": "12/99",
                    "cvv": "000",
                    "kredi_borcu": 0.0,
                    "kredi_notu": 2000,
                    "durum": "aktif",
                    "is_admin": True
                }
            },
            "kasa": 0.0,
            "bekleyen_talepler": {},
            "ayarlar": {
                "komisyon_odeme": 0.60,
                "komisyon_cekme": 0.10,
                "komisyon_havale": 0.05,
                "havale_sinir": 100.0,
                "kredi_faiz": 0.05,
                "gecikme_cezasi": 50.0
            }
        }
    with open(DB_FILE, "r", encoding="utf-8") as f:
        db = json.load(f)
        # Admin hesabı yoksa ekle
        if "admin" not in db["users"]:
            db["users"]["admin"] = {
                "ad_soyad": "Sistem Yöneticisi",
                "password": "123",
                "bakiye": 0.0,
                "iban": "TR000000000000000000000000",
                "kart_no": "4444 0000 0000 0000",
                "skt": "12/99",
                "cvv": "000",
                "kredi_borcu": 0.0,
                "kredi_notu": 2000,
                "durum": "aktif",
                "is_admin": True
            }
        return db

def save_db(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

def auto_backup_discord():
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        with open(DB_FILE, "rb") as f:
            files = {"file": ("bank_db_yedek.json", f, "application/json")}
            payload = {"content": f"📂 **[OTOMATİK YEDEK]** Veritabanı Güncellendi ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})"}
            requests.post(DISCORD_WEBHOOK_URL, data=payload, files=files)
    except Exception as e:
        print(f"Backup hatasi: {e}")

# --- YARDIMCI FONKSİYONLAR ---
def generate_iban():
    return "TR" + "".join([str(random.randint(0, 9)) for _ in range(24)])

def generate_card_number():
    return "4444 " + " ".join(["".join([str(random.randint(0, 9)) for _ in range(4)]) for _ in range(3)])

def generate_req_id():
    return "REQ-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))

def clean_iban(raw_iban: str) -> str:
    cleaned = raw_iban.strip().upper().replace(" ", "")
    if not cleaned.startswith("TR"):
        cleaned = "TR" + cleaned
    return cleaned

def clean_card_number(raw_card: str) -> str:
    digits = "".join(filter(str.isdigit, raw_card))
    return " ".join([digits[i:i+4] for i in range(0, len(digits), 4)])

def validate_positive_amount(val_str):
    try:
        val = float(str(val_str).replace(",", "."))
        if math.isnan(val) or math.isinf(val) or val <= 0: return None
        return round(val, 2)
    except ValueError:
        return None

# --- ZAMANLANMIŞ FAİZ DÖNGÜSÜ ---
def faiz_isle_job():
    db = load_db()
    for email, u_data in db["users"].items():
        if u_data.get("kredi_borcu", 0) > 0:
            u_data["kredi_gun_sayisi"] = u_data.get("kredi_gun_sayisi", 0) + 1
            gun = u_data["kredi_gun_sayisi"]
            if gun % 7 == 0:
                faiz_orani = db["ayarlar"].get("kredi_faiz", 0.05)
                ceza = db["ayarlar"].get("gecikme_cezasi", 50.0)
                u_data["kredi_borcu"] = round((u_data["kredi_borcu"] * (1 + faiz_orani)) + ceza, 2)
                u_data["kredi_notu"] = max(0, u_data.get("kredi_notu", 1000) - 25)
    save_db(db)
    auto_backup_discord()

scheduler = BackgroundScheduler()
scheduler.add_job(faiz_isle_job, 'interval', hours=24)
scheduler.start()

# --- ENDPOINTLER ---

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    email = data.get("email", "").strip().lower()
    password = data.get("password")

    # Sabit Admin Girişi Kontrolü (Admin / 123)
    if email in ["admin", "admin@dekontika.com"] and password == "123":
        session['user_email'] = "admin"
        return jsonify({"status": "success", "email": "admin", "is_admin": True})

    db = load_db()
    user = db["users"].get(email)

    if not user or user["password"] != password:
        return jsonify({"status": "error", "message": "E-posta veya şifre hatalı."}), 400

    if user.get("durum") == "dondurulmus":
        return jsonify({"status": "error", "message": "Hesabınız bloke edilmiştir."}), 403

    session['user_email'] = email
    return jsonify({"status": "success", "email": email, "is_admin": user.get("is_admin", False)})

@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    email = data.get("email", "").strip().lower()
    password = data.get("password")
    ad_soyad = data.get("ad_soyad", "").strip()

    if not email or not password or not ad_soyad:
        return jsonify({"status": "error", "message": "Tüm alanları doldurun."}), 400

    db = load_db()
    if email in db["users"]:
        return jsonify({"status": "error", "message": "Bu e-posta adresi zaten kayıtlı."}), 400

    db["users"][email] = {
        "ad_soyad": ad_soyad,
        "password": password,
        "bakiye": 0.0,
        "iban": generate_iban(),
        "kart_no": generate_card_number(),
        "skt": "12/28",
        "cvv": str(random.randint(100, 999)),
        "kredi_borcu": 0.0,
        "kredi_notu": 1000,
        "kredi_gun_sayisi": 0,
        "durum": "aktif",
        "is_admin": False
    }
    save_db(db)
    session['user_email'] = email
    auto_backup_discord()

    return jsonify({"status": "success", "message": "Hesap başarıyla oluşturuldu!", "email": email, "is_admin": False})

@app.route('/api/logout', methods=['POST'])
def logout():
    session.pop('user_email', None)
    return jsonify({"status": "success"})

@app.route('/api/user-data', methods=['GET'])
def get_user_data():
    email = session.get('user_email')
    if not email:
        return jsonify({"status": "unauthorized"}), 401
    
    db = load_db()
    user = db["users"].get(email)
    if not user:
        return jsonify({"status": "error"}), 404

    return jsonify({
        "status": "success",
        "user": user,
        "email": email
    })

@app.route('/api/admin/users', methods=['GET'])
def get_admin_users():
    email = session.get('user_email')
    db = load_db()
    if not email or not db["users"].get(email, {}).get("is_admin"):
        return jsonify({"status": "unauthorized"}), 403

    return jsonify({
        "status": "success",
        "users": db["users"],
        "bekleyen_talepler": db.get("bekleyen_talepler", {}),
        "kasa": db.get("kasa", 0)
    })

# --- JSON YEDEK YÜKLEME (RESTORE) ---
@app.route('/api/admin/restore-db', methods=['POST'])
def restore_db():
    email = session.get('user_email')
    db = load_db()
    if not email or not db["users"].get(email, {}).get("is_admin"):
        return jsonify({"status": "unauthorized"}), 403

    if 'file' not in request.files:
        return jsonify({"status": "error", "message": "Dosya seçilmedi."}), 400

    file = request.files['file']
    try:
        new_db = json.load(file)
        # Admin hesabının silinmediğinden emin ol
        if "users" in new_db and "admin" not in new_db["users"]:
            new_db["users"]["admin"] = db["users"]["admin"]
        
        save_db(new_db)
        auto_backup_discord()
        return jsonify({"status": "success", "message": "Veritabanı başarıyla yüklendi ve güncellendi!"})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Geçersiz JSON dosyası: {str(e)}"}), 400

@app.route('/api/action/odeme', methods=['POST'])
def action_odeme():
    email = session.get('user_email')
    data = request.json
    fiyat_val = validate_positive_amount(data.get("fiyat"))
    
    if not fiyat_val: return jsonify({"status": "error", "message": "Geçersiz tutar."}), 400

    db = load_db()
    hesap = db["users"].get(email)
    
    komisyon = round(fiyat_val * db["ayarlar"]["komisyon_odeme"], 2)
    toplam = round(fiyat_val + komisyon, 2)

    if hesap["bakiye"] < toplam:
        return jsonify({"status": "error", "message": f"Bakiye Yetersiz! Toplam: {toplam} TL"}), 400

    hesap["bakiye"] = round(hesap["bakiye"] - toplam, 2)
    req_id = generate_req_id()
    db["bekleyen_talepler"][req_id] = {
        "tur": "satin_alim", "user_id": email, "tutar": fiyat_val, 
        "komisyon": komisyon, "toplam": toplam, "link": data.get("aciklama"), "tarih": datetime.now().strftime("%d/%m/%Y %H:%M")
    }
    save_db(db)
    auto_backup_discord()
    return jsonify({"status": "success", "message": f"Ödeme alındı, onay bekleniyor. İşlem Kodu: {req_id}"})

@app.route('/api/action/havale', methods=['POST'])
def action_havale():
    email = session.get('user_email')
    data = request.json
    tutar_val = validate_positive_amount(data.get("tutar"))
    hedef_iban = clean_iban(data.get("iban", ""))

    if not tutar_val: return jsonify({"status": "error", "message": "Geçersiz tutar."}), 400

    db = load_db()
    gonderen = db["users"].get(email)
    alici_email = next((uid for uid, udata in db["users"].items() if udata.get("iban") == hedef_iban), None)

    if not alici_email: return jsonify({"status": "error", "message": "Alıcı IBAN bulunamadı."}), 400
    if alici_email == email: return jsonify({"status": "error", "message": "Kendinize havale yapamazsınız."}), 400

    komisyon = round(tutar_val * db["ayarlar"]["komisyon_havale"], 2) if tutar_val > db["ayarlar"]["havale_sinir"] else 0.0
    toplam = round(tutar_val + komisyon, 2)

    if gonderen["bakiye"] < toplam: return jsonify({"status": "error", "message": "Bakiye yetersiz."}), 400

    db["users"][email]["bakiye"] = round(db["users"][email]["bakiye"] - toplam, 2)
    db["users"][alici_email]["bakiye"] = round(db["users"][alici_email]["bakiye"] + tutar_val, 2)
    db["kasa"] = round(db["kasa"] + komisyon, 2)
    save_db(db)
    auto_backup_discord()
    return jsonify({"status": "success", "message": "Havale başarıyla gerçekleşti."})

@app.route('/api/action/kredi-cek', methods=['POST'])
def action_kredi_cek():
    email = session.get('user_email')
    data = request.json
    tutar_val = validate_positive_amount(data.get("tutar"))

    if not tutar_val: return jsonify({"status": "error", "message": "Geçersiz tutar."}), 400

    db = load_db()
    hesap = db["users"].get(email)
    if hesap.get("kredi_notu", 1000) < 500:
        return jsonify({"status": "error", "message": "Kredi notunuz çok düşük."}), 400

    req_id = generate_req_id()
    db["bekleyen_talepler"][req_id] = {
        "tur": "kredi", "user_id": email, "tutar": tutar_val, "tarih": datetime.now().strftime("%d/%m/%Y %H:%M")
    }
    save_db(db)
    auto_backup_discord()
    return jsonify({"status": "success", "message": "Kredi talebi yöneticiye iletildi."})

@app.route('/api/action/kredi-ode', methods=['POST'])
def action_kredi_ode():
    email = session.get('user_email')
    data = request.json
    odeme_val = validate_positive_amount(data.get("tutar"))

    if not odeme_val: return jsonify({"status": "error", "message": "Geçersiz tutar."}), 400

    db = load_db()
    hesap = db["users"].get(email)

    if hesap["kredi_borcu"] <= 0: return jsonify({"status": "error", "message": "Borcunuz bulunmuyor."}), 400
    if hesap["bakiye"] < odeme_val: return jsonify({"status": "error", "message": "Bakiye yetersiz."}), 400

    if odeme_val > hesap["kredi_borcu"]: odeme_val = hesap["kredi_borcu"]

    hesap["bakiye"] = round(hesap["bakiye"] - odeme_val, 2)
    hesap["kredi_borcu"] = round(hesap["kredi_borcu"] - odeme_val, 2)
    db["kasa"] = round(db["kasa"] + odeme_val, 2)
    hesap["kredi_notu"] += int(odeme_val * 0.05)

    if hesap["kredi_borcu"] <= 0:
        hesap["kredi_borcu"], hesap["kredi_gun_sayisi"] = 0.0, 0

    save_db(db)
    auto_backup_discord()
    return jsonify({"status": "success", "message": "Kredi borcu ödemesi alındı."})

@app.route('/api/admin/approve-request', methods=['POST'])
def admin_approve():
    email = session.get('user_email')
    db = load_db()
    if not email or not db["users"].get(email, {}).get("is_admin"): return jsonify({"status": "unauthorized"}), 403

    req_id = request.json.get("req_id")
    action = request.json.get("action")

    if req_id not in db["bekleyen_talepler"]:
        return jsonify({"status": "error", "message": "Talep bulunamadı."}), 400

    talep = db["bekleyen_talepler"][req_id]
    u_email = talep["user_id"]

    if action == "onay":
        if talep["tur"] == "satin_alim":
            db["kasa"] = round(db["kasa"] + talep["toplam"], 2)
        elif talep["tur"] == "kredi":
            db["users"][u_email]["bakiye"] = round(db["users"][u_email]["bakiye"] + talep["tutar"], 2)
            db["users"][u_email]["kredi_borcu"] = round(db["users"][u_email]["kredi_borcu"] + talep["tutar"], 2)
            db["users"][u_email]["kredi_gun_sayisi"] = 0
    elif action == "red":
        if talep["tur"] in ["satin_alim"]:
            db["users"][u_email]["bakiye"] = round(db["users"][u_email]["bakiye"] + talep["toplam"], 2)

    del db["bekleyen_talepler"][req_id]
    save_db(db)
    auto_backup_discord()
    return jsonify({"status": "success"})

@app.route('/api/admin/grafik', methods=['GET'])
def admin_grafik():
    db = load_db()
    toplam_kasa = db.get("kasa", 0)
    toplam_kredi = sum(u.get("kredi_borcu", 0) for u in db["users"].values())
    toplam_mevduat = sum(u.get("bakiye", 0) for u in db["users"].values())

    labels = ['Banka Kasasi', 'Dagitilan Krediler', 'Musteri Bakiyeleri']
    sizes = [toplam_kasa, toplam_kredi, toplam_mevduat]
    colors = ['#2ecc71', '#e74c3c', '#3498db']
    if sum(sizes) == 0: sizes = [1, 1, 1]

    plt.figure(figsize=(5, 5), facecolor='#1e1e2f')
    plt.rcParams['text.color'] = 'white'
    plt.pie(sizes, labels=labels, colors=colors, autopct='%1.1f%%', startangle=140)
    plt.title('Merkez Bankasi Varlik Dagilimi', color='white')

    buffer = io.BytesIO()
    plt.savefig(buffer, format='png', bbox_inches='tight', transparent=True)
    buffer.seek(0)
    plt.close()

    return app.response_class(buffer.getvalue(), mimetype='image/png')

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
