"""WSGI 入口：启动前初始化数据库，供 gunicorn / docker 使用。"""

import os

from payroll_app.app import DEFAULT_DB, create_app, init_db

DB_PATH = os.environ.get("PAYROLL_DB", DEFAULT_DB)

init_db(DB_PATH)
app = create_app(DB_PATH)
