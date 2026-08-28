# -*- coding: utf-8 -*-
"""قاعدة البيانات SQLite — نظام الحسابات المتكامل"""
import re
import shutil
import sqlite3
import secrets
import sys
import json
from datetime import date, datetime
from pathlib import Path
from werkzeug.security import generate_password_hash

# في النسخة المجمّعة (EXE) البيانات تُحفظ بجوار الملف التنفيذي وليس داخل مجلد مؤقت
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent
INSTANCE_DIR = BASE_DIR / "instance"
DB_PATH = INSTANCE_DIR / "accounting.db"
SECRET_PATH = INSTANCE_DIR / "secret.key"
BACKUP_DIR = INSTANCE_DIR / "backups"

# المناطق الافتراضية — تُنسخ لقاعدة البيانات أول تشغيل فقط
DEFAULT_REGIONS = [
    "المركز الرئيسي",
    "المنطقة المركزية",
    "المنطقة الشمالية",
    "المنطقة الشرقية",
    "المنطقة الجنوبية",
]

# يُحدّث ديناميكيًا عبر load_regions() بعد تهيئة القاعدة
REGIONS: list[str] = []

ACCOUNT_TYPES = ["أصول", "خصوم", "حقوق ملكية", "إيرادات", "مصروفات", "أخرى"]

STARTER_ACCOUNTS = [
    ("1101", "الصندوق", "أصول"),
    ("1102", "البنك الأهلي", "أصول"),
    ("1103", "بنك مصر", "أصول"),
    ("1201", "العملاء (المدينون)", "أصول"),
    ("1301", "المخزون", "أصول"),
    ("1401", "الأصول الثابتة", "أصول"),
    ("1402", "مجمع الإهلاك", "أصول"),
    ("2101", "الموردون (الدائنون)", "خصوم"),
    ("2102", "مصروفات مستحقة", "خصوم"),
    ("2201", "قروض طويلة الأجل", "خصوم"),
    ("3101", "رأس المال", "حقوق ملكية"),
    ("3102", "المسحوبات الشخصية", "حقوق ملكية"),
    ("3103", "الأرباح المحتجزة", "حقوق ملكية"),
    ("4101", "إيرادات المبيعات", "إيرادات"),
    ("4102", "إيرادات أخرى", "إيرادات"),
    ("5101", "رواتب وأجور", "مصروفات"),
    ("5102", "إيجارات", "مصروفات"),
    ("5103", "كهرباء ومياه", "مصروفات"),
    ("5104", "هاتف وإنترنت", "مصروفات"),
    ("5105", "صيانة", "مصروفات"),
    ("5106", "وقود ومواصلات", "مصروفات"),
    ("5107", "قرطاسية ومطبوعات", "مصروفات"),
    ("5108", "إعلانات ودعاية", "مصروفات"),
    ("5109", "ضيافة", "مصروفات"),
    ("5110", "مصروفات بنكية", "مصروفات"),
    ("5111", "إهلاك", "مصروفات"),
    ("5112", "ضرائب ورسوم", "مصروفات"),
    ("5113", "مصروفات متنوعة", "مصروفات"),
    ("9901", "حساب وسيط - سداد", "أخرى"),
    ("9902", "فروقات تسوية", "أخرى"),
]


def connect():
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def query(sql, args=()):
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def query_one(sql, args=()):
    rows = query(sql, args)
    return rows[0] if rows else None


def execute(sql, args=()):
    with connect() as conn:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.lastrowid


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    full_name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'accountant',
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    acc_no TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    type TEXT DEFAULT '',
    opening_balance REAL NOT NULL DEFAULT 0,
    budget REAL NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS journal_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    movement_no TEXT UNIQUE NOT NULL,
    entry_date TEXT NOT NULL,
    description TEXT DEFAULT '',
    region TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','posted')),
    entry_type TEXT DEFAULT 'عادي',
    created_by INTEGER REFERENCES users(id),
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS journal_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id INTEGER NOT NULL REFERENCES journal_entries(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    debit REAL NOT NULL DEFAULT 0,
    credit REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user TEXT,
    action TEXT,
    details TEXT,
    at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS regions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_lines_entry ON journal_lines(entry_id);
CREATE INDEX IF NOT EXISTS idx_lines_account ON journal_lines(account_id);
CREATE INDEX IF NOT EXISTS idx_entries_date ON journal_entries(entry_date);
CREATE INDEX IF NOT EXISTS idx_entries_status ON journal_entries(status);
"""


def load_secret():
    SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SECRET_PATH.exists():
        return SECRET_PATH.read_bytes()
    key = secrets.token_hex(32).encode()
    SECRET_PATH.write_bytes(key)
    return key


def init_db():
    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect()
    conn.executescript(SCHEMA)

    # ترقية قواعد البيانات القديمة: إضافة عمود نوع القيد
    existing_cols = {r["name"] for r in conn.execute("PRAGMA table_info(journal_entries)")}
    if "entry_type" not in existing_cols:
        conn.execute("ALTER TABLE journal_entries ADD COLUMN entry_type TEXT DEFAULT 'عادي'")
        conn.commit()

    # ترقية: إضافة عمود الميزانية التقديرية للحسابات
    acc_cols = {r["name"] for r in conn.execute("PRAGMA table_info(accounts)")}
    if "budget" not in acc_cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN budget REAL NOT NULL DEFAULT 0")
        conn.commit()

    # المستخدمون الافتراضيون
    default_users = [
        ("admin", "admin123", "مدير النظام", "admin"),
        ("ahmed", "ahmed123", "محاسب / أحمد عبدالله", "accountant"),
        ("viewer", "viewer123", "مستخدم مشاهدة", "viewer"),
    ]
    for username, pwd, full_name, role in default_users:
        conn.execute(
            "INSERT OR IGNORE INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
            (username, generate_password_hash(pwd), full_name, role),
        )

    # دليل حسابات ابتدائي (رقم، اسم، نوع، ميزانية تقديرية)
    BUDGET_ACCOUNTS = [
        ("5101", "رواتب وأجور", "مصروفات", 480000),
        ("5102", "إيجارات", "مصروفات", 120000),
        ("5103", "كهرباء ومياه", "مصروفات", 36000),
        ("5104", "هاتف وإنترنت", "مصروفات", 18000),
        ("5105", "صيانة", "مصروفات", 24000),
        ("5106", "وقود ومواصلات", "مصروفات", 30000),
        ("5107", "قرطاسية ومطبوعات", "مصروفات", 12000),
        ("5108", "إعلانات ودعاية", "مصروفات", 15000),
        ("5109", "ضيافة", "مصروفات", 10000),
        ("5110", "صيانة سيرفر وشبكات", "مصروفات", 20000),
        ("5201", "تأمينات", "مصروفات", 25000),
        ("5202", "استشارات قانونية", "مصروفات", 8000),
        ("5301", "اشتراكات برامج", "مصروفات", 15000),
        ("5401", "مهمات سفر", "مصروفات", 20000),
    ]
    budget_map = {a[0]: a[3] for a in BUDGET_ACCOUNTS}
    for acc_no, name, acc_type in STARTER_ACCOUNTS:
        budget = budget_map.get(acc_no, 0)
        conn.execute(
            "INSERT OR IGNORE INTO accounts (acc_no, name, type, budget) VALUES (?,?,?,?)",
            (acc_no, name, acc_type, budget),
        )

    # الإعدادات الافتراضية
    year = datetime_now().year
    defaults = {
        "company_name": "نظام محاسبي متكامل",
        "period_from": f"{year}-01-01",
        "period_to": f"{year}-12-31",
        "signatures": json.dumps([
            {"title": "مدير إدارة الميزانية", "name": "محاسب / احمد عبدالله"},
            {"title": "مدير الإدارة العامة المالية", "name": "محاسب / محمد احمد سيد"},
        ], ensure_ascii=False),
    }
    for k, v in defaults.items():
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?,?)", (k, v))

    # المناطق الافتراضية (أول مرة فقط)
    for region_name in DEFAULT_REGIONS:
        conn.execute("INSERT OR IGNORE INTO regions (name) VALUES (?)", (region_name,))

    # قيود تجريبية للبدء (يمكن حذفها من السيستم)
    demo = [
        ("1", f"{year}-08-01", "إيداع نقدي بالبنك", "المركز الرئيسي", "posted",
         [("1102", 50000, 0), ("1101", 0, 50000)]),
        ("2", f"{year}-08-05", "سداد إيجار المكتب بشيك", "المنطقة الشمالية", "posted",
         [("5102", 12000, 0), ("1102", 0, 12000)]),
        ("3", f"{year}-08-10", "شراء أثاث مكتبي بالآجل", "المنطقة المركزية", "draft",
         [("1401", 8000, 0), ("2101", 0, 8000)]),
    ]
    if conn.execute("SELECT COUNT(*) c FROM journal_entries").fetchone()["c"] == 0:
        admin = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]
        for mv, dt, desc, region, status, lines in demo:
            cur = conn.execute(
                "INSERT INTO journal_entries (movement_no, entry_date, description, region, status, created_by) VALUES (?,?,?,?,?,?)",
                (mv, dt, desc, region, status, admin),
            )
            for acc_no, d, c in lines:
                acc = conn.execute("SELECT id FROM accounts WHERE acc_no=?", (acc_no,)).fetchone()
                conn.execute(
                    "INSERT INTO journal_lines (entry_id, account_id, debit, credit) VALUES (?,?,?,?)",
                    (cur.lastrowid, acc["id"], d, c),
                )
    conn.commit()
    conn.close()
    load_regions()


def datetime_now():
    from datetime import datetime
    return datetime.now()


def get_setting(key, default=""):
    row = query_one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


def set_setting(key, value):
    execute(
        "INSERT INTO settings (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


# ------------------------------------------------------------------
# المناطق
# ------------------------------------------------------------------

def load_regions():
    """يحمّل المناطق النشطة من قاعدة البيانات ويحدّث المتغير العام REGIONS"""
    global REGIONS
    try:
        REGIONS = [r["name"] for r in query("SELECT name FROM regions WHERE is_active=1 ORDER BY id")]
    except Exception:
        REGIONS = list(DEFAULT_REGIONS)
    return REGIONS


def get_all_regions():
    """يرجع جميع المناطق مع عدد القيود المرتبطة بها (للإعدادات)"""
    rows = query("""
        SELECT r.id, r.name, r.is_active, r.created_at,
               (SELECT COUNT(*) FROM journal_entries WHERE region=r.name) AS usage_count
        FROM regions r ORDER BY r.id
    """)
    return rows


def add_region(name):
    name = (name or "").strip()
    if not name or len(name) > 120:
        return None, "اسم المنطقة مطلوب (حد أقصى 120 حرف)"
    try:
        rid = execute("INSERT INTO regions (name) VALUES (?)", (name,))
        load_regions()
        return rid, None
    except Exception:
        return None, f"المنطقة «{name}» موجودة بالفعل"


def rename_region(rid, new_name):
    new_name = (new_name or "").strip()
    if not new_name:
        return "اسم المنطقة مطلوب"
    existing = query_one("SELECT name FROM regions WHERE id=?", (rid,))
    if not existing:
        return "المنطقة غير موجودة"
    old_name = existing["name"]
    try:
        execute("UPDATE regions SET name=? WHERE id=?", (new_name, rid))
    except Exception:
        return f"المنطقة «{new_name}» موجودة بالفعل"
    if old_name != new_name:
        execute("UPDATE journal_entries SET region=? WHERE region=?", (new_name, old_name))
    load_regions()
    return None


def deactivate_region(rid):
    r = query_one("SELECT name FROM regions WHERE id=?", (rid,))
    if not r:
        return "المنطقة غير موجودة"
    execute("UPDATE regions SET is_active=0 WHERE id=?", (rid,))
    load_regions()
    return None


def activate_region(rid):
    r = query_one("SELECT name FROM regions WHERE id=?", (rid,))
    if not r:
        return "المنطقة غير موجودة"
    execute("UPDATE regions SET is_active=1 WHERE id=?", (rid,))
    load_regions()
    return None


def delete_region(rid):
    """حذف نهائي فقط لو لا قيود مرتبطة"""
    r = query_one(
        "SELECT name FROM regions WHERE id=?", (rid,))
    if not r:
        return "المنطقة غير موجودة"
    cnt = query_one(
        "SELECT COUNT(*) AS c FROM journal_entries WHERE region=?", (r["name"],))["c"]
    if cnt:
        return f"لا يمكن الحذف — في {cnt} قيد مرتبط بهذه المنطقة. عطّلها بدلاً من الحذف"
    execute("DELETE FROM regions WHERE id=?", (rid,))
    load_regions()
    return None


def audit(user, action, details=""):
    execute("INSERT INTO audit_log (user, action, details) VALUES (?,?,?)", (user, action, details))


# ------------------------------------------------------------------
# النسخ الاحتياطي
# ------------------------------------------------------------------

_last_auto_check = None


def create_backup(prefix="manual"):
    """ينشئ نسخة احتياطية آمنة من قاعدة البيانات ويرجع اسم الملف"""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime_now().strftime("%Y-%m-%d_%H%M%S")
    dest = BACKUP_DIR / f"{prefix}-{ts}.db"
    conn = connect()
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    shutil.copy2(DB_PATH, dest)
    return dest.name


def auto_monthly_backup():
    """نسخة احتياطية أوتوماتيك أول ما يبدأ شهر جديد — تُفحص مرة واحدة يوميًا"""
    global _last_auto_check
    today = date.today()
    if _last_auto_check == today:
        return
    _last_auto_check = today
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    marker = today.strftime("%Y-%m")
    has_this_month = any(p.name.startswith(f"auto-{marker}") for p in BACKUP_DIR.glob("auto-*.db"))
    if not has_this_month:
        create_backup(prefix=f"auto-{today.isoformat()}")
    # الاحتفاظ بأحدث 24 نسخة فقط (حسب وقت الإنشاء وليس ترتيب الأسماء)
    all_backups = sorted(BACKUP_DIR.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in all_backups[24:]:
        try:
            old.unlink()
        except OSError:
            pass


SAFE_BACKUP_NAME = re.compile(r"^(auto|manual)-[A-Za-z0-9_\-.]+\.db$")


def list_backups():
    if not BACKUP_DIR.exists():
        return []
    out = []
    for p in sorted(BACKUP_DIR.glob("*.db"), reverse=True):
        if SAFE_BACKUP_NAME.match(p.name):
            out.append({
                "name": p.name,
                "size": p.stat().st_size,
                "modified": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
            })
    return out


if __name__ == "__main__":
    init_db()
    print("تم إنشاء قاعدة البيانات بنجاح")
