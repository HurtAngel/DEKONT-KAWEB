import hmac
import io
import json
import math
import os
import random
import re
import secrets
import string
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta

try:
    import fcntl
except ImportError:
    fcntl = None

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import requests
from flask import Flask, Response, jsonify, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash
from pymongo import MongoClient
import certifi

# ---------------------------------------------------------------------------
# YAPILANDIRMA
# ---------------------------------------------------------------------------
DB_FILE = os.getenv("DB_FILE", "bank_db.json")
LOCK_FILE = DB_FILE + ".lock"
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
BACKUP_MIN_INTERVAL = int(os.getenv("BACKUP_MIN_INTERVAL", "300"))
MONGO_URI = os.getenv("MONGO_URI")
mongo_collection = None

if MONGO_URI:
    try:
        mongo_client = MongoClient(
            MONGO_URI,
            tls=True,
            tlsAllowInvalidCertificates=True,
            serverSelectionTimeoutMS=5000
        )
        mongo_db = mongo_client["dekontika_bank"]
        mongo_collection = mongo_db["system_state"]
        mongo_client.admin.command('ping')
        print("MongoDB Atlas baglantisi basarili.")
    except Exception as exc:
        print(f"MongoDB baglanti hatasi, yerel JSON'a donuluyor: {exc}")
        mongo_collection = None

def mongo_load():
    if mongo_collection is not None:
        try:
            doc = mongo_collection.find_one({"_id": "main_db"})
            if doc:
                doc.pop("_id", None)
                return doc
        except Exception as e:
            print(f"Mongo okuma hatasi: {e}")
    return None

def mongo_save(data):
    if mongo_collection is not None:
        try:
            payload = dict(data)
            payload.pop("_id", None)
            mongo_collection.update_one({"_id": "main_db"}, {"$set": payload}, upsert=True)
            return True
        except Exception as e:
            print(f"Mongo yazma hatasi: {e}")
    return False

MAX_TUTAR = 10_000_000.0
MAX_BEKLEYEN_TALEP = 10
MAX_AKTIF_KREDI = 3
MAX_DEKONT = 5000
KISITLI_GUNLUK_HAVALE = 500.0
MAX_SELFIE_BYTES = 500_000
MAX_KOD_YUKLEME = 1000

DEFAULT_AYARLAR = {
    "komisyon_odeme": 0.60,
    "komisyon_cekme": 0.10,
    "komisyon_havale": 0.05,
    "havale_sinir": 100.0,
    "kisitli_gunluk_havale": 500.0,
    "kredi_faiz": 0.05,
    "gecikme_cezasi": 50.0,
    "kredi_gecikme_ceza_orani": 0.02,
    "gecikme_kredi_notu_ceza": 50,
    "kredi_ek_zaman_gun": 7,
    "komisyon_epin_satis": 0.25,
    "komisyon_slip": 0.03,
    "ucd_onay_suresi_dk": 10,
    "temsilci_kar_payi_orani": 0.20,
    "referans_dogrulama_odulu": 50.0,
    "canli_yenileme_sn": 4,
}

ADMIN_ALIASES = ("admin", "admin@dekontika.com")

app = Flask(__name__)

def _load_secret_key():
    env_key = os.getenv("SECRET_KEY")
    if env_key:
        return env_key
    dir_path = os.path.dirname(os.path.abspath(DB_FILE))
    path = os.path.join(dir_path, ".dekontika_secret")
    try:
        with open(path, "r", encoding="utf-8") as f:
            key = f.read().strip()
            if key:
                return key
    except OSError:
        pass
    key = secrets.token_hex(32)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(key)
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key

app.secret_key = _load_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "1") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=25 * 1024 * 1024,
    JSON_AS_ASCII=False,
)

if os.getenv("TRUST_PROXY", "1") == "1":
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# ---------------------------------------------------------------------------
# HATA YÖNETİMİ
# ---------------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status

@app.errorhandler(ApiError)
def handle_api_error(e):
    return jsonify(status="error", message=e.message), e.status

@app.errorhandler(413)
def handle_too_large(_e):
    return jsonify(status="error", message="Dosya boyutu çok büyük."), 413

@app.errorhandler(Exception)
def handle_unexpected(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        if request.path.startswith("/api/"):
            return jsonify(status="error", message=e.description or e.name), e.code
        return e
    app.logger.exception("Beklenmeyen sistem hatası")
    return jsonify(status="error", message="Sunucu tarafında beklenmeyen bir hata oluştu."), 500

# ---------------------------------------------------------------------------
# VERİTABANI KİLİTLİ YÖNETİM (JSON & MongoDB)
# ---------------------------------------------------------------------------
_thread_lock = threading.Lock()

@contextmanager
def db_lock():
    with _thread_lock:
        fh = None
        if fcntl and mongo_collection is None:
            try:
                fh = open(LOCK_FILE, "a+")
                fcntl.flock(fh, fcntl.LOCK_EX)
            except OSError:
                fh = None
        try:
            yield
        finally:
            if fh:
                try:
                    fcntl.flock(fh, fcntl.LOCK_UN)
                    fh.close()
                except OSError:
                    pass

def now_str():
    return datetime.now().strftime("%d/%m/%Y %H:%M:%S")

def ts_to_str(ts):
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%d/%m/%Y %H:%M")
    except Exception:
        return "-"

def iso_now(delta_days=0):
    return (datetime.now() + timedelta(days=delta_days)).strftime("%Y-%m-%dT%H:%M:%S")

def parse_iso(value):
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError):
        return None

def is_hashed(pw):
    return isinstance(pw, str) and pw.startswith(("scrypt:", "pbkdf2:"))

def verify_password(stored, given):
    if not isinstance(given, str) or not isinstance(stored, str):
        return False
    if is_hashed(stored):
        return check_password_hash(stored, given)
    return hmac.compare_digest(stored.encode("utf-8"), given.encode("utf-8"))

def _rand_digits(n):
    return "".join(random.choice(string.digits) for _ in range(n))

def generate_iban(db=None):
    used = {u.get("iban") for u in (db or {}).get("users", {}).values()}
    while True:
        iban = "TR" + _rand_digits(24)
        if iban not in used:
            return iban

def generate_card_number(db=None):
    used = {u.get("kart_no") for u in (db or {}).get("users", {}).values()}
    while True:
        card = "4444 " + " ".join(_rand_digits(4) for _ in range(3))
        if card not in used:
            return card

def _rand_id(prefix, k=6):
    return prefix + "".join(random.choices(string.ascii_uppercase + string.digits, k=k))

def generate_req_id(): return _rand_id("REQ-")
def generate_kredi_id(): return _rand_id("KRD-")
def generate_dekont_no(): return _rand_id("DKN-", 8)
def generate_3d_id(): return _rand_id("3DS-", 8)
def generate_slip_code(db=None):
    used = set((db or {}).get("slipler", {}).keys())
    while True:
        code = "SLIP-" + _rand_digits(6)
        if code not in used:
            return code

def generate_ref_kod(db=None):
    used = {u.get("ref_kod") for u in (db or {}).get("users", {}).values()}
    while True:
        code = "REF-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
        if code not in used:
            return code

def generate_epin_id(db):
    while True:
        pid = _rand_id("EPIN-")
        if pid not in db.get("epin_urunler", {}):
            return pid

def new_user(db, ad_soyad, password, *, admin=False, bakiye=0.0, referans_eden=None):
    return {
        "ad_soyad": ad_soyad,
        "password": generate_password_hash(password),
        "bakiye": bakiye,
        "iban": "TR" + "0" * 24 if admin else generate_iban(db),
        "kart_no": "4444 0000 0000 0000" if admin else generate_card_number(db),
        "skt": "12/99" if admin else "12/28",
        "cvv": "000" if admin else str(random.randint(100, 999)),
        "kredi_notu": 2000 if admin else 1000,
        "durum": "aktif",
        "dogrulandi": True if admin else False,
        "is_hesabi": False,
        "is_admin": admin,
        "is_temsilci": True if admin else False,
        "ucd_aktif": True,
        "ref_kod": "REF-MERKEZ" if admin else generate_ref_kod(db),
        "referans_eden": referans_eden,
        "ref_odul_verildi": True if admin else False,
        "temsilci_kazanc": 0.0,
        "selfie": None,
        "krediler": [],
        "satinalinan_epinler": [],
    }

def default_db():
    return {
        "users": {},
        "kasa": 0.0,
        "bekleyen_talepler": {},
        "dogrulama_talepleri": {},
        "is_hesabi_talepleri": {},
        "temsilci_talepleri": {},
        "bekleyen_kart_onaylari": {},
        "kuponlar": {},
        "slipler": {},
        "harici_odemeler": {},
        "dekontlar": [],
        "epin_urunler": {},
        "ayarlar": dict(DEFAULT_AYARLAR),
    }

def add_dekont(db, user_email, tur, tutar, aciklama, epin_kod=None):
    user = db["users"].get(user_email)
    dekont_entry = {
        "dekont_no": generate_dekont_no(),
        "user_email": user_email,
        "ad_soyad": user.get("ad_soyad") if user else user_email,
        "iban": user.get("iban") if user else "-",
        "tur": tur,
        "tutar": round(float(tutar), 2),
        "aciklama": aciklama,
        "tarih": now_str(),
        "bakiye_sonrasi": round(user["bakiye"], 2) if user else None,
    }
    if epin_kod:
        dekont_entry["epin_kod"] = epin_kod
    db["dekontlar"].insert(0, dekont_entry)
    del db["dekontlar"][MAX_DEKONT:]
    return dekont_entry["dekont_no"]

def grant_referral_verification_reward(db, verified_email):
    """Davet edilen kullanıcı hesabını doğruladığında davet edene (standart veya temsilci) ödül verir."""
    u = db["users"].get(verified_email)
    if not u or u.get("ref_odul_verildi"):
        return
    u["ref_odul_verildi"] = True
    ref_email = u.get("referans_eden")
    if not ref_email:
        return
    davet_eden = db["users"].get(ref_email)
    if not davet_eden or davet_eden.get("durum") == "dondurulmus":
        return
    odul = round(float(db["ayarlar"].get("referans_dogrulama_odulu", 50.0)), 2)
    if odul > 0:
        davet_eden["bakiye"] = round(davet_eden["bakiye"] + odul, 2)
        davet_eden["temsilci_kazanc"] = round(davet_eden.get("temsilci_kazanc", 0.0) + odul, 2)
        add_dekont(db, ref_email, "REFERANS_ODULU", odul, f"Davet Doğrulama Ödülü ({verified_email} hesabını doğruladı)")

def distribute_kasa_profit(db, kaynak_email, kar_tutari, islem_adi):
    """Kasa kar elde ettiğinde, işlemi yapan kullanıcının referans temsilcisi varsa pay aktarır."""
    kar_tutari = round(float(kar_tutari), 2)
    if kar_tutari <= 0:
        return
    u = db["users"].get(kaynak_email)
    ref_email = u.get("referans_eden") if u else None
    temsilci = db["users"].get(ref_email) if ref_email else None

    pay_orani = float(db["ayarlar"].get("temsilci_kar_payi_orani", 0.20))
    if temsilci and temsilci.get("is_temsilci") and temsilci.get("durum") == "aktif" and pay_orani > 0:
        temsilci_payi = round(kar_tutari * pay_orani, 2)
        kasa_net = round(kar_tutari - temsilci_payi, 2)
        if temsilci_payi > 0:
            db["kasa"] = round(db["kasa"] + kasa_net, 2)
            temsilci["bakiye"] = round(temsilci["bakiye"] + temsilci_payi, 2)
            temsilci["temsilci_kazanc"] = round(temsilci.get("temsilci_kazanc", 0.0) + temsilci_payi, 2)
            add_dekont(db, ref_email, "TEMSILCI_KOMISYONU", temsilci_payi, f"Referans Geliri ({kaynak_email} - {islem_adi})")
            return
    db["kasa"] = round(db["kasa"] + kar_tutari, 2)

def apply_single_loan_penalty(db, email, u, k):
    """Tek bir krediye gecikme cezası ve kredi notu düşümü uygular."""
    ceza_orani = float(db["ayarlar"].get("kredi_gecikme_ceza_orani", 0.02))
    not_cezasi = int(db["ayarlar"].get("gecikme_kredi_notu_ceza", 50))
    ceza_tutari = round(k["kalan_borc"] * ceza_orani, 2)
    if ceza_tutari > 0:
        k["kalan_borc"] = round(k["kalan_borc"] + ceza_tutari, 2)
        k["toplam_borc"] = round(k.get("toplam_borc", k["kalan_borc"]) + ceza_tutari, 2)
        kalan_h = max(1, int(k.get("kalan_hafta", 1)))
        k["haftalik_taksit"] = round(k["kalan_borc"] / kalan_h, 2)
        k["gecikme_sayisi"] = int(k.get("gecikme_sayisi", 0)) + 1
        k["sonraki_taksit_ts"] = time.time() + (7 * 86400)
        u["kredi_notu"] = max(0, int(u.get("kredi_notu", 1000)) - not_cezasi)
        add_dekont(
            db, email, "KREDI_GECIKME_CEZASI", -ceza_tutari,
            f"Kredi ({k['kredi_id']}) gecikme cezası (+%{round(ceza_orani*100, 1)})"
        )
        return True
    return False

def process_overdue_loans(db, force_all=False):
    """Haftalık kredi taksiti gecikenlere (muaf değilse) otomatik ceza ve kredi notu düşümü uygular."""
    now_ts = time.time()
    etkilenen = 0

    for email, u in db.get("users", {}).items():
        for k in u.get("krediler", []):
            if k.get("kalan_borc", 0) <= 0:
                continue
            if "sonraki_taksit_ts" not in k:
                k["sonraki_taksit_ts"] = now_ts + (7 * 86400)
            if k.get("ceza_muaf", False) and not force_all:
                continue
            if force_all or now_ts > float(k["sonraki_taksit_ts"]):
                if apply_single_loan_penalty(db, email, u, k):
                    etkilenen += 1
    return etkilenen

def cleanup_expired_3d(db):
    """Süresi dolan 3D Secure kart onay taleplerini temizler (kupon ve bakiye yanmaz)."""
    now_ts = time.time()
    expired_keys = [
        oid for oid, req in db.get("bekleyen_kart_onaylari", {}).items()
        if req.get("durum") == "bekliyor" and now_ts > float(req.get("bitis_ts", 0))
    ]
    for oid in expired_keys:
        req = db["bekleyen_kart_onaylari"][oid]
        req["durum"] = "zaman_asimi"
        if req.get("harici_odeme_id") and req["harici_odeme_id"] in db.get("harici_odemeler", {}):
            db["harici_odemeler"][req["harici_odeme_id"]]["durum"] = "zaman_asimi"
        del db["bekleyen_kart_onaylari"][oid]
    return len(expired_keys)

def normalize_db(db, create_admin=False):
    for key, default in default_db().items():
        if key == "kasa":
            try:
                db["kasa"] = float(db.get("kasa") or 0.0)
            except (TypeError, ValueError):
                db["kasa"] = 0.0
        elif key not in db or not isinstance(db[key], type(default)):
            db[key] = default

    for key, val in DEFAULT_AYARLAR.items():
        db["ayarlar"].setdefault(key, val)

    for email, u in list(db["users"].items()):
        u.setdefault("ad_soyad", email)
        u.setdefault("bakiye", 0.0)
        u.setdefault("kredi_notu", 1000)
        u.setdefault("durum", "aktif")
        u.setdefault("dogrulandi", False)
        u.setdefault("is_hesabi", False)
        u.setdefault("is_admin", False)
        u.setdefault("is_temsilci", bool(u.get("is_admin", False)))
        u.setdefault("ucd_aktif", True)
        u.setdefault("ref_kod", "REF-MERKEZ" if u.get("is_admin") else generate_ref_kod(db))
        u.setdefault("referans_eden", None)
        u.setdefault("ref_odul_verildi", bool(u.get("dogrulandi", False)))
        u.setdefault("temsilci_kazanc", 0.0)
        u.setdefault("selfie", None)
        u.setdefault("krediler", [])
        u.setdefault("satinalinan_epinler", [])
        u.setdefault("iban", generate_iban(db))
        u.setdefault("kart_no", generate_card_number(db))
        u.setdefault("skt", "12/28")
        u.setdefault("cvv", str(random.randint(100, 999)))
        if not is_hashed(u.get("password")):
            plain = str(u.get("password") or secrets.token_hex(8))
            u["password"] = generate_password_hash(plain)
        for k in u.get("krediler", []):
            k.setdefault("sonraki_taksit_ts", time.time() + (7 * 86400))
            k.setdefault("gecikme_sayisi", 0)
            k.setdefault("ceza_muaf", False)

    if create_admin and "admin" not in db["users"]:
        env_pw = os.getenv("ADMIN_PASSWORD", "1234")
        adm = new_user(db, "Sistem Yöneticisi", env_pw, admin=True, bakiye=10000.0)
        db["users"]["admin"] = adm

    cleanup_expired_3d(db)
    process_overdue_loans(db, force_all=False)
    return db

def load_db():
    cloud_data = mongo_load()
    if cloud_data is not None:
        return normalize_db(cloud_data)

    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                db = json.load(f)
        except Exception:
            db = default_db()
    else:
        db = default_db()
    return normalize_db(db)

def save_db(data):
    mongo_save(data)
    tmp = f"{DB_FILE}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, DB_FILE)

@contextmanager
def transaction():
    with db_lock():
        db = load_db()
        yield db
        save_db(db)
    mark_backup_dirty()

def init_db():
    with db_lock():
        db = load_db() if os.path.exists(DB_FILE) else default_db()
        normalize_db(db, create_admin=True)
        save_db(db)

# ---------------------------------------------------------------------------
# YEDEKLEME
# ---------------------------------------------------------------------------
_backup_state = {"dirty": False, "last": 0.0}
_backup_lock = threading.Lock()

def mark_backup_dirty():
    if DISCORD_WEBHOOK_URL:
        with _backup_lock:
            _backup_state["dirty"] = True

def flush_backup_if_due(force=False):
    if not DISCORD_WEBHOOK_URL:
        return
    with _backup_lock:
        due = _backup_state["dirty"] and (time.time() - _backup_state["last"] >= BACKUP_MIN_INTERVAL)
        if not (due or force):
            return
        _backup_state["dirty"] = False
        _backup_state["last"] = time.time()
    try:
        with open(DB_FILE, "rb") as f:
            payload = {"content": f"[OTOMATİK YEDEK] Veritabanı güncellendi ({now_str()})"}
            requests.post(
                DISCORD_WEBHOOK_URL,
                data=payload,
                files={"file": ("bank_db_yedek.json", f, "application/json")},
                timeout=15
            )
    except Exception as exc:
        app.logger.warning(f"Yedekleme hatasi: {exc}")

# ---------------------------------------------------------------------------
# YARDIMCI FONKSİYONLAR
# ---------------------------------------------------------------------------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}

def clean_iban(raw):
    cleaned = str(raw or "").strip().upper().replace(" ", "")
    if cleaned and not cleaned.startswith("TR"):
        cleaned = "TR" + cleaned
    return cleaned

def clean_card_number(raw):
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    return " ".join(digits[i : i + 4] for i in range(0, len(digits), 4))

def find_card_owner(db, kart_no, skt, cvv):
    c_no = clean_card_number(kart_no)
    s_skt = str(skt or "").strip()
    s_cvv = str(cvv or "").strip()
    for email, u in db.get("users", {}).items():
        if (clean_card_number(u.get("kart_no")) == c_no and
            str(u.get("skt", "")).strip() == s_skt and
            str(u.get("cvv", "")).strip() == s_cvv):
            return email, u
    return None, None

def parse_amount(value, label="tutar", required=True):
    if value in (None, ""):
        if required:
            raise ApiError(f"Geçerli bir {label} girin.")
        return None
    s = str(value).strip().replace(" ", "").replace(",", ".")
    try:
        val = float(s)
    except ValueError:
        raise ApiError(f"Geçerli bir {label} girin.")
    if math.isnan(val) or math.isinf(val) or val <= 0:
        raise ApiError(f"{label.capitalize()} sıfırdan büyük olmalı.")
    val = round(val, 2)
    if val > MAX_TUTAR:
        raise ApiError(f"{label.capitalize()} çok yüksek.")
    return val

def validate_and_calc_coupon(db, kupon_kodu, user_email, urun_id, baz_fiyat):
    if not kupon_kodu:
        return None, 0.0, baz_fiyat
    kod = str(kupon_kodu).strip().upper()
    kupon = db.get("kuponlar", {}).get(kod)
    if not kupon or not kupon.get("aktif", True):
        raise ApiError("Geçersiz veya pasif indirim kuponu.")
    if time.time() > float(kupon.get("bitis_ts", 0)):
        raise ApiError("Bu kuponun kullanım süresi dolmuş.")
    hedef_k = kupon.get("hedef_kullanici", "genel")
    if hedef_k not in ("genel", "", user_email):
        raise ApiError("Bu kupon sizin hesabınız için tanımlanmamış.")
    hedef_u = kupon.get("hedef_urun", "tumu")
    if urun_id and hedef_u not in ("tumu", "", urun_id):
        raise ApiError("Bu kupon seçilen üründe geçerli değil.")
    kullananlar = kupon.get("kullananlar", [])
    if len(kullananlar) >= int(kupon.get("toplam_limit", 100)):
        raise ApiError("Bu kuponun toplam kullanım limiti dolmuş.")
    if kullananlar.count(user_email) >= int(kupon.get("kisi_basi_limit", 1)):
        raise ApiError("Bu kuponu maksimum kullanım hakkınız kadar kullandınız.")

    deger = float(kupon.get("deger", 0.0))
    if kupon.get("tur") == "yuzde":
        indirim = round(baz_fiyat * (deger / 100.0), 2)
    else:
        indirim = round(deger, 2)

    indirim = min(indirim, round(baz_fiyat - 0.01, 2)) if baz_fiyat > 0.01 else 0.0
    indirim = max(0.0, indirim)
    yeni_fiyat = round(baz_fiyat - indirim, 2)
    return kupon, indirim, yeni_fiyat

def execute_epin_delivery(db, alici_email, kart_sahibi_email, urun_id, kupon_kodu=None):
    """Stok, bakiye ve kupon kontrolünü yaparak E-Pin teslimatını ve muhasebesini tamamlar."""
    alici = db["users"].get(alici_email)
    kart_sahibi = db["users"].get(kart_sahibi_email)
    if not alici or not kart_sahibi:
        raise ApiError("Kullanıcı veya kart sahibi bulunamadı.")
    if kart_sahibi.get("durum") == "dondurulmus":
        raise ApiError("Kart sahibinin hesabı dondurulmuş.")

    urun = db.get("epin_urunler", {}).get(urun_id)
    if not urun or not urun.get("aktif", True) or len(urun.get("stok_kodlar", [])) == 0:
        raise ApiError("Ürün stoku tükendiği için işlem iptal edildi (Kupon ve bakiyeniz korundu).")

    satici = urun.get("satici", "admin")
    satici_isteyen_fiyat = float(urun["fiyat"])
    ek_pay_orani = float(db["ayarlar"].get("komisyon_epin_satis", 0.25))
    banka_payi = round(satici_isteyen_fiyat * ek_pay_orani, 2)
    baz_toplam = round(satici_isteyen_fiyat + banka_payi, 2)

    kupon, indirim, odenecek_tutar = validate_and_calc_coupon(db, kupon_kodu, alici_email, urun_id, baz_toplam)

    if kart_sahibi["bakiye"] < odenecek_tutar:
        raise ApiError(f"Kart bakiyesi yetersiz! Gereken: {odenecek_tutar} TL")

    # Bakiyeyi düş ve kodu çek
    kart_sahibi["bakiye"] = round(kart_sahibi["bakiye"] - odenecek_tutar, 2)
    kod = urun["stok_kodlar"].pop(0)

    # Kupon hakkını sadece işlem tamamen başarılı olduğunda düş
    if kupon is not None:
        kupon.setdefault("kullananlar", []).append(alici_email)

    # Muhasebe dağılımı
    if satici == "admin":
        distribute_kasa_profit(db, alici_email, odenecek_tutar, "Merkez E-Pin Satışı")
    else:
        if satici in db["users"]:
            db["users"][satici]["bakiye"] = round(db["users"][satici]["bakiye"] + satici_isteyen_fiyat, 2)
        net_banka_kar = max(0.0, round(odenecek_tutar - satici_isteyen_fiyat, 2))
        distribute_kasa_profit(db, alici_email, net_banka_kar, "E-Pin Pazar Komisyonu")
        add_dekont(db, satici, "EPIN_SATIS", satici_isteyen_fiyat, f"Satış: {urun['baslik']} ({kod})")

    alici["satinalinan_epinler"].insert(0, {
        "urun_adi": urun["baslik"],
        "kod": kod,
        "fiyat": odenecek_tutar,
        "indirim": indirim,
        "tarih": now_str(),
        "satici_ad": seller_name(db, satici),
    })

    kupon_notu = f" [Kupon: {kupon['kod']} (-{indirim} TL)]" if kupon else ""
    if kart_sahibi_email != alici_email:
        add_dekont(db, kart_sahibi_email, "HARICI_KART_ODEME", -odenecek_tutar, f"Harici Kart E-Pin: {urun['baslik']} (Alıcı: {alici_email}){kupon_notu}")
        add_dekont(db, alici_email, "EPIN_SATIN_ALIM", odenecek_tutar, f"E-Pin Alımı ({kart_sahibi_email} kartıyla): {urun['baslik']}{kupon_notu}", epin_kod=kod)
    else:
        add_dekont(db, alici_email, "EPIN_SATIN_ALIM", odenecek_tutar, f"E-Pin Alımı: {urun['baslik']}{kupon_notu}", epin_kod=kod)

    return kod, odenecek_tutar, indirim

def require_user(db):
    email = session.get("user_email")
    if not email:
        raise ApiError("Giriş yapmalısınız.", 401)
    user = db["users"].get(email)
    if not user:
        session.pop("user_email", None)
        raise ApiError("Giriş yapmalısınız.", 401)
    if user.get("durum") == "dondurulmus":
        session.pop("user_email", None)
        raise ApiError("Hesabınız bloke edilmiştir.", 403)
    return email, user

def require_admin(db):
    email, user = require_user(db)
    if not user.get("is_admin"):
        raise ApiError("Yönetici yetkisi gerekir.", 403)
    return email, user

def require_seller(db):
    email, user = require_user(db)
    if not (user.get("is_admin") or user.get("is_hesabi")):
        raise ApiError("Bu işlem sadece İş Hesapları ve Yöneticiler içindir.", 403)
    return email, user

def pending_of(db, email):
    return [(rid, t) for rid, t in db.get("bekleyen_talepler", {}).items() if t.get("user_id") == email]

def ensure_pending_room(db, email):
    if len(pending_of(db, email)) >= MAX_BEKLEYEN_TALEP:
        raise ApiError(f"En fazla {MAX_BEKLEYEN_TALEP} bekleyen talebiniz olabilir.")

def public_user(email, u, db=None):
    ref_sayisi = 0
    if db is not None:
        ref_sayisi = sum(1 for usr in db.get("users", {}).values() if usr.get("referans_eden") == email)
    return {
        "email": email,
        "ad_soyad": u.get("ad_soyad"),
        "bakiye": round(u.get("bakiye", 0.0), 2),
        "iban": u.get("iban"),
        "kart_no": u.get("kart_no"),
        "skt": u.get("skt"),
        "cvv": u.get("cvv"),
        "kredi_notu": u.get("kredi_notu"),
        "durum": u.get("durum"),
        "dogrulandi": bool(u.get("dogrulandi")),
        "is_hesabi": bool(u.get("is_hesabi")),
        "is_admin": bool(u.get("is_admin")),
        "is_temsilci": bool(u.get("is_temsilci")),
        "ucd_aktif": bool(u.get("ucd_aktif", True)),
        "ref_kod": u.get("ref_kod"),
        "referans_eden": u.get("referans_eden"),
        "temsilci_kazanc": round(u.get("temsilci_kazanc", 0.0), 2),
        "referans_sayisi": ref_sayisi,
    }

def seller_name(db, satici):
    if satici == "admin":
        return "DEKONTİKA (Merkez)"
    s = db["users"].get(satici)
    return s["ad_soyad"] if s else "Bilinmiyor"

def seller_can_sell(db, satici):
    if satici == "admin":
        return True
    s = db["users"].get(satici)
    return bool(s and (s.get("is_hesabi") or s.get("is_admin")) and s.get("durum") != "dondurulmus")

def market_listing(db):
    items = []
    ek_pay_orani = db["ayarlar"].get("komisyon_epin_satis", 0.25)

    for pid, p in db.get("epin_urunler", {}).items():
        if not p.get("aktif", True) or not seller_can_sell(db, p.get("satici", "admin")):
            continue
        
        satici = p.get("satici", "admin")
        satici_isteyen_fiyat = float(p.get("fiyat", 0.0))
        
        banka_payi = round(satici_isteyen_fiyat * ek_pay_orani, 2)
        satis_fiyati = round(satici_isteyen_fiyat + banka_payi, 2)

        items.append({
            "id": pid,
            "baslik": p.get("baslik"),
            "fiyat": satis_fiyati,
            "satici_kazanci": satici_isteyen_fiyat,
            "banka_payi": banka_payi,
            "stok": len(p.get("stok_kodlar", [])),
            "satici": satici,
            "satici_ad": seller_name(db, satici),
            "platform": satici == "admin",
        })
    items.sort(key=lambda x: (x["stok"] == 0, str(x["baslik"]).lower()))
    return items

def pending_view(rid, t, db=None):
    out = {k: v for k, v in t.items() if k != "kanit"}
    out["id"] = rid
    if db is not None:
        u = db["users"].get(t.get("user_id"))
        out["user_ad"] = u["ad_soyad"] if u else t.get("user_id")
    return out

def format_all_loans_for_admin(db):
    """Sistemdeki tüm aktif kredileri tüm verileri ve gecikme durumlarıyla listeler."""
    now_ts = time.time()
    out = []
    for email, u in db.get("users", {}).items():
        for k in u.get("krediler", []):
            sonraki_ts = float(k.get("sonraki_taksit_ts", now_ts + 7 * 86400))
            kalan_sn = int(sonraki_ts - now_ts)
            vadesi_gecti = kalan_sn < 0
            out.append({
                "email": email,
                "ad_soyad": u.get("ad_soyad", email),
                "kredi_notu": u.get("kredi_notu", 1000),
                "kredi_id": k.get("kredi_id"),
                "ana_para": round(float(k.get("ana_para", 0.0)), 2),
                "toplam_borc": round(float(k.get("toplam_borc", 0.0)), 2),
                "kalan_borc": round(float(k.get("kalan_borc", 0.0)), 2),
                "toplam_hafta": int(k.get("toplam_hafta", 3)),
                "kalan_hafta": int(k.get("kalan_hafta", 1)),
                "haftalik_taksit": round(float(k.get("haftalik_taksit", 0.0)), 2),
                "faiz_orani": k.get("faiz_orani", 0),
                "baslangic_tarihi": k.get("tarih", "-"),
                "sonraki_taksit_ts": sonraki_ts,
                "sonraki_taksit_tarihi": ts_to_str(sonraki_ts),
                "kalan_sn": kalan_sn,
                "vadesi_gecti": vadesi_gecti,
                "gecikme_sayisi": int(k.get("gecikme_sayisi", 0)),
                "ceza_muaf": bool(k.get("ceza_muaf", False)),
            })
    out.sort(key=lambda x: (not x["vadesi_gecti"], x["sonraki_taksit_ts"]))
    return out

# ---------------------------------------------------------------------------
# CSRF & GÜVENLİK
# ---------------------------------------------------------------------------
@app.before_request
def csrf_protect():
    if request.path.startswith("/api/v1/"):
        return
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.path.startswith("/api/"):
        token = session.get("csrf")
        sent = request.headers.get("X-CSRF-Token", "")
        if not token or not hmac.compare_digest(token, sent):
            return jsonify(status="error", message="Oturum doğrulaması başarısız (CSRF hatası)."), 403

@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp

# ---------------------------------------------------------------------------
# GİRİŞ & KAYIT
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/session", methods=["GET"])
def api_session():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    authed, is_admin = False, False
    email = session.get("user_email")
    db = load_db()
    if email:
        u = db["users"].get(email)
        if u and u.get("durum") != "dondurulmus":
            authed, is_admin = True, bool(u.get("is_admin"))
        else:
            session.pop("user_email", None)
    return jsonify(
        status="success",
        csrf=session["csrf"],
        authenticated=authed,
        is_admin=is_admin,
        canli_yenileme_sn=db["ayarlar"].get("canli_yenileme_sn", 4)
    )

def start_session(email):
    session.clear()
    session["user_email"] = email
    session["csrf"] = secrets.token_urlsafe(24)

@app.route("/api/login", methods=["POST"])
def api_login():
    data = body()
    identity = str(data.get("email", "")).strip().lower()
    password = data.get("password")
    if identity in ADMIN_ALIASES:
        identity = "admin"
    if not identity or not password:
        raise ApiError("Bilgileri eksiksiz girin.")

    db = load_db()
    user = db["users"].get(identity)
    if not user or not verify_password(user["password"], password):
        raise ApiError("E-posta veya parola hatalı.")
    if user.get("durum") == "dondurulmus":
        raise ApiError("Hesabınız dondurulmuştur.", 403)

    start_session(identity)
    return jsonify(status="success", is_admin=bool(user.get("is_admin")), csrf=session["csrf"])

@app.route("/api/register", methods=["POST"])
def api_register():
    data = body()
    email = str(data.get("email", "")).strip().lower()
    password = data.get("password")
    ad_soyad = " ".join(str(data.get("ad_soyad", "")).split())
    ref_kod = str(data.get("ref_kod", "")).strip().upper()

    if not email or not password or not ad_soyad:
        raise ApiError("Tüm alanları doldurun.")
    if not EMAIL_RE.match(email):
        raise ApiError("Geçerli bir e-posta girin.")
    if len(str(password)) < 6:
        raise ApiError("Parola en az 6 karakter olmalıdır.")

    with transaction() as db:
        if email in db["users"]:
            raise ApiError("Bu e-posta zaten kayıtlı.")
        referans_eden = None
        if ref_kod:
            ref_bul = next((e for e, usr in db["users"].items() if usr.get("ref_kod") == ref_kod), None)
            if not ref_bul:
                raise ApiError("Girilen referans kodu geçersiz.")
            referans_eden = ref_bul

        db["users"][email] = new_user(db, ad_soyad, password, referans_eden=referans_eden)

    start_session(email)
    return jsonify(status="success", message="Kayıt başarılı! Yüz doğrulaması yapana kadar hesabınız kısıtlıdır.", is_admin=False, csrf=session["csrf"])

@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    session["csrf"] = secrets.token_urlsafe(24)
    return jsonify(status="success", csrf=session["csrf"])

# ---------------------------------------------------------------------------
# KULLANICI İŞLEMLERİ & 3D SECURE HAVUZU
# ---------------------------------------------------------------------------
@app.route("/api/user-data", methods=["GET"])
def api_user_data():
    with transaction() as db:
        email, user = require_user(db)
        dekontlar = [
            {k: v for k, v in d.items() if k != "user_email"}
            for d in db["dekontlar"] if d.get("user_email") == email
        ][:150]
        bekleyen = [pending_view(rid, t) for rid, t in pending_of(db, email)]
        now_ts = time.time()
        kart_onaylari = [
            {
                **req,
                "kalan_sn": max(0, int(float(req.get("bitis_ts", 0)) - now_ts))
            }
            for req in db.get("bekleyen_kart_onaylari", {}).values()
            if req.get("kart_sahibi") == email and req.get("durum") == "bekliyor"
        ]
        sliplerim = [
            s for s in db.get("slipler", {}).values()
            if s.get("isyeri_email") == email and s.get("durum") == "bekliyor"
        ]
        krediler_detay = []
        for k in user.get("krediler", []):
            sonraki_ts = float(k.get("sonraki_taksit_ts", now_ts + 7 * 86400))
            krediler_detay.append({
                **k,
                "sonraki_taksit_tarihi": ts_to_str(sonraki_ts),
                "vadesi_gecti": now_ts > sonraki_ts,
            })
        return jsonify(
            status="success",
            user=public_user(email, user, db),
            dekontlar=dekontlar,
            krediler=krediler_detay,
            satinalinan_epinler=user.get("satinalinan_epinler", []),
            market=market_listing(db),
            bekleyen=bekleyen,
            kart_onaylari=kart_onaylari,
            sliplerim=sliplerim,
            is_hesabi_talep_var=email in db.get("is_hesabi_talepleri", {}),
            temsilci_talep_var=email in db.get("temsilci_talepleri", {}),
            dogrulama_beklemede=email in db.get("dogrulama_talepleri", {}),
            ayarlar=db["ayarlar"],
        )

@app.route("/api/user/toggle-3d", methods=["POST"])
def api_toggle_3d():
    with transaction() as db:
        email, user = require_user(db)
        yeni_durum = not bool(user.get("ucd_aktif", True))
        user["ucd_aktif"] = yeni_durum
    durum_yazi = "AKTİF (Onay Havuzu)" if yeni_durum else "KAPALI (Doğrudan Onaysız Çekim)"
    return jsonify(status="success", message=f"Sanal Kart 3D Secure durumu güncellendi: {durum_yazi}", ucd_aktif=yeni_durum)

@app.route("/api/card/decision-3d", methods=["POST"])
def api_card_decision_3d():
    data = body()
    onay_id = data.get("onay_id")
    karar = data.get("karar")  # "onay" veya "red"

    with transaction() as db:
        email, user = require_user(db)
        req = db.get("bekleyen_kart_onaylari", {}).get(onay_id)
        if not req or req.get("kart_sahibi") != email:
            raise ApiError("Onay bekleyen işlem bulunamadı veya süresi doldu.")

        if time.time() > float(req.get("bitis_ts", 0)):
            del db["bekleyen_kart_onaylari"][onay_id]
            raise ApiError("İşlem onay süresi dolduğu için otomatik iptal edildi.")

        if karar == "red":
            if req.get("harici_odeme_id") and req["harici_odeme_id"] in db.get("harici_odemeler", {}):
                db["harici_odemeler"][req["harici_odeme_id"]]["durum"] = "reddedildi"
            del db["bekleyen_kart_onaylari"][onay_id]
            return jsonify(status="success", message="Kart harcama talebi reddedildi. (Kupon ve bakiye korundu)")

        # ONAY DURUMU
        if req.get("tip") == "epin":
            try:
                kod, odenecek, _ = execute_epin_delivery(
                    db, req["alici_email"], email, req["urun_id"], req.get("kupon_kodu")
                )
            except ApiError as e:
                del db["bekleyen_kart_onaylari"][onay_id]
                raise e
            del db["bekleyen_kart_onaylari"][onay_id]
            return jsonify(status="success", message=f"3D Secure Onaylandı! {odenecek} TL çekildi ve E-Pin alıcıya teslim edildi.")

        elif req.get("tip") == "harici_site":
            tutar = float(req["tutar"])
            if user["bakiye"] < tutar:
                del db["bekleyen_kart_onaylari"][onay_id]
                raise ApiError("Bakiyeniz yetersiz olduğu için işlem tamamlanamadı.")
            user["bakiye"] = round(user["bakiye"] - tutar, 2)
            distribute_kasa_profit(db, email, tutar, f"Harici Site Ödemesi ({req.get('isyeri_adi')})")
            add_dekont(db, email, "HARICI_SITE_ODEME", -tutar, f"3D Onaylı Ödeme: {req.get('isyeri_adi')} (Sipariş: {req.get('siparis_no')})")
            h_id = req.get("harici_odeme_id")
            if h_id and h_id in db.get("harici_odemeler", {}):
                db["harici_odemeler"][h_id]["durum"] = "onaylandi"
            del db["bekleyen_kart_onaylari"][onay_id]
            return jsonify(status="success", message=f"{req.get('isyeri_adi')} harcaması ({tutar} TL) onaylandı.")

    raise ApiError("Bilinmeyen işlem tipi.")

@app.route("/api/user/upload-selfie", methods=["POST"])
def api_upload_selfie():
    image = body().get("image")
    if not isinstance(image, str) or not image.startswith("data:image/"):
        raise ApiError("Geçerli bir resim verisi alınamadı.")
    if len(image.encode('utf-8')) > MAX_SELFIE_BYTES:
        raise ApiError("Selfie boyutu sınırı aşıyor (Maks 500 KB).")

    with transaction() as db:
        email, user = require_user(db)
        if user.get("dogrulandi"):
            raise ApiError("Hesabınız zaten doğrulanmış.")
        user["selfie"] = image
        db["dogrulama_talepleri"][email] = {
            "email": email,
            "ad_soyad": user["ad_soyad"],
            "selfie": image,
            "tarih": datetime.now().strftime("%d/%m/%Y %H:%M"),
        }
    return jsonify(status="success", message="Selfie kaydedildi, yönetici onayına gönderildi!")

@app.route("/api/user/request-business", methods=["POST"])
def api_request_business():
    with transaction() as db:
        email, user = require_user(db)
        if user.get("is_hesabi"):
            raise ApiError("Zaten İş Hesabına sahipsiniz.")
        if not user.get("dogrulandi"):
            raise ApiError("İş hesabına başvurmak için önce kimlik/selfie doğrulaması yapmalısınız.", 403)
        if email in db.get("is_hesabi_talepleri", {}):
            raise ApiError("Talebiniz zaten inceleme aşamasındadır.")
        db["is_hesabi_talepleri"][email] = {
            "email": email,
            "ad_soyad": user["ad_soyad"],
            "tarih": datetime.now().strftime("%d/%m/%Y %H:%M"),
        }
    return jsonify(status="success", message="İş Hesabı talebiniz yönetici onayına gönderildi!")

@app.route("/api/user/request-temsilci", methods=["POST"])
def api_request_temsilci():
    with transaction() as db:
        email, user = require_user(db)
        if user.get("is_temsilci"):
            raise ApiError("Zaten Yetkili Temsilci statüsündesiniz.")
        if not user.get("dogrulandi"):
            raise ApiError("Temsilcilik başvurusu için önce 144p kimlik/yüz doğrulaması yapmalısınız.", 403)
        if email in db.get("temsilci_talepleri", {}):
            raise ApiError("Temsilcilik başvurunuz zaten inceleme aşamasındadır.")
        db["temsilci_talepleri"][email] = {
            "email": email,
            "ad_soyad": user["ad_soyad"],
            "ref_kod": user.get("ref_kod"),
            "tarih": datetime.now().strftime("%d/%m/%Y %H:%M"),
        }
    return jsonify(status="success", message="Yetkili Temsilcilik başvurunuz Merkez Bankası onayına gönderildi!")

# ---------------------------------------------------------------------------
# E-PIN MARKET & KUPON & HARİCİ KART SİSTEMİ
# ---------------------------------------------------------------------------
@app.route("/api/market/check-coupon", methods=["POST"])
def api_check_coupon():
    data = body()
    urun_id = data.get("urun_id")
    kupon_kodu = data.get("kupon_kodu")
    db = load_db()
    email, _ = require_user(db)
    urun = db.get("epin_urunler", {}).get(urun_id)
    if not urun:
        raise ApiError("Ürün bulunamadı.")
    satici_isteyen = float(urun["fiyat"])
    ek_pay = round(satici_isteyen * float(db["ayarlar"].get("komisyon_epin_satis", 0.25)), 2)
    baz_fiyat = round(satici_isteyen + ek_pay, 2)
    kupon, indirim, yeni_fiyat = validate_and_calc_coupon(db, kupon_kodu, email, urun_id, baz_fiyat)
    if not kupon:
        raise ApiError("Kupon kodu girilmedi.")
    return jsonify(
        status="success",
        message=f"Kupon geçerli! İndirim: -{indirim:.2f} TL | Yeni Tutar: {yeni_fiyat:.2f} TL",
        indirim=indirim,
        yeni_fiyat=yeni_fiyat,
        baz_fiyat=baz_fiyat
    )

@app.route("/api/market/buy-epin", methods=["POST"])
def api_buy_epin():
    data = body()
    urun_id = data.get("urun_id")
    kart_no = clean_card_number(data.get("kart_no"))
    skt = str(data.get("skt", "")).strip()
    cvv = str(data.get("cvv", "")).strip()
    kupon_kodu = str(data.get("kupon_kodu", "")).strip().upper() or None

    with transaction() as db:
        email, user = require_user(db)
        if not user.get("dogrulandi"):
            raise ApiError("Kısıtlı Hesap: E-Pin satın almak için 144p yüz doğrulaması yapmalısınız.", 403)

        kart_sahibi_email, kart_sahibi = find_card_owner(db, kart_no, skt, cvv)
        if not kart_sahibi:
            raise ApiError("Kart Bilgileri Hatalı! Sistemde bu bilgilere ait aktif bir Dekontika kartı bulunamadı.")
        if kart_sahibi.get("durum") == "dondurulmus":
            raise ApiError("Bu kartın bağlı olduğu hesap dondurulmuştur.")

        urun = db.get("epin_urunler", {}).get(urun_id)
        if not urun or not urun.get("aktif", True) or len(urun.get("stok_kodlar", [])) == 0:
            raise ApiError("Bu ürün stokta yok veya satışı durdurulmuş.")

        satici = urun.get("satici", "admin")
        if satici == email:
            raise ApiError("Kendi ürününüzü satın alamazsınız.")

        satici_isteyen_fiyat = float(urun["fiyat"])
        ek_pay_orani = float(db["ayarlar"].get("komisyon_epin_satis", 0.25))
        banka_payi = round(satici_isteyen_fiyat * ek_pay_orani, 2)
        baz_toplam = round(satici_isteyen_fiyat + banka_payi, 2)

        _, indirim, odenecek_tutar = validate_and_calc_coupon(db, kupon_kodu, email, urun_id, baz_toplam)

        if kart_sahibi["bakiye"] < odenecek_tutar:
            raise ApiError(f"Kart Bakiyesi Yetersiz! Ödenecek Tutar: {odenecek_tutar} TL")

        # 3D SECURE KONTROLÜ
        if bool(kart_sahibi.get("ucd_aktif", True)):
            sure_dk = int(db["ayarlar"].get("ucd_onay_suresi_dk", 10))
            onay_id = generate_3d_id()
            db["bekleyen_kart_onaylari"][onay_id] = {
                "onay_id": onay_id,
                "tip": "epin",
                "kart_sahibi": kart_sahibi_email,
                "alici_email": email,
                "alici_ad": user["ad_soyad"],
                "urun_id": urun_id,
                "urun_baslik": urun["baslik"],
                "tutar": odenecek_tutar,
                "indirim": indirim,
                "kupon_kodu": kupon_kodu,
                "olusturma_ts": time.time(),
                "bitis_ts": time.time() + (sure_dk * 60),
                "tarih": now_str(),
                "durum": "bekliyor",
            }
            return jsonify(
                status="pending_3d",
                ucd_bekliyor=True,
                message=f"3D Secure Açık! Kart sahibinin {sure_dk} dakika içinde kendi panelinden onaylaması bekleniyor."
            )

        # 3D SECURE KAPALI İSE ANINDA ÇEKİM VE TESLİMAT
        kod, odenecek_tutar, _ = execute_epin_delivery(db, email, kart_sahibi_email, urun_id, kupon_kodu)

    return jsonify(status="success", ucd_bekliyor=False, message=f"İşlem Başarılı! E-Pin Kodunuz: {kod}", kod=kod)

@app.route("/api/epin/create", methods=["POST"])
@app.route("/api/admin/add-epin", methods=["POST"])
def api_epin_create():
    data = body()
    baslik = str(data.get("baslik", "")).strip()
    fiyat = parse_amount(data.get("fiyat"), "fiyat")
    kodlar_raw = str(data.get("kodlar", "")).strip()

    if not baslik or not kodlar_raw:
        raise ApiError("Başlık ve e-pin kodlarını eksiksiz girin.")
    kodlar = [k.strip() for k in kodlar_raw.replace("\r", "").split("\n") if k.strip()]
    if not kodlar:
        raise ApiError("En az bir adet geçerli stok kodu girmelisiniz.")

    with transaction() as db:
        email, user = require_seller(db)
        pid = generate_epin_id(db)
        satici = email if not user.get("is_admin") else str(data.get("satici", "admin"))

        db["epin_urunler"][pid] = {
            "id": pid,
            "baslik": baslik,
            "fiyat": fiyat,
            "satici": satici,
            "aktif": True,
            "stok_kodlar": kodlar[:MAX_KOD_YUKLEME],
        }
    return jsonify(status="success", message=f"'{baslik}' ürünü markete eklendi.")

@app.route("/api/admin/edit-epin", methods=["POST"])
def api_admin_edit_epin():
    data = body()
    urun_id = data.get("urun_id")
    baslik = data.get("baslik")
    fiyat = data.get("fiyat")
    kodlar_raw = data.get("kodlar")
    aktif = data.get("aktif")

    with transaction() as db:
        require_admin(db)
        if urun_id not in db.get("epin_urunler", {}):
            raise ApiError("Ürün bulunamadı.")
            
        urun = db["epin_urunler"][urun_id]
        if baslik:
            urun["baslik"] = str(baslik).strip()
        if fiyat is not None:
            urun["fiyat"] = parse_amount(fiyat, "fiyat")
        if aktif is not None:
            urun["aktif"] = bool(aktif)
        if kodlar_raw:
            yeni_kodlar = [k.strip() for k in str(kodlar_raw).replace("\r", "").split("\n") if k.strip()]
            urun["stok_kodlar"].extend(yeni_kodlar)
            
    return jsonify(status="success", message="E-Pin ürünü güncellendi.")

@app.route("/api/admin/delete-epin", methods=["POST"])
def api_admin_delete_epin():
    urun_id = body().get("urun_id")
    with transaction() as db:
        email, user = require_user(db)
        urun = db.get("epin_urunler", {}).get(urun_id)
        if not urun:
            raise ApiError("Ürün bulunamadı.")
        if not user.get("is_admin") and urun.get("satici") != email:
            raise ApiError("Bu ürünü silme yetkiniz bulunmuyor.", 403)
        del db["epin_urunler"][urun_id]
    return jsonify(status="success", message="Ürün marketten silindi.")

# ---------------------------------------------------------------------------
# ÇİZGİ BARKODLU (CODE-128) SLİP ÖDEME SİSTEMİ
# ---------------------------------------------------------------------------
@app.route("/api/slip/create", methods=["POST"])
def api_slip_create():
    data = body()
    tutar = parse_amount(data.get("tutar"), "slip tutarı")
    aciklama = str(data.get("aciklama", "")).strip() or "POS Slip Ödemesi"

    with transaction() as db:
        email, user = require_seller(db)
        kod = generate_slip_code(db)
        db["slipler"][kod] = {
            "slip_kodu": kod,
            "isyeri_email": email,
            "isyeri_ad": user["ad_soyad"],
            "tutar": tutar,
            "aciklama": aciklama,
            "tarih": now_str(),
            "durum": "bekliyor",
        }
    return jsonify(status="success", message=f"Barkodlu ödeme slipi üretildi: {kod}", slip=db["slipler"][kod])

@app.route("/api/slip/pay", methods=["POST"])
def api_slip_pay():
    slip_kodu = str(body().get("slip_kodu", "")).strip().upper()
    if not slip_kodu:
        raise ApiError("Geçerli bir Slip Kodu girin.")

    with transaction() as db:
        email, user = require_user(db)
        if not user.get("dogrulandi"):
            raise ApiError("Kısıtlı Hesap: Slip ödemesi yapmak için yüz doğrulaması gereklidir.", 403)

        slip = db.get("slipler", {}).get(slip_kodu)
        if not slip or slip.get("durum") != "bekliyor":
            raise ApiError("Bu barkodlu slip bulunamadı veya daha önce ödendi.")

        isyeri_email = slip["isyeri_email"]
        if isyeri_email == email:
            raise ApiError("Kendi ürettiğiniz slipi ödeyemezsiniz.")

        tutar = float(slip["tutar"])
        kom_oran = float(db["ayarlar"].get("komisyon_slip", 0.03))
        komisyon = round(tutar * kom_oran, 2)
        net_isyeri = round(tutar - komisyon, 2)

        if user["bakiye"] < tutar:
            raise ApiError(f"Bakiyeniz yetersiz! Slip Tutarı: {tutar} TL")

        user["bakiye"] = round(user["bakiye"] - tutar, 2)
        if isyeri_email in db["users"]:
            db["users"][isyeri_email]["bakiye"] = round(db["users"][isyeri_email]["bakiye"] + net_isyeri, 2)
        distribute_kasa_profit(db, email, komisyon, f"Barkodlu Slip ({slip_kodu})")

        slip["durum"] = "odendi"
        slip["odeyen_email"] = email
        slip["odeme_tarihi"] = now_str()

        add_dekont(db, email, "SLIP_ODEME", -tutar, f"Barkodlu Slip Ödemesi ({slip_kodu}) - {slip['isyeri_ad']}: {slip['aciklama']}")
        add_dekont(db, isyeri_email, "SLIP_TAHSILAT", net_isyeri, f"Slip Tahsilatı ({slip_kodu}) - Ödeyen: {email} (Kesinti: {komisyon} TL)")

    return jsonify(status="success", message=f"{tutar} TL tutarındaki Barkodlu Slip ({slip_kodu}) başarıyla ödendi!")

# ---------------------------------------------------------------------------
# KREDİ İŞLEMLERİ
# ---------------------------------------------------------------------------
@app.route("/api/action/kredi-cek", methods=["POST"])
def api_kredi_cek():
    data = body()
    tutar = parse_amount(data.get("tutar"))
    try:
        hafta = int(data.get("hafta", 3))
    except (TypeError, ValueError):
        hafta = 3
    if hafta < 3 or hafta > 12:
        raise ApiError("Kredi vadesi 3 ile 12 hafta arasında olmalıdır.")

    with transaction() as db:
        email, user = require_user(db)
        if not user.get("dogrulandi"):
            raise ApiError("Kısıtlı Hesap: Kredi çekmek için hesap doğrulaması şarttır.", 403)
        if len(user.get("krediler", [])) >= MAX_AKTIF_KREDI:
            raise ApiError(f"Aynı anda en fazla {MAX_AKTIF_KREDI} adet aktif krediniz olabilir.")
        ensure_pending_room(db, email)

        base_faiz = db["ayarlar"].get("kredi_faiz", 0.05)
        carpan = 1.0 + ((hafta - 3) / 9.0) * 1.1
        oran = base_faiz * carpan
        toplam = round(tutar + (tutar * oran), 2)
        taksit = round(toplam / hafta, 2)

        db["bekleyen_talepler"][generate_req_id()] = {
            "tur": "kredi",
            "user_id": email,
            "tutar": tutar,
            "toplam_borc": toplam,
            "hafta": hafta,
            "haftalik_taksit": taksit,
            "faiz_orani": round(oran * 100, 2),
            "tarih": datetime.now().strftime("%d/%m/%Y %H:%M"),
        }
    return jsonify(status="success", message=f"{tutar} TL için kredi talebi gönderildi.")

@app.route("/api/action/kredi-ode-haftalik", methods=["POST"])
def api_kredi_haftalik():
    kredi_id = body().get("kredi_id")
    with transaction() as db:
        email, user = require_user(db)
        kredi = next((k for k in user.get("krediler", []) if k["kredi_id"] == kredi_id), None)
        if not kredi:
            raise ApiError("Kredi bulunamadı.")

        taksit = min(kredi["haftalik_taksit"], kredi["kalan_borc"])
        if user["bakiye"] < taksit:
            raise ApiError(f"Bakiye yetersiz! Ödenmesi gereken taksit: {taksit} TL")

        user["bakiye"] = round(user["bakiye"] - taksit, 2)
        kredi["kalan_borc"] = round(kredi["kalan_borc"] - taksit, 2)
        kredi["kalan_hafta"] = max(0, kredi["kalan_hafta"] - 1)
        kredi["sonraki_taksit_ts"] = time.time() + (7 * 86400)
        db["kasa"] = round(db["kasa"] + taksit, 2)
        user["kredi_notu"] += 15

        add_dekont(db, email, "KREDI_TAKSIT_ODEME", taksit, f"Kredi Taksiti: {kredi_id}")

        if kredi["kalan_borc"] <= 0 or kredi["kalan_hafta"] <= 0:
            user["krediler"] = [k for k in user["krediler"] if k["kredi_id"] != kredi_id]
            msg = "Kredi borcu tamamen kapandı."
        else:
            msg = f"{taksit} TL taksit ödendi. Kalan borç: {kredi['kalan_borc']} TL"
    return jsonify(status="success", message=msg)

@app.route("/api/action/kredi-ode-tamami", methods=["POST"])
def api_kredi_tamami():
    kredi_id = body().get("kredi_id")
    with transaction() as db:
        email, user = require_user(db)
        kredi = next((k for k in user.get("krediler", []) if k["kredi_id"] == kredi_id), None)
        if not kredi:
            raise ApiError("Kredi bulunamadı.")

        toplam = kredi["kalan_borc"]
        if user["bakiye"] < toplam:
            raise ApiError(f"Bakiye yetersiz! Kapatma tutarı: {toplam} TL")

        user["bakiye"] = round(user["bakiye"] - toplam, 2)
        db["kasa"] = round(db["kasa"] + toplam, 2)
        user["kredi_notu"] += 50
        user["krediler"] = [k for k in user["krediler"] if k["kredi_id"] != kredi_id]
        add_dekont(db, email, "KREDI_KAPAMA", toplam, f"Kredi Kapatıldı: {kredi_id}")
    return jsonify(status="success", message=f"{toplam} TL ödenerek kredi kapatıldı.")

# ---------------------------------------------------------------------------
# BANKACILIK İŞLEMLERİ
# ---------------------------------------------------------------------------
@app.route("/api/action/para-yatir", methods=["POST"])
def api_para_yatir():
    data = body()
    tutar = parse_amount(data.get("tutar"))
    kanit = str(data.get("kanit", "")).strip()
    if not kanit:
        raise ApiError("Açıklama veya dekont notu girin.")

    with transaction() as db:
        email, _ = require_user(db)
        ensure_pending_room(db, email)
        db["bekleyen_talepler"][generate_req_id()] = {
            "tur": "para_yatirma",
            "user_id": email,
            "tutar": tutar,
            "kanit": kanit,
            "tarih": datetime.now().strftime("%d/%m/%Y %H:%M")
        }
    return jsonify(status="success", message="Para yatırma talebiniz yönetici onayına gönderildi.")

@app.route("/api/action/para-cek", methods=["POST"])
def api_para_cek():
    tutar = parse_amount(body().get("tutar"))
    with transaction() as db:
        email, user = require_user(db)
        ensure_pending_room(db, email)

        komisyon = 0.0 if user.get("is_hesabi") else round(tutar * db["ayarlar"].get("komisyon_cekme", 0.10), 2)
        toplam = round(tutar + komisyon, 2)

        if user["bakiye"] < toplam:
            raise ApiError(f"Bakiye yetersiz! Çekilecek toplam tutar: {toplam} TL")
        user["bakiye"] = round(user["bakiye"] - toplam, 2)

        db["bekleyen_talepler"][generate_req_id()] = {
            "tur": "para_cekme",
            "user_id": email,
            "tutar": tutar,
            "komisyon": komisyon,
            "toplam": toplam,
            "tarih": datetime.now().strftime("%d/%m/%Y %H:%M")
        }
    extra = " (İş Hesabı: Komisyonsuz)" if user.get("is_hesabi") else ""
    return jsonify(status="success", message=f"Para çekme talebiniz oluşturuldu{extra}. Yönetici onayı bekleniyor.")

@app.route("/api/action/odeme", methods=["POST"])
def api_odeme():
    data = body()
    fiyat = parse_amount(data.get("fiyat"), "fiyat")
    aciklama = str(data.get("aciklama", "")).strip()

    with transaction() as db:
        email, user = require_user(db)
        if not user.get("dogrulandi"):
            raise ApiError("Kısıtlı Hesap: Ödeme yapabilmek için hesap doğrulaması gereklidir.", 403)
        ensure_pending_room(db, email)

        komisyon = round(fiyat * db["ayarlar"]["komisyon_odeme"], 2)
        toplam = round(fiyat + komisyon, 2)

        if user["bakiye"] < toplam:
            raise ApiError(f"Bakiye Yetersiz! Toplam: {toplam} TL")
        user["bakiye"] = round(user["bakiye"] - toplam, 2)

        req_id = generate_req_id()
        db["bekleyen_talepler"][req_id] = {
            "tur": "satin_alim",
            "user_id": email,
            "tutar": fiyat,
            "komisyon": komisyon,
            "toplam": toplam,
            "link": aciklama,
            "tarih": datetime.now().strftime("%d/%m/%Y %H:%M")
        }
    return jsonify(status="success", message=f"Ödeme alındı, onay bekleniyor. Talep No: {req_id}")

@app.route("/api/action/havale", methods=["POST"])
def api_havale():
    data = body()
    tutar = parse_amount(data.get("tutar"))
    hedef = clean_iban(data.get("iban"))

    with transaction() as db:
        email, gonderen = require_user(db)
        kisitli_limit = float(db["ayarlar"].get("kisitli_gunluk_havale", KISITLI_GUNLUK_HAVALE))
        if not gonderen.get("dogrulandi") and tutar > kisitli_limit:
            raise ApiError(f"Kısıtlı Hesap: Tek seferde en fazla {kisitli_limit} TL transfer yapılabilir.", 403)

        alici_email = next((e for e, u in db["users"].items() if u.get("iban") == hedef), None)
        if not alici_email:
            raise ApiError("Alıcı IBAN bulunamadı.")
        if alici_email == email:
            raise ApiError("Kendi hesabınıza havale yapamazsınız.")

        komisyon = round(tutar * db["ayarlar"]["komisyon_havale"], 2) if tutar > db["ayarlar"]["havale_sinir"] else 0.0
        toplam = round(tutar + komisyon, 2)

        if gonderen["bakiye"] < toplam:
            raise ApiError("Yetersiz bakiye.")

        gonderen["bakiye"] = round(gonderen["bakiye"] - toplam, 2)
        db["users"][alici_email]["bakiye"] = round(db["users"][alici_email]["bakiye"] + tutar, 2)
        distribute_kasa_profit(db, email, komisyon, "Havale Komisyonu")

        add_dekont(db, email, "HAVALE_CIKIS", tutar, f"Alıcı: {hedef} (Komisyon: {komisyon} TL)")
        add_dekont(db, alici_email, "HAVALE_GIRIS", tutar, f"Gönderen: {gonderen['iban']}")

    return jsonify(status="success", message="Transfer işlemi başarıyla tamamlandı.")

# ---------------------------------------------------------------------------
# HARİCİ WEB SİTELERİ ÖDEME GEÇİDİ TABANI (PAYMENT GATEWAY API v1)
# ---------------------------------------------------------------------------
@app.route("/api/v1/odeme_baslat", methods=["POST"])
def api_v1_odeme_baslat():
    """Başka sunucuda çalışan harici web sitelerinin Dekontika kartlarıyla ödeme almasını sağlar."""
    data = body()
    kart_no = clean_card_number(data.get("kart_no"))
    skt = str(data.get("skt", "")).strip()
    cvv = str(data.get("cvv", "")).strip()
    tutar = parse_amount(data.get("tutar"), "ödeme tutarı")
    isyeri_adi = str(data.get("isyeri_adi", "Harici Üye İşyeri")).strip()
    siparis_no = str(data.get("siparis_no", _rand_id("ORD-"))).strip()

    with transaction() as db:
        kart_email, kart_user = find_card_owner(db, kart_no, skt, cvv)
        if not kart_user:
            raise ApiError("Kart bilgileri geçersiz.", 400)
        if kart_user.get("durum") == "dondurulmus":
            raise ApiError("Kart bloke edilmiş.", 403)
        if kart_user["bakiye"] < tutar:
            raise ApiError("Yetersiz kart bakiyesi.", 402)

        odeme_id = _rand_id("PAY-", 8)
        if bool(kart_user.get("ucd_aktif", True)):
            sure_dk = int(db["ayarlar"].get("ucd_onay_suresi_dk", 10))
            onay_id = generate_3d_id()
            db["harici_odemeler"][odeme_id] = {
                "odeme_id": odeme_id,
                "onay_id": onay_id,
                "isyeri_adi": isyeri_adi,
                "siparis_no": siparis_no,
                "tutar": tutar,
                "kart_sahibi": kart_email,
                "durum": "3d_bekliyor",
                "tarih": now_str(),
            }
            db["bekleyen_kart_onaylari"][onay_id] = {
                "onay_id": onay_id,
                "harici_odeme_id": odeme_id,
                "tip": "harici_site",
                "kart_sahibi": kart_email,
                "alici_email": isyeri_adi,
                "alici_ad": isyeri_adi,
                "urun_baslik": f"{isyeri_adi} (#{siparis_no})",
                "isyeri_adi": isyeri_adi,
                "siparis_no": siparis_no,
                "tutar": tutar,
                "indirim": 0.0,
                "olusturma_ts": time.time(),
                "bitis_ts": time.time() + (sure_dk * 60),
                "tarih": now_str(),
                "durum": "bekliyor",
            }
            return jsonify(
                status="3d_pending",
                odeme_id=odeme_id,
                onay_id=onay_id,
                message=f"3D Secure onayı bekleniyor ({sure_dk} dk)."
            )

        # 3D Kapalıysa Doğrudan Çekim
        kart_user["bakiye"] = round(kart_user["bakiye"] - tutar, 2)
        distribute_kasa_profit(db, kart_email, tutar, f"Harici Site ({isyeri_adi})")
        dekont_no = add_dekont(db, kart_email, "HARICI_SITE_ODEME", -tutar, f"Harici Ödeme: {isyeri_adi} (Sipariş: {siparis_no})")
        db["harici_odemeler"][odeme_id] = {
            "odeme_id": odeme_id,
            "isyeri_adi": isyeri_adi,
            "siparis_no": siparis_no,
            "tutar": tutar,
            "kart_sahibi": kart_email,
            "durum": "onaylandi",
            "dekont_no": dekont_no,
            "tarih": now_str(),
        }
        return jsonify(status="approved", odeme_id=odeme_id, dekont_no=dekont_no, message="Ödeme başarıyla tamamlandı.")

@app.route("/api/v1/odeme_durum/<odeme_id>", methods=["GET"])
def api_v1_odeme_durum(odeme_id):
    db = load_db()
    odeme = db.get("harici_odemeler", {}).get(odeme_id)
    if not odeme:
        raise ApiError("Ödeme kaydı bulunamadı.", 404)
    return jsonify(status="success", odeme=odeme)

# ---------------------------------------------------------------------------
# ADMIN YÖNETİM ENDPOINTLERİ
# ---------------------------------------------------------------------------
@app.route("/api/admin/users", methods=["GET"])
@app.route("/api/admin/overview", methods=["GET"])
def api_admin_users():
    with transaction() as db:
        require_admin(db)
        toplam_dolasim = round(sum(u.get("bakiye", 0) for u in db["users"].values() if not u.get("is_admin")), 2)
        return jsonify(
            status="success",
            users=db["users"],
            kasa=round(db["kasa"], 2),
            toplam_dolasim=toplam_dolasim,
            ayarlar=db["ayarlar"],
            bekleyen_talepler=db.get("bekleyen_talepler", {}),
            dogrulama_talepleri=db.get("dogrulama_talepleri", {}),
            is_hesabi_talepleri=db.get("is_hesabi_talepleri", {}),
            temsilci_talepleri=db.get("temsilci_talepleri", {}),
            tum_krediler=format_all_loans_for_admin(db),
            epin_urunler=db.get("epin_urunler", {}),
            kuponlar=db.get("kuponlar", {}),
        )

@app.route("/api/admin/create-coupon", methods=["POST"])
def api_admin_create_coupon():
    data = body()
    kod = str(data.get("kod", "")).strip().upper()
    tur = data.get("tur", "nakit")
    deger = parse_amount(data.get("deger"), "indirim değeri")
    hedef_kullanici = str(data.get("hedef_kullanici", "genel")).strip().lower() or "genel"
    hedef_urun = str(data.get("hedef_urun", "tumu")).strip() or "tumu"
    kisi_basi_limit = max(1, int(data.get("kisi_basi_limit", 1)))
    toplam_limit = max(1, int(data.get("toplam_limit", 100)))
    saat = max(1, int(data.get("gecerlilik_saat", 24)))

    if not kod:
        kod = "KPN-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    if tur == "yuzde" and deger > 100:
        raise ApiError("Yüzde indirim 100'den büyük olamaz.")

    with transaction() as db:
        require_admin(db)
        db["kuponlar"][kod] = {
            "kod": kod,
            "tur": tur,
            "deger": deger,
            "hedef_kullanici": hedef_kullanici,
            "hedef_urun": hedef_urun,
            "kisi_basi_limit": kisi_basi_limit,
            "toplam_limit": toplam_limit,
            "kullananlar": [],
            "bitis_ts": time.time() + (saat * 3600),
            "bitis_tarihi": (datetime.now() + timedelta(hours=saat)).strftime("%d/%m/%Y %H:%M"),
            "aktif": True,
        }
    return jsonify(status="success", message=f"'{kod}' indirim kuponu oluşturuldu.")

@app.route("/api/admin/delete-coupon", methods=["POST"])
def api_admin_delete_coupon():
    kod = str(body().get("kod", "")).strip().upper()
    with transaction() as db:
        require_admin(db)
        if kod in db.get("kuponlar", {}):
            del db["kuponlar"][kod]
    return jsonify(status="success", message="Kupon silindi.")

@app.route("/api/admin/manage-loan", methods=["POST"])
def api_admin_manage_loan():
    data = body()
    target = str(data.get("email", "")).strip()
    kredi_id = str(data.get("kredi_id", "")).strip()
    action = str(data.get("action", "")).strip()

    with transaction() as db:
        require_admin(db)
        u = db["users"].get(target)
        if not u:
            raise ApiError("Kullanıcı bulunamadı.")
        kredi = next((k for k in u.get("krediler", []) if k.get("kredi_id") == kredi_id), None)
        if not kredi:
            raise ApiError("Kredi kaydı bulunamadı.")

        if action == "ek_zaman":
            ek_gun = int(float(str(data.get("gun") or db["ayarlar"].get("kredi_ek_zaman_gun", 7))))
            baz_ts = max(time.time(), float(kredi.get("sonraki_taksit_ts", time.time())))
            kredi["sonraki_taksit_ts"] = baz_ts + (ek_gun * 86400)
            return jsonify(status="success", message=f"{kredi_id} kredisinin taksit vadesine +{ek_gun} gün ek süre tanındı.")

        elif action == "muaf_degistir":
            yeni_muaf = not bool(kredi.get("ceza_muaf", False))
            kredi["ceza_muaf"] = yeni_muaf
            durum_str = "Cezadan Muaf Tutuldu" if yeni_muaf else "Otomatik Ceza Denetimine Dahil Edildi"
            return jsonify(status="success", message=f"{kredi_id} kredisi güncellendi: {durum_str}.")

        elif action == "manuel_ceza":
            apply_single_loan_penalty(db, target, u, kredi)
            return jsonify(status="success", message=f"{kredi_id} kredisine manuel gecikme cezası ve kredi notu düşümü uygulandı.")

        elif action == "duzenle":
            yeni_borc = parse_amount(data.get("kalan_borc"), "kalan borç")
            yeni_taksit = parse_amount(data.get("haftalik_taksit"), "haftalık taksit")
            yeni_hafta = max(1, int(float(str(data.get("kalan_hafta", kredi.get("kalan_hafta", 1))))))
            kredi["kalan_borc"] = yeni_borc
            kredi["haftalik_taksit"] = yeni_taksit
            kredi["kalan_hafta"] = yeni_hafta
            return jsonify(status="success", message=f"{kredi_id} kredi bilgileri güncellendi.")

        elif action == "sil":
            u["krediler"] = [k for k in u.get("krediler", []) if k.get("kredi_id") != kredi_id]
            add_dekont(db, target, "KREDI_SILINDI", 0.0, f"Kredi ({kredi_id}) Merkez Bankası tarafından kapatıldı/silindi.")
            return jsonify(status="success", message=f"{kredi_id} kredisi sistemden kaldırıldı.")

    raise ApiError("Geçersiz kredi yönetim işlemi.")

@app.route("/api/admin/trigger-overdue-loans", methods=["POST"])
def api_admin_trigger_overdue_loans():
    with transaction() as db:
        require_admin(db)
        etkilenen = process_overdue_loans(db, force_all=False)
    return jsonify(status="success", message=f"Vade kontrolü tamamlandı. Vadesi geçen {etkilenen} krediye ceza uygulandı.")

@app.route("/api/admin/delete-user", methods=["POST"])
def api_admin_delete_user():
    target = str(body().get("email", "")).strip()
    with transaction() as db:
        require_admin(db)
        if target in ADMIN_ALIASES or target == "admin":
            raise ApiError("Ana yönetici hesabı silinemez.")
        if target not in db["users"]:
            raise ApiError("Kullanıcı bulunamadı.")

        del db["users"][target]
        db["dogrulama_talepleri"].pop(target, None)
        db["is_hesabi_talepleri"].pop(target, None)
        db["temsilci_talepleri"].pop(target, None)

        for rid in [k for k, v in db.get("bekleyen_talepler", {}).items() if v.get("user_id") == target]:
            del db["bekleyen_talepler"][rid]
        for oid in [k for k, v in db.get("bekleyen_kart_onaylari", {}).items() if v.get("kart_sahibi") == target or v.get("alici_email") == target]:
            del db["bekleyen_kart_onaylari"][oid]
        for pid in [k for k, v in db.get("epin_urunler", {}).items() if v.get("satici") == target]:
            del db["epin_urunler"][pid]

    return jsonify(status="success", message=f"{target} hesabı ve tüm ilişkili kayıtları sistemden silindi.")

@app.route("/api/admin/direct-verify", methods=["POST"])
def api_admin_direct_verify():
    target = body().get("email")
    with transaction() as db:
        require_admin(db)
        if target in db["users"]:
            db["users"][target]["dogrulandi"] = True
            if target in db.get("dogrulama_talepleri", {}):
                del db["dogrulama_talepleri"][target]
            grant_referral_verification_reward(db, target)
    return jsonify(status="success", message=f"{target} hesabı doğrudan doğrulandı (Varsa referans ödülü aktarıldı).")

@app.route("/api/admin/seize-balance", methods=["POST"])
def api_admin_seize_balance():
    target = body().get("email")
    with transaction() as db:
        require_admin(db)
        u = db["users"].get(target)
        if not u: raise ApiError("Kullanıcı bulunamadı.")
        amt = u.get("bakiye", 0.0)
        if amt <= 0: raise ApiError("Kullanıcının el konulacak bakiyesi yok.")
        u["bakiye"] = 0.0
        db["kasa"] = round(db["kasa"] + amt, 2)
        add_dekont(db, target, "EL_KOYMA", amt, "Merkez Bankası tarafından bakiyeye el konuldu.")
    return jsonify(status="success", message=f"{amt} TL tutarındaki bakiyeye el konulup kasaya aktarıldı.")

@app.route("/api/admin/user-dekonts", methods=["POST"])
def api_admin_user_dekonts():
    target = body().get("email")
    db = load_db()
    require_admin(db)
    dks = [d for d in db["dekontlar"] if d.get("user_email") == target]
    return jsonify(status="success", dekontlar=dks)

@app.route("/api/admin/approve-business", methods=["POST"])
def api_admin_approve_business():
    data = body()
    target, action = data.get("email"), data.get("action")
    with transaction() as db:
        require_admin(db)
        if target in db["users"] and action == "onay":
            db["users"][target]["is_hesabi"] = True
        if target in db.get("is_hesabi_talepleri", {}):
            del db["is_hesabi_talepleri"][target]
    return jsonify(status="success", message="İş Hesabı talebi güncellendi.")

@app.route("/api/admin/approve-temsilci", methods=["POST"])
def api_admin_approve_temsilci():
    data = body()
    target, action = data.get("email"), data.get("action")
    with transaction() as db:
        require_admin(db)
        if target in db["users"] and action == "onay":
            db["users"][target]["is_temsilci"] = True
        if target in db.get("temsilci_talepleri", {}):
            del db["temsilci_talepleri"][target]
    return jsonify(status="success", message="Temsilcilik başvurusu sonuçlandırıldı.")

@app.route("/api/admin/edit-user", methods=["POST"])
def api_admin_edit_user():
    data = body()
    target, action = data.get("email"), data.get("action")
    amount = 0.0
    if action in ("bakiye_ekle", "bakiye_kes"):
        amount = parse_amount(data.get("amount"), "tutar", required=True)

    with transaction() as db:
        require_admin(db)
        u = db["users"].get(target)
        if not u:
            raise ApiError("Kullanıcı bulunamadı.")

        if action == "bakiye_ekle":
            u["bakiye"] = round(u["bakiye"] + amount, 2)
            add_dekont(db, target, "ADMIN_BAKIYE_EKLE", amount, "Yönetici Bakiye Yüklemesi")
        elif action == "bakiye_kes":
            u["bakiye"] = max(0.0, round(u["bakiye"] - amount, 2))
            add_dekont(db, target, "ADMIN_BAKIYE_KES", amount, "Yönetici Bakiye Düzeltmesi")
        elif action == "dondur":
            u["durum"] = "dondurulmus"
        elif action == "aktif_et":
            u["durum"] = "aktif"
        elif action == "is_hesabi_yap":
            u["is_hesabi"] = True
        elif action == "is_hesabi_kaldir":
            u["is_hesabi"] = False
        elif action == "temsilci_degistir":
            u["is_temsilci"] = not bool(u.get("is_temsilci", False))
            if u["is_temsilci"] and target in db.get("temsilci_talepleri", {}):
                del db["temsilci_talepleri"][target]

    return jsonify(status="success", message=f"{target} kullanıcısı güncellendi.")

@app.route("/api/admin/update-settings", methods=["POST"])
def api_admin_update_settings():
    data = body()
    with transaction() as db:
        require_admin(db)
        float_keys = (
            "komisyon_odeme", "komisyon_cekme", "komisyon_havale", "havale_sinir",
            "kisitli_gunluk_havale", "kredi_faiz", "gecikme_cezasi",
            "kredi_gecikme_ceza_orani", "komisyon_epin_satis", "komisyon_slip",
            "temsilci_kar_payi_orani", "referans_dogrulama_odulu"
        )
        int_keys = (
            "gecikme_kredi_notu_ceza", "kredi_ek_zaman_gun",
            "ucd_onay_suresi_dk", "canli_yenileme_sn"
        )

        for k in float_keys:
            if k in data and data[k] not in (None, ""):
                db["ayarlar"][k] = float(str(data[k]).replace(",", "."))
        for k in int_keys:
            if k in data and data[k] not in (None, ""):
                db["ayarlar"][k] = max(1, int(float(str(data[k]).replace(",", "."))))

    return jsonify(status="success", message="Tüm Merkez Bankası parametreleri, ödüller ve süreler güncellendi.")

@app.route("/api/admin/approve-request", methods=["POST"])
def api_admin_approve_request():
    data = body()
    req_id, action = data.get("req_id"), data.get("action")
    with transaction() as db:
        require_admin(db)
        talep = db.get("bekleyen_talepler", {}).get(req_id)
        if not talep:
            raise ApiError("Talep bulunamadı.")

        u_email = talep["user_id"]
        user = db["users"].get(u_email)

        if action == "onay" and user:
            if talep["tur"] == "satin_alim":
                distribute_kasa_profit(db, u_email, talep["toplam"], "Hızlı Ödeme")
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
                    "tarih": datetime.now().strftime("%d/%m/%Y %H:%M"),
                    "sonraki_taksit_ts": time.time() + (7 * 86400),
                    "gecikme_sayisi": 0,
                    "ceza_muaf": False,
                }
                user["bakiye"] = round(user["bakiye"] + talep["tutar"], 2)
                user["krediler"].append(kredi_obj)
                add_dekont(db, u_email, "KREDI_CEKIM", talep["tutar"], f"Kredi Onaylandı ({talep['hafta']} Hafta)")
            elif talep["tur"] == "para_yatirma":
                user["bakiye"] = round(user["bakiye"] + talep["tutar"], 2)
                add_dekont(db, u_email, "PARA_YATIRMA", talep["tutar"], f"Dekont: {talep.get('kanit')}")
            elif talep["tur"] == "para_cekme":
                distribute_kasa_profit(db, u_email, talep["komisyon"], "Para Çekim Komisyonu")
                add_dekont(db, u_email, "PARA_CEKME", talep["tutar"], f"Nakit Çekim (Komisyon: {talep['komisyon']} TL)")
        elif action == "red" and user:
            if talep["tur"] in ("satin_alim", "para_cekme"):
                user["bakiye"] = round(user["bakiye"] + talep["toplam"], 2)

        del db["bekleyen_talepler"][req_id]
    return jsonify(status="success", message="Talep sonuçlandırıldı.")

@app.route("/api/admin/verify-user", methods=["POST"])
def api_admin_verify_user():
    data = body()
    target, action = data.get("email"), data.get("action")
    with transaction() as db:
        require_admin(db)
        if target in db["users"] and action == "onay":
            db["users"][target]["dogrulandi"] = True
            grant_referral_verification_reward(db, target)
        if target in db.get("dogrulama_talepleri", {}):
            del db["dogrulama_talepleri"][target]
    return jsonify(status="success", message="Doğrulama durumu güncellendi.")

# --- DOSYA İLE YEDEK İNDİRME VE GERİ YÜKLEME ---
@app.route("/api/admin/backup", methods=["GET"])
def api_admin_backup():
    db = load_db()
    require_admin(db)
    with db_lock():
        with open(DB_FILE, "rb") as f:
            raw = f.read()
    name = f"dekontika_yedek_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    return Response(raw, mimetype="application/json", headers={"Content-Disposition": f'attachment; filename="{name}"'})

@app.route("/api/admin/restore-db", methods=["POST"])
def api_admin_restore():
    db = load_db()
    require_admin(db)
    f = request.files.get("file")
    if not f:
        raise ApiError("Dosya seçilmedi.")
    try:
        new_db = json.load(f)
        if "users" in new_db and "admin" not in new_db["users"]:
            new_db["users"]["admin"] = db["users"]["admin"]
        normalize_db(new_db, create_admin=True)
        save_db(new_db)
        mark_backup_dirty()
        return jsonify(status="success", message="Veritabanı başarıyla geri yüklendi.")
    except Exception as e:
        raise ApiError(f"Geri yükleme hatası: {str(e)}")

# --- MATPLOTLIB GRAFİK ENDPOINT'İ ---
@app.route("/api/admin/grafik", methods=["GET"])
def api_admin_grafik():
    db = load_db()
    require_admin(db)
    toplam_kasa = round(db.get("kasa", 0), 2)
    toplam_mevduat = round(sum(u.get("bakiye", 0) for u in db["users"].values() if not u.get("is_admin")), 2)

    labels = ['Banka Kasasi', 'Musteri Bakiyeleri']
    sizes = [toplam_kasa, toplam_mevduat]
    colors = ['#2ecc71', '#3498db']
    if sum(sizes) == 0:
        sizes = [1, 1]

    fig, ax = plt.subplots(figsize=(5, 5), facecolor='#1e1e2f')
    ax.pie(sizes, labels=labels, colors=colors, autopct='%1.1f%%', startangle=140, textprops={'color': "white"})
    ax.set_title('Merkez Bankasi Varlik Dagilimi', color='white')

    buffer = io.BytesIO()
    fig.savefig(buffer, format='png', bbox_inches='tight', transparent=True)
    buffer.seek(0)
    plt.close(fig)

    return Response(buffer.getvalue(), mimetype='image/png')

init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
