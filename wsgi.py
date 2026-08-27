# -*- coding: utf-8 -*-
"""ملف تشغيل للنشر على استضافة سحابية (PythonAnywhere / gunicorn)

يعمل مع متغير SMART_BASE للمجلد الذي يحتوي app.py
على PythonAnywhere يكون افتراضيا /home/<username>/SmartAccounting
"""
import os
import sys

BASE = os.environ.get(
    "SMART_BASE",
    os.path.join(os.getenv("HOME", ""), "SmartAccounting"),
)
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import database as db

db.init_db()

from app import app as application  # noqa: E402

if __name__ == "__main__":
    application.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False)