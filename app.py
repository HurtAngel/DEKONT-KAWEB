"""DEKONTİKA - Merkez Bankası & E-Pin Marketi

Gerekli paketler: flask, requests  (APScheduler ve matplotlib artık gerekmiyor)

Ortam değişkenleri (hepsi isteğe bağlı):
    SECRET_KEY               Oturum anahtarı. Verilmezse bir kez üretilip dosyada saklanır.
    ADMIN_PASSWORD           İlk kurulumda oluşturulacak admin şifresi. Verilmezse rastgele üretilir
                             ve konsola yazdırılır.
    DB_FILE                  Veritabanı dosyası (varsayılan: bank_db.json)
    DISCORD_WEBHOOK_URL      Yedeklerin gönderileceği webhook
    BACKUP_MIN_INTERVAL      İki Discord yedeği arası minimum saniye (varsayılan: 300)
    COOKIE_SECURE=1          HTTPS arkasında çerezleri "Secure" yapar
    TRUST_PROXY=1            Reverse proxy (Render, Heroku, Nginx) arkasında gerçek IP'yi okur
    DISABLE_BACKGROUND=1     Arka plan işlerini (gecikme cezası, yedek) kapatır
"""
import hmac
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
    import fcntl  # Linux / macOS: birden fazla worker arasında dosya kilidi
except ImportError:  # Windows
    fcntl = None

import requests
from flask import Flask, Response, jsonify, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash

# ---------------------------------------------------------------------------
# YAPILANDIRMA
# ---------------------------------------------------------------------------
DB_FILE = os.getenv("DB_FILE", "bank_db.json")
LOCK_FILE = DB_FILE + ".lock"
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
BACKUP_MIN_INTERVAL = int(os.getenv("BACKUP_MIN_INTERVAL", "300"))

MAX_TUTAR = 10_000_000.0
MAX_BEKLEYEN_TALEP = 10
MAX_AKTIF_KREDI = 3
MAX_DEKONT = 5000
KISITLI_GUNLUK_HAVALE = 500.0
MAX_SELFIE_BYTES = 300_000
MAX_KOD_YUKLEME = 1000

DEFAULT_AYARLAR = {
    "komisyon_odeme": 0.60,
    "komisyon_cekme": 0.10,
    "komisyon_havale": 0.05,
    "havale_sinir": 100.0,
    "kredi_faiz": 0.05,
    "gecikme_cezasi": 50.0,
    "komisyon_epin_satis": 0.0,
}

ADMIN_ALIASES = ("admin", "admin@dekontika.com")

app = Flask(__name__)


def _load_secret_key():
    env_key = os.getenv("SECRET_KEY")
    if env_key:
        return env_key
    path = os.path.join(os.path.dirname(os.path.abspath(DB_FILE)), ".dekontika_secret")
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
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=25 * 1024 * 1024,
    JSON_AS_ASCII=False,
)
if os.getenv("TRUST_PROXY") == "1":
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)


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
    return jsonify(status="error", message="Dosya çok büyük."), 413


@app.errorhandler(Exception)
def handle_unexpected(e):
    from werkzeug.exceptions import HTTPException

    if isinstance(e, HTTPException):
        if request.path.startswith("/api/"):
            return jsonify(status="error", message=e.description or e.name), e.code
        return e
    app.logger.exception("Beklenmeyen hata")
    return jsonify(status="error", message="Beklenmeyen bir hata oluştu. Lütfen tekrar deneyin."), 500


# ---------------------------------------------------------------------------
# VERİTABANI (kilitli, atomik yazım)
# ---------------------------------------------------------------------------
_thread_lock = threading.Lock()


@contextmanager
def db_lock():
    """Thread'ler ve (Linux'ta) birden fazla süreç arasında karşılıklı dışlama."""
    with _thread_lock:
        fh = None
        if fcntl:
            fh = open(LOCK_FILE, "a+")
            fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fh:
                fcntl.flock(fh, fcntl.LOCK_UN)
                fh.close()


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


def generate_req_id():
    return _rand_id("REQ-")


def generate_kredi_id():
    return _rand_id("KRD-")


def generate_epin_id(db):
    while True:
        pid = _rand_id("EPIN-")
        if pid not in db["epin_urunler"]:
            return pid


def generate_dekont_no():
    return _rand_id("DKN-", 8)


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
    """Eksik alanları tamamlar, düz metin şifreleri hashler, eski kayıtları yeni şemaya taşır.

    create_admin=True yalnızca başlangıçta ve yedek yüklemede kullanılır: admin yoksa oluşturur ve
    admin şifresi hâlâ varsayılan "123" ise şifre değiştirme uyarısı işaretler.
    """
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

    for email, u in db["users"].items():
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
            if u.get("is_admin") and plain == "123":
                u["sifre_degistir_zorunlu"] = True
            u["password"] = generate_password_hash(plain)
        elif create_admin and u.get("is_admin") and check_password_hash(u["password"], "123"):
            u["sifre_degistir_zorunlu"] = True
        for k in u["krediler"]:
            if not k.get("sonraki_vade"):
                try:
                    base = datetime.strptime(k.get("tarih", ""), "%d/%m/%Y %H:%M")
                except ValueError:
                    base = datetime.now()
                odenen = max(0, k.get("toplam_hafta", 3) - k.get("kalan_hafta", 0))
                k["sonraki_vade"] = (base + timedelta(days=7 * (odenen + 1))).strftime("%Y-%m-%dT%H:%M:%S")
            k.setdefault("gecikme_sayisi", 0)

    for pid, p in db["epin_urunler"].items():
        p.setdefault("id", pid)
        p.setdefault("satici", "admin")
        p.setdefault("satilan", 0)
        p.setdefault("aktif", True)
        p.setdefault("stok_kodlar", [])

    if create_admin and "admin" not in db["users"]:
        env_pw = os.getenv("ADMIN_PASSWORD")
        pw = env_pw or secrets.token_urlsafe(9)
        adm = new_user(db, "Sistem Yöneticisi", pw, admin=True, bakiye=0.0)
        adm["sifre_degistir_zorunlu"] = not env_pw
        db["users"]["admin"] = adm
        if not env_pw:
            print("=" * 60)
            print("[DEKONTİKA] Yönetici hesabı oluşturuldu.")
            print(f"            Kullanıcı adı: admin    Şifre: {pw}")
            print("            Giriş yaptıktan sonra şifrenizi değiştirin.")
            print("=" * 60)
    return db


def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r", encoding="utf-8") as f:
            db = json.load(f)
    else:
        db = default_db()
    return normalize_db(db)


def save_db(data):
    tmp = f"{DB_FILE}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, DB_FILE)


@contextmanager
def transaction():
    """Okur, kilit altında değiştirmene izin verir, hata çıkmazsa atomik olarak yazar."""
    with db_lock():
        db = load_db()
        yield db
        save_db(db)
    mark_backup_dirty()


def init_db():
    with db_lock():
        if os.path.exists(DB_FILE):
            with open(DB_FILE, "r", encoding="utf-8") as f:
                db = json.load(f)
        else:
            db = default_db()
        normalize_db(db, create_admin=True)
        save_db(db)


# ---------------------------------------------------------------------------
# DISCORD YEDEĞİ (arka planda, hız sınırlı)
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
                timeout=20,
            )
    except Exception as exc:  # noqa: BLE001
        print(f"Yedekleme hatası: {exc}")


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
    s = str(value).strip()
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        val = float(s)
    except ValueError:
        raise ApiError(f"Geçerli bir {label} girin.")
    if math.isnan(val) or math.isinf(val) or val <= 0:
        raise ApiError(f"{label.capitalize()} sıfırdan büyük olmalı.")
    val = round(val, 2)
    if val <= 0:
        raise ApiError(f"{label.capitalize()} en az 0,01 olmalı.")
    if val > MAX_TUTAR:
        raise ApiError(f"{label.capitalize()} en fazla {int(MAX_TUTAR):,} olabilir.".replace(",", "."))
    return val


def parse_rate(value, label):
    try:
        val = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        raise ApiError(f"{label} için geçerli bir sayı girin.")
    if math.isnan(val) or val < 0 or val > 1:
        raise ApiError(f"{label} 0 ile %100 arasında olmalı.")
    return round(val, 4)


def add_dekont(db, user_email, tur, tutar, aciklama):
    user = db["users"].get(user_email)
    db["dekontlar"].insert(
        0,
        {
            "dekont_no": generate_dekont_no(),
            "user_email": user_email,
            "tur": tur,
            "tutar": round(float(tutar), 2),
            "aciklama": aciklama,
            "tarih": now_str(),
            "bakiye_sonrasi": round(user["bakiye"], 2) if user else None,
        },
    )
    del db["dekontlar"][MAX_DEKONT:]


def mask_code(code):
    return ("*" * max(0, len(code) - 4)) + code[-4:]


# --- oturum ve yetki ------------------------------------------------------
def require_user(db):
    email = session.get("user_email")
    if not email:
        raise ApiError("Devam etmek için giriş yapın.", 401)
    user = db["users"].get(email)
    if not user:
        session.pop("user_email", None)
        raise ApiError("Devam etmek için giriş yapın.", 401)
    if user.get("durum") == "dondurulmus":
        session.pop("user_email", None)
        raise ApiError("Hesabınız bloke edilmiştir.", 403)
    return email, user


def require_admin(db):
    email, user = require_user(db)
    if not user.get("is_admin"):
        raise ApiError("Bu işlem için yönetici yetkisi gerekir.", 403)
    return email, user


def require_seller(db):
    email, user = require_user(db)
    if not (user.get("is_admin") or user.get("is_hesabi")):
        raise ApiError("E-Pin satışı yalnızca iş hesapları için açıktır.", 403)
    return email, user


def pending_of(db, email):
    return [(rid, t) for rid, t in db["bekleyen_talepler"].items() if t.get("user_id") == email]


def ensure_pending_room(db, email):
    if len(pending_of(db, email)) >= MAX_BEKLEYEN_TALEP:
        raise ApiError(f"En fazla {MAX_BEKLEYEN_TALEP} bekleyen talebiniz olabilir. Mevcut taleplerin sonuçlanmasını bekleyin.")


def public_user(email, u):
    return {
        "email": email,
        "ad_soyad": u["ad_soyad"],
        "bakiye": round(u["bakiye"], 2),
        "iban": u["iban"],
        "kart_no": u["kart_no"],
        "skt": u["skt"],
        "cvv": u["cvv"],
        "kredi_notu": u["kredi_notu"],
        "durum": u["durum"],
        "dogrulandi": bool(u["dogrulandi"]),
        "is_hesabi": bool(u["is_hesabi"]),
        "is_admin": bool(u["is_admin"]),
        "sifre_degistir_zorunlu": bool(u.get("sifre_degistir_zorunlu")),
    }


def seller_name(db, satici):
    if satici == "admin":
        return "DEKONTİKA"
    s = db["users"].get(satici)
    return s["ad_soyad"] if s else "Bilinmiyor"


def seller_can_sell(db, satici):
    if satici == "admin":
        return True
    s = db["users"].get(satici)
    return bool(s and s.get("is_hesabi") and s.get("durum") != "dondurulmus")


def market_listing(db):
    items = []
    for pid, p in db["epin_urunler"].items():
        if not p.get("aktif", True) or not seller_can_sell(db, p.get("satici", "admin")):
            continue
        items.append(
            {
                "id": pid,
                "baslik": p["baslik"],
                "fiyat": p["fiyat"],
                "stok": len(p.get("stok_kodlar", [])),
                "satici": p.get("satici", "admin"),
                "satici_ad": seller_name(db, p.get("satici", "admin")),
                "platform": p.get("satici", "admin") == "admin",
            }
        )
    items.sort(key=lambda x: (x["stok"] == 0, x["baslik"].lower()))
    return items


def pending_view(rid, t, db=None):
    out = {k: v for k, v in t.items() if k not in ("kanit",)}
    out["id"] = rid
    if db is not None:
        u = db["users"].get(t.get("user_id"))
        out["user_ad"] = u["ad_soyad"] if u else t.get("user_id")
    return out


# ---------------------------------------------------------------------------
# GÜVENLİK BAŞLIKLARI, CSRF
# ---------------------------------------------------------------------------
@app.before_request
def csrf_protect():
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.path.startswith("/api/"):
        token = session.get("csrf")
        sent = request.headers.get("X-CSRF-Token", "")
        if not token or not hmac.compare_digest(token, sent):
            return (
                jsonify(status="error", message="Oturum doğrulaması başarısız. Sayfayı yenileyip tekrar deneyin."),
                403,
            )
    return None


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Permissions-Policy"] = "camera=(self), microphone=(), geolocation=()"
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    else:
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; "
            "img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
    return resp


# ---------------------------------------------------------------------------
# GİRİŞ / KAYIT
# ---------------------------------------------------------------------------
_login_fails = {}
_fail_lock = threading.Lock()
RL_WINDOW = 600
RL_MAX = 8


def _rl_key(identity):
    return f"{request.remote_addr}|{identity}"


def rl_check(key):
    now = time.time()
    with _fail_lock:
        arr = [t for t in _login_fails.get(key, []) if now - t < RL_WINDOW]
        _login_fails[key] = arr
        if len(arr) >= RL_MAX:
            wait = int((RL_WINDOW - (now - arr[0])) // 60) + 1
            raise ApiError(f"Çok fazla hatalı deneme. {wait} dakika sonra tekrar deneyin.", 429)


def rl_fail(key):
    with _fail_lock:
        _login_fails.setdefault(key, []).append(time.time())


def rl_clear(key):
    with _fail_lock:
        _login_fails.pop(key, None)


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
    session.permanent = True


@app.route("/api/login", methods=["POST"])
def api_login():
    data = body()
    identity = str(data.get("email", "")).strip().lower()
    password = data.get("password")
    if identity in ADMIN_ALIASES:
        identity = "admin"
    if not identity or not password:
        raise ApiError("E-posta ve parolayı girin.")

    key = _rl_key(identity)
    rl_check(key)

    db = load_db()
    user = db["users"].get(identity)
    if not user or not verify_password(user["password"], password):
        rl_fail(key)
        raise ApiError("E-posta veya parola hatalı.", 400)
    if user.get("durum") == "dondurulmus":
        raise ApiError("Hesabınız bloke edilmiştir. Yönetici ile iletişime geçin.", 403)

    rl_clear(key)
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
    if not EMAIL_RE.match(email) or len(email) > 120:
        raise ApiError("Geçerli bir e-posta adresi girin.")
    if not isinstance(password, str) or len(password) < 6 or len(password) > 128:
        raise ApiError("Parola en az 6 karakter olmalı.")
    if len(ad_soyad) < 2 or len(ad_soyad) > 60:
        raise ApiError("Ad soyad 2 ile 60 karakter arasında olmalı.")

    with transaction() as db:
        if email in db["users"]:
            raise ApiError("Bu e-posta adresi zaten kayıtlı.")
        db["users"][email] = new_user(db, ad_soyad, password)

    start_session(email)
    return jsonify(
        status="success",
        message="Hesabınız oluşturuldu. Yüz doğrulaması yapana kadar hesabınız kısıtlı kalır.",
        is_admin=False,
        csrf=session["csrf"],
    )


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    session["csrf"] = secrets.token_urlsafe(24)
    return jsonify(status="success", csrf=session["csrf"])


@app.route("/api/user/change-password", methods=["POST"])
def api_change_password():
    data = body()
    current, new = data.get("current"), data.get("new")
    if not isinstance(new, str) or len(new) < 6 or len(new) > 128:
        raise ApiError("Yeni parola en az 6 karakter olmalı.")
    with transaction() as db:
        _, user = require_user(db)
        if not verify_password(user["password"], current):
            raise ApiError("Mevcut parola hatalı.")
        if current == new:
            raise ApiError("Yeni parola mevcut parolayla aynı olamaz.")
        user["password"] = generate_password_hash(new)
        user.pop("sifre_degistir_zorunlu", None)
    return jsonify(status="success", message="Parolanız güncellendi.")


# ---------------------------------------------------------------------------
# KULLANICI VERİSİ
# ---------------------------------------------------------------------------
def public_ayarlar(db):
    return dict(db["ayarlar"])


@app.route("/api/user-data", methods=["GET"])
def api_user_data():
    db = load_db()
    email, user = require_user(db)
    dekontlar = [
        {k: v for k, v in d.items() if k != "user_email"}
        for d in db["dekontlar"]
        if d.get("user_email") == email
    ][:150]
    bekleyen = [pending_view(rid, t) for rid, t in pending_of(db, email)]
    bekleyen.sort(key=lambda x: x.get("tarih", ""), reverse=True)
    return jsonify(
        status="success",
        user=public_user(email, user),
        dekontlar=dekontlar,
        krediler=user.get("krediler", []),
        satinalinan_epinler=user.get("satinalinan_epinler", []),
        market=market_listing(db),
        bekleyen=bekleyen,
        is_hesabi_talep_var=email in db["is_hesabi_talepleri"],
        dogrulama_beklemede=email in db["dogrulama_talepleri"],
        ayarlar=public_ayarlar(db),
    )


@app.route("/api/user/upload-selfie", methods=["POST"])
def api_upload_selfie():
    image = body().get("image")
    if not isinstance(image, str) or not image.startswith("data:image/jpeg;base64,"):
        raise ApiError("Fotoğraf alınamadı. Lütfen tekrar deneyin.")
    if len(image) > MAX_SELFIE_BYTES:
        raise ApiError("Fotoğraf çok büyük.")
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
    return jsonify(status="success", message="Fotoğrafınız yönetici incelemesine gönderildi.")


@app.route("/api/user/request-business", methods=["POST"])
def api_request_business():
    with transaction() as db:
        email, user = require_user(db)
        if user.get("is_hesabi"):
            raise ApiError("Zaten bir iş hesabınız var.")
        if not user.get("dogrulandi"):
            raise ApiError("İş hesabı başvurusu için önce yüz doğrulamasını tamamlamalısınız.", 403)
        if email in db["is_hesabi_talepleri"]:
            raise ApiError("Başvurunuz zaten inceleniyor.")
        db["is_hesabi_talepleri"][email] = {
            "email": email,
            "ad_soyad": user["ad_soyad"],
            "tarih": datetime.now().strftime("%d/%m/%Y %H:%M"),
        }
    return jsonify(status="success", message="İş hesabı başvurunuz yönetici onayına gönderildi.")


# ---------------------------------------------------------------------------
# E-PIN MARKETİ (alıcı tarafı)
# ---------------------------------------------------------------------------
@app.route("/api/market", methods=["GET"])
def api_market():
    db = load_db()
    require_user(db)
    return jsonify(status="success", market=market_listing(db))


@app.route("/api/market/buy-epin", methods=["POST"])
def api_buy_epin():
    data = body()
    urun_id = data.get("urun_id")
    kart_no = clean_card_number(data.get("kart_no"))
    skt = str(data.get("skt", "")).strip()
    cvv = str(data.get("cvv", "")).strip()

    with transaction() as db:
        email, user = require_user(db)

        ok = (
            hmac.compare_digest(clean_card_number(user["kart_no"]), kart_no)
            and hmac.compare_digest(user["skt"].strip(), skt)
            and hmac.compare_digest(user["cvv"].strip(), cvv)
        )
        if not ok:
            raise ApiError("Kart bilgileri hatalı. Kart numarası, SKT veya CVV eşleşmiyor.")

        urun = db["epin_urunler"].get(urun_id)
        if not urun or not urun.get("aktif", True) or not seller_can_sell(db, urun.get("satici", "admin")):
            raise ApiError("Bu ürün şu an satışta değil.")
        if not urun["stok_kodlar"]:
            raise ApiError("Bu ürünün stoğu tükendi.")
        satici = urun.get("satici", "admin")
        if satici == email:
            raise ApiError("Kendi ilanınızı satın alamazsınız.")

        fiyat = urun["fiyat"]
        if user["bakiye"] < fiyat:
            raise ApiError(f"Bakiyeniz yetersiz. Ürün fiyatı: {fiyat:.2f} TL")

        user["bakiye"] = round(user["bakiye"] - fiyat, 2)
        kod = urun["stok_kodlar"].pop(0)
        urun["satilan"] = urun.get("satilan", 0) + 1

        if satici == "admin":
            db["kasa"] = round(db["kasa"] + fiyat, 2)
            komisyon = 0.0
        else:
            komisyon = round(fiyat * db["ayarlar"].get("komisyon_epin_satis", 0.0), 2)
            gelir = round(fiyat - komisyon, 2)
            db["users"][satici]["bakiye"] = round(db["users"][satici]["bakiye"] + gelir, 2)
            db["kasa"] = round(db["kasa"] + komisyon, 2)
            add_dekont(db, satici, "EPIN_SATIS", gelir, f"{urun['baslik']} satışı ({mask_code(kod)})")

        user["satinalinan_epinler"].insert(
            0,
            {
                "urun_adi": urun["baslik"],
                "kod": kod,
                "fiyat": fiyat,
                "tarih": now_str(),
                "satici_ad": seller_name(db, satici),
            },
        )
        add_dekont(db, email, "EPIN_SATIN_ALIM", fiyat, f"{urun['baslik']} ({mask_code(kod)})")

    return jsonify(status="success", message="Ödeme alındı. E-Pin kodunuz hazır.", kod=kod, urun_adi=urun["baslik"])


# ---------------------------------------------------------------------------
# E-PIN YÖNETİMİ (iş hesapları + admin)
# ---------------------------------------------------------------------------
def parse_codes(raw, existing=()):
    seen = set(existing)
    codes, dup = [], 0
    for line in str(raw or "").replace("\r", "").split("\n"):
        c = line.strip()
        if not c:
            continue
        if len(c) > 200:
            raise ApiError("Bir kod 200 karakterden uzun olamaz.")
        if c in seen:
            dup += 1
            continue
        seen.add(c)
        codes.append(c)
    if len(codes) > MAX_KOD_YUKLEME:
        raise ApiError(f"Tek seferde en fazla {MAX_KOD_YUKLEME} kod yükleyebilirsiniz.")
    return codes, dup


def parse_title(raw):
    title = " ".join(str(raw or "").split())
    if len(title) < 2 or len(title) > 80:
        raise ApiError("Ürün başlığı 2 ile 80 karakter arasında olmalı.")
    return title


def managed_product(db, email, user, urun_id):
    urun = db["epin_urunler"].get(urun_id)
    if not urun:
        raise ApiError("Ürün bulunamadı.", 404)
    if not (user.get("is_admin") or (user.get("is_hesabi") and urun.get("satici") == email)):
        raise ApiError("Bu ilan üzerinde işlem yapma yetkiniz yok.", 403)
    return urun


def manage_view(db, pid, p):
    return {
        "id": pid,
        "baslik": p["baslik"],
        "fiyat": p["fiyat"],
        "aktif": bool(p.get("aktif", True)),
        "satici": p.get("satici", "admin"),
        "satici_ad": seller_name(db, p.get("satici", "admin")),
        "stok": len(p["stok_kodlar"]),
        "satilan": p.get("satilan", 0),
        "kodlar": list(p["stok_kodlar"]),
    }


@app.route("/api/epin/manage", methods=["GET"])
def api_epin_manage():
    db = load_db()
    email, user = require_seller(db)
    is_admin = bool(user.get("is_admin"))
    urunler = [
        manage_view(db, pid, p)
        for pid, p in db["epin_urunler"].items()
        if is_admin or p.get("satici") == email
    ]
    urunler.sort(key=lambda x: x["baslik"].lower())
    satislar = [
        {k: v for k, v in d.items() if k != "user_email"}
        for d in db["dekontlar"]
        if d.get("user_email") == email and d.get("tur") == "EPIN_SATIS"
    ][:100]
    out = {
        "status": "success",
        "urunler": urunler,
        "satislar": satislar,
        "komisyon_epin_satis": db["ayarlar"].get("komisyon_epin_satis", 0.0),
    }
    if is_admin:
        out["saticilar"] = [{"email": "admin", "ad": "DEKONTİKA (Banka)"}] + [
            {"email": e, "ad": u["ad_soyad"]} for e, u in db["users"].items() if u.get("is_hesabi")
        ]
    return jsonify(out)


@app.route("/api/epin/create", methods=["POST"])
@app.route("/api/admin/add-epin", methods=["POST"])
def api_epin_create():
    data = body()
    baslik = parse_title(data.get("baslik"))
    fiyat = parse_amount(data.get("fiyat"), "fiyat")
    with transaction() as db:
        email, user = require_seller(db)
        satici = email
        if user.get("is_admin"):
            satici = str(data.get("satici") or "admin")
            if satici != "admin" and not (db["users"].get(satici) or {}).get("is_hesabi"):
                raise ApiError("Seçilen satıcı bir iş hesabı değil.")
        elif satici == "admin":
            raise ApiError("Yetkisiz işlem.", 403)
        kodlar, dup = parse_codes(data.get("kodlar"))
        if not kodlar:
            raise ApiError("En az bir geçerli E-Pin kodu girin.")
        pid = generate_epin_id(db)
        db["epin_urunler"][pid] = {
            "id": pid,
            "baslik": baslik,
            "fiyat": fiyat,
            "satici": satici,
            "aktif": True,
            "satilan": 0,
            "stok_kodlar": kodlar,
        }
    extra = f" ({dup} tekrar eden kod atlandı)" if dup else ""
    return jsonify(status="success", message=f"'{baslik}' {len(kodlar)} kod stokla yayınlandı{extra}.", id=pid)


@app.route("/api/epin/update", methods=["POST"])
def api_epin_update():
    data = body()
    with transaction() as db:
        email, user = require_seller(db)
        urun = managed_product(db, email, user, data.get("urun_id"))
        if "baslik" in data:
            urun["baslik"] = parse_title(data["baslik"])
        if "fiyat" in data:
            urun["fiyat"] = parse_amount(data["fiyat"], "fiyat")
        if "aktif" in data:
            urun["aktif"] = bool(data["aktif"])
        eklenen, dup = 0, 0
        if str(data.get("kodlar_ekle") or "").strip():
            yeni, dup = parse_codes(data["kodlar_ekle"], urun["stok_kodlar"])
            urun["stok_kodlar"].extend(yeni)
            eklenen = len(yeni)
        view = manage_view(db, urun["id"], urun)
    msg = "İlan güncellendi."
    if eklenen:
        msg = f"İlan güncellendi, {eklenen} kod eklendi."
    if dup:
        msg += f" {dup} tekrar eden kod atlandı."
    return jsonify(status="success", message=msg, urun=view)


@app.route("/api/epin/remove-code", methods=["POST"])
def api_epin_remove_code():
    data = body()
    with transaction() as db:
        email, user = require_seller(db)
        urun = managed_product(db, email, user, data.get("urun_id"))
        kod = data.get("kod")
        if kod not in urun["stok_kodlar"]:
            raise ApiError("Kod bulunamadı. Satılmış olabilir.")
        urun["stok_kodlar"].remove(kod)
        view = manage_view(db, urun["id"], urun)
    return jsonify(status="success", message="Kod stoktan kaldırıldı.", urun=view)


@app.route("/api/epin/delete", methods=["POST"])
@app.route("/api/admin/delete-epin", methods=["POST"])
def api_epin_delete():
    data = body()
    with transaction() as db:
        email, user = require_seller(db)
        urun = managed_product(db, email, user, data.get("urun_id"))
        del db["epin_urunler"][urun["id"]]
    return jsonify(status="success", message=f"'{urun['baslik']}' ilanı kaldırıldı.")


# ---------------------------------------------------------------------------
# KREDİ
# ---------------------------------------------------------------------------
@app.route("/api/action/kredi-cek", methods=["POST"])
def api_kredi_cek():
    data = body()
    tutar = parse_amount(data.get("tutar"))
    try:
        hafta = int(data.get("hafta", 3))
    except (TypeError, ValueError):
        hafta = 0
    if hafta < 3 or hafta > 12:
        raise ApiError("Kredi süresi 3 ile 12 hafta arasında olmalı.")

    with transaction() as db:
        email, user = require_user(db)
        if not user.get("dogrulandi"):
            raise ApiError("Kısıtlı hesap. Kredi için önce yüz doğrulaması yapmalısınız.", 403)
        if len(user["krediler"]) >= MAX_AKTIF_KREDI:
            raise ApiError(f"En fazla {MAX_AKTIF_KREDI} aktif krediniz olabilir.")
        if any(t.get("tur") == "kredi" for _, t in pending_of(db, email)):
            raise ApiError("Onay bekleyen bir kredi başvurunuz zaten var.")
        ensure_pending_room(db, email)

        base_faiz = db["ayarlar"]["kredi_faiz"]
        carpan = 1.0 + ((hafta - 3) / 9.0) * 3.0
        oran = base_faiz * carpan
        toplam = round(tutar + tutar * oran, 2)
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
    return jsonify(
        status="success",
        message=f"{tutar:.2f} TL için {hafta} haftalık kredi başvurunuz alındı (%{round(oran * 100, 1)} faiz). Yönetici onayı bekleniyor.",
    )


def _close_loan(user, kredi):
    user["krediler"] = [k for k in user["krediler"] if k["kredi_id"] != kredi["kredi_id"]]


@app.route("/api/action/kredi-ode-haftalik", methods=["POST"])
def api_kredi_haftalik():
    kredi_id = body().get("kredi_id")
    with transaction() as db:
        email, user = require_user(db)
        kredi = next((k for k in user["krediler"] if k["kredi_id"] == kredi_id), None)
        if not kredi:
            raise ApiError("Kredi bulunamadı.")

        taksit = kredi["haftalik_taksit"]
        if kredi["kalan_hafta"] <= 1 or taksit > kredi["kalan_borc"]:
            taksit = kredi["kalan_borc"]
        taksit = round(taksit, 2)
        if user["bakiye"] < taksit:
            raise ApiError(f"Bakiyeniz yetersiz. Ödenecek taksit: {taksit:.2f} TL")

        user["bakiye"] = round(user["bakiye"] - taksit, 2)
        kredi["kalan_borc"] = round(kredi["kalan_borc"] - taksit, 2)
        kredi["kalan_hafta"] = max(0, kredi["kalan_hafta"] - 1)
        db["kasa"] = round(db["kasa"] + taksit, 2)
        user["kredi_notu"] = min(2000, user["kredi_notu"] + 15)

        vade = parse_iso(kredi.get("sonraki_vade")) or datetime.now()
        base = vade if vade > datetime.now() else datetime.now()
        kredi["sonraki_vade"] = (base + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")

        add_dekont(db, email, "KREDI_TAKSIT_ODEME", taksit, f"Kredi {kredi['kredi_id']}, {kredi['kalan_hafta']} hafta kaldı")

        if kredi["kalan_borc"] <= 0.009:
            _close_loan(user, kredi)
            msg = "Kredinin tamamı ödendi ve kapandı."
        else:
            msg = f"{taksit:.2f} TL taksit ödendi. Kalan borç: {kredi['kalan_borc']:.2f} TL"
    return jsonify(status="success", message=msg)


@app.route("/api/action/kredi-ode-tamami", methods=["POST"])
def api_kredi_tamami():
    kredi_id = body().get("kredi_id")
    with transaction() as db:
        email, user = require_user(db)
        kredi = next((k for k in user["krediler"] if k["kredi_id"] == kredi_id), None)
        if not kredi:
            raise ApiError("Kredi bulunamadı.")
        toplam = round(kredi["kalan_borc"], 2)
        if user["bakiye"] < toplam:
            raise ApiError(f"Bakiyeniz yetersiz. Kapanış tutarı: {toplam:.2f} TL")
        user["bakiye"] = round(user["bakiye"] - toplam, 2)
        db["kasa"] = round(db["kasa"] + toplam, 2)
        user["kredi_notu"] = min(2000, user["kredi_notu"] + 50)
        _close_loan(user, kredi)
        add_dekont(db, email, "KREDI_KAPAMA", toplam, f"Kredi {kredi['kredi_id']} erken kapatıldı")
    return jsonify(status="success", message=f"{toplam:.2f} TL ödendi, {kredi_id} kapandı.")


# ---------------------------------------------------------------------------
# DİĞER KULLANICI İŞLEMLERİ
# ---------------------------------------------------------------------------
@app.route("/api/action/para-yatir", methods=["POST"])
def api_para_yatir():
    data = body()
    tutar = parse_amount(data.get("tutar"))
    kanit = " ".join(str(data.get("kanit", "")).split())[:200]
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
            "tarih": datetime.now().strftime("%d/%m/%Y %H:%M"),
        }
    return jsonify(status="success", message="Para yatırma talebiniz yönetici onayına gönderildi.")


@app.route("/api/action/para-cek", methods=["POST"])
def api_para_cek():
    tutar = parse_amount(body().get("tutar"))
    with transaction() as db:
        email, user = require_user(db)
        ensure_pending_room(db, email)
        oran = 0.0 if user.get("is_hesabi") else db["ayarlar"]["komisyon_cekme"]
        komisyon = round(tutar * oran, 2)
        toplam = round(tutar + komisyon, 2)
        if user["bakiye"] < toplam:
            raise ApiError(f"Bakiyeniz yetersiz. Komisyon dahil çekim tutarı: {toplam:.2f} TL")
        # Tutar talep anında bloke edilir; aynı bakiyeyle birden fazla talep açılamaz.
        user["bakiye"] = round(user["bakiye"] - toplam, 2)
        db["bekleyen_talepler"][generate_req_id()] = {
            "tur": "para_cekme",
            "user_id": email,
            "tutar": tutar,
            "komisyon": komisyon,
            "toplam": toplam,
            "tarih": datetime.now().strftime("%d/%m/%Y %H:%M"),
        }
    extra = "iş hesabı: komisyon yok" if user.get("is_hesabi") else f"%{round(oran * 100, 2):g} komisyon"
    return jsonify(status="success", message=f"Para çekme talebiniz oluşturuldu ({extra}). Tutar bloke edildi, yönetici onayı bekleniyor.")


@app.route("/api/action/odeme", methods=["POST"])
def api_odeme():
    data = body()
    fiyat = parse_amount(data.get("fiyat"), "fiyat")
    aciklama = " ".join(str(data.get("aciklama", "")).split())[:200]
    if not aciklama:
        raise ApiError("Ürün adı veya açıklama girin.")
    with transaction() as db:
        email, user = require_user(db)
        if not user.get("dogrulandi"):
            raise ApiError("Kısıtlı hesap. Önce yüz doğrulaması yapmalısınız.", 403)
        ensure_pending_room(db, email)
        komisyon = round(fiyat * db["ayarlar"]["komisyon_odeme"], 2)
        toplam = round(fiyat + komisyon, 2)
        if user["bakiye"] < toplam:
            raise ApiError(f"Bakiyeniz yetersiz. Komisyon dahil toplam: {toplam:.2f} TL")
        user["bakiye"] = round(user["bakiye"] - toplam, 2)
        rid = generate_req_id()
        db["bekleyen_talepler"][rid] = {
            "tur": "satin_alim",
            "user_id": email,
            "tutar": fiyat,
            "komisyon": komisyon,
            "toplam": toplam,
            "link": aciklama,
            "tarih": datetime.now().strftime("%d/%m/%Y %H:%M"),
        }
    return jsonify(status="success", message=f"Ödeme alındı, onay bekleniyor. İşlem kodu: {rid}")


def _havale_bugun(db, email):
    bugun = datetime.now().strftime("%d/%m/%Y")
    return sum(
        d["tutar"]
        for d in db["dekontlar"]
        if d.get("user_email") == email and d.get("tur") == "HAVALE_CIKIS" and str(d.get("tarih", "")).startswith(bugun)
    )


@app.route("/api/action/havale", methods=["POST"])
def api_havale():
    data = body()
    tutar = parse_amount(data.get("tutar"))
    hedef = clean_iban(data.get("iban"))
    if not hedef:
        raise ApiError("Alıcı IBAN girin.")
    with transaction() as db:
        email, gonderen = require_user(db)
        if not gonderen.get("dogrulandi"):
            kalan = KISITLI_GUNLUK_HAVALE - _havale_bugun(db, email)
            if tutar > kalan:
                raise ApiError(
                    f"Kısıtlı hesapta günlük havale limiti {KISITLI_GUNLUK_HAVALE:.0f} TL. Bugün kalan limitiniz: {max(0, kalan):.2f} TL",
                    403,
                )
        alici_email = next((e for e, u in db["users"].items() if u.get("iban") == hedef), None)
        if not alici_email:
            raise ApiError("Bu IBAN'a ait bir hesap bulunamadı.")
        if alici_email == email:
            raise ApiError("Kendi hesabınıza havale yapamazsınız.")
        alici = db["users"][alici_email]
        if alici.get("durum") == "dondurulmus":
            raise ApiError("Alıcı hesap şu an işlem kabul etmiyor.")

        ayar = db["ayarlar"]
        komisyon = round(tutar * ayar["komisyon_havale"], 2) if tutar > ayar["havale_sinir"] else 0.0
        toplam = round(tutar + komisyon, 2)
        if gonderen["bakiye"] < toplam:
            raise ApiError(f"Bakiyeniz yetersiz. Komisyon dahil toplam: {toplam:.2f} TL")

        gonderen["bakiye"] = round(gonderen["bakiye"] - toplam, 2)
        alici["bakiye"] = round(alici["bakiye"] + tutar, 2)
        db["kasa"] = round(db["kasa"] + komisyon, 2)
        add_dekont(db, email, "HAVALE_CIKIS", tutar, f"Alıcı: {hedef}" + (f" (komisyon {komisyon:.2f} TL)" if komisyon else ""))
        add_dekont(db, alici_email, "HAVALE_GIRIS", tutar, f"Gönderen: {gonderen['iban']}")
    return jsonify(status="success", message="Havale gerçekleşti.")


@app.route("/api/action/talep-iptal", methods=["POST"])
def api_talep_iptal():
    req_id = body().get("req_id")
    with transaction() as db:
        email, user = require_user(db)
        talep = db["bekleyen_talepler"].get(req_id)
        if not talep or talep.get("user_id") != email:
            raise ApiError("Talep bulunamadı.", 404)
        if talep["tur"] in ("satin_alim", "para_cekme"):
            user["bakiye"] = round(user["bakiye"] + talep["toplam"], 2)
            add_dekont(db, email, "TALEP_IPTAL", talep["toplam"], f"{req_id} iptal edildi, tutar iade edildi")
        del db["bekleyen_talepler"][req_id]
    return jsonify(status="success", message="Talep iptal edildi.")


# ---------------------------------------------------------------------------
# ADMİN
# ---------------------------------------------------------------------------
def admin_user_view(email, u):
    return {
        "email": email,
        "ad_soyad": u["ad_soyad"],
        "bakiye": round(u["bakiye"], 2),
        "iban": u["iban"],
        "durum": u["durum"],
        "dogrulandi": bool(u["dogrulandi"]),
        "is_hesabi": bool(u["is_hesabi"]),
        "is_admin": bool(u["is_admin"]),
        "kredi_notu": u["kredi_notu"],
        "kredi_sayisi": len(u["krediler"]),
        "epin_sayisi": len(u["satinalinan_epinler"]),
    }


@app.route("/api/admin/overview", methods=["GET"])
@app.route("/api/admin/users", methods=["GET"])
def api_admin_overview():
    db = load_db()
    _, admin = require_admin(db)
    users = [admin_user_view(e, u) for e, u in db["users"].items()]
    musteriler = [u for u in db["users"].values() if not u["is_admin"]]
    talepler = [pending_view(rid, t, db) for rid, t in db["bekleyen_talepler"].items()]
    talepler.sort(key=lambda x: x.get("tarih", ""))
    stats = {
        "kullanici": len(musteriler),
        "is_hesabi": sum(1 for u in musteriler if u["is_hesabi"]),
        "mevduat": round(sum(u["bakiye"] for u in musteriler), 2),
        "kredi": round(sum(k.get("kalan_borc", 0) for u in db["users"].values() for k in u["krediler"]), 2),
        "ilan": len(db["epin_urunler"]),
        "stok": sum(len(p["stok_kodlar"]) for p in db["epin_urunler"].values()),
    }
    return jsonify(
        status="success",
        kasa=round(db["kasa"], 2),
        ayarlar=db["ayarlar"],
        istatistik=stats,
        users=users,
        bekleyen_talepler=talepler,
        dogrulama_talepleri=list(db["dogrulama_talepleri"].values()),
        is_hesabi_talepleri=list(db["is_hesabi_talepleri"].values()),
        sifre_degistir_zorunlu=bool(admin.get("sifre_degistir_zorunlu")),
    )


@app.route("/api/admin/approve-business", methods=["POST"])
def api_admin_approve_business():
    data = body()
    target, action = data.get("email"), data.get("action")
    if action not in ("onay", "red"):
        raise ApiError("Geçersiz işlem.")
    with transaction() as db:
        require_admin(db)
        if target not in db["is_hesabi_talepleri"]:
            raise ApiError("Başvuru bulunamadı.", 404)
        if action == "onay" and target in db["users"]:
            db["users"][target]["is_hesabi"] = True
        del db["is_hesabi_talepleri"][target]
    return jsonify(status="success", message="İş hesabı başvurusu onaylandı." if action == "onay" else "Başvuru reddedildi.")


@app.route("/api/admin/edit-user", methods=["POST"])
def api_admin_edit_user():
    data = body()
    target, action = data.get("email"), data.get("action")
    with transaction() as db:
        admin_email, _ = require_admin(db)
        u = db["users"].get(target)
        if not u:
            raise ApiError("Kullanıcı bulunamadı.", 404)

        if action in ("bakiye_ekle", "bakiye_kes"):
            amount = parse_amount(data.get("amount"))
            if action == "bakiye_ekle":
                u["bakiye"] = round(u["bakiye"] + amount, 2)
                add_dekont(db, target, "ADMIN_BAKIYE_EKLE", amount, "Yönetici bakiye yüklemesi")
                msg = f"{amount:.2f} TL eklendi."
            else:
                if amount > u["bakiye"]:
                    raise ApiError(f"Bakiye yetersiz. En fazla {u['bakiye']:.2f} TL kesilebilir.")
                u["bakiye"] = round(u["bakiye"] - amount, 2)
                add_dekont(db, target, "ADMIN_BAKIYE_KES", amount, "Yönetici bakiye düzeltmesi")
                msg = f"{amount:.2f} TL kesildi."
        elif action == "dondur":
            if target == admin_email or u.get("is_admin"):
                raise ApiError("Yönetici hesapları dondurulamaz.")
            u["durum"] = "dondurulmus"
            msg = "Hesap donduruldu."
        elif action == "aktif_et":
            u["durum"] = "aktif"
            msg = "Hesap aktif edildi."
        elif action == "is_hesabi_yap":
            u["is_hesabi"] = True
            db["is_hesabi_talepleri"].pop(target, None)
            msg = "Hesap iş hesabına yükseltildi."
        elif action == "is_hesabi_kaldir":
            u["is_hesabi"] = False
            msg = "Hesap bireysele düşürüldü. İlanları yayından kalktı."
        elif action == "dogrula":
            u["dogrulandi"] = True
            db["dogrulama_talepleri"].pop(target, None)
            msg = "Hesap doğrulandı."
        else:
            raise ApiError("Geçersiz işlem.")
    return jsonify(status="success", message=msg)


@app.route("/api/admin/update-settings", methods=["POST"])
def api_admin_update_settings():
    data = body()
    with transaction() as db:
        require_admin(db)
        a = db["ayarlar"]
        a["komisyon_odeme"] = parse_rate(data.get("komisyon_odeme", a["komisyon_odeme"]), "Ödeme komisyonu")
        a["komisyon_cekme"] = parse_rate(data.get("komisyon_cekme", a["komisyon_cekme"]), "Çekim komisyonu")
        a["komisyon_havale"] = parse_rate(data.get("komisyon_havale", a["komisyon_havale"]), "Havale komisyonu")
        a["kredi_faiz"] = parse_rate(data.get("kredi_faiz", a["kredi_faiz"]), "Kredi faizi")
        a["komisyon_epin_satis"] = parse_rate(
            data.get("komisyon_epin_satis", a["komisyon_epin_satis"]), "E-Pin satış komisyonu"
        )
        try:
            ceza = float(str(data.get("gecikme_cezasi", a["gecikme_cezasi"])).replace(",", "."))
            sinir = float(str(data.get("havale_sinir", a["havale_sinir"])).replace(",", "."))
        except ValueError:
            raise ApiError("Tutar alanlarına geçerli sayı girin.")
        if not (0 <= ceza <= MAX_TUTAR) or not (0 <= sinir <= MAX_TUTAR):
            raise ApiError("Tutarlar negatif olamaz.")
        a["gecikme_cezasi"] = round(ceza, 2)
        a["havale_sinir"] = round(sinir, 2)
    return jsonify(status="success", message="Ayarlar kaydedildi.")


@app.route("/api/admin/approve-request", methods=["POST"])
def api_admin_approve_request():
    data = body()
    req_id, action = data.get("req_id"), data.get("action")
    if action not in ("onay", "red"):
        raise ApiError("Geçersiz işlem.")
    with transaction() as db:
        require_admin(db)
        talep = db["bekleyen_talepler"].get(req_id)
        if not talep:
            raise ApiError("Talep bulunamadı. Başka bir yönetici işlemiş olabilir.", 404)
        u_email = talep["user_id"]
        user = db["users"].get(u_email)
        tur = talep["tur"]

        if user is not None:
            if action == "onay":
                if tur == "satin_alim":
                    db["kasa"] = round(db["kasa"] + talep["toplam"], 2)
                    add_dekont(db, u_email, "SATIN_ALIM", talep["toplam"], f"Ürün: {talep.get('link')}")
                elif tur == "kredi":
                    kredi = {
                        "kredi_id": generate_kredi_id(),
                        "ana_para": talep["tutar"],
                        "toplam_borc": talep["toplam_borc"],
                        "kalan_borc": talep["toplam_borc"],
                        "toplam_hafta": talep["hafta"],
                        "kalan_hafta": talep["hafta"],
                        "haftalik_taksit": talep["haftalik_taksit"],
                        "faiz_orani": talep["faiz_orani"],
                        "gecikme_sayisi": 0,
                        "sonraki_vade": iso_now(7),
                        "tarih": datetime.now().strftime("%d/%m/%Y %H:%M"),
                    }
                    user["bakiye"] = round(user["bakiye"] + talep["tutar"], 2)
                    user["krediler"].append(kredi)
                    add_dekont(db, u_email, "KREDI_CEKIM", talep["tutar"], f"Kredi onaylandı ({talep['hafta']} hafta)")
                elif tur == "para_yatirma":
                    user["bakiye"] = round(user["bakiye"] + talep["tutar"], 2)
                    add_dekont(db, u_email, "PARA_YATIRMA", talep["tutar"], f"Not: {talep.get('kanit')}")
                elif tur == "para_cekme":
                    db["kasa"] = round(db["kasa"] + talep["komisyon"], 2)
                    add_dekont(
                        db, u_email, "PARA_CEKME", talep["tutar"], f"Nakit çekim (komisyon {talep['komisyon']:.2f} TL)"
                    )
            else:  # red
                if tur in ("satin_alim", "para_cekme"):
                    user["bakiye"] = round(user["bakiye"] + talep["toplam"], 2)
                    add_dekont(db, u_email, "IADE", talep["toplam"], f"{req_id} reddedildi, tutar iade edildi")
                else:
                    etiket = {"kredi": "Kredi", "para_yatirma": "Para yatırma"}.get(tur, "Talep")
                    add_dekont(db, u_email, "TALEP_RED", talep.get("tutar", 0), f"{etiket} talebi reddedildi")
        del db["bekleyen_talepler"][req_id]
    return jsonify(status="success", message="Talep onaylandı." if action == "onay" else "Talep reddedildi.")


@app.route("/api/admin/verify-user", methods=["POST"])
def api_admin_verify_user():
    data = body()
    target, action = data.get("email"), data.get("action")
    if action not in ("onay", "red"):
        raise ApiError("Geçersiz işlem.")
    with transaction() as db:
        require_admin(db)
        if target not in db["dogrulama_talepleri"]:
            raise ApiError("Doğrulama talebi bulunamadı.", 404)
        if action == "onay" and target in db["users"]:
            db["users"][target]["dogrulandi"] = True
        elif target in db["users"]:
            db["users"][target]["selfie"] = None
        del db["dogrulama_talepleri"][target]
    return jsonify(status="success", message="Hesap doğrulandı." if action == "onay" else "Doğrulama reddedildi.")


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
    current = load_db()
    require_admin(current)
    f = request.files.get("file")
    if not f:
        raise ApiError("Dosya seçilmedi.")
    try:
        new_db = json.load(f)
    except (ValueError, UnicodeDecodeError):
        raise ApiError("Dosya geçerli bir JSON değil.")
    if not isinstance(new_db, dict) or not isinstance(new_db.get("users"), dict):
        raise ApiError("Yedek dosyası beklenen yapıda değil (users alanı yok).")
    for email, u in new_db["users"].items():
        if not isinstance(u, dict) or "bakiye" not in u or "iban" not in u:
            raise ApiError(f"'{email}' kaydı geçersiz. Dosya yüklenmedi.")

    with db_lock():
        if os.path.exists(DB_FILE):
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            try:
                with open(DB_FILE, "rb") as src, open(f"{DB_FILE}.onceki_{stamp}", "wb") as dst:
                    dst.write(src.read())
            except OSError:
                pass
        if "admin" not in new_db["users"] and "admin" in current["users"]:
            new_db["users"]["admin"] = current["users"]["admin"]
        normalize_db(new_db, create_admin=True)
        save_db(new_db)
    mark_backup_dirty()
    return jsonify(status="success", message="Veritabanı yüklendi. Önceki sürüm yedeklendi.")


# ---------------------------------------------------------------------------
# ARKA PLAN İŞLERİ
# ---------------------------------------------------------------------------
def gecikme_kontrol():
    """Vadesi geçen her kredi için, geçen hafta başına bir kez ceza uygular."""
    now = datetime.now()
    with transaction() as db:
        ceza = db["ayarlar"]["gecikme_cezasi"]
        for email, u in db["users"].items():
            for k in u["krediler"]:
                if k.get("kalan_borc", 0) <= 0:
                    continue
                vade = parse_iso(k.get("sonraki_vade"))
                guard = 0
                while vade and now > vade and guard < 52:
                    guard += 1
                    k["kalan_borc"] = round(k["kalan_borc"] + ceza, 2)
                    k["gecikme_sayisi"] = k.get("gecikme_sayisi", 0) + 1
                    u["kredi_notu"] = max(0, u["kredi_notu"] - 25)
                    vade += timedelta(days=7)
                    add_dekont(db, email, "KREDI_GECIKME_CEZASI", ceza, f"Kredi {k['kredi_id']} gecikme cezası")
                if guard:
                    k["sonraki_vade"] = vade.strftime("%Y-%m-%dT%H:%M:%S")


def background_loop():
    last_check = 0.0
    while True:
        try:
            if time.time() - last_check >= 3600:
                gecikme_kontrol()
                last_check = time.time()
            flush_backup_if_due()
        except Exception as exc:  # noqa: BLE001
            print(f"Arka plan işi hatası: {exc}")
        time.sleep(60)


_bg_started = False


def start_background():
    global _bg_started
    if _bg_started or os.getenv("DISABLE_BACKGROUND") == "1":
        return
    _bg_started = True
    threading.Thread(target=background_loop, name="dekontika-bg", daemon=True).start()


init_db()
start_background()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)