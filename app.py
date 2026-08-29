# -*- coding: utf-8 -*-
"""نظام الحسابات المتكامل — محاسب / أحمد عبدالله"""
from datetime import date, datetime, timedelta
from functools import wraps
import collections
import json
import os
import re
import sys
import time

from flask import (Flask, abort, g, jsonify, redirect, render_template,
                   request, send_file, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import database as db
import excel_tools as xl

ROLES = {"admin": "مدير النظام", "accountant": "محاسب", "viewer": "مشاهدة فقط"}
ENTRY_TYPES = ["عادي", "تسوية", "افتتاحي", "إقفال"]

# في النسخة المجمّعة (EXE) القوالب والملفات الثابتة تُفك داخل مجلد مؤقت _MEIPASS
if getattr(sys, "frozen", False):
    _RES_DIR = sys._MEIPASS
else:
    _RES_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__,
            template_folder=os.path.join(_RES_DIR, "templates"),
            static_folder=os.path.join(_RES_DIR, "static"))
app.secret_key = db.load_secret()
app.permanent_session_lifetime = timedelta(hours=12)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=10 * 1024 * 1024,  # 10MB upload limit
)

# ------------------------------------------------------------------
# الحماية والأمان
# ------------------------------------------------------------------

# Rate limiting بسيط
_login_attempts = collections.defaultdict(lambda: collections.deque())
LOGIN_MAX = 5
LOGIN_WINDOW = 300  # 5 دقائق


def _check_rate_limit(ip):
    now = time.time()
    attempts = _login_attempts[ip]
    while attempts and attempts[0] < now - LOGIN_WINDOW:
        attempts.popleft()
    return len(attempts) < LOGIN_MAX


def _record_login_attempt(ip):
    _login_attempts[ip].append(time.time())


@app.after_request
def _security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "font-src 'self'; connect-src 'self'"
    )
    return response


def _sanitize(value):
    """إزالة محتوى ضار من المدخلات النصية"""
    if not isinstance(value, str):
        return value
    value = value.strip()
    value = re.sub(r'<script[^>]*>.*?</script>', '', value, flags=re.IGNORECASE | re.DOTALL)
    value = re.sub(r'on\w+\s*=', '', value, flags=re.IGNORECASE)
    return value


def _sanitize_dict(d):
    if not isinstance(d, dict):
        return d
    return {k: _sanitize(v) if isinstance(v, str) else v for k, v in d.items()}


# ------------------------------------------------------------------
# المصادقة والصلاحيات
# ------------------------------------------------------------------

@app.before_request
def before():
    allowed = {"login", "static"}
    if request.endpoint in allowed:
        return None
    uid = session.get("uid")
    g.user = db.query_one("SELECT * FROM users WHERE id=?", (uid,)) if uid else None
    if g.user is None:
        if request.path.startswith("/api/"):
            return jsonify(error="انتهت الجلسة، يرجى تسجيل الدخول"), 401
        return redirect(url_for("login"))
    try:
        db.auto_monthly_backup()
    except Exception:
        pass
    return None


def can_write():
    return g.user["role"] in ("admin", "accountant")


def require_write(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not can_write():
            return jsonify(error="صلاحيتك للمشاهدة فقط"), 403
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if g.user["role"] != "admin":
            return jsonify(error="هذه الصفحة لمدير النظام فقط"), 403
        return f(*args, **kwargs)
    return wrapper


# ------------------------------------------------------------------
# سياق عام للقوالب
# ------------------------------------------------------------------

def _signatures():
    """قائمة التواقيع القابلة للتعديل من الإعدادات [{title, name}]"""
    try:
        return json.loads(db.get_setting("signatures") or "[]")
    except Exception:
        return []


@app.context_processor
def inject_globals():
    user = getattr(g, "user", None)
    return {
        "REGIONS": db.load_regions(),
        "user": user,
        "role_name": ROLES.get(user["role"], "") if user else "",
        "company_name": db.get_setting("company_name"),
        "period_from": db.get_setting("period_from"),
        "period_to": db.get_setting("period_to"),
        "signatures": _signatures(),
        "today": date.today().isoformat(),
    }


# ------------------------------------------------------------------
# تسجيل الدخول
# ------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    ip = request.remote_addr or "0.0.0.0"
    if request.method == "POST":
        if not _check_rate_limit(ip):
            error = "تم حظر تسجيل الدخول مؤقتًا — كثرة المحاولات الخاطئة. حاول بعد 5 دقائق"
            return render_template("login.html", error=error)
        username = _sanitize(request.form.get("username", ""))
        password = request.form.get("password", "")
        user = db.query_one("SELECT * FROM users WHERE username=?", (username,))
        if user and check_password_hash(user["password_hash"], password):
            session.permanent = True
            session["uid"] = user["id"]
            _login_attempts[ip].clear()
            db.audit(user["full_name"], "تسجيل دخول")
            return redirect(url_for("dashboard"))
        _record_login_attempt(ip)
        error = "اسم المستخدم أو كلمة المرور غير صحيحة"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    if getattr(g, "user", None):
        db.audit(g.user["full_name"], "تسجيل خروج")
    session.clear()
    return redirect(url_for("login"))


# ------------------------------------------------------------------
# الصفحات
# ------------------------------------------------------------------

@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/accounts")
def accounts_page():
    return render_template("accounts.html")


@app.route("/accounts/tree")
def accounts_tree_page():
    return render_template("accounts_tree.html")


@app.route("/journal")
def journal_page():
    return render_template("journal.html")


@app.route("/ledger")
def ledger_page():
    return render_template("ledger.html")


@app.route("/trial-balance")
def trial_balance_page():
    return render_template("trial_balance.html")


@app.route("/budget")
def budget_page():
    return render_template("budget.html")


@app.route("/users")
@admin_required
def users_page():
    users = db.query("SELECT id, username, full_name, role, created_at FROM users ORDER BY id")
    return render_template("users.html", users=users)


@app.route("/settings")
def settings_page():
    return render_template("settings.html")


@app.route("/audit")
@admin_required
def audit_page():
    logs = db.query(
        "SELECT user, action, details, at FROM audit_log ORDER BY id DESC LIMIT 300"
    )
    return render_template("audit.html", logs=logs)


@app.route("/help")
def help_page():
    return render_template("help.html")


# ------------------------------------------------------------------
# APIs — دليل الحسابات
# ------------------------------------------------------------------

@app.route("/api/accounts")
def api_accounts():
    q = request.args.get("q", "").strip()
    sql = "SELECT * FROM accounts"
    args = []
    if q:
        sql += " WHERE acc_no LIKE ? OR name LIKE ?"
        args = [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY CAST(acc_no AS INTEGER), acc_no"
    return jsonify(accounts=db.query(sql, args))


@app.route("/api/accounts", methods=["POST"])
@require_write
def api_accounts_create():
    d = request.json or {}
    acc_no = (d.get("acc_no") or "").strip()
    name = (d.get("name") or "").strip()
    if not acc_no or not name:
        return jsonify(error="رقم الحساب واسم الحساب مطلوبان"), 400
    if db.query_one("SELECT id FROM accounts WHERE acc_no=?", (acc_no,)):
        return jsonify(error=f"رقم الحساب {acc_no} موجود بالفعل"), 400
    acc_id = db.execute(
        "INSERT INTO accounts (acc_no, name, type, opening_balance, budget) VALUES (?,?,?,?,?)",
        (acc_no, name, (d.get("type") or "").strip(), float(d.get("opening_balance") or 0), float(d.get("budget") or 0)),
    )
    db.audit(g.user["full_name"], "إضافة حساب", f"{acc_no} - {name}")
    return jsonify(ok=True, id=acc_id)


@app.route("/api/accounts/<int:acc_id>", methods=["PUT"])
@require_write
def api_accounts_update(acc_id):
    d = request.json or {}
    acc_no = (d.get("acc_no") or "").strip()
    name = (d.get("name") or "").strip()
    if not acc_no or not name:
        return jsonify(error="رقم الحساب واسم الحساب مطلوبان"), 400
    dup = db.query_one("SELECT id FROM accounts WHERE acc_no=? AND id<>?", (acc_no, acc_id))
    if dup:
        return jsonify(error=f"رقم الحساب {acc_no} مستخدم بالفعل في حساب آخر"), 400
    db.execute(
        "UPDATE accounts SET acc_no=?, name=?, type=?, opening_balance=?, budget=? WHERE id=?",
        (acc_no, name, (d.get("type") or "").strip(), float(d.get("opening_balance") or 0), float(d.get("budget") or 0), acc_id),
    )
    db.audit(g.user["full_name"], "تعديل حساب", f"{acc_no} - {name}")
    return jsonify(ok=True)


@app.route("/api/accounts/<int:acc_id>", methods=["DELETE"])
@require_write
def api_accounts_delete(acc_id):
    used = db.query_one("SELECT id FROM journal_lines WHERE account_id=? LIMIT 1", (acc_id,))
    if used:
        return jsonify(error="لا يمكن حذف الحساب لوجود حركات عليه في دفتر اليومية"), 400
    acc = db.query_one("SELECT * FROM accounts WHERE id=?", (acc_id,))
    if not acc:
        return jsonify(error="الحساب غير موجود"), 404
    db.execute("DELETE FROM accounts WHERE id=?", (acc_id,))
    db.audit(g.user["full_name"], "حذف حساب", f"{acc['acc_no']} - {acc['name']}")
    return jsonify(ok=True)


@app.route("/api/accounts/wipe", methods=["POST"])
@admin_required
def api_accounts_wipe():
    """مسح الشجرة بالكامل: كل الحسابات وكل القيود نهائيًا (الإدارة فقط)."""
    n_acc = db.query_one("SELECT COUNT(*) AS c FROM accounts")["c"]
    n_ent = db.query_one("SELECT COUNT(*) AS c FROM journal_entries")["c"]
    if not n_acc:
        return jsonify(error="لا توجد حسابات لمسحها"), 400
    db.execute("DELETE FROM journal_entries")
    db.execute("DELETE FROM accounts")
    db.audit(g.user["full_name"], "مسح الشجرة",
             f"حذف نهائي: {n_acc} حساب و{n_ent} قيد")
    return jsonify(ok=True, deleted_accounts=n_acc, deleted_entries=n_ent)


@app.route("/api/accounts/budget", methods=["POST"])
@require_write
def api_accounts_bulk_budget():
    d = request.json or {}
    items = d.get("items") or []
    if not items:
        return jsonify(error="لا توجد بيانات للتحديث"), 400
    updated = 0
    for item in items:
        acc_id = item.get("id")
        budget = float(item.get("budget") or 0)
        if acc_id:
            db.execute("UPDATE accounts SET budget=? WHERE id=?", (budget, acc_id))
            updated += 1
    db.audit(g.user["full_name"], "تحديث ميزانيات", f"{updated} حساب")
    return jsonify(ok=True, updated=updated)


@app.route("/api/accounts/import", methods=["POST"])
@require_write
def api_accounts_import():
    f = request.files.get("file")
    if not f or not f.filename.lower().endswith(".xlsx"):
        return jsonify(error="اختر ملف إكسيل بصيغة .xlsx"), 400
    rows, errors = xl.read_accounts_template(f.stream)
    added, updated = 0, 0
    for r in rows:
        existing = db.query_one("SELECT id FROM accounts WHERE acc_no=?", (r["acc_no"],))
        if existing:
            db.execute(
                "UPDATE accounts SET name=?, type=?, opening_balance=? WHERE id=?",
                (r["name"], r["type"], r["opening"], existing["id"]),
            )
            updated += 1
        else:
            db.execute(
                "INSERT INTO accounts (acc_no, name, type, opening_balance) VALUES (?,?,?,?)",
                (r["acc_no"], r["name"], r["type"], r["opening"]),
            )
            added += 1
    db.audit(g.user["full_name"], "استيراد دليل حسابات",
             f"جديد: {added} | محدّث: {updated}")
    return jsonify(ok=True, added=added, updated=updated, errors=errors[:10])


# ------------------------------------------------------------------
# APIs — دفتر اليومية
# ------------------------------------------------------------------

def next_movement_no():
    rows = db.query("SELECT movement_no FROM journal_entries")
    mx = 0
    for r in rows:
        try:
            mx = max(mx, int(float(str(r["movement_no"]).replace(",", ""))))
        except (ValueError, TypeError):
            continue
    return str(mx + 1)


@app.route("/api/next-movement-no")
@require_write
def api_next_movement():
    return jsonify(no=next_movement_no())


def _validate_entry_payload(d):
    entry_date = (d.get("entry_date") or "").strip()
    region = (d.get("region") or "").strip()
    movement = (d.get("movement_no") or "").strip()
    lines = d.get("lines") or []
    if not entry_date:
        return None, "التاريخ مطلوب"
    try:
        datetime.strptime(entry_date, "%Y-%m-%d")
    except ValueError:
        return None, "صيغة التاريخ غير صحيحة"
    if region not in db.load_regions():
        return None, "اختر المنطقة الصحيحة"
    if not movement:
        return None, "رقم الحركة مطلوب"
    entry_type = (d.get("entry_type") or "عادي").strip()
    if entry_type not in ENTRY_TYPES:
        return None, "نوع القيد غير صحيح"
    clean = []
    for ln in lines:
        try:
            acc_id = int(ln.get("account_id") or 0)
            debit = round(float(ln.get("debit") or 0), 2)
            credit = round(float(ln.get("credit") or 0), 2)
        except (TypeError, ValueError):
            return None, "قيم المدين والدائن غير صحيحة"
        if debit < 0 or credit < 0:
            return None, "لا يُسمح بقيم سالبة"
        if debit > 0 and credit > 0:
            return None, "لا يمكن أن يكون السطر مدينًا ودائنًا معًا"
        if debit == 0 and credit == 0:
            continue
        if not db.query_one("SELECT id FROM accounts WHERE id=?", (acc_id,)):
            return None, "أحد السطور بدون حساب صحيح"
        clean.append({"account_id": acc_id, "debit": debit, "credit": credit})
    if len(clean) < 2:
        return None, "القيد يحتاج سطرين على الأقل (مدين ودائن)"
    td = sum(l["debit"] for l in clean)
    tc = sum(l["credit"] for l in clean)
    if abs(td - tc) > 0.004:
        return None, f"القيد غير متوازن: مدين {td:,.2f} ≠ دائن {tc:,.2f}"
    if td <= 0:
        return None, "إجمالي القيد يجب أن يكون أكبر من صفر"
    return {
        "movement_no": movement,
        "entry_date": entry_date,
        "region": region,
        "description": (d.get("description") or "").strip(),
        "status": "posted" if d.get("status") == "posted" else "draft",
        "entry_type": entry_type,
        "lines": clean,
    }, None


def _entry_full(entry_id):
    e = db.query_one("SELECT * FROM journal_entries WHERE id=?", (entry_id,))
    if not e:
        return None
    e["lines"] = db.query(
        """SELECT jl.id, jl.account_id, a.acc_no, a.name, jl.debit, jl.credit
           FROM journal_lines jl JOIN accounts a ON a.id = jl.account_id
           WHERE jl.entry_id=? ORDER BY jl.id""",
        (entry_id,),
    )
    e["total_debit"] = round(sum(l["debit"] for l in e["lines"]), 2)
    e["total_credit"] = round(sum(l["credit"] for l in e["lines"]), 2)
    return e


def journal_filters(args):
    where, args_ = [], []
    f, t = (args.get("from") or "").strip(), (args.get("to") or "").strip()
    if f:
        where.append("je.entry_date >= ?")
        args_.append(f)
    if t:
        where.append("je.entry_date <= ?")
        args_.append(t)
    if args.get("region"):
        where.append("je.region = ?")
        args_.append(args["region"])
    if args.get("status") in ("draft", "posted"):
        where.append("je.status = ?")
        args_.append(args["status"])
    if args.get("type") in ENTRY_TYPES:
        where.append("je.entry_type = ?")
        args_.append(args["type"])
    if args.get("acc_no"):
        where.append("""EXISTS (SELECT 1 FROM journal_lines jl
                       JOIN accounts a ON a.id=jl.account_id
                       WHERE jl.entry_id=je.id AND a.acc_no LIKE ?)""")
        args_.append(f"%{args['acc_no']}%")
    q = (args.get("q") or "").strip()
    if q:
        like = f"%{q}%"
        where.append("(je.movement_no LIKE ? OR je.description LIKE ?)")
        args_.extend([like, like])
    return ("WHERE " + " AND ".join(where)) if where else "", args_


@app.route("/api/journal")
def api_journal_list():
    where, args_ = journal_filters(request.args)
    limit = min(int(request.args.get("limit", 500)), 2000)
    ids = db.query(
        f"""SELECT je.id FROM journal_entries je {where}
            ORDER BY je.entry_date DESC, CAST(je.movement_no AS INTEGER) DESC
            LIMIT {limit}""",
        args_,
    )
    entries = [_entry_full(r["id"]) for r in ids]
    return jsonify(entries=[e for e in entries if e], truncated=len(ids) >= limit)


@app.route("/api/journal", methods=["POST"])
@require_write
def api_journal_create():
    data, err = _validate_entry_payload(request.json or {})
    if err:
        return jsonify(error=err), 400
    if db.query_one("SELECT id FROM journal_entries WHERE movement_no=?", (data["movement_no"],)):
        return jsonify(error=f"رقم الحركة {data['movement_no']} مستخدم بالفعل"), 400
    eid = db.execute(
        """INSERT INTO journal_entries (movement_no, entry_date, description, region, status, entry_type, created_by)
           VALUES (?,?,?,?,?,?,?)""",
        (data["movement_no"], data["entry_date"], data["description"],
         data["region"], data["status"], data["entry_type"], g.user["id"]),
    )
    for ln in data["lines"]:
        db.execute(
            "INSERT INTO journal_lines (entry_id, account_id, debit, credit) VALUES (?,?,?,?)",
            (eid, ln["account_id"], ln["debit"], ln["credit"]),
        )
    db.audit(g.user["full_name"],
             "إنشاء قيد " + ("ومرحلته" if data["status"] == "posted" else "(مسودة)"),
             f"رقم الحركة {data['movement_no']}")
    return jsonify(ok=True, id=eid)


@app.route("/api/journal/<int:eid>", methods=["PUT"])
@require_write
def api_journal_update(eid):
    e = db.query_one("SELECT * FROM journal_entries WHERE id=?", (eid,))
    if not e:
        return jsonify(error="القيد غير موجود"), 404
    if e["status"] == "posted":
        return jsonify(error="لا يمكن تعديل قيد مرحل — قم بإلغاء الترحيل أولًا"), 400
    data, err = _validate_entry_payload(request.json or {})
    if err:
        return jsonify(error=err), 400
    dup = db.query_one(
        "SELECT id FROM journal_entries WHERE movement_no=? AND id<>?",
        (data["movement_no"], eid),
    )
    if dup:
        return jsonify(error=f"رقم الحركة {data['movement_no']} مستخدم بالفعل"), 400
    conn = db.connect()
    with conn:
        conn.execute(
            """UPDATE journal_entries SET movement_no=?, entry_date=?, description=?, region=?, status=?, entry_type=?
               WHERE id=?""",
            (data["movement_no"], data["entry_date"], data["description"],
             data["region"], data["status"], data["entry_type"], eid),
        )
        conn.execute("DELETE FROM journal_lines WHERE entry_id=?", (eid,))
        for ln in data["lines"]:
            conn.execute(
                "INSERT INTO journal_lines (entry_id, account_id, debit, credit) VALUES (?,?,?,?)",
                (eid, ln["account_id"], ln["debit"], ln["credit"]),
            )
    finally_close(conn)
    db.audit(g.user["full_name"], "تعديل قيد", f"رقم الحركة {data['movement_no']}")
    return jsonify(ok=True)


def finally_close(conn):
    try:
        conn.close()
    except Exception:
        pass


@app.route("/api/journal/<int:eid>", methods=["DELETE"])
@require_write
def api_journal_delete(eid):
    e = db.query_one("SELECT * FROM journal_entries WHERE id=?", (eid,))
    if not e:
        return jsonify(error="القيد غير موجود"), 404
    if e["status"] == "posted" and g.user["role"] != "admin":
        return jsonify(error="حذف القيود المرحلة لمدير النظام فقط — قم بإلغاء الترحيل أولًا"), 403
    db.execute("DELETE FROM journal_entries WHERE id=?", (eid,))
    db.audit(g.user["full_name"], "حذف قيد", f"رقم الحركة {e['movement_no']}")
    return jsonify(ok=True)


@app.route("/api/journal/bulk-delete", methods=["POST"])
@admin_required
def api_journal_bulk_delete():
    d = request.get_json(silent=True) or {}
    scope = (d.get("scope") or "").strip()
    if scope == "month":
        month = (d.get("month") or "").strip()
        if not re.fullmatch(r"\d{4}-\d{2}", month):
            return jsonify(error="اختر الشهر بشكل صحيح (YYYY-MM)"), 400
        where, args, label = "strftime('%Y-%m', entry_date)=?", (month,), f"شهر {month}"
    elif scope == "all":
        where, args, label = "1=1", (), "جميع القيود"
    else:
        return jsonify(error="نطاق غير صحيح"), 400
    n = db.query_one(f"SELECT COUNT(*) AS c FROM journal_entries WHERE {where}", args)["c"]
    if not n:
        return jsonify(error=f"لا توجد قيود في {label}"), 400
    db.execute(f"DELETE FROM journal_entries WHERE {where}", args)
    db.audit(g.user["full_name"], "مسح قيود", f"حذف {n} قيد ({label})")
    return jsonify(ok=True, deleted=n)


@app.route("/api/journal/<int:eid>/post", methods=["POST"])
@require_write
def api_journal_post(eid):
    e = db.query_one("SELECT * FROM journal_entries WHERE id=?", (eid,))
    if not e:
        return jsonify(error="القيد غير موجود"), 404
    if e["status"] == "posted":
        return jsonify(error="القيد مرحل بالفعل"), 400
    lines = db.query(
        "SELECT debit, credit FROM journal_lines WHERE entry_id=?", (eid,)
    )
    td = round(sum(l["debit"] for l in lines), 2)
    tc = round(sum(l["credit"] for l in lines), 2)
    if abs(td - tc) > 0.004:
        return jsonify(error="لا يمكن ترحيل قيد غير متوازن"), 400
    db.execute("UPDATE journal_entries SET status='posted' WHERE id=?", (eid,))
    db.audit(g.user["full_name"], "ترحيل قيد", f"رقم الحركة {e['movement_no']}")
    return jsonify(ok=True)


@app.route("/api/journal/<int:eid>/unpost", methods=["POST"])
@require_write
def api_journal_unpost(eid):
    e = db.query_one("SELECT * FROM journal_entries WHERE id=?", (eid,))
    if not e:
        return jsonify(error="القيد غير موجود"), 404
    if e["status"] != "posted":
        return jsonify(error="القيد غير مرحل أصلًا"), 400
    db.execute("UPDATE journal_entries SET status='draft' WHERE id=?", (eid,))
    db.audit(g.user["full_name"], "إلغاء ترحيل قيد", f"رقم الحركة {e['movement_no']}")
    return jsonify(ok=True)


@app.route("/api/journal/import", methods=["POST"])
@require_write
def api_journal_import():
    f = request.files.get("file")
    if not f or not f.filename.lower().endswith(".xlsx"):
        return jsonify(error="اختر ملف إكسيل بصيغة .xlsx"), 400
    region = request.form.get("region", "")
    post_flag = request.form.get("post") == "1"
    if region not in db.load_regions():
        region = db.load_regions()[0]
    groups, errors = xl.read_journal_template(f.stream)
    if not groups:
        errors.insert(0, "لم يتم العثور على أي بيانات في الملف")
        return jsonify(ok=False, added=0, drafts=0, new_accounts=0, skipped=0, errors=errors[:10])
    added, drafts_count, new_accounts, skipped = 0, 0, 0, len(errors)
    for g_ in groups:
        lines = []
        for ln in g_["lines"]:
            acc = db.query_one("SELECT id FROM accounts WHERE acc_no=?", (ln["acc_no"],))
            if not acc:
                name = ln["name"] or f"حساب مستورد {ln['acc_no']}"
                acc_id = db.execute(
                    "INSERT INTO accounts (acc_no, name, type) VALUES (?,?,'')",
                    (ln["acc_no"], name),
                )
                new_accounts += 1
            else:
                acc_id = acc["id"]
            lines.append({"account_id": acc_id, "debit": ln["debit"], "credit": ln["credit"]})
        td = round(sum(l["debit"] for l in lines), 2)
        tc = round(sum(l["credit"] for l in lines), 2)
        balanced = abs(td - tc) <= 0.004
        status = "posted" if (balanced and post_flag) else "draft"
        if not balanced:
            drafts_count += 1
            if g_["movement"].startswith("#تلقائي"):
                errors.append(f"قيد بتاريخ {g_['date']} غير متوازن (مدين {td:,.2f} / دائن {tc:,.2f}) وأُضيف كمسودة")
            else:
                errors.append(f"رقم الحركة {g_['movement']} غير متوازن (مدين {td:,.2f} / دائن {tc:,.2f}) وأُضيف كمسودة")
        # ضمان عدم تكرار رقم الحركة
        mv = g_["movement"]
        seq = 2
        while db.query_one("SELECT id FROM journal_entries WHERE movement_no=?", (mv,)):
            mv = f"{g_['movement']}-{seq}"
            seq += 1
        entry_region = (g_.get("region") or "").strip()
        if entry_region and entry_region not in db.load_regions():
            errors.append(f"رقم الحركة {mv}: منطقة «{entry_region}» غير معروفة — استُخدمت {region}")
            entry_region = ""
        if not entry_region:
            entry_region = region
        dt = g_["date"] or date.today()
        eid = db.execute(
            """INSERT INTO journal_entries (movement_no, entry_date, description, region, status, entry_type, created_by)
               VALUES (?,?,?,?,?,?,?)""",
            (mv, dt.isoformat(), g_["desc"], entry_region, status, "عادي", g.user["id"]),
        )
        for ln in lines:
            db.execute(
                "INSERT INTO journal_lines (entry_id, account_id, debit, credit) VALUES (?,?,?,?)",
                (eid, ln["account_id"], ln["debit"], ln["credit"]),
            )
        added += 1
    db.audit(g.user["full_name"], "استيراد دفتر يومية",
             f"قيود: {added} | حسابات جديدة: {new_accounts}")
    return jsonify(ok=True, added=added, drafts=drafts_count, new_accounts=new_accounts,
                   skipped=skipped, errors=errors[:10])


# ------------------------------------------------------------------
# APIs — الأستاذ العام
# ------------------------------------------------------------------

def ledger_query(acc_no, f, t, region):
    acc = db.query_one("SELECT * FROM accounts WHERE acc_no=?", (acc_no,))
    if not acc:
        return None, "رقم الحساب غير موجود"
    opening_net = acc["opening_balance"]
    sql_open = """SELECT COALESCE(SUM(jl.debit - jl.credit),0) s
                  FROM journal_lines jl JOIN journal_entries je ON je.id=jl.entry_id
                  WHERE jl.account_id=? AND je.status='posted'"""
    args_o = [acc["id"]]
    if region:
        sql_open += " AND je.region=?"
        args_o.append(region)
    if f:
        sql_open += " AND je.entry_date < ?"
        args_o.append(f)
    opening_net += (db.query_one(sql_open, args_o)["s"] or 0)

    where_p = ["jl.account_id = ?", "je.status = 'posted'"]
    args_p = [acc["id"]]
    if region:
        where_p.append("je.region = ?")
        args_p.append(region)
    if f:
        where_p.append("je.entry_date >= ?")
        args_p.append(f)
    if t:
        where_p.append("je.entry_date <= ?")
        args_p.append(t)
    rows = db.query(
        f"""SELECT je.entry_date AS date, je.movement_no, je.description, je.region,
                   jl.debit, jl.credit
            FROM journal_lines jl JOIN journal_entries je ON je.id = jl.entry_id
            WHERE {' AND '.join(where_p)}
            ORDER BY je.entry_date, je.id, jl.id""",
        args_p,
    )
    balance = round(opening_net, 2)
    td = tc = 0.0
    out = []
    for rw in rows:
        rw["debit"] = round(rw["debit"], 2)
        rw["credit"] = round(rw["credit"], 2)
        td += rw["debit"]
        tc += rw["credit"]
        balance = round(balance + rw["debit"] - rw["credit"], 2)
        rw["balance"] = balance
        out.append(rw)
    totals = {"debit": round(td, 2), "credit": round(tc, 2), "balance": round(balance, 2)}
    return {"account": acc, "opening": round(opening_net, 2), "rows": out, "totals": totals}, None


@app.route("/api/ledger")
def api_ledger():
    acc_no = (request.args.get("acc_no") or "").strip()
    f = (request.args.get("from") or "").strip()
    t = (request.args.get("to") or "").strip()
    region = (request.args.get("region") or "").strip()
    if not acc_no:
        return jsonify(error="اختر رقم الحساب"), 400
    result, err = ledger_query(acc_no, f, t, region)
    if err:
        return jsonify(error=err), 404
    return jsonify(result)


# ------------------------------------------------------------------
# APIs — ميزان المراجعة
# ------------------------------------------------------------------

def trial_query(f, t, region):
    region_sql = "AND je.region=?" if region else ""
    base_args = [region] if region else []

    openings = db.query(
        f"""SELECT a.id, a.acc_no, a.name, a.opening_balance ob,
                   COALESCE(SUM(jl.debit - jl.credit),0) pre
            FROM accounts a
            LEFT JOIN journal_lines jl ON jl.account_id=a.id
            LEFT JOIN journal_entries je ON je.id=jl.entry_id
                 AND je.status='posted' {('AND je.region=?' if region else '')}
                 {('AND je.entry_date<?' if f else '')}
            GROUP BY a.id""",
        base_args + ([f] if f else []),
    )

    period = {
        r["account_id"]: r
        for r in db.query(
            f"""SELECT jl.account_id, SUM(jl.debit) d, SUM(jl.credit) c
                FROM journal_lines jl JOIN journal_entries je ON je.id=jl.entry_id
                WHERE je.status='posted'
                  {('AND je.entry_date>=?' if f else '')}
                  {('AND je.entry_date<=?' if t else '')}
                  {region_sql}
                GROUP BY jl.account_id""",
            ([f] if f else []) + ([t] if t else []) + base_args,
        )
    }

    rows, tot = [], {"o_debit": 0.0, "o_credit": 0.0, "p_debit": 0.0,
                     "p_credit": 0.0, "f_debit": 0.0, "f_credit": 0.0}
    for o in openings:
        pd = round((period.get(o["id"], {}).get("d") or 0), 2)
        pc = round((period.get(o["id"], {}).get("c") or 0), 2)
        net_open = round((o["ob"] or 0) + (o["pre"] or 0), 2)
        od = net_open if net_open > 0 else 0.0
        oc = -net_open if net_open < 0 else 0.0
        net_final = round(net_open + pd - pc, 2)
        fd = net_final if net_final > 0 else 0.0
        fc = -net_final if net_final < 0 else 0.0
        if od == 0 and oc == 0 and pd == 0 and pc == 0:
            continue
        rows.append([o["acc_no"], o["name"], od, oc, pd, pc, fd, fc])
        tot["o_debit"] += od
        tot["o_credit"] += oc
        tot["p_debit"] += pd
        tot["p_credit"] += pc
        tot["f_debit"] += fd
        tot["f_credit"] += fc

    rows.sort(key=lambda r: (len(r[0]), r[0]))
    for k in tot:
        tot[k] = round(tot[k], 2)
    balanced = abs(tot["p_debit"] - tot["p_credit"]) <= 0.01
    return {"rows": rows, "totals": tot, "balanced": balanced}


@app.route("/api/trial-balance")
def api_trial():
    f = (request.args.get("from") or "").strip()
    t = (request.args.get("to") or "").strip()
    region = (request.args.get("region") or "").strip()
    return jsonify(trial_query(f, t, region))


# ------------------------------------------------------------------
# الموازنة التقديرية
# ------------------------------------------------------------------

@app.route("/api/budget")
def api_budget():
    f = (request.args.get("from") or "").strip()
    t = (request.args.get("to") or "").strip()
    region = (request.args.get("region") or "").strip()
    acc_type = (request.args.get("type") or "").strip()

    accounts = db.query(
        "SELECT id, acc_no, name, type, budget FROM accounts ORDER BY CAST(acc_no AS INTEGER), acc_no"
    )

    where_parts = ["je.status='posted'"]
    params: list = []
    if f:
        where_parts.append("je.entry_date >= ?")
        params.append(f)
    if t:
        where_parts.append("je.entry_date <= ?")
        params.append(t)
    if region:
        where_parts.append("je.region = ?")
        params.append(region)

    where = " AND ".join(where_parts)

    actuals = {}
    if accounts:
        placeholders = ",".join("?" * len(accounts))
        rows = db.query(
            f"""SELECT a.id,
                       COALESCE(SUM(CASE WHEN jl.debit > 0 THEN jl.debit ELSE 0 END), 0) actual_debit,
                       COALESCE(SUM(CASE WHEN jl.credit > 0 THEN jl.credit ELSE 0 END), 0) actual_credit
                FROM accounts a
                LEFT JOIN journal_lines jl ON jl.account_id = a.id
                LEFT JOIN journal_entries je ON je.id = jl.entry_id AND {where}
                WHERE a.id IN ({placeholders})
                GROUP BY a.id""",
            params + [a["id"] for a in accounts],
        )
        actuals = {r["id"]: {"debit": round(r["actual_debit"], 2), "credit": round(r["actual_credit"], 2)} for r in rows}

    results = []
    total_budget = 0
    total_actual = 0
    for a in accounts:
        if acc_type and a["type"] != acc_type:
            continue
        budget = a["budget"]
        act = actuals.get(a["id"], {"debit": 0, "credit": 0})
        if a["type"] in ("مصروفات",):
            actual = act["debit"]
        elif a["type"] in ("إيرادات",):
            actual = act["credit"]
        else:
            actual = act["debit"] - act["credit"]
        actual = round(actual, 2)
        variance = round(budget - actual, 2)
        pct = round((actual / budget * 100) if budget else 0, 1)
        if budget == 0:
            status = "بدون ميزانية"
        elif pct <= 70:
            status = "أقل من المتوقع"
        elif pct <= 90:
            status = "ضمن الخطة"
        elif pct <= 100:
            status = "قريب من الحد"
        elif pct <= 110:
            status = "يزيد قليلاً"
        else:
            status = "تجاوز الحد"
        total_budget += budget
        total_actual += actual
        results.append({
            "id": a["id"], "acc_no": a["acc_no"], "name": a["name"],
            "type": a["type"], "budget": budget, "actual": actual,
            "variance": variance, "pct": pct, "status": status,
        })

    total_var = round(total_budget - total_actual, 2)
    total_pct = round((total_actual / total_budget * 100) if total_budget else 0, 1)
    return jsonify(
        entries=results,
        summary={"budget": total_budget, "actual": total_actual,
                 "variance": total_var, "pct": total_pct},
    )


# ------------------------------------------------------------------
# التقارير المالية: قائمة الدخل / المركز المالي / التدفقات النقدية
# ------------------------------------------------------------------

def _posted_filter_parts(f, t, region):
    parts = ["je.status='posted'"]
    params: list = []
    if region:
        parts.append("je.region=?")
        params.append(region)
    if f:
        parts.append("je.entry_date>=?")
        params.append(f)
    if t:
        parts.append("je.entry_date<=?")
        params.append(t)
    return parts, params


def income_query(f, t, region):
    where, params = _posted_filter_parts(f, t, region)
    rows = db.query(
        f"""SELECT a.id, a.acc_no, a.name, a.type,
                   COALESCE(SUM(jl.debit), 0) d, COALESCE(SUM(jl.credit), 0) c
            FROM accounts a
            LEFT JOIN journal_lines jl ON jl.account_id = a.id
            LEFT JOIN journal_entries je ON je.id = jl.entry_id AND {' AND '.join(where)}
            WHERE a.type IN ('إيرادات', 'مصروفات')
            GROUP BY a.id
            ORDER BY CAST(a.acc_no AS INTEGER), a.acc_no""",
        params,
    )
    revenues, expenses = [], []
    tot_r = tot_e = 0.0
    for r in rows:
        if r["type"] == "إيرادات":
            amt = round(r["c"] - r["d"], 2)
            revenues.append({"acc_no": r["acc_no"], "name": r["name"], "amount": amt})
            tot_r += amt
        else:
            amt = round(r["d"] - r["c"], 2)
            expenses.append({"acc_no": r["acc_no"], "name": r["name"], "amount": amt})
            tot_e += amt
    tot_r, tot_e = round(tot_r, 2), round(tot_e, 2)
    return {"revenues": revenues, "revenues_total": tot_r,
            "expenses": expenses, "expenses_total": tot_e,
            "net": round(tot_r - tot_e, 2)}


def _balances_asof(t, region):
    """الرصيد النهائي لكل الحسابات حتى تاريخ t (+ المنطقة)"""
    where, params = [], []
    if region:
        where.append("je.region=?")
        params.append(region)
    if t:
        where.append("je.entry_date<=?")
        params.append(t)
    cond = (" AND " + " AND ".join(where)) if where else ""
    rows = db.query(
        f"""SELECT a.id, a.acc_no, a.name, a.type, a.opening_balance ob,
                   COALESCE(SUM(jl.debit - jl.credit), 0) mv
            FROM accounts a
            LEFT JOIN journal_lines jl ON jl.account_id = a.id
            LEFT JOIN journal_entries je ON je.id = jl.entry_id
                 AND je.status='posted'{cond}
            GROUP BY a.id
            ORDER BY CAST(a.acc_no AS INTEGER), a.acc_no""",
        params,
    )
    return {r["acc_no"]: {"id": r["id"], "acc_no": r["acc_no"], "name": r["name"],
                          "type": r["type"], "amount": round((r["ob"] or 0) + (r["mv"] or 0), 2)}
            for r in rows}


def balance_query(f, t, region):
    bal = _balances_asof(t, region)
    assets, liab_eq = [], []
    tot_a = tot_l = 0.0
    for a in bal.values():
        if a["type"] == "أصول":
            assets.append(a)
            tot_a += a["amount"]
        elif a["type"] in ("خصوم", "حقوق ملكية"):
            liab_eq.append(a)
            tot_l += a["amount"]
    net_cum = 0.0
    for a in bal.values():
        if a["type"] == "إيرادات":
            net_cum += a["amount"]
        elif a["type"] == "مصروفات":
            net_cum -= a["amount"]
    net_cum = round(net_cum, 2)
    net_period = income_query(f, t, region)["net"]
    total_with_income = round(tot_l + net_cum, 2)
    diff = round(tot_a - total_with_income, 2)
    return {"assets": assets, "assets_total": round(tot_a, 2),
            "liabilities_equity": liab_eq, "liab_equity_total": round(tot_l, 2),
            "net_cumulative": net_cum, "net_period": net_period,
            "total_with_income": total_with_income, "difference": diff,
            "balanced": abs(diff) <= 0.02}


def cashflow_query(f, t, region):
    cash = db.query(
        """SELECT id, acc_no, name FROM accounts
           WHERE type='أصول'
             AND (name LIKE '%صندوق%' OR name LIKE '%بنك%' OR name LIKE '%نقد%')
           ORDER BY CAST(acc_no AS INTEGER), acc_no"""
    )
    rows, tot = [], {"opening": 0.0, "inflow": 0.0, "outflow": 0.0, "closing": 0.0}
    for a in cash:
        ob = db.query_one("SELECT opening_balance FROM accounts WHERE id=?", (a["id"],))["opening_balance"] or 0
        opening = ob
        if region or f:
            cond, params = ["jl.account_id=?", "je.status='posted'"], [a["id"]]
            if f:
                cond.append("je.entry_date<?")
                params.append(f)
            if region:
                cond.append("je.region=?")
                params.append(region)
            opening += (db.query_one(
                f"SELECT COALESCE(SUM(jl.debit - jl.credit), 0) s FROM journal_lines jl "
                f"JOIN journal_entries je ON je.id = jl.entry_id WHERE {' AND '.join(cond)}",
                params)["s"] or 0)
        inflow = outflow = 0.0
        cond, params = ["jl.account_id=?", "je.status='posted'"], [a["id"]]
        if f:
            cond.append("je.entry_date>=?")
            params.append(f)
        if t:
            cond.append("je.entry_date<=?")
            params.append(t)
        if region:
            cond.append("je.region=?")
            params.append(region)
        s = db.query_one(
            f"""SELECT COALESCE(SUM(CASE WHEN jl.debit > 0 THEN jl.debit ELSE 0 END), 0) i,
                       COALESCE(SUM(CASE WHEN jl.credit > 0 THEN jl.credit ELSE 0 END), 0) o
                FROM journal_lines jl JOIN journal_entries je ON je.id = jl.entry_id
                WHERE {' AND '.join(cond)}""",
            params,
        )
        inflow, outflow = round(s["i"], 2), round(s["o"], 2)
        opening = round(opening, 2)
        closing = round(opening + inflow - outflow, 2)
        rows.append({"acc_no": a["acc_no"], "name": a["name"], "opening": opening,
                     "inflow": inflow, "outflow": outflow, "closing": closing})
        tot["opening"] += opening
        tot["inflow"] += inflow
        tot["outflow"] += outflow
        tot["closing"] += closing
    for k in tot:
        tot[k] = round(tot[k], 2)
    return {"rows": rows, "totals": tot, "net_flow": round(tot["inflow"] - tot["outflow"], 2)}


@app.route("/income-statement")
def income_statement_page():
    return render_template("income_statement.html")


@app.route("/balance-sheet")
def balance_sheet_page():
    return render_template("balance_sheet.html")


@app.route("/cash-flow")
def cash_flow_page():
    return render_template("cash_flow.html")


@app.route("/api/income-statement")
def api_income():
    f = (request.args.get("from") or "").strip()
    t = (request.args.get("to") or "").strip()
    region = (request.args.get("region") or "").strip()
    return jsonify(income_query(f, t, region))


@app.route("/api/balance-sheet")
def api_balance():
    f = (request.args.get("from") or "").strip()
    t = (request.args.get("to") or "").strip()
    region = (request.args.get("region") or "").strip()
    return jsonify(balance_query(f, t, region))


@app.route("/api/cash-flow")
def api_cashflow():
    f = (request.args.get("from") or "").strip()
    t = (request.args.get("to") or "").strip()
    region = (request.args.get("region") or "").strip()
    return jsonify(cashflow_query(f, t, region))


@app.route("/income-statement/export.xlsx")
def export_income_xlsx():
    f = (request.args.get("from") or "").strip()
    t = (request.args.get("to") or "").strip()
    region = (request.args.get("region") or "").strip()
    data = income_query(f, t, region)
    extra = f"المنطقة: {region}" if region else ""
    buf = xl.export_income(data, db.get_setting("company_name"), extra,
                           acc_user=g.user["full_name"], signatures=_signatures())
    return send_file(buf, as_attachment=True,
                     download_name="قائمة الدخل.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/balance-sheet/export.xlsx")
def export_balance_xlsx():
    f = (request.args.get("from") or "").strip()
    t = (request.args.get("to") or "").strip()
    region = (request.args.get("region") or "").strip()
    data = balance_query(f, t, region)
    extra = f"المنطقة: {region}" if region else ""
    buf = xl.export_balance(data, db.get_setting("company_name"), extra,
                            acc_user=g.user["full_name"], signatures=_signatures())
    return send_file(buf, as_attachment=True,
                     download_name="المركز المالي.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/cash-flow/export.xlsx")
def export_cashflow_xlsx():
    f = (request.args.get("from") or "").strip()
    t = (request.args.get("to") or "").strip()
    region = (request.args.get("region") or "").strip()
    data = cashflow_query(f, t, region)
    extra = f"المنطقة: {region}" if region else ""
    buf = xl.export_cashflow(data, db.get_setting("company_name"), extra,
                             acc_user=g.user["full_name"], signatures=_signatures())
    return send_file(buf, as_attachment=True,
                     download_name="التدفقات النقدية.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ------------------------------------------------------------------
# شجرة الحسابات
# ------------------------------------------------------------------

TYPE_LABELS = ["أصول", "خصوم", "حقوق ملكية", "إيرادات", "مصروفات"]

SUB_GROUP_LABELS = {
    "أصول": {"11": "أصول متداولة", "12": "حسابات مدينة", "13": "مخزون",
              "14": "أصول ثابتة", "15": "أخرى"},
    "خصوم": {"21": "خصوم متداولة", "22": "خصوم طويلة الأجل", "25": "أخرى"},
    "حقوق ملكية": {"31": "رأس المال والأرباح", "39": "أخرى"},
    "إيرادات": {"41": "إيرادات التشغيل", "42": "إيرادات أخرى", "49": "أخرى"},
    "مصروفات": {"51": "رواتب وأجور", "52": "خدمات", "53": "مصاريف إدارية",
                 "54": "سفر ومهمات", "55": "صرفيات أخرى", "59": "أخرى"},
}


@app.route("/api/accounts/tree")
def api_accounts_tree():
    accounts = db.query(
        "SELECT id, acc_no, name, type, budget FROM accounts ORDER BY CAST(acc_no AS INTEGER), acc_no"
    )
    tree = []
    for t in TYPE_LABELS:
        type_accounts = [a for a in accounts if a["type"] == t]
        sort_key = type(t).__name__
        if not type_accounts:
            continue
        subgroups = {}
        for a in type_accounts:
            code = str(a["acc_no"])[:2]
            label = SUB_GROUP_LABELS.get(t, {}).get(code, "حسابات عامة")
            subgroups.setdefault((code, label), []).append(a)
        nodes = []
        for (code, label), items in subgroups.items():
            nodes.append({
                "code": code, "label": label, "is_group": True, "children": [
                    {"id": a["id"], "acc_no": a["acc_no"], "name": a["name"],
                     "type": a["type"], "budget": a["budget"], "is_group": False}
                    for a in items
                ]
            })
        tree.append({"type": t, "is_type": True, "children": nodes})
    return jsonify(tree=tree)


# ------------------------------------------------------------------
# لوحة التحكم
# ------------------------------------------------------------------

@app.route("/api/dashboard")
def api_dashboard():
    stats = {
        "accounts": db.query_one("SELECT COUNT(*) c FROM accounts")["c"],
        "entries": db.query_one("SELECT COUNT(*) c FROM journal_entries")["c"],
        "drafts": db.query_one("SELECT COUNT(*) c FROM journal_entries WHERE status='draft'")["c"],
    }
    sums = db.query_one(
        """SELECT COALESCE(SUM(jl.debit),0) d, COALESCE(SUM(jl.credit),0) c
           FROM journal_lines jl JOIN journal_entries je ON je.id=jl.entry_id
           WHERE je.status='posted'"""
    )
    stats["total_debit"] = round(sums["d"], 2)
    stats["total_credit"] = round(sums["c"], 2)
    recent_ids = db.query(
        "SELECT id FROM journal_entries ORDER BY id DESC LIMIT 8"
    )
    recent = [_entry_full(r["id"]) for r in recent_ids]
    return jsonify(stats=stats, recent=[e for e in recent if e])


# ------------------------------------------------------------------
# المستخدمون والإعدادات
# ------------------------------------------------------------------

@app.route("/api/users", methods=["POST"])
@admin_required
def api_users_create():
    d = request.json or {}
    username = _sanitize((d.get("username") or "").strip().lower())
    full_name = _sanitize((d.get("full_name") or "").strip())
    role = d.get("role") if d.get("role") in ROLES else "accountant"
    password = d.get("password") or ""
    if not username or not full_name or len(password) < 8:
        return jsonify(error="بيانات غير مكتملة (كلمة المرور 8 أحرف على الأقل)"), 400
    if not re.search(r'[A-Za-z]', password) or not re.search(r'\d', password):
        return jsonify(error="كلمة المرور يجب أن تحتوي على حروف وأرقام"), 400
    if db.query_one("SELECT id FROM users WHERE username=?", (username,)):
        return jsonify(error="اسم المستخدم موجود بالفعل"), 400
    db.execute(
        "INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
        (username, generate_password_hash(password), full_name, role),
    )
    db.audit(g.user["full_name"], "إضافة مستخدم", f"{username} ({ROLES[role]})")
    return jsonify(ok=True)


@app.route("/api/users/<int:uid>/password", methods=["POST"])
@admin_required
def api_users_password(uid):
    d = request.json or {}
    password = d.get("password") or ""
    if len(password) < 8:
        return jsonify(error="كلمة المرور 8 أحرف على الأقل"), 400
    if not re.search(r'[A-Za-z]', password) or not re.search(r'\d', password):
        return jsonify(error="كلمة المرور يجب أن تحتوي على حروف وأرقام"), 400
    u = db.query_one("SELECT * FROM users WHERE id=?", (uid,))
    if not u:
        return jsonify(error="المستخدم غير موجود"), 404
    db.execute("UPDATE users SET password_hash=? WHERE id=?",
               (generate_password_hash(password), uid))
    db.audit(g.user["full_name"], "تغيير كلمة مرور", u["username"])
    return jsonify(ok=True)


@app.route("/api/users/<int:uid>", methods=["DELETE"])
@admin_required
def api_users_delete(uid):
    if uid == g.user["id"]:
        return jsonify(error="لا يمكنك حذف نفسك"), 400
    admins = db.query("SELECT id FROM users WHERE role='admin'")
    u = db.query_one("SELECT * FROM users WHERE id=?", (uid,))
    if not u:
        return jsonify(error="المستخدم غير موجود"), 404
    if u["role"] == "admin" and len(admins) <= 1:
        return jsonify(error="لا يمكن حذف آخر مدير نظام"), 400
    db.execute("DELETE FROM users WHERE id=?", (uid,))
    db.audit(g.user["full_name"], "حذف مستخدم", u["username"])
    return jsonify(ok=True)


@app.route("/api/me/password", methods=["POST"])
def api_me_password():
    d = request.json or {}
    old = d.get("old") or ""
    new = d.get("new") or ""
    if not check_password_hash(g.user["password_hash"], old):
        return jsonify(error="كلمة المرور الحالية غير صحيحة"), 400
    if len(new) < 8:
        return jsonify(error="كلمة المرور الجديدة 8 أحرف على الأقل"), 400
    if new == old:
        return jsonify(error="كلمة المرور الجديدة مختلفة عن الحالية"), 400
    if not re.search(r'[A-Za-z]', new) or not re.search(r'\d', new):
        return jsonify(error="كلمة المرور يجب أن تحتوي على حروف وأرقام"), 400
    db.execute("UPDATE users SET password_hash=? WHERE id=?",
               (generate_password_hash(new), g.user["id"]))
    db.audit(g.user["full_name"], "تغيير كلمة مروره الشخصية")
    return jsonify(ok=True)


# ------------------------------------------------------------------
# APIs — إدارة المناطق
# ------------------------------------------------------------------

@app.route("/api/regions", methods=["GET"])
@admin_required
def api_regions_list():
    return jsonify(regions=db.get_all_regions())


@app.route("/api/regions", methods=["POST"])
@admin_required
def api_regions_add():
    d = request.json or {}
    rid, err = db.add_region(d.get("name", ""))
    if err:
        return jsonify(error=err), 400
    db.audit(g.user["full_name"], "إضافة منطقة", d.get("name", "").strip())
    return jsonify(ok=True, id=rid)


@app.route("/api/regions/<int:rid>", methods=["PUT"])
@admin_required
def api_regions_rename(rid):
    d = request.json or {}
    err = db.rename_region(rid, d.get("name", ""))
    if err:
        return jsonify(error=err), 400
    db.audit(g.user["full_name"], "تعديل منطقة", d.get("name", "").strip())
    return jsonify(ok=True)


@app.route("/api/regions/<int:rid>/deactivate", methods=["POST"])
@admin_required
def api_regions_deactivate(rid):
    err = db.deactivate_region(rid)
    if err:
        return jsonify(error=err), 400
    db.audit(g.user["full_name"], "تعطيل منطقة", str(rid))
    return jsonify(ok=True)


@app.route("/api/regions/<int:rid>/activate", methods=["POST"])
@admin_required
def api_regions_activate(rid):
    err = db.activate_region(rid)
    if err:
        return jsonify(error=err), 400
    db.audit(g.user["full_name"], "تفعيل منطقة", str(rid))
    return jsonify(ok=True)


@app.route("/api/regions/<int:rid>", methods=["DELETE"])
@admin_required
def api_regions_delete(rid):
    err = db.delete_region(rid)
    if err:
        return jsonify(error=err), 400
    db.audit(g.user["full_name"], "حذف منطقة", str(rid))
    return jsonify(ok=True)


@app.route("/api/settings", methods=["POST"])
@admin_required
def api_settings():
    d = request.json or {}
    company = (d.get("company_name") or "").strip() or "اسم الشركة"
    pf = (d.get("period_from") or "").strip()
    pt = (d.get("period_to") or "").strip()
    for v in (pf, pt):
        if v:
            try:
                datetime.strptime(v, "%Y-%m-%d")
            except ValueError:
                return jsonify(error="صيغة تاريخ الفترة غير صحيحة"), 400
    db.set_setting("company_name", company)
    if pf:
        db.set_setting("period_from", pf)
    if pt:
        db.set_setting("period_to", pt)
    sigs = d.get("signatures")
    if isinstance(sigs, list):
        clean = []
        for s in sigs:
            if not isinstance(s, dict):
                continue
            title = (s.get("title") or "").strip()
            name = (s.get("name") or "").strip()
            if title and name:
                clean.append({"title": title, "name": name})
        db.set_setting("signatures", json.dumps(clean, ensure_ascii=False))
    db.audit(g.user["full_name"], "تعديل الإعدادات", company)
    return jsonify(ok=True)


# ------------------------------------------------------------------
# النسخ الاحتياطي
# ------------------------------------------------------------------

@app.route("/api/backup", methods=["POST"])
@admin_required
def api_backup_create():
    try:
        name = db.create_backup(prefix="manual")
    except Exception as e:
        return jsonify(error=f"فشل إنشاء النسخة: {e}"), 500
    db.audit(g.user["full_name"], "نسخة احتياطية يدوية", name)
    return jsonify(ok=True, name=name)


@app.route("/api/backups")
@admin_required
def api_backups_list():
    return jsonify(backups=db.list_backups())


@app.route("/api/backups/<name>")
@admin_required
def api_backup_download(name):
    import re as _re
    if not db.SAFE_BACKUP_NAME.match(name):
        abort(400)
    path = db.BACKUP_DIR / name
    if not path.exists():
        abort(404)
    return send_file(path, as_attachment=True, download_name=name)


@app.route("/api/restore/<name>", methods=["POST"])
@admin_required
def api_backup_restore(name):
    try:
        db.restore_backup(name)
    except (ValueError, FileNotFoundError) as e:
        return jsonify(error=f"فشل الاستعادة: {e}"), 400
    except Exception as e:
        return jsonify(error=f"فشل الاستعادة: {e}"), 500
    db.audit(g.user["full_name"], "استعادة نسخة احتياطية", name)
    return jsonify(ok=True, message="تمت الاستعادة بنجاح — أعد تسجيل الدخول")


# ------------------------------------------------------------------
# التصدير والقوالب
# ------------------------------------------------------------------

def _subtitle(extra_parts):
    pf, pt = db.get_setting("period_from"), db.get_setting("period_to")
    parts = [db.get_setting("company_name")]
    if pf or pt:
        parts.append(f"الفترة من {xl.fmt_date(pf)} إلى {xl.fmt_date(pt)}")
    for p in extra_parts:
        if p:
            parts.append(p)
    return parts


@app.route("/journal/export.xlsx")
def export_journal_xlsx():
    where, args_ = journal_filters(request.args)
    ids = db.query(
        f"""SELECT je.id FROM journal_entries je {where}
            ORDER BY je.entry_date, CAST(je.movement_no AS INTEGER)""",
        args_,
    )
    entries = [e for e in (_entry_full(r["id"]) for r in ids) if e]
    region = request.args.get("region", "")
    buf = xl.export_journal(entries, db.get_setting("company_name"),
                            f"المنطقة: {region}" if region else "",
                            acc_user=g.user["full_name"], signatures=_signatures())
    return send_file(buf, as_attachment=True,
                     download_name="دفتر اليومية.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/ledger/export.xlsx")
def export_ledger_xlsx():
    acc_no = (request.args.get("acc_no") or "").strip()
    f = (request.args.get("from") or "").strip()
    t = (request.args.get("to") or "").strip()
    region = (request.args.get("region") or "").strip()
    result, err = ledger_query(acc_no, f, t, region)
    if err:
        abort(404)
    extra = f"المنطقة: {region}" if region else ""
    buf = xl.export_ledger(result["account"], result["opening"], result["rows"],
                           result["totals"], db.get_setting("company_name"), extra,
                           acc_user=g.user["full_name"], signatures=_signatures())
    return send_file(buf, as_attachment=True,
                     download_name=f"الأستاذ العام - {result['account']['name']}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/trial-balance/export.xlsx")
def export_trial_xlsx():
    f = (request.args.get("from") or "").strip()
    t = (request.args.get("to") or "").strip()
    region = (request.args.get("region") or "").strip()
    data = trial_query(f, t, region)
    extra = f"المنطقة: {region}" if region else ""
    buf = xl.export_trial(data["rows"], data["totals"],
                          db.get_setting("company_name"), extra,
                          acc_user=g.user["full_name"], signatures=_signatures())
    return send_file(buf, as_attachment=True,
                     download_name="ميزان المراجعة.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/budget/export.xlsx")
def export_budget_xlsx():
    f = (request.args.get("from") or "").strip()
    t = (request.args.get("to") or "").strip()
    region = (request.args.get("region") or "").strip()
    acc_type = (request.args.get("type") or "").strip()

    accounts = db.query(
        "SELECT id, acc_no, name, type, budget FROM accounts ORDER BY CAST(acc_no AS INTEGER), acc_no"
    )
    where_parts = ["je.status='posted'"]
    params: list = []
    if f:
        where_parts.append("je.entry_date >= ?")
        params.append(f)
    if t:
        where_parts.append("je.entry_date <= ?")
        params.append(t)
    if region:
        where_parts.append("je.region = ?")
        params.append(region)
    where = " AND ".join(where_parts)

    actuals = {}
    if accounts:
        placeholders = ",".join("?" * len(accounts))
        rows = db.query(
            f"""SELECT a.id,
                       COALESCE(SUM(CASE WHEN jl.debit > 0 THEN jl.debit ELSE 0 END), 0) actual_debit,
                       COALESCE(SUM(CASE WHEN jl.credit > 0 THEN jl.credit ELSE 0 END), 0) actual_credit
                FROM accounts a
                LEFT JOIN journal_lines jl ON jl.account_id = a.id
                LEFT JOIN journal_entries je ON je.id = jl.entry_id AND {where}
                WHERE a.id IN ({placeholders})
                GROUP BY a.id""",
            params + [a["id"] for a in accounts],
        )
        actuals = {r["id"]: {"debit": round(r["actual_debit"], 2), "credit": round(r["actual_credit"], 2)} for r in rows}

    entries = []
    total_budget = 0
    total_actual = 0
    for a in accounts:
        if acc_type and a["type"] != acc_type:
            continue
        budget = a["budget"]
        act = actuals.get(a["id"], {"debit": 0, "credit": 0})
        if a["type"] in ("مصروفات",):
            actual = act["debit"]
        elif a["type"] in ("إيرادات",):
            actual = act["credit"]
        else:
            actual = act["debit"] - act["credit"]
        actual = round(actual, 2)
        variance = round(budget - actual, 2)
        pct = round((actual / budget * 100) if budget else 0, 1)
        if budget == 0:
            status = "بدون ميزانية"
        elif pct <= 70:
            status = "أقل من المتوقع"
        elif pct <= 90:
            status = "ضمن الخطة"
        elif pct <= 100:
            status = "قريب من الحد"
        elif pct <= 110:
            status = "يزيد قليلاً"
        else:
            status = "تجاوز الحد"
        total_budget += budget
        total_actual += actual
        entries.append({"acc_no": a["acc_no"], "name": a["name"], "type": a["type"],
                         "budget": budget, "actual": actual, "variance": variance,
                         "pct": pct, "status": status})

    total_var = round(total_budget - total_actual, 2)
    total_pct = round((total_actual / total_budget * 100) if total_budget else 0, 1)
    summary = {"budget": total_budget, "actual": total_actual, "variance": total_var, "pct": total_pct}
    extra = f"المنطقة: {region}" if region else ""
    buf = xl.export_budget(entries, summary, db.get_setting("company_name"), extra,
                           acc_user=g.user["full_name"], signatures=_signatures())
    return send_file(buf, as_attachment=True,
                     download_name="الموازنة التقديرية.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/accounts/export.xlsx")
def export_accounts_xlsx():
    accounts = db.query(
        "SELECT * FROM accounts ORDER BY CAST(acc_no AS INTEGER), acc_no"
    )
    buf = xl.export_accounts(accounts, db.get_setting("company_name"),
                             acc_user=g.user["full_name"], signatures=_signatures())
    return send_file(buf, as_attachment=True,
                     download_name="دليل حسابات.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/templates/accounts.xlsx")
def template_accounts():
    return send_file(xl.blank_template_accounts(), as_attachment=True,
                     download_name="دليل حسابات.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/templates/journal.xlsx")
def template_journal():
    return send_file(xl.blank_template_journal(), as_attachment=True,
                     download_name="دفتر اليومية.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ------------------------------------------------------------------
if __name__ == "__main__":
    import logging
    from logging.handlers import RotatingFileHandler

    db.init_db()
    db.auto_monthly_backup()

    # تسجيل الأحداث في ملف بدل الشاشة (للتشغيل بدون نافذة CMD)
    INSTANCE_DIR = db.INSTANCE_DIR
    _handler = RotatingFileHandler(
        INSTANCE_DIR / "server.log", maxBytes=1_000_000,
        backupCount=2, encoding="utf-8",
    )
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger("werkzeug").addHandler(_handler)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    import socket
    try:
        ip = socket.gethostbyname(socket.gethostname())
    except OSError:
        ip = "127.0.0.1"

    import os as _os
    PORT = int(_os.environ.get("PORT", "5000"))

    # في النسخة المجمّعة (EXE) نفتح المتصفح تلقائيًا بعد إقلاع السيرفر
    if getattr(sys, "frozen", False):
        import threading
        import webbrowser
        threading.Timer(2.0, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()

    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
