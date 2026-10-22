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
                    "bakiye": 10000.0,
                    "iban": "TR000000000000000000000000",
                    "kart_no": "4444 0000 0000 0000",
                    "skt": "12/99",
                    "cvv": "000",
                    "kredi_notu": 2000,
                    "durum": "aktif",
                    "dogrulandi": True,
                    "is_admin": True,
                    "hesap_turu": "is_hesabi",
                    "selfie": None,
                    "krediler": [],
                    "satinalinan_epinler": []
                }
            },
            "kasa": 0.0,
            "bekleyen_talepler": {},
            "dogrulama_talepleri": {},
            "dekontlar": [],
            "epin_urunler": {},
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
        if "admin" not in db["users"]:
            db["users"]["admin"] = {
                "ad_soyad": "Sistem Yöneticisi",
                "password": "123",
                "bakiye": 10000.0,
                "iban": "TR000000000000000000000000",
                "kart_no": "4444 0000 0000 0000",
                "skt": "12/99",
                "cvv": "000",
                "kredi_notu": 2000,
                "durum": "aktif",
                "dogrulandi": True,
                "is_admin": True,
                "hesap_turu": "is_hesabi",
                "selfie": None,
                "krediler": [],
                "satinalinan_epinler": []
            }
        for u in db["users"].values():
            if "krediler" not in u: u["krediler"] = []
            if "satinalinan_epinler" not in u: u["satinalinan_epinler"] = []
            if "hesap_turu" not in u: u["hesap_turu"] = "bireysel"
        if "dogrulama_talepleri" not in db: db["dogrulama_talepleri"] = {}
        if "dekontlar" not in db: db["dekontlar"] = []
        if "epin_urunler" not in db: db["epin_urunler"] = {}
        return db

def save_db(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

def auto_backup_discord():
    if not DISCORD_WEBHOOK_URL: return
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

def generate_kredi_id():
    return "KRD-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))

def generate_epin_id():
    return "EPIN-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))

def generate_dekont_no():
    return "DKN-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=8))

def clean_iban(raw_iban: str) -> str:
    cleaned = raw_iban.strip().upper().replace(" ", "")
    if not cleaned.startswith("TR"): cleaned = "TR" + cleaned
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

def add_dekont(db, user_email, tur, tutar, aciklama):
    dekont = {
        "dekont_no": generate_dekont_no(),
        "user_email": user_email,
        "tur": tur,
        "tutar": tutar,
        "aciklama": aciklama,
        "tarih": datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    }
    db["dekontlar"].insert(0, dekont)
    if len(db["dekontlar"]) > 100: db["dekontlar"].pop()

# --- HAFTALIK CEZA DÖNGÜSÜ ---
def faiz_isle_job():
    db = load_db()
    for email, u_data in db["users"].items():
        for kredi in u_data.get("krediler", []):
            if kredi.get("kalan_borc", 0) > 0:
                kredi["gecikmis_gun"] = kredi.get("gecikmis_gun", 0) + 1
                if kredi["gecikmis_gun"] % 7 == 0:
                    ceza = db["ayarlar"].get("gecikme_cezasi", 50.0)
                    kredi["kalan_borc"] = round(kredi["kalan_borc"] + ceza, 2)
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

    if "@" not in email or "." not in email:
        return jsonify({"status": "error", "message": "Geçerli bir e-posta yazın."}), 400

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
        "kredi_notu": 1000,
        "durum": "aktif",
        "dogrulandi": False,
        "is_admin": False,
        "hesap_turu": "bireysel",
        "selfie": None,
        "krediler": [],
        "satinalinan_epinler": []
    }
    save_db(db)
    session['user_email'] = email
    auto_backup_discord()

    return jsonify({"status": "success", "message": "Kayıt başarılı! Hesabınız KISITLI statüdedir.", "email": email, "is_admin": False})

@app.route('/api/logout', methods=['POST'])
def logout():
    session.pop('user_email', None)
    return jsonify({"status": "success"})

@app.route('/api/user-data', methods=['GET'])
def get_user_data():
    email = session.get('user_email')
    if not email: return jsonify({"status": "unauthorized"}), 401
    
    db = load_db()
    user = db["users"].get(email)
    if not user: return jsonify({"status": "error"}), 404

    user_dekontlar = [d for d in db.get("dekontlar", []) if d.get("user_email") == email]

    return jsonify({
        "status": "success", 
        "user": user, 
        "email": email, 
        "dekontlar": user_dekontlar,
        "krediler": user.get("krediler", []),
        "satinalinan_epinler": user.get("satinalinan_epinler", []),
        "epin_urunler": db.get("epin_urunler", {})
    })

@app.route('/api/user/upload-selfie', methods=['POST'])
def upload_selfie():
    email = session.get('user_email')
    if not email: return jsonify({"status": "unauthorized"}), 401

    data = request.json
    image_data = data.get("image")
    if not image_data: return jsonify({"status": "error", "message": "Resim alınamadı."}), 400

    db = load_db()
    db["users"][email]["selfie"] = image_data
    db["dogrulama_talepleri"][email] = {
        "email": email,
        "ad_soyad": db["users"][email]["ad_soyad"],
        "selfie": image_data,
        "tarih": datetime.now().strftime("%d/%m/%Y %H:%M")
    }
    save_db(db)
    auto_backup_discord()
    return jsonify({"status": "success", "message": "144p Selfie kaydedildi ve admin onayına gönderildi!"})

# --- E-PIN MARKETİ İŞLEMLERİ (SANAL KART DOĞRULAMALI) ---

@app.route('/api/market/buy-epin', methods=['POST'])
def buy_epin():
    email = session.get('user_email')
    if not email: return jsonify({"status": "unauthorized"}), 401

    data = request.json
    urun_id = data.get("urun_id")
    kart_no = clean_card_number(data.get("kart_no", ""))
    skt = data.get("skt", "").strip()
    cvv = data.get("cvv", "").strip()

    db = load_db()
    user = db["users"].get(email)

    if clean_card_number(user["kart_no"]) != kart_no or user["skt"].strip() != skt or user["cvv"].strip() != cvv:
        return jsonify({"status": "error", "message": "❌ Kart Bilgileri Hatalı! (Kart No, SKT veya CVV uyuşmuyor)"}), 400

    urun = db["epin_urunler"].get(urun_id)
    if not urun or len(urun.get("stok_kodlar", [])) == 0:
        return jsonify({"status": "error", "message": "Bu ürün tükenmiş veya stokta yok."}), 400

    fiyat = urun["fiyat"]
    if user["bakiye"] < fiyat:
        return jsonify({"status": "error", "message": f"Yetersiz Bakiye! Ürün Fiyatı: {fiyat} TL"}), 400

    user["bakiye"] = round(user["bakiye"] - fiyat, 2)
    db["kasa"] = round(db["kasa"] + fiyat, 2)

    satilan_kod = urun["stok_kodlar"].pop(0)

    if len(urun["stok_kodlar"]) == 0:
        del db["epin_urunler"][urun_id]

    epin_kayit = {
        "urun_adi": urun["baslik"],
        "kod": satilan_kod,
        "fiyat": fiyat,
        "tarih": datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    }
    user["satinalinan_epinler"].insert(0, epin_kayit)

    add_dekont(db, email, "EPIN_SATIN_ALIM", fiyat, f"E-Pin: {urun['baslik']} ({satilan_kod})")

    save_db(db)
    auto_backup_discord()

    return jsonify({
        "status": "success", 
        "message": f"🎉 İşlem Başarılı! E-Pin Kodunuz: {satilan_kod}",
        "kod": satilan_kod
    })

# --- KREDİ BAŞVURU VE ÖDEME İŞLEMLERİ ---

@app.route('/api/action/kredi-cek', methods=['POST'])
def action_kredi_cek():
    email = session.get('user_email')
    db = load_db()
    hesap = db["users"].get(email)

    if not hesap.get("dogrulandi"):
        return jsonify({"status": "error", "message": "❌ Kısıtlı Hesap! Kredi çekmek için yüz doğrulaması şarttır."}), 403

    data = request.json
    tutar_val = validate_positive_amount(data.get("tutar"))
    
    try:
        hafta = int(data.get("hafta", 3))
        if hafta < 3 or hafta > 12: raise ValueError
    except:
        return jsonify({"status": "error", "message": "Kredi süresi 3 ile 12 hafta arasında olmalıdır."}), 400

    if not tutar_val: return jsonify({"status": "error", "message": "Geçersiz tutar."}), 400

    base_faiz = db["ayarlar"].get("kredi_faiz", 0.05)
    kat_carpan = 1.0 + ((hafta - 3) / 9.0) * 3.0
    toplam_faiz_orani = base_faiz * kat_carpan
    
    toplam_borc = round(tutar_val + (tutar_val * toplam_faiz_orani), 2)
    haftalik_taksit = round(toplam_borc / hafta, 2)

    req_id = generate_req_id()
    db["bekleyen_talepler"][req_id] = {
        "tur": "kredi",
        "user_id": email,
        "tutar": tutar_val,
        "toplam_borc": toplam_borc,
        "hafta": hafta,
        "haftalik_taksit": haftalik_taksit,
        "faiz_orani": round(toplam_faiz_orani * 100, 2),
        "tarih": datetime.now().strftime("%d/%m/%Y %H:%M")
    }
    save_db(db)
    auto_backup_discord()
    return jsonify({"status": "success", "message": f"{tutar_val} TL için {hafta} haftalık kredi talebiniz oluşturuldu (%{round(toplam_faiz_orani*100,1)} faiz eklendi). Admin onayı bekleniyor."})

@app.route('/api/action/kredi-ode-haftalik', methods=['POST'])
def action_kredi_ode_haftalik():
    email = session.get('user_email')
    kredi_id = request.json.get("kredi_id")

    db = load_db()
    hesap = db["users"].get(email)
    krediler = hesap.get("krediler", [])

    kredi = next((k for k in krediler if k["kredi_id"] == kredi_id), None)
    if not kredi: return jsonify({"status": "error", "message": "Kredi bulunamadı."}), 400

    taksit = kredi["haftalik_taksit"]
    if taksit > kredi["kalan_borc"]: taksit = kredi["kalan_borc"]

    if hesap["bakiye"] < taksit:
        return jsonify({"status": "error", "message": f"Bakiye yetersiz! Haftalık taksit: {taksit} TL"}), 400

    hesap["bakiye"] = round(hesap["bakiye"] - taksit, 2)
    kredi["kalan_borc"] = round(kredi["kalan_borc"] - taksit, 2)
    kredi["kalan_hafta"] = max(0, kredi["kalan_hafta"] - 1)
    db["kasa"] = round(db["kasa"] + taksit, 2)
    hesap["kredi_notu"] += 15

    add_dekont(db, email, "KREDI_TAKSIT_ODEME", taksit, f"Kredi: {kredi_id} ({kredi['kalan_hafta']} Hafta Kaldı)")

    if kredi["kalan_borc"] <= 0 or kredi["kalan_hafta"] <= 0:
        hesap["krediler"] = [k for k in krediler if k["kredi_id"] != kredi_id]
        msg = "🎉 Tebrikler! Kredinin tamamı başarıyla kapandı."
    else:
        msg = f"✅ {taksit} TL haftalık taksit ödendi. Kalan borç: {kredi['kalan_borc']} TL"

    save_db(db)
    auto_backup_discord()
    return jsonify({"status": "success", "message": msg})

@app.route('/api/action/kredi-ode-tamami', methods=['POST'])
def action_kredi_ode_tamami():
    email = session.get('user_email')
    kredi_id = request.json.get("kredi_id")

    db = load_db()
    hesap = db["users"].get(email)
    krediler = hesap.get("krediler", [])

    kredi = next((k for k in krediler if k["kredi_id"] == kredi_id), None)
    if not kredi: return jsonify({"status": "error", "message": "Kredi bulunamadı."}), 400

    toplam = kredi["kalan_borc"]
    if hesap["bakiye"] < toplam:
        return jsonify({"status": "error", "message": f"Bakiye yetersiz! Kapanış tutarı: {toplam} TL"}), 400

    hesap["bakiye"] = round(hesap["bakiye"] - toplam, 2)
    db["kasa"] = round(db["kasa"] + toplam, 2)
    hesap["kredi_notu"] += 50

    hesap["krediler"] = [k for k in krediler if k["kredi_id"] != kredi_id]
    add_dekont(db, email, "KREDI_KAPAMA", toplam, f"Kredi Erken Kapatıldı: {kredi_id}")

    save_db(db)
    auto_backup_discord()
    return jsonify({"status": "success", "message": f"🎉 {toplam} TL ödenerek {kredi_id} kredisi tamamen kapatıldı!"})

# --- DİĞER İŞLEMLER ---

@app.route('/api/action/para-yatir', methods=['POST'])
def action_para_yatir():
    email = session.get('user_email')
    data = request.json
    tutar_val = validate_positive_amount(data.get("tutar"))
    kanit = data.get("kanit", "").strip()

    if not tutar_val or not kanit: return jsonify({"status": "error", "message": "Geçerli tutar ve dekont girin."}), 400

    db = load_db()
    req_id = generate_req_id()
    db["bekleyen_talepler"][req_id] = {
        "tur": "para_yatirma", "user_id": email, "tutar": tutar_val, "kanit": kanit, "tarih": datetime.now().strftime("%d/%m/%Y %H:%M")
    }
    save_db(db)
    auto_backup_discord()
    return jsonify({"status": "success", "message": "Para yatırma talebiniz admin onayına gönderildi."})

@app.route('/api/action/para-cek', methods=['POST'])
def action_para_cek():
    email = session.get('user_email')
    data = request.json
    tutar_val = validate_positive_amount(data.get("tutar"))

    if not tutar_val: return jsonify({"status": "error", "message": "Geçersiz tutar."}), 400

    db = load_db()
    hesap = db["users"].get(email)

    # İŞ HESABI İÇİN %0 ÇEKİM KOMİSYONU KONTROLÜ
    if hesap.get("hesap_turu") == "is_hesabi":
        komisyon_orani = 0.0
    else:
        komisyon_orani = db["ayarlar"].get("komisyon_cekme", 0.10)

    komisyon = round(tutar_val * komisyon_orani, 2)
    toplam_kesinti = round(tutar_val + komisyon, 2)

    if hesap["bakiye"] < toplam_kesinti:
        return jsonify({"status": "error", "message": f"Bakiye yetersiz! Toplam Çekim: {toplam_kesinti} TL (Komisyon: {komisyon} TL)"}), 400

    req_id = generate_req_id()
    db["bekleyen_talepler"][req_id] = {
        "tur": "para_cekme", "user_id": email, "tutar": tutar_val, "komisyon": komisyon, "toplam": toplam_kesinti, "tarih": datetime.now().strftime("%d/%m/%Y %H:%M")
    }
    save_db(db)
    auto_backup_discord()
    
    msg_extra = " (İş Hesabı - %0 Komisyon)" if komisyon_orani == 0.0 else f" (%{int(komisyon_orani*100)} Komisyon)"
    return jsonify({"status": "success", "message": f"Para çekme talebiniz oluşturuldu{msg_extra}. Admin onayı bekleniyor."})

@app.route('/api/action/odeme', methods=['POST'])
def action_odeme():
    email = session.get('user_email')
    db = load_db()
    hesap = db["users"].get(email)

    if not hesap.get("dogrulandi"):
        return jsonify({"status": "error", "message": "❌ Kısıtlı Hesap! Yüz doğrulaması yapmalısınız."}), 403

    data = request.json
    fiyat_val = validate_positive_amount(data.get("fiyat"))
    if not fiyat_val: return jsonify({"status": "error", "message": "Geçersiz tutar."}), 400

    komisyon = round(fiyat_val * db["ayarlar"]["komisyon_odeme"], 2)
    toplam = round(fiyat_val + komisyon, 2)

    if hesap["bakiye"] < toplam: return jsonify({"status": "error", "message": f"Bakiye Yetersiz! Toplam: {toplam} TL"}), 400

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

    if not gonderen.get("dogrulandi") and tutar_val > 500.0:
        return jsonify({"status": "error", "message": "❌ Kısıtlı Hesap! Maks 500 TL havale yapabilirsiniz."}), 403

    alici_email = next((uid for uid, udata in db["users"].items() if udata.get("iban") == hedef_iban), None)
    if not alici_email: return jsonify({"status": "error", "message": "Alıcı IBAN bulunamadı."}), 400
    if alici_email == email: return jsonify({"status": "error", "message": "Kendinize havale yapamazsınız."}), 400

    komisyon = round(tutar_val * db["ayarlar"]["komisyon_havale"], 2) if tutar_val > db["ayarlar"]["havale_sinir"] else 0.0
    toplam = round(tutar_val + komisyon, 2)

    if gonderen["bakiye"] < toplam: return jsonify({"status": "error", "message": "Bakiye yetersiz."}), 400

    db["users"][email]["bakiye"] = round(db["users"][email]["bakiye"] - toplam, 2)
    db["users"][alici_email]["bakiye"] = round(db["users"][alici_email]["bakiye"] + tutar_val, 2)
    db["kasa"] = round(db["kasa"] + komisyon, 2)

    add_dekont(db, email, "HAVALE_CIKIS", tutar_val, f"Alıcı: {hedef_iban}")
    add_dekont(db, alici_email, "HAVALE_GIRIS", tutar_val, f"Gönderen: {gonderen['iban']}")

    save_db(db)
    auto_backup_discord()
    return jsonify({"status": "success", "message": "Havale gerçekleşti."})

# --- ADMIN YÖNETİM ENDPOINTLERİ ---

@app.route('/api/admin/users', methods=['GET'])
def get_admin_users():
    email = session.get('user_email')
    db = load_db()
    if not email or not db["users"].get(email, {}).get("is_admin"): return jsonify({"status": "unauthorized"}), 403

    return jsonify({
        "status": "success",
        "users": db["users"],
        "bekleyen_talepler": db.get("bekleyen_talepler", {}),
        "dogrulama_talepleri": db.get("dogrulama_talepleri", {}),
        "epin_urunler": db.get("epin_urunler", {}),
        "ayarlar": db.get("ayarlar", {}),
        "kasa": db.get("kasa", 0)
    })

@app.route('/api/admin/add-epin', methods=['POST'])
def admin_add_epin():
    email = session.get('user_email')
    db = load_db()
    if not email or not db["users"].get(email, {}).get("is_admin"): return jsonify({"status": "unauthorized"}), 403

    data = request.json
    baslik = data.get("baslik", "").strip()
    fiyat_val = validate_positive_amount(data.get("fiyat"))
    kodlar_raw = data.get("kodlar", "").strip()

    if not baslik or not fiyat_val or not kodlar_raw:
        return jsonify({"status": "error", "message": "Başlık, fiyat ve en az 1 e-pin kodu girmelisiniz."}), 400

    kod_listesi = [k.strip() for k in kodlar_raw.replace("\r", "").split("\n") if k.strip()]

    if len(kod_listesi) == 0:
        return jsonify({"status": "error", "message": "Geçerli e-pin kodu bulunamadı."}), 400

    urun_id = generate_epin_id()
    db["epin_urunler"][urun_id] = {
        "id": urun_id,
        "baslik": baslik,
        "fiyat": fiyat_val,
        "stok_kodlar": kod_listesi
    }

    save_db(db)
    auto_backup_discord()
    return jsonify({"status": "success", "message": f"✅ '{baslik}' ürünü {len(kod_listesi)} adet stok ile e-pin marketine eklendi!"})

@app.route('/api/admin/delete-epin', methods=['POST'])
def admin_delete_epin():
    email = session.get('user_email')
    db = load_db()
    if not email or not db["users"].get(email, {}).get("is_admin"): return jsonify({"status": "unauthorized"}), 403

    urun_id = request.json.get("urun_id")
    if urun_id in db["epin_urunler"]:
        del db["epin_urunler"][urun_id]
        save_db(db)
        auto_backup_discord()
        return jsonify({"status": "success", "message": "Ürün marketten silindi."})
    return jsonify({"status": "error", "message": "Ürün bulunamadı."}), 400

@app.route('/api/admin/edit-user', methods=['POST'])
def admin_edit_user():
    email = session.get('user_email')
    db = load_db()
    if not email or not db["users"].get(email, {}).get("is_admin"): return jsonify({"status": "unauthorized"}), 403

    data = request.json
    target_email = data.get("email")
    action = data.get("action")
    amount = validate_positive_amount(data.get("amount")) if data.get("amount") else 0.0

    if target_email not in db["users"]: return jsonify({"status": "error", "message": "Kullanıcı bulunamadı."}), 400

    u = db["users"][target_email]

    if action == "bakiye_ekle":
        u["bakiye"] = round(u["bakiye"] + amount, 2)
        add_dekont(db, target_email, "ADMIN_BAKIYE_EKLE", amount, "Admin Bakiye Yüklemesi")
    elif action == "bakiye_kes":
        u["bakiye"] = max(0.0, round(u["bakiye"] - amount, 2))
        add_dekont(db, target_email, "ADMIN_BAKIYE_KES", amount, "Admin Bakiye Düzeltmesi")
    elif action == "dondur":
        u["durum"] = "dondurulmus"
    elif action == "aktif_et":
        u["durum"] = "aktif"
    elif action == "is_hesabi_yap":
        u["hesap_turu"] = "is_hesabi"
    elif action == "bireysel_yap":
        u["hesap_turu"] = "bireysel"

    save_db(db)
    auto_backup_discord()
    return jsonify({"status": "success", "message": f"{target_email} üzerinde işlem tamamlandı."})

@app.route('/api/admin/update-settings', methods=['POST'])
def admin_update_settings():
    email = session.get('user_email')
    db = load_db()
    if not email or not db["users"].get(email, {}).get("is_admin"): return jsonify({"status": "unauthorized"}), 403

    data = request.json
    try:
        db["ayarlar"]["komisyon_odeme"] = float(data.get("komisyon_odeme", 0.60))
        db["ayarlar"]["komisyon_cekme"] = float(data.get("komisyon_cekme", 0.10))
        db["ayarlar"]["komisyon_havale"] = float(data.get("komisyon_havale", 0.05))
        db["ayarlar"]["kredi_faiz"] = float(data.get("kredi_faiz", 0.05))
        db["ayarlar"]["gecikme_cezasi"] = float(data.get("gecikme_cezasi", 50.0))
        save_db(db)
        auto_backup_discord()
        return jsonify({"status": "success", "message": "Sistem ayarları güncellendi!"})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Hata: {str(e)}"}), 400

@app.route('/api/admin/approve-request', methods=['POST'])
def admin_approve():
    email = session.get('user_email')
    db = load_db()
    if not email or not db["users"].get(email, {}).get("is_admin"): return jsonify({"status": "unauthorized"}), 403

    req_id = request.json.get("req_id")
    action = request.json.get("action")

    if req_id not in db["bekleyen_talepler"]: return jsonify({"status": "error", "message": "Talep bulunamadı."}), 400

    talep = db["bekleyen_talepler"][req_id]
    u_email = talep["user_id"]

    if action == "onay":
        if talep["tur"] == "satin_alim":
            db["kasa"] = round(db["kasa"] + talep["toplam"], 2)
            add_dekont(db, u_email, "SATIN_ALIM", talep["tutar"], f"Ürün: {talep.get('link')}")
        elif talep["tur"] == "kredi":
            kredi_obj = {
                "kredi_id": generate_kredi_id(),
                "ana_para": talep["tutar"],
                "toplam_borc": talep["toplam_borc"],
                "kalan_borc": talep["toplam_borc"],
                "toplam_hafta": talep["hafta"],
                "kalan_hafta": talep["hafta"],
                "haftalik_taksit": talep["haftalik_taksit"],
                "faiz_orani": talep["faiz_orani"],
                "gecikmis_gun": 0,
                "tarih": datetime.now().strftime("%d/%m/%Y %H:%M")
            }
            db["users"][u_email]["bakiye"] = round(db["users"][u_email]["bakiye"] + talep["tutar"], 2)
            db["users"][u_email]["krediler"].append(kredi_obj)
            add_dekont(db, u_email, "KREDI_CEKIM", talep["tutar"], f"Kredi Onaylandı ({talep['hafta']} Haftalık)")
        elif talep["tur"] == "para_yatirma":
            db["users"][u_email]["bakiye"] = round(db["users"][u_email]["bakiye"] + talep["tutar"], 2)
            add_dekont(db, u_email, "PARA_YATIRMA", talep["tutar"], f"Dekont: {talep.get('kanit')}")
        elif talep["tur"] == "para_cekme":
            db["users"][u_email]["bakiye"] = round(db["users"][u_email]["bakiye"] - talep["toplam"], 2)
            db["kasa"] = round(db["kasa"] + talep["komisyon"], 2)
            add_dekont(db, u_email, "PARA_CEKME", talep["tutar"], f"Nakit Çekim (Komisyon: {talep['komisyon']} TL)")

    elif action == "red":
        if talep["tur"] in ["satin_alim"]:
            db["users"][u_email]["bakiye"] = round(db["users"][u_email]["bakiye"] + talep["toplam"], 2)

    del db["bekleyen_talepler"][req_id]
    save_db(db)
    auto_backup_discord()
    return jsonify({"status": "success"})

@app.route('/api/admin/verify-user', methods=['POST'])
def admin_verify_user():
    email = session.get('user_email')
    db = load_db()
    if not email or not db["users"].get(email, {}).get("is_admin"): return jsonify({"status": "unauthorized"}), 403

    target_email = request.json.get("email")
    action = request.json.get("action")

    if target_email in db["users"]:
        if action == "onay": db["users"][target_email]["dogrulandi"] = True
        if target_email in db["dogrulama_talepleri"]: del db["dogrulama_talepleri"][target_email]

    save_db(db)
    auto_backup_discord()
    return jsonify({"status": "success"})

@app.route('/api/admin/restore-db', methods=['POST'])
def restore_db():
    email = session.get('user_email')
    db = load_db()
    if not email or not db["users"].get(email, {}).get("is_admin"): return jsonify({"status": "unauthorized"}), 403

    if 'file' not in request.files: return jsonify({"status": "error", "message": "Dosya seçilmedi."}), 400

    file = request.files['file']
    try:
        new_db = json.load(file)
        if "users" in new_db and "admin" not in new_db["users"]: new_db["users"]["admin"] = db["users"]["admin"]
        save_db(new_db)
        auto_backup_discord()
        return jsonify({"status": "success", "message": "Veritabanı yüklendi!"})
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 400

@app.route('/api/admin/grafik', methods=['GET'])
def admin_grafik():
    db = load_db()
    toplam_kasa = db.get("kasa", 0)
    toplam_kredi = sum(sum(k.get("kalan_borc", 0) for k in u.get("krediler", [])) for u in db["users"].values())
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
