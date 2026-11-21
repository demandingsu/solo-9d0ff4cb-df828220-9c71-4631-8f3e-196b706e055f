"""薪资成本中心勘误入账系统（Flask + SQLite）。

规则摘要（详见 README.md）：
- 原始薪资单只能导入开放入账月；薪资所属月即该入账月。
- 每个月的明细一经封账不可再改；封账后只开放新月份，不回退重开。
- 勘误只能提交到开放入账月：
  * 原单所属月未封账 → 直接改原行的成本中心与金额（仍在原所属月）；
  * 原单所属月已封账 → 在开放入账月冲回上次生效值，再按更正值入账，
    两条流水都带原单号与薪资所属月；已封月明细保持原样。
- 勘误号幂等：同号同内容只处理一次，同号不同内容整批拒绝。
- 所有写入使用单事务；失败整体回滚，原状态保持不变。
"""

import csv
import io
import os
import re
import sqlite3
from datetime import datetime, timezone

from flask import (
    Flask,
    Response,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(BASE_DIR, "data", "payroll.db")

KIND_ORIGINAL = "original"
KIND_REVERSE = "reverse"
KIND_REBOOK = "rebook"
KIND_LABELS = {
    KIND_ORIGINAL: "原始",
    KIND_REVERSE: "冲回",
    KIND_REBOOK: "重入账",
}

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
CENTER_RE = re.compile(r"^[A-Za-z0-9_.\-/]+$")
INT_RE = re.compile(r"^-?\d+$")

ORIGINAL_FIELDS = ["薪资单号", "成本中心", "金额分"]
ADJUST_FIELDS = ["勘误号", "薪资单号", "成本中心", "金额分"]


class ValidationError(Exception):
    """输入校验失败，向用户展示。"""


class ConflictError(Exception):
    """业务状态冲突（封账、重复、幂等冲突等）。"""


# --------------------------------------------------------------------- DB

def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS months (
    month      TEXT PRIMARY KEY,                 -- YYYY-MM
    status     TEXT NOT NULL CHECK (status IN ('open', 'closed')),
    closed_at  TEXT
);

-- 每张薪资单的当前生效状态
CREATE TABLE IF NOT EXISTS slips (
    slip_no      TEXT PRIMARY KEY,
    slip_month   TEXT NOT NULL,                  -- 薪资所属月
    cost_center  TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    entry_month  TEXT NOT NULL,                  -- 当前生效金额所在入账月
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- 入账流水，按入账月出报表；金额带符号，冲回为负
CREATE TABLE IF NOT EXISTS postings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_month  TEXT NOT NULL,
    slip_no      TEXT NOT NULL,
    slip_month   TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('original', 'reverse', 'rebook')),
    cost_center  TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    adjust_no    TEXT,
    revision     INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_postings_entry ON postings(entry_month, id);
CREATE INDEX IF NOT EXISTS idx_postings_slip  ON postings(slip_no, id);

-- 已处理的勘误号（幂等 + 冲突检测）
CREATE TABLE IF NOT EXISTS adjustments (
    adjust_no    TEXT PRIMARY KEY,
    fingerprint  TEXT NOT NULL,
    slip_no      TEXT NOT NULL,
    slip_month   TEXT NOT NULL,
    cost_center  TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    mode         TEXT NOT NULL CHECK (mode IN ('inplace', 'reversal')),
    entry_month  TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

-- 薪资单生效值变更历史（审计用）
CREATE TABLE IF NOT EXISTS slip_revisions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    slip_no      TEXT NOT NULL,
    revision     INTEGER NOT NULL,
    adjust_no    TEXT NOT NULL,
    cost_center  TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    mode         TEXT NOT NULL,
    entry_month  TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

-- 数据库层防线：已封月明细不可插入/修改/删除
CREATE TRIGGER IF NOT EXISTS postings_no_insert_closed
BEFORE INSERT ON postings
WHEN NOT EXISTS (
    SELECT 1 FROM months WHERE month = NEW.entry_month AND status = 'open'
)
BEGIN
    SELECT RAISE(ABORT, 'entry month is not open');
END;

CREATE TRIGGER IF NOT EXISTS postings_no_update_closed
BEFORE UPDATE ON postings
WHEN EXISTS (
    SELECT 1 FROM months WHERE month = OLD.entry_month AND status = 'closed'
) OR NOT EXISTS (
    SELECT 1 FROM months WHERE month = NEW.entry_month AND status = 'open'
)
BEGIN
    SELECT RAISE(ABORT, 'entry month is closed');
END;

CREATE TRIGGER IF NOT EXISTS postings_no_delete_closed
BEFORE DELETE ON postings
WHEN EXISTS (
    SELECT 1 FROM months WHERE month = OLD.entry_month AND status = 'closed'
)
BEGIN
    SELECT RAISE(ABORT, 'entry month is closed');
END;

-- 封账不可逆：不允许把 closed 改回 open
CREATE TRIGGER IF NOT EXISTS months_no_reopen
BEFORE UPDATE OF status ON months
WHEN OLD.status = 'closed' AND NEW.status = 'open'
BEGIN
    SELECT RAISE(ABORT, 'closed month cannot be reopened');
END;
"""


def connect(path):
    conn = sqlite3.connect(path, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        # 首次启动：默认把当前月设为开放月，方便直接开始导入。
        if conn.execute("SELECT COUNT(*) FROM months").fetchone()[0] == 0:
            conn.execute(
                "INSERT INTO months(month, status, closed_at) VALUES (?, 'open', NULL)",
                (_current_month(),),
            )
    finally:
        conn.close()

def _current_month():
    today = datetime.now(timezone.utc).date()
    return f"{today.year:04d}-{today.month:02d}"


def get_db():
    if "db" not in g:
        g.db = connect(g.get("db_path", DEFAULT_DB))
    return g.db


# ------------------------------------------------------------------ 规范化

def _norm_month(value, field="月份"):
    value = (value or "").strip()
    if not MONTH_RE.match(value):
        raise ValidationError(f"{field}格式应为 YYYY-MM：{value!r}")
    return value


def _norm_center(value):
    value = (value or "").strip()
    if not value:
        raise ValidationError("成本中心不能为空")
    if not CENTER_RE.match(value):
        raise ValidationError(f"成本中心仅允许字母、数字及 _ . - /：{value!r}")
    return value


def _norm_cents(value):
    value = (value or "").strip()
    if value == "":
        raise ValidationError("金额（分）不能为空")
    if not INT_RE.match(value):
        raise ValidationError(f"金额（分）必须为整数：{value!r}")
    return int(value)


def _norm_id(value, field):
    value = (value or "").strip()
    if not value:
        raise ValidationError(f"{field}不能为空")
    if len(value) > 64:
        raise ValidationError(f"{field}过长（最多 64 字符）")
    if any(ch in value for ch in "\t\r\n,，\u0000"):
        raise ValidationError(f"{field}含非法字符：{value!r}")
    return value


# ------------------------------------------------------------------ 月份

def get_month(conn, month):
    return conn.execute("SELECT * FROM months WHERE month=?", (month,)).fetchone()


def ensure_month_open(conn, month):
    row = get_month(conn, month)
    if row is None:
        raise ConflictError(f"月份 {month} 不存在，请先在“月份管理”中开放")
    if row["status"] != "open":
        raise ConflictError(f"入账月 {month} 已封账，不能写入")


def open_month(conn, month):
    """新建一个开放月份。已存在且开放则无操作；已封账不可重开。"""
    month = _norm_month(month, "要开放的月份")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = get_month(conn, month)
        if row is None:
            conn.execute(
                "INSERT INTO months(month, status, closed_at) VALUES (?, 'open', NULL)",
                (month,),
            )
            state = "created"
        elif row["status"] == "open":
            state = "already_open"
        else:
            raise ConflictError(
                f"月份 {month} 已封账，封账不可逆；请开放新的月份"
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return state


def close_month(conn, month):
    """封账：锁定该入账月明细。不改动任何明细行。"""
    month = _norm_month(month, "要封账的月份")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = get_month(conn, month)
        if row is None:
            raise ConflictError(f"月份 {month} 不存在")
        if row["status"] != "open":
            raise ConflictError(f"月份 {month} 已经是封账状态")
        conn.execute(
            "UPDATE months SET status='closed', closed_at=? WHERE month=?",
            (_now(), month),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return month


# ------------------------------------------------------------------ CSV 解析

def _read_header(reader, expected):
    try:
        header = next(reader)
    except StopIteration:
        raise ValidationError("CSV 内容为空")
    header = [h.strip().lstrip("﻿") for h in header]
    if header != expected:
        raise ValidationError(
            "表头必须为：%s（实际：%s）" % (",".join(expected), ",".join(header))
        )


def parse_original_csv(text):
    """表头：薪资单号,成本中心,金额分。整份文件先校验，任一错误则全部拒绝。"""
    if not text.strip():
        raise ValidationError("CSV 内容为空")
    reader = csv.reader(io.StringIO(text))
    _read_header(reader, ORIGINAL_FIELDS)
    rows, errors, seen = [], [], set()
    for lineno, raw in enumerate(reader, start=2):
        if not raw or all(c.strip() == "" for c in raw):
            continue
        if len(raw) != len(ORIGINAL_FIELDS):
            errors.append(f"第 {lineno} 行列数为 {len(raw)}，应为 {len(ORIGINAL_FIELDS)}")
            continue
        try:
            row = {
                "slip_no": _norm_id(raw[0], "薪资单号"),
                "cost_center": _norm_center(raw[1]),
                "amount_cents": _norm_cents(raw[2]),
                "line": lineno,
            }
        except ValidationError as exc:
            errors.append(f"第 {lineno} 行：{exc}")
            continue
        if row["slip_no"] in seen:
            errors.append(f"第 {lineno} 行：薪资单号 {row['slip_no']} 在文件内重复")
            continue
        seen.add(row["slip_no"])
        rows.append(row)
    if errors:
        raise ValidationError("；".join(errors))
    if not rows:
        raise ValidationError("CSV 中没有有效数据行")
    return rows


def parse_adjustment_csv(text):
    """表头：勘误号,薪资单号,成本中心,金额分。整份文件先校验。"""
    if not text.strip():
        raise ValidationError("CSV 内容为空")
    reader = csv.reader(io.StringIO(text))
    _read_header(reader, ADJUST_FIELDS)
    rows, errors, seen = [], [], set()
    for lineno, raw in enumerate(reader, start=2):
        if not raw or all(c.strip() == "" for c in raw):
            continue
        if len(raw) != len(ADJUST_FIELDS):
            errors.append(f"第 {lineno} 行列数为 {len(raw)}，应为 {len(ADJUST_FIELDS)}")
            continue
        try:
            row = {
                "adjust_no": _norm_id(raw[0], "勘误号"),
                "slip_no": _norm_id(raw[1], "薪资单号"),
                "cost_center": _norm_center(raw[2]),
                "amount_cents": _norm_cents(raw[3]),
                "line": lineno,
            }
        except ValidationError as exc:
            errors.append(f"第 {lineno} 行：{exc}")
            continue
        if row["adjust_no"] in seen:
            errors.append(f"第 {lineno} 行：勘误号 {row['adjust_no']} 在文件内重复")
            continue
        seen.add(row["adjust_no"])
        rows.append(row)
    if errors:
        raise ValidationError("；".join(errors))
    if not rows:
        raise ValidationError("CSV 中没有有效数据行")
    return rows


# ------------------------------------------------------------------ 原单导入

def ingest_originals(conn, entry_month, text):
    """把一批原始薪资单导入开放入账月，薪资所属月=入账月。单事务。"""
    entry_month = _norm_month(entry_month, "入账月")
    rows = parse_original_csv(text)

    conn.execute("BEGIN IMMEDIATE")
    try:
        ensure_month_open(conn, entry_month)
        conflicts = []
        for row in rows:
            if conn.execute(
                "SELECT 1 FROM slips WHERE slip_no=?", (row["slip_no"],)
            ).fetchone():
                conflicts.append(row["slip_no"])
        if conflicts:
            raise ConflictError(
                "薪资单已存在，请走勘误流程："
                + "、".join(conflicts[:10])
                + ("…" if len(conflicts) > 10 else "")
            )
        now = _now()
        for row in rows:
            conn.execute(
                "INSERT INTO postings(entry_month, slip_no, slip_month, kind,"
                " cost_center, amount_cents, adjust_no, revision, created_at)"
                " VALUES (?,?,?,'original',?,?,NULL,0,?)",
                (entry_month, row["slip_no"], entry_month,
                 row["cost_center"], row["amount_cents"], now),
            )
            conn.execute(
                "INSERT INTO slips(slip_no, slip_month, cost_center, amount_cents,"
                " entry_month, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (row["slip_no"], entry_month, row["cost_center"],
                 row["amount_cents"], entry_month, now, now),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return len(rows)


# ------------------------------------------------------------------ 勘误

def _fingerprint(row):
    return "\x1f".join([row["slip_no"], row["cost_center"], str(row["amount_cents"])])


def _next_revision(conn, slip_no):
    row = conn.execute(
        "SELECT COALESCE(MAX(revision), 0) AS r FROM slip_revisions WHERE slip_no=?",
        (slip_no,),
    ).fetchone()
    return row["r"] + 1


def apply_adjustment(conn, row, entry_month):
    """在已开始的事务内应用一条勘误。

    返回 (status, detail)，status ∈ applied / duplicate_skipped。
    """
    fp = _fingerprint(row)
    prior = conn.execute(
        "SELECT * FROM adjustments WHERE adjust_no=?", (row["adjust_no"],)
    ).fetchone()
    if prior is not None:
        if prior["fingerprint"] != fp:
            raise ConflictError(
                f"勘误号 {row['adjust_no']} 已用不同内容处理过，拒绝本次上传"
            )
        return "duplicate_skipped", {
            "adjust_no": row["adjust_no"],
            "mode": prior["mode"],
            "entry_month": prior["entry_month"],
            "slip_no": prior["slip_no"],
        }

    # 新勘误才需要开放入账月；重复重放无需写入，前面已处理。
    ensure_month_open(conn, entry_month)

    slip = conn.execute(
        "SELECT * FROM slips WHERE slip_no=?", (row["slip_no"],)
    ).fetchone()
    if slip is None:
        raise ValidationError(f"薪资单 {row['slip_no']} 不存在，无法勘误")

    slip_month = slip["slip_month"]
    month_row = get_month(conn, slip_month)
    slip_closed = month_row is not None and month_row["status"] == "closed"
    mode = "reversal" if slip_closed else "inplace"
    revision = _next_revision(conn, row["slip_no"])
    now = _now()

    if not slip_closed:
        if entry_month != slip_month:
            raise ConflictError(
                f"原单所属月 {slip_month} 尚未封账，勘误应提交到该月直接改原行；"
                f"当前选择的入账月为 {entry_month}"
            )
        # 原单未封账：直接改原行（只改成本中心与金额），仍留在原所属月。
        conn.execute(
            "UPDATE postings SET cost_center=?, amount_cents=?, adjust_no=?, revision=?"
            " WHERE kind='original' AND slip_no=?",
            (row["cost_center"], row["amount_cents"], row["adjust_no"], revision,
             row["slip_no"]),
        )
        effective_month = slip_month
    else:
        # 原单已封账：在开放入账月冲回上次生效值，再按更正值重入账。
        conn.execute(
            "INSERT INTO postings(entry_month, slip_no, slip_month, kind,"
            " cost_center, amount_cents, adjust_no, revision, created_at)"
            " VALUES (?,?,?,'reverse',?,?,?,?,?)",
            (entry_month, row["slip_no"], slip_month, slip["cost_center"],
             -slip["amount_cents"], row["adjust_no"], revision, now),
        )
        conn.execute(
            "INSERT INTO postings(entry_month, slip_no, slip_month, kind,"
            " cost_center, amount_cents, adjust_no, revision, created_at)"
            " VALUES (?,?,?,'rebook',?,?,?,?,?)",
            (entry_month, row["slip_no"], slip_month, row["cost_center"],
             row["amount_cents"], row["adjust_no"], revision, now),
        )
        effective_month = entry_month

    conn.execute(
        "UPDATE slips SET cost_center=?, amount_cents=?, entry_month=?, updated_at=?"
        " WHERE slip_no=?",
        (row["cost_center"], row["amount_cents"], effective_month, now,
         row["slip_no"]),
    )
    conn.execute(
        "INSERT INTO slip_revisions(slip_no, revision, adjust_no, cost_center,"
        " amount_cents, mode, entry_month, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (row["slip_no"], revision, row["adjust_no"], row["cost_center"],
         row["amount_cents"], mode, effective_month, now),
    )
    conn.execute(
        "INSERT INTO adjustments(adjust_no, fingerprint, slip_no, slip_month,"
        " cost_center, amount_cents, mode, entry_month, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (row["adjust_no"], fp, row["slip_no"], slip_month, row["cost_center"],
         row["amount_cents"], mode, effective_month, now),
    )
    return "applied", {
        "adjust_no": row["adjust_no"],
        "mode": mode,
        "entry_month": effective_month,
        "slip_no": row["slip_no"],
    }


def ingest_adjustments(conn, entry_month, text):
    """批量勘误：单事务，任一写入失败则整批回滚。"""
    entry_month = _norm_month(entry_month, "入账月")
    rows = parse_adjustment_csv(text)

    counts = {"applied": 0, "duplicate_skipped": 0}
    details = []
    conn.execute("BEGIN IMMEDIATE")
    try:
        for row in rows:
            status, detail = apply_adjustment(conn, row, entry_month)
            counts[status] += 1
            details.append({"line": row["line"], "status": status, **detail})
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return counts, details


# ------------------------------------------------------------------ 查询 / 导出

def month_summary(conn):
    out = []
    for m in conn.execute("SELECT * FROM months ORDER BY month").fetchall():
        totals = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(amount_cents), 0) AS total"
            " FROM postings WHERE entry_month=?", (m["month"],)
        ).fetchone()
        out.append({
            "month": m["month"],
            "status": m["status"],
            "closed_at": m["closed_at"],
            "postings": totals["n"],
            "total_cents": totals["total"],
        })
    return out


def month_rows(conn, month):
    return conn.execute(
        "SELECT * FROM postings WHERE entry_month=? ORDER BY id", (month,)
    ).fetchall()


def adjustment_history(conn, limit=100):
    return conn.execute(
        "SELECT * FROM adjustments ORDER BY created_at DESC, adjust_no LIMIT ?",
        (limit,),
    ).fetchall()


def slip_rows(conn, limit=100):
    return conn.execute(
        "SELECT * FROM slips ORDER BY slip_no LIMIT ?", (limit,)
    ).fetchall()


def export_csv(conn, month):
    month = _norm_month(month, "导出月份")
    if get_month(conn, month) is None:
        raise ValidationError(f"月份 {month} 不存在")
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["入账月", "薪资单号", "薪资所属月", "流水类型", "成本中心",
                     "金额分", "勘误号", "修订号"])
    for r in month_rows(conn, month):
        writer.writerow([
            r["entry_month"], r["slip_no"], r["slip_month"],
            KIND_LABELS.get(r["kind"], r["kind"]),
            r["cost_center"], r["amount_cents"], r["adjust_no"] or "",
            r["revision"],
        ])
    return buf.getvalue()


# ------------------------------------------------------------------ 应用

def create_app(db_path=None, testing=False):
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-change-me")
    app.config["TESTING"] = testing
    app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
    app.config["DB_PATH"] = db_path or os.environ.get("PAYROLL_DB", DEFAULT_DB)

    @app.before_request
    def _bind_db():
        g.db_path = app.config["DB_PATH"]

    @app.teardown_request
    def _close_db(exc):
        db = g.pop("db", None)
        if db is None:
            return
        if exc is not None and db.in_transaction:
            try:
                db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        db.close()

    @app.errorhandler(413)
    def _too_large(_exc):
        flash("文件过大（上限 5 MB）", "error")
        return redirect(url_for("index"))

    @app.get("/")
    def index():
        conn = get_db()
        summary = month_summary(conn)
        return render_template(
            "index.html",
            months=summary,
            open_months=[m["month"] for m in summary if m["status"] == "open"],
            adjustments=adjustment_history(conn),
            slips=slip_rows(conn),
        )

    @app.post("/months/open")
    def month_open():
        conn = get_db()
        month = request.form.get("month", "")
        try:
            state = open_month(conn, month)
            label = {"created": "已开放", "already_open": "本来就开放"}[state]
            flash(f"月份 {month.strip()} {label}", "ok")
        except (ValidationError, ConflictError, sqlite3.IntegrityError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("index"))

    @app.post("/months/close")
    def month_close():
        conn = get_db()
        month = request.form.get("month", "")
        try:
            close_month(conn, month)
            flash(f"月份 {month.strip()} 已封账，明细已锁定", "ok")
        except (ValidationError, ConflictError, sqlite3.IntegrityError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("index"))

    @app.post("/import/originals")
    def import_originals():
        conn = get_db()
        month = request.form.get("entry_month", "")
        upload = request.files.get("file")
        try:
            if upload is None or not upload.filename:
                raise ValidationError("请选择 CSV 文件")
            text = upload.read().decode("utf-8-sig")
            n = ingest_originals(conn, month, text)
            flash(f"已向入账月 {month.strip()} 导入 {n} 张原始薪资单", "ok")
        except (ValidationError, ConflictError, sqlite3.IntegrityError) as exc:
            flash(f"导入失败，未写入任何数据：{exc}", "error")
        except UnicodeDecodeError:
            flash("导入失败，未写入任何数据：文件必须为 UTF-8 编码", "error")
        return redirect(url_for("index"))

    @app.post("/import/adjustments")
    def import_adjustments():
        conn = get_db()
        month = request.form.get("entry_month", "")
        upload = request.files.get("file")
        try:
            if upload is None or not upload.filename:
                raise ValidationError("请选择 CSV 文件")
            text = upload.read().decode("utf-8-sig")
            counts, details = ingest_adjustments(conn, month, text)
            flash(
                f"勘误处理完成：生效 {counts['applied']} 条，"
                f"重复跳过 {counts['duplicate_skipped']} 条",
                "ok",
            )
            for d in details[:50]:
                action = "直接改原行" if d["mode"] == "inplace" else "冲回 + 重入账"
                flash(
                    f"行 {d['line']}：勘误 {d['adjust_no']} / 薪资单 {d['slip_no']} "
                    f"→ {action}（生效入账月 {d['entry_month']}）",
                    "detail",
                )
        except (ValidationError, ConflictError, sqlite3.IntegrityError) as exc:
            flash(f"勘误失败，未写入任何数据：{exc}", "error")
        except UnicodeDecodeError:
            flash("勘误失败，未写入任何数据：文件必须为 UTF-8 编码", "error")
        return redirect(url_for("index"))

    @app.get("/month/<month>")
    def month_detail(month):
        conn = get_db()
        try:
            month = _norm_month(month, "月份")
        except ValidationError as exc:
            flash(str(exc), "error")
            return redirect(url_for("index"))
        if get_month(conn, month) is None:
            flash(f"月份 {month} 不存在", "error")
            return redirect(url_for("index"))
        return render_template(
            "month.html",
            month=month,
            info=get_month(conn, month),
            rows=month_rows(conn, month),
            labels=KIND_LABELS,
        )

    @app.get("/export/<month>.csv")
    def export(month):
        conn = get_db()
        try:
            body = export_csv(conn, month)
        except ValidationError as exc:
            flash(str(exc), "error")
            return redirect(url_for("index"))
        return Response(
            body,
            mimetype="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="postings-{month}.csv"'
            },
        )

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return app


def main():
    db_path = os.environ.get("PAYROLL_DB", DEFAULT_DB)
    init_db(db_path)
    app = create_app(db_path)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()
