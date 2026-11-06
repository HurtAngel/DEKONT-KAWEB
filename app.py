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
from database import mongo_load, mongo_save
from pymongo import MongoClient

# ---------------------------------------------------------------------------
# YAPILANDIRMA
# ---------------------------------------------------------------------------
DB_FILE = os.getenv("DB_FILE", "bank_db.json")
LOCK_FILE = DB_FILE + ".lock"
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
BACKUP_MIN_INTERVAL = int(os.getenv("BACKUP_MIN_INTERVAL", "300"))
MONGO_URI = os.getenv("MONGO_URI")

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
    "kredi_faiz": 0.05,
    "gecikme_cezasi": 50.0,
    "komisyon_epin_satis": 0.25,
}

ADMIN_ALIASES = ("admin", "admin@dekontika.com")

app = Flask(__name__)

# --- MongoDB Atlas Kurulumu ---
mongo_collection = None
if MONGO_URI:
    try:
        mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        mongo_db = mongo_client["dekontika_bank"]
        mongo_collection = mongo_db["system_state"]
        mongo_client.admin.command('ping')
        print("MongoDB Atlas bağlantısı başarılı.")
    except Exception as exc:
        print(f"MongoDB bağlantı hatası, yerel JSON'a dönülüyor: {exc}")
        mongo_collection = None

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
        # MongoDB devredeyse dosya kilidi almamıza gerek kalmaz.
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

def generate_epin_id(db):
    while True:
        pid = _rand_id("EPIN-")
        if pid not in db.get("epin_urunler", {}):
            return pid

def new_user(db, ad_soyad, password, *, admin=False, bakiye=0.0):
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
        "dekontlar": [],
        "epin_urunler": {},
        "ayarlar": dict(DEFAULT_AYARLAR),
    }

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

    if create_admin and "admin" not in db["users"]:
        env_pw = os.getenv("ADMIN_PASSWORD", "1234")
        adm = new_user(db, "Sistem Yöneticisi", env_pw, admin=True, bakiye=10000.0)
        db["users"]["admin"] = adm

    return db

def load_db():
    if mongo_collection is not None:
        try:
            doc = mongo_collection.find_one({"_id": "main_db"})
            if doc:
                doc.pop("_id", None)
                return normalize_db(doc)
            # Atlas'ta veri yoksa ilk kurulum
            initial = default_db()
            normalize_db(initial, create_admin=True)
            mongo_collection.update_one({"_id": "main_db"}, {"$set": initial}, upsert=True)
            return initial
        except Exception as e:
            print(f"MongoDB okuma hatası: {e}")

    # Fallback JSON okuma
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
    if mongo_collection is not None:
        try:
            payload = dict(data)
            payload.pop("_id", None)
            mongo_collection.update_one({"_id": "main_db"}, {"$set": payload}, upsert=True)
            return
        except Exception as e:
            print(f"MongoDB yazma hatası: {e}")

    # Fallback JSON yazma
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

def parse_rate(value, label):
    try:
        val = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        raise ApiError(f"{label} için geçerli sayı girin.")
    if math.isnan(val) or val < 0 or val > 1:
        raise ApiError(f"{label} 0 ile %100 arasında olmalı.")
    return round(val, 4)

def add_dekont(db, user_email, tur, tutar, aciklama):
    user = db["users"].get(user_email)
    db["dekontlar"].insert(0, {
        "dekont_no": generate_dekont_no(),
        "user_email": user_email,
        "tur": tur,
        "tutar": round(float(tutar), 2),
        "aciklama": aciklama,
        "tarih": now_str(),
        "bakiye_sonrasi": round(user["bakiye"], 2) if user else None,
    })
    del db["dekontlar"][MAX_DEKONT:]

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
        raise ApiError("E-Pin satışı eklemek sadece İş Hesapları ve Yöneticiler içindir.", 403)
    return email, user

def pending_of(db, email):
    return [(rid, t) for rid, t in db.get("bekleyen_talepler", {}).items() if t.get("user_id") == email]

def ensure_pending_room(db, email):
    if len(pending_of(db, email)) >= MAX_BEKLEYEN_TALEP:
        raise ApiError(f"En fazla {MAX_BEKLEYEN_TALEP} bekleyen talebiniz olabilir.")

def public_user(email, u):
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

# ---------------------------------------------------------------------------
# CSRF & GÜVENLİK
# ---------------------------------------------------------------------------
@app.before_request
def csrf_protect():
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
    if email:
        u = load_db()["users"].get(email)
        if u and u.get("durum") != "dondurulmus":
            authed, is_admin = True, bool(u.get("is_admin"))
        else:
            session.pop("user_email", None)
    return jsonify(status="success", csrf=session["csrf"], authenticated=authed, is_admin=is_admin)

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

    if not email or not password or not ad_soyad:
        raise ApiError("Tüm alanları doldurun.")
    if not EMAIL_RE.match(email):
        raise ApiError("Geçerli bir e-posta girin.")
    if len(str(password)) < 6:
        raise ApiError("Parola en az 6 karakter olmalıdır.")

    with transaction() as db:
        if email in db["users"]:
            raise ApiError("Bu e-posta zaten kayıtlı.")
        db["users"][email] = new_user(db, ad_soyad, password)

    start_session(email)
    return jsonify(status="success", message="Kayıt başarılı! Yüz doğrulaması yapana kadar hesabınız kısıtlıdır.", is_admin=False, csrf=session["csrf"])

@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    session["csrf"] = secrets.token_urlsafe(24)
    return jsonify(status="success", csrf=session["csrf"])

# ---------------------------------------------------------------------------
# KULLANICI İŞLEMLERİ
# ---------------------------------------------------------------------------
@app.route("/api/user-data", methods=["GET"])
def api_user_data():
    db = load_db()
    email, user = require_user(db)
    dekontlar = [
        {k: v for k, v in d.items() if k != "user_email"}
        for d in db["dekontlar"] if d.get("user_email") == email
    ][:150]
    bekleyen = [pending_view(rid, t) for rid, t in pending_of(db, email)]
    return jsonify(
        status="success",
        user=public_user(email, user),
        dekontlar=dekontlar,
        krediler=user.get("krediler", []),
        satinalinan_epinler=user.get("satinalinan_epinler", []),
        market=market_listing(db),
        bekleyen=bekleyen,
        is_hesabi_talep_var=email in db.get("is_hesabi_talepleri", {}),
        dogrulama_beklemede=email in db.get("dogrulama_talepleri", {}),
        ayarlar=db["ayarlar"],
    )

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

# ---------------------------------------------------------------------------
# E-PIN MARKET SİSTEMİ
# ---------------------------------------------------------------------------
@app.route("/api/market/buy-epin", methods=["POST"])
def api_buy_epin():
    data = body()
    urun_id = data.get("urun_id")
    kart_no = clean_card_number(data.get("kart_no"))
    skt = str(data.get("skt", "")).strip()
    cvv = str(data.get("cvv", "")).strip()

    with transaction() as db:
        email, user = require_user(db)
        if clean_card_number(user["kart_no"]) != kart_no or user["skt"].strip() != skt or user["cvv"].strip() != cvv:
            raise ApiError("Kart Bilgileri Hatalı! (Kart No, SKT veya CVV uyuşmuyor)")

        urun = db.get("epin_urunler", {}).get(urun_id)
        if not urun or not urun.get("aktif", True) or len(urun.get("stok_kodlar", [])) == 0:
            raise ApiError("Bu ürün stokta yok veya satışı durdurulmuş.")

        satici = urun.get("satici", "admin")
        if satici == email:
            raise ApiError("Kendi ürününüzü satın alamazsınız.")

        satici_isteyen_fiyat = float(urun["fiyat"])
        ek_pay_orani = db["ayarlar"].get("komisyon_epin_satis", 0.25)
        banka_payi = round(satici_isteyen_fiyat * ek_pay_orani, 2)
        toplam_fiyat = round(satici_isteyen_fiyat + banka_payi, 2)

        if user["bakiye"] < toplam_fiyat:
            raise ApiError(f"Yetersiz Bakiye! Fiyat (Banka Payı Dahil): {toplam_fiyat} TL")

        user["bakiye"] = round(user["bakiye"] - toplam_fiyat, 2)
        kod = urun["stok_kodlar"].pop(0)

        if satici == "admin":
            db["kasa"] = round(db["kasa"] + toplam_fiyat, 2)
        else:
            if satici in db["users"]:
                db["users"][satici]["bakiye"] = round(db["users"][satici]["bakiye"] + satici_isteyen_fiyat, 2)
            db["kasa"] = round(db["kasa"] + banka_payi, 2)
            add_dekont(db, satici, "EPIN_SATIS", satici_isteyen_fiyat, f"Satış: {urun['baslik']} ({kod})")

        user["satinalinan_epinler"].insert(0, {
            "urun_adi": urun["baslik"],
            "kod": kod,
            "fiyat": toplam_fiyat,
            "tarih": now_str(),
            "satici_ad": seller_name(db, satici),
        })
        add_dekont(db, email, "EPIN_SATIN_ALIM", toplam_fiyat, f"E-Pin Alımı: {urun['baslik']} ({kod})")

    return jsonify(status="success", message=f"İşlem Başarılı! E-Pin Kodunuz: {kod}", kod=kod)

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
        
        # Satıcı admin değilse, başkası adına ürün ekleyemez
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
        carpan = 1.0 + ((hafta - 3) / 9.0) * 3.0
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
        if not gonderen.get("dogrulandi") and tutar > KISITLI_GUNLUK_HAVALE:
            raise ApiError(f"Kısıtlı Hesap: Günlük en fazla {KISITLI_GUNLUK_HAVALE} TL transfer yapılabilir.", 403)

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
        db["kasa"] = round(db["kasa"] + komisyon, 2)

        add_dekont(db, email, "HAVALE_CIKIS", tutar, f"Alıcı: {hedef}")
        add_dekont(db, alici_email, "HAVALE_GIRIS", tutar, f"Gönderen: {gonderen['iban']}")

    return jsonify(status="success", message="Transfer işlemi başarıyla tamamlandı.")

# ---------------------------------------------------------------------------
# ADMIN YÖNETİM ENDPOINTLERİ
# ---------------------------------------------------------------------------
@app.route("/api/admin/users", methods=["GET"])
@app.route("/api/admin/overview", methods=["GET"])
def api_admin_users():
    db = load_db()
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
        epin_urunler=db.get("epin_urunler", {}),
    )

@app.route("/api/admin/direct-verify", methods=["POST"])
def api_admin_direct_verify():
    target = body().get("email")
    with transaction() as db:
        require_admin(db)
        if target in db["users"]:
            db["users"][target]["dogrulandi"] = True
            if target in db.get("dogrulama_talepleri", {}):
                del db["dogrulama_talepleri"][target]
    return jsonify(status="success", message=f"{target} hesabı doğrudan doğrulandı.")

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

@app.route("/api/admin/edit-user", methods=["POST"])
def api_admin_edit_user():
    data = body()
    target, action = data.get("email"), data.get("action")
    amount = parse_amount(data.get("amount"), required=False) or 0.0

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

    return jsonify(status="success", message=f"{target} kullanıcısı güncellendi.")

@app.route("/api/admin/update-settings", methods=["POST"])
def api_admin_update_settings():
    data = body()
    with transaction() as db:
        require_admin(db)
        if "komisyon_odeme" in data:
            db["ayarlar"]["komisyon_odeme"] = float(data["komisyon_odeme"])
        if "komisyon_cekme" in data:
            db["ayarlar"]["komisyon_cekme"] = float(data["komisyon_cekme"])
        if "komisyon_havale" in data:
            db["ayarlar"]["komisyon_havale"] = float(data["komisyon_havale"])
        if "kredi_faiz" in data:
            db["ayarlar"]["kredi_faiz"] = float(data["kredi_faiz"])
        if "gecikme_cezasi" in data:
            db["ayarlar"]["gecikme_cezasi"] = float(data["gecikme_cezasi"])
        if "komisyon_epin_satis" in data:
            db["ayarlar"]["komisyon_epin_satis"] = float(data["komisyon_epin_satis"])
            
    return jsonify(status="success", message="Sistem ayarları güncellendi.")

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
                    "tarih": datetime.now().strftime("%d/%m/%Y %H:%M"),
                }
                user["bakiye"] = round(user["bakiye"] + talep["tutar"], 2)
                user["krediler"].append(kredi_obj)
                add_dekont(db, u_email, "KREDI_CEKIM", talep["tutar"], f"Kredi Onaylandı ({talep['hafta']} Hafta)")
            elif talep["tur"] == "para_yatirma":
                user["bakiye"] = round(user["bakiye"] + talep["tutar"], 2)
                add_dekont(db, u_email, "PARA_YATIRMA", talep["tutar"], f"Dekont: {talep.get('kanit')}")
            elif talep["tur"] == "para_cekme":
                db["kasa"] = round(db["kasa"] + talep["komisyon"], 2)
                add_dekont(db, u_email, "PARA_CEKME", talep["tutar"], f"Nakit Çekim (Komisyon: {talep['komisyon']} TL)")
        elif action == "red":
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
