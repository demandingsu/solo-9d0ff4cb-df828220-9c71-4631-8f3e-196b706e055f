"""端到端自测：封账与勘误交错、连续改挂、幂等重放、失败回滚。"""

import io
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from payroll_app import app as appmod  # noqa: E402


ORIG_HEADER = "薪资单号,成本中心,金额分\n"
ADJ_HEADER = "勘误号,薪资单号,成本中心,金额分\n"


@pytest.fixture()
def client(tmp_path):
    db = str(tmp_path / "test.db")
    appmod.init_db(db)
    application = appmod.create_app(db, testing=True)
    with application.test_client() as c:
        c.db_path = db
        yield c


def post_csv(client, url, month, body, field="file"):
    return client.post(
        url,
        data={
            "entry_month": month,
            field: (io.BytesIO(body.encode("utf-8")), "upload.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def q(db, sql, args=()):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def postings(db, month):
    return q(db, "SELECT * FROM postings WHERE entry_month=? ORDER BY id", (month,))


def net(db, month):
    return q(db, "SELECT COALESCE(SUM(amount_cents),0) AS n FROM postings WHERE entry_month=?",
             (month,))[0]["n"]


def slip(db, no):
    return q(db, "SELECT * FROM slips WHERE slip_no=?", (no,))[0]


def open_close(client, month):
    return client.post("/months/close", data={"month": month}, follow_redirects=True)


def add_month(client, month):
    return client.post("/months/open", data={"month": month}, follow_redirects=True)


# --------------------------------------------------------------- 基础流程

def test_import_then_export_by_entry_month(client):
    add_month(client, "2026-12")
    post_csv(client, "/import/originals", "2026-12",
             ORIG_HEADER + "S1,CC-A,1000\nS2,CC-B,2500\n")
    rows = postings(client.db_path, "2026-12")
    assert [(r["slip_no"], r["cost_center"], r["amount_cents"]) for r in rows] == [
        ("S1", "CC-A", 1000), ("S2", "CC-B", 2500)]
    resp = client.get("/export/2026-12.csv")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert "薪资单号" in body and "S1" in body and "CC-A" in body
    assert resp.headers["Content-Disposition"].endswith('postings-2026-12.csv"')


def test_import_rejects_bad_rows_atomically(client):
    add_month(client, "2026-12")
    resp = post_csv(client, "/import/originals", "2026-12",
                    ORIG_HEADER + "S1,CC-A,1000\nS2,CC-B,abc\n")
    assert "未写入任何数据" in resp.data.decode("utf-8")
    assert postings(client.db_path, "2026-12") == []
    assert q(client.db_path, "SELECT * FROM slips") == []


def test_import_into_closed_month_rejected(client):
    add_month(client, "2026-12")
    post_csv(client, "/import/originals", "2026-12", ORIG_HEADER + "S1,CC-A,1\n")
    open_close(client, "2026-12")
    resp = post_csv(client, "/import/originals", "2026-12", ORIG_HEADER + "S2,CC-B,2\n")
    assert "已封账" in resp.data.decode("utf-8")
    assert q(client.db_path, "SELECT * FROM slips")[0]["slip_no"] == "S1"


def test_import_into_missing_month_rejected(client):
    resp = post_csv(client, "/import/originals", "2031-05", ORIG_HEADER + "S1,CC-A,1\n")
    assert "不存在" in resp.data.decode("utf-8")
    assert q(client.db_path, "SELECT * FROM slips") == []


# ------------------------------------------------- 未封账：直接改原行

def test_unclosed_inplace_update(client):
    add_month(client, "2026-12")
    post_csv(client, "/import/originals", "2026-12", ORIG_HEADER + "S1,CC-A,1000\n")
    resp = post_csv(client, "/import/adjustments", "2026-12",
                    ADJ_HEADER + "A1,S1,CC-B,1200\n")
    assert "生效 1 条" in resp.data.decode("utf-8")
    rows = postings(client.db_path, "2026-12")
    assert len(rows) == 1
    assert (rows[0]["kind"], rows[0]["cost_center"], rows[0]["amount_cents"]) == (
        "original", "CC-B", 1200)
    assert rows[0]["adjust_no"] == "A1"
    s = slip(client.db_path, "S1")
    assert (s["cost_center"], s["amount_cents"], s["entry_month"]) == ("CC-B", 1200, "2026-12")


def test_unclosed_adjustment_must_target_slip_month(client):
    add_month(client, "2026-12")
    add_month(client, "2027-01")
    post_csv(client, "/import/originals", "2026-12", ORIG_HEADER + "S1,CC-A,1000\n")
    resp = post_csv(client, "/import/adjustments", "2027-01", ADJ_HEADER + "A1,S1,CC-B,1200\n")
    assert "尚未封账" in resp.data.decode("utf-8")
    assert postings(client.db_path, "2027-01") == []


# ------------------------------------------------- 已封账：冲回 + 重入账

def test_closed_reversal_then_rebook(client):
    add_month(client, "2026-12")
    post_csv(client, "/import/originals", "2026-12", ORIG_HEADER + "S1,CC-A,1000\n")
    open_close(client, "2026-12")
    add_month(client, "2027-01")
    post_csv(client, "/import/adjustments", "2027-01", ADJ_HEADER + "A1,S1,CC-B,1200\n")

    dec = postings(client.db_path, "2026-12")
    assert len(dec) == 1 and dec[0]["cost_center"] == "CC-A" and dec[0]["amount_cents"] == 1000

    jan = postings(client.db_path, "2027-01")
    assert len(jan) == 2
    rev, reb = jan
    assert (rev["kind"], rev["cost_center"], rev["amount_cents"]) == ("reverse", "CC-A", -1000)
    assert (reb["kind"], reb["cost_center"], reb["amount_cents"]) == ("rebook", "CC-B", 1200)
    for row in jan:
        assert row["slip_no"] == "S1" and row["slip_month"] == "2026-12"
    assert net(client.db_path, "2027-01") == 200


def test_consecutive_readjustments_use_last_effective(client):
    """连续改挂：每次冲回的都是上次生效值，只在新开放月留痕。"""
    add_month(client, "2026-12")
    post_csv(client, "/import/originals", "2026-12", ORIG_HEADER + "S1,CC-A,1000\n")
    open_close(client, "2026-12")

    add_month(client, "2027-01")
    post_csv(client, "/import/adjustments", "2027-01", ADJ_HEADER + "A1,S1,CC-B,1200\n")
    open_close(client, "2027-01")

    add_month(client, "2027-02")
    post_csv(client, "/import/adjustments", "2027-02", ADJ_HEADER + "A2,S1,CC-C,900\n")
    open_close(client, "2027-02")

    add_month(client, "2027-03")
    post_csv(client, "/import/adjustments", "2027-03", ADJ_HEADER + "A3,S1,CC-D,700\n")

    assert [p["cost_center"] for p in postings(client.db_path, "2026-12")] == ["CC-A"]
    jan = postings(client.db_path, "2027-01")
    assert [(p["kind"], p["cost_center"], p["amount_cents"]) for p in jan] == [
        ("reverse", "CC-A", -1000), ("rebook", "CC-B", 1200)]
    feb = postings(client.db_path, "2027-02")
    assert [(p["kind"], p["cost_center"], p["amount_cents"]) for p in feb] == [
        ("reverse", "CC-B", -1200), ("rebook", "CC-C", 900)]
    mar = postings(client.db_path, "2027-03")
    assert [(p["kind"], p["cost_center"], p["amount_cents"]) for p in mar] == [
        ("reverse", "CC-C", -900), ("rebook", "CC-D", 700)]
    s = slip(client.db_path, "S1")
    assert (s["cost_center"], s["amount_cents"], s["entry_month"]) == ("CC-D", 700, "2027-03")


def test_adjustment_completed_before_close_is_in_that_month(client):
    """勘误先完成，当月明细就包含它（即使随后就封账）。"""
    add_month(client, "2026-12")
    post_csv(client, "/import/originals", "2026-12", ORIG_HEADER + "S1,CC-A,1000\n")
    post_csv(client, "/import/adjustments", "2026-12", ADJ_HEADER + "A1,S1,CC-B,1100\n")
    open_close(client, "2026-12")
    dec = postings(client.db_path, "2026-12")
    assert [(p["cost_center"], p["amount_cents"]) for p in dec] == [("CC-B", 1100)]
    # 封账后不能再写
    add_month(client, "2027-01")
    resp = post_csv(client, "/import/adjustments", "2026-12", ADJ_HEADER + "A2,S1,CC-C,1\n")
    assert "不存在" in resp.data.decode("utf-8") or "已封账" in resp.data.decode("utf-8")


def test_close_before_adjustment_puts_it_in_new_open_month(client):
    add_month(client, "2026-12")
    post_csv(client, "/import/originals", "2026-12", ORIG_HEADER + "S1,CC-A,1000\n")
    open_close(client, "2026-12")
    add_month(client, "2027-01")
    post_csv(client, "/import/adjustments", "2027-01", ADJ_HEADER + "A1,S1,CC-B,1200\n")
    dec = postings(client.db_path, "2026-12")
    assert len(dec) == 1 and dec[0]["cost_center"] == "CC-A"
    assert len(postings(client.db_path, "2027-01")) == 2


# --------------------------------------------------------------- 幂等

def test_same_adjust_no_same_content_processed_once(client):
    add_month(client, "2026-12")
    post_csv(client, "/import/originals", "2026-12", ORIG_HEADER + "S1,CC-A,1000\n")
    open_close(client, "2026-12")
    add_month(client, "2027-01")
    body = ADJ_HEADER + "A1,S1,CC-B,1200\n"
    post_csv(client, "/import/adjustments", "2027-01", body)
    resp = post_csv(client, "/import/adjustments", "2027-01", body)
    assert "重复跳过 1 条" in resp.data.decode("utf-8")
    assert len(postings(client.db_path, "2027-01")) == 2
    assert len(q(client.db_path, "SELECT * FROM adjustments")) == 1


def test_same_adjust_no_replay_in_later_month_is_skipped(client):
    """原样重传，即使换了入账月，也只处理一次。"""
    add_month(client, "2026-12")
    post_csv(client, "/import/originals", "2026-12", ORIG_HEADER + "S1,CC-A,1000\n")
    open_close(client, "2026-12")
    add_month(client, "2027-01")
    post_csv(client, "/import/adjustments", "2027-01", ADJ_HEADER + "A1,S1,CC-B,1200\n")
    open_close(client, "2027-01")
    add_month(client, "2027-02")
    resp = post_csv(client, "/import/adjustments", "2027-02", ADJ_HEADER + "A1,S1,CC-B,1200\n")
    assert "重复跳过 1 条" in resp.data.decode("utf-8")
    assert postings(client.db_path, "2027-02") == []


def test_same_adjust_no_different_content_rejected(client):
    add_month(client, "2026-12")
    post_csv(client, "/import/originals", "2026-12", ORIG_HEADER + "S1,CC-A,1000\n")
    open_close(client, "2026-12")
    add_month(client, "2027-01")
    post_csv(client, "/import/adjustments", "2027-01", ADJ_HEADER + "A1,S1,CC-B,1200\n")
    before = len(postings(client.db_path, "2027-01"))
    resp = post_csv(client, "/import/adjustments", "2027-01", ADJ_HEADER + "A1,S1,CC-B,9999\n")
    assert "不同内容" in resp.data.decode("utf-8")
    assert len(postings(client.db_path, "2027-01")) == before
    assert slip(client.db_path, "S1")["amount_cents"] == 1200


# --------------------------------------------------------------- 失败回滚

def test_batch_failure_rolls_back_everything(client):
    add_month(client, "2026-12")
    post_csv(client, "/import/originals", "2026-12", ORIG_HEADER + "S1,CC-A,1000\n")
    open_close(client, "2026-12")
    add_month(client, "2027-01")
    body = ADJ_HEADER + "A1,S1,CC-B,1200\nA2,NOPE,CC-C,50\n"
    resp = post_csv(client, "/import/adjustments", "2027-01", body)
    assert "未写入任何数据" in resp.data.decode("utf-8")
    assert postings(client.db_path, "2027-01") == []
    assert q(client.db_path, "SELECT * FROM adjustments") == []


def test_db_trigger_blocks_writes_into_closed_month(client):
    """绕过应用层直接写库也必须被拒绝。"""
    add_month(client, "2026-12")
    post_csv(client, "/import/originals", "2026-12", ORIG_HEADER + "S1,CC-A,1000\n")
    open_close(client, "2026-12")
    conn = sqlite3.connect(client.db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO postings(entry_month, slip_no, slip_month, kind,"
                " cost_center, amount_cents, created_at) VALUES"
                " ('2026-12','S9','2026-12','original','CC-X',1,'now')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE postings SET amount_cents=0 WHERE slip_no='S1'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM postings WHERE slip_no='S1'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE months SET status='open' WHERE month='2026-12'")
    finally:
        conn.close()


def test_reopen_closed_month_rejected(client):
    add_month(client, "2026-12")
    open_close(client, "2026-12")
    resp = add_month(client, "2026-12")
    assert "封账不可逆" in resp.data.decode("utf-8")
    row = q(client.db_path, "SELECT status FROM months WHERE month='2026-12'")[0]
    assert row["status"] == "closed"


# --------------------------------------------------------------- 复杂交错

def test_interleaved_close_and_adjustments_multi_slip(client):
    add_month(client, "2026-12")
    post_csv(client, "/import/originals", "2026-12",
             ORIG_HEADER + "S1,CC-A,1000\nS2,CC-B,2000\n")
    # 12 月未封账：S1 直接改，S2 不动
    post_csv(client, "/import/adjustments", "2026-12", ADJ_HEADER + "A1,S1,CC-A2,1100\n")
    open_close(client, "2026-12")

    add_month(client, "2027-01")
    # 原单 S2 仍在新开放月导入
    post_csv(client, "/import/originals", "2027-01", ORIG_HEADER + "S3,CC-C,3000\n")
    # 对已封 S1 勘误 -> 冲回 1100/CC-A2，重入 1300/CC-A3
    post_csv(client, "/import/adjustments", "2027-01", ADJ_HEADER + "A2,S1,CC-A3,1300\n")
    # 对未封 S3 勘误 -> 直接改原行
    post_csv(client, "/import/adjustments", "2027-01", ADJ_HEADER + "A3,S3,CC-C2,3300\n")
    open_close(client, "2027-01")

    add_month(client, "2027-02")
    # 重放 A1（原样）应跳过
    resp = post_csv(client, "/import/adjustments", "2027-02", ADJ_HEADER + "A1,S1,CC-A2,1100\n")
    assert "重复跳过 1 条" in resp.data.decode("utf-8")
    assert postings(client.db_path, "2027-02") == []

    add_month(client, "2027-03")
    post_csv(client, "/import/adjustments", "2027-03", ADJ_HEADER + "A4,S3,CC-C3,3100\n")

    dec = postings(client.db_path, "2026-12")
    assert [(p["slip_no"], p["kind"], p["cost_center"], p["amount_cents"]) for p in dec] == [
        ("S1", "original", "CC-A2", 1100), ("S2", "original", "CC-B", 2000)]
    jan = postings(client.db_path, "2027-01")
    assert [(p["slip_no"], p["kind"], p["cost_center"], p["amount_cents"]) for p in jan] == [
        ("S3", "original", "CC-C2", 3300),
        ("S1", "reverse", "CC-A2", -1100),
        ("S1", "rebook", "CC-A3", 1300)]
    feb_rows = postings(client.db_path, "2027-02")
    assert feb_rows == []
    mar = postings(client.db_path, "2027-03")
    assert [(p["slip_no"], p["kind"], p["cost_center"], p["amount_cents"]) for p in mar] == [
        ("S3", "reverse", "CC-C2", -3300), ("S3", "rebook", "CC-C3", 3100)]

    assert net(client.db_path, "2026-12") == 3100
    assert net(client.db_path, "2027-01") == 3500
    assert net(client.db_path, "2027-03") == -200
    assert slip(client.db_path, "S1")["entry_month"] == "2027-01"
    assert slip(client.db_path, "S3")["entry_month"] == "2027-03"


def test_adjustment_into_closed_entry_month_rejected(client):
    add_month(client, "2026-12")
    post_csv(client, "/import/originals", "2026-12", ORIG_HEADER + "S1,CC-A,1000\n")
    open_close(client, "2026-12")
    resp = post_csv(client, "/import/adjustments", "2026-12", ADJ_HEADER + "A1,S1,CC-B,1\n")
    assert "不存在" in resp.data.decode("utf-8") or "已封账" in resp.data.decode("utf-8")
    assert q(client.db_path, "SELECT * FROM adjustments") == []


def test_duplicate_original_requires_adjustment(client):
    add_month(client, "2026-12")
    post_csv(client, "/import/originals", "2026-12", ORIG_HEADER + "S1,CC-A,1000\n")
    resp = post_csv(client, "/import/originals", "2026-12", ORIG_HEADER + "S1,CC-A,1000\n")
    assert "已存在" in resp.data.decode("utf-8")
    assert len(postings(client.db_path, "2026-12")) == 1


def test_export_contains_signed_reversal(client):
    add_month(client, "2026-12")
    post_csv(client, "/import/originals", "2026-12", ORIG_HEADER + "S1,CC-A,1000\n")
    open_close(client, "2026-12")
    add_month(client, "2027-01")
    post_csv(client, "/import/adjustments", "2027-01", ADJ_HEADER + "A1,S1,CC-B,1200\n")
    body = client.get("/export/2027-01.csv").data.decode("utf-8")
    lines = [ln for ln in body.strip().splitlines() if ln]
    assert lines[0].startswith("入账月,薪资单号,薪资所属月,流水类型")
    assert any("冲回" in ln and "-1000" in ln and "2026-12" in ln for ln in lines)
    assert any("重入账" in ln and "1200" in ln and "CC-B" in ln for ln in lines)


# --------------------------------------------------------------- 并发幂等

def test_concurrent_same_adjust_no_applied_once(tmp_path):
    import threading

    db = str(tmp_path / "concurrent.db")
    appmod.init_db(db)
    conn = appmod.connect(db)
    appmod.open_month(conn, "2026-12")
    appmod.ingest_originals(conn, "2026-12", ORIG_HEADER + "S1,CC-A,1000\n")
    appmod.close_month(conn, "2026-12")
    appmod.open_month(conn, "2027-01")
    conn.close()

    body = ADJ_HEADER + "A1,S1,CC-B,1200\n"
    results, errors = [], []
    barrier = threading.Barrier(2)

    def worker():
        c = appmod.connect(db)
        try:
            barrier.wait()
            counts, _ = appmod.ingest_adjustments(c, "2027-01", body)
            results.append(counts)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            c.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert sum(r["applied"] for r in results) == 1
    assert sum(r["duplicate_skipped"] for r in results) == 1
    assert len(postings(db, "2027-01")) == 2


# --------------------------------------------------------------- 其它边界

def test_inplace_twice_keeps_single_original_row(client):
    add_month(client, "2026-12")
    post_csv(client, "/import/originals", "2026-12", ORIG_HEADER + "S1,CC-A,1000\n")
    post_csv(client, "/import/adjustments", "2026-12", ADJ_HEADER + "A1,S1,CC-B,1100\n")
    post_csv(client, "/import/adjustments", "2026-12", ADJ_HEADER + "A2,S1,CC-C,900\n")
    rows = postings(client.db_path, "2026-12")
    assert len(rows) == 1
    assert (rows[0]["cost_center"], rows[0]["amount_cents"], rows[0]["adjust_no"]) == (
        "CC-C", 900, "A2")
    hist = q(client.db_path,
             "SELECT adjust_no, mode FROM slip_revisions WHERE slip_no='S1' ORDER BY id")
    assert [(h["adjust_no"], h["mode"]) for h in hist] == [("A1", "inplace"), ("A2", "inplace")]


def test_negative_amount_and_center_validation(client):
    add_month(client, "2026-12")
    resp = post_csv(client, "/import/originals", "2026-12",
                    ORIG_HEADER + "S1,CC A,1000\n")
    assert "成本中心" in resp.data.decode("utf-8")
    resp = post_csv(client, "/import/originals", "2026-12",
                    ORIG_HEADER + "S1,CC-A,10.5\n")
    assert "整数" in resp.data.decode("utf-8")
    resp = post_csv(client, "/import/originals", "2026-12",
                    "薪资单号,金额分,成本中心\nS1,1000,CC-A\n")
    assert "表头" in resp.data.decode("utf-8")
    assert q(client.db_path, "SELECT * FROM slips") == []


def test_export_missing_month_is_flash_not_500(client):
    resp = client.get("/export/2099-01.csv", follow_redirects=True)
    assert resp.status_code == 200
    assert "不存在" in resp.data.decode("utf-8")


def test_batch_conflict_midway_rolls_back_prior_rows(client):
    add_month(client, "2026-12")
    post_csv(client, "/import/originals", "2026-12", ORIG_HEADER + "S1,CC-A,1000\n")
    open_close(client, "2026-12")
    add_month(client, "2027-01")
    post_csv(client, "/import/adjustments", "2027-01", ADJ_HEADER + "A1,S1,CC-B,1200\n")
    open_close(client, "2027-01")
    add_month(client, "2027-02")

    # 第二行与已处理 A1 同号不同内容：整批拒绝，第一行也不生效
    body = ADJ_HEADER + "A2,S1,CC-C,1300\nA1,S1,CC-B,9999\n"
    resp = post_csv(client, "/import/adjustments", "2027-02", body)
    assert "不同内容" in resp.data.decode("utf-8")
    assert postings(client.db_path, "2027-02") == []
    assert slip(client.db_path, "S1")["amount_cents"] == 1200
    assert q(client.db_path, "SELECT * FROM adjustments WHERE adjust_no='A2'") == []


def test_batch_duplicate_and_new_in_same_file(client):
    add_month(client, "2026-12")
    post_csv(client, "/import/originals", "2026-12", ORIG_HEADER + "S1,CC-A,1000\nS2,CC-B,2000\n")
    open_close(client, "2026-12")
    add_month(client, "2027-01")
    post_csv(client, "/import/adjustments", "2027-01", ADJ_HEADER + "A1,S1,CC-C,1100\n")
    open_close(client, "2027-01")
    add_month(client, "2027-02")
    body = ADJ_HEADER + "A1,S1,CC-C,1100\nA2,S2,CC-D,2200\n"
    resp = post_csv(client, "/import/adjustments", "2027-02", body)
    text = resp.data.decode("utf-8")
    assert "生效 1 条" in text and "重复跳过 1 条" in text
    rows = postings(client.db_path, "2027-02")
    assert [(p["slip_no"], p["kind"], p["amount_cents"]) for p in rows] == [
        ("S2", "reverse", -2000), ("S2", "rebook", 2200)]


def test_unexpected_write_error_mid_batch_keeps_original_state(client, monkeypatch):
    """模拟写库中途异常：入账事务内已有写入也要整体回滚。"""
    add_month(client, "2026-12")
    post_csv(client, "/import/originals", "2026-12", ORIG_HEADER + "S1,CC-A,1000\nS2,CC-B,2000\n")
    open_close(client, "2026-12")
    add_month(client, "2027-01")

    real_executemany = appmod._executemany
    calls = {"written": False}

    def flaky(conn, sql, params):
        if calls["written"] and params:
            # 已有一组真实写入落进事务后，下一组写入时爆炸
            raise RuntimeError("simulated disk failure")
        result = real_executemany(conn, sql, params)
        if params:
            calls["written"] = True
        return result

    monkeypatch.setattr(appmod, "_executemany", flaky)
    with pytest.raises(RuntimeError):
        body = ADJ_HEADER + "A1,S1,CC-C,1100\nA2,S2,CC-D,2100\n"
        with appmod.connect(client.db_path) as raw:
            appmod.ingest_adjustments(raw, "2027-01", body)

    monkeypatch.setattr(appmod, "_executemany", real_executemany)
    assert postings(client.db_path, "2027-01") == []
    assert q(client.db_path, "SELECT * FROM adjustments") == []
    assert q(client.db_path, "SELECT * FROM slip_revisions") == []
    assert slip(client.db_path, "S1")["amount_cents"] == 1000
    assert slip(client.db_path, "S2")["cost_center"] == "CC-B"


def test_replay_into_closed_month_is_idempotent_noop(client):
    """原样重传即使目标月已封账也只跳过，不报错、不写入（幂等重试安全）。"""
    add_month(client, "2026-12")
    post_csv(client, "/import/originals", "2026-12", ORIG_HEADER + "S1,CC-A,1000\n")
    open_close(client, "2026-12")
    add_month(client, "2027-01")
    body = ADJ_HEADER + "A1,S1,CC-B,1200\n"
    post_csv(client, "/import/adjustments", "2027-01", body)
    open_close(client, "2027-01")
    resp = post_csv(client, "/import/adjustments", "2027-01", body)
    assert "重复跳过 1 条" in resp.data.decode("utf-8")
    assert len(postings(client.db_path, "2027-01")) == 2
    assert len(q(client.db_path, "SELECT * FROM adjustments")) == 1


def test_export_after_close_still_available(client):
    add_month(client, "2026-12")
    post_csv(client, "/import/originals", "2026-12", ORIG_HEADER + "S1,CC-A,1000\n")
    open_close(client, "2026-12")
    resp = client.get("/export/2026-12.csv")
    assert resp.status_code == 200
    assert "S1" in resp.data.decode("utf-8")


def test_import_same_slip_in_new_month_rejected(client):
    add_month(client, "2026-12")
    post_csv(client, "/import/originals", "2026-12", ORIG_HEADER + "S1,CC-A,1000\n")
    open_close(client, "2026-12")
    add_month(client, "2027-01")
    resp = post_csv(client, "/import/originals", "2027-01", ORIG_HEADER + "S1,CC-A,1000\n")
    assert "已存在" in resp.data.decode("utf-8")
    assert postings(client.db_path, "2027-01") == []


# ------------------------------------------- 批内连续勘误（准备/入账两阶段）

def test_consecutive_adjustments_same_slip_in_one_batch(client):
    """批内连续勘误同一薪资单：后面的冲回前面刚生效的值，按 CSV 顺序落账。"""
    add_month(client, "2026-12")
    post_csv(client, "/import/originals", "2026-12", ORIG_HEADER + "S1,CC-A,1000\n")
    open_close(client, "2026-12")
    add_month(client, "2027-01")
    resp = post_csv(client, "/import/adjustments", "2027-01",
                    ADJ_HEADER + "A1,S1,CC-B,1200\nA2,S1,CC-C,900\n")
    assert "生效 2 条" in resp.data.decode("utf-8")
    jan = postings(client.db_path, "2027-01")
    assert [(p["kind"], p["cost_center"], p["amount_cents"], p["adjust_no"], p["revision"])
            for p in jan] == [
        ("reverse", "CC-A", -1000, "A1", 1), ("rebook", "CC-B", 1200, "A1", 1),
        ("reverse", "CC-B", -1200, "A2", 2), ("rebook", "CC-C", 900, "A2", 2)]
    s = slip(client.db_path, "S1")
    assert (s["cost_center"], s["amount_cents"], s["entry_month"]) == ("CC-C", 900, "2027-01")
    hist = q(client.db_path,
             "SELECT adjust_no, revision, mode FROM slip_revisions"
             " WHERE slip_no='S1' ORDER BY id")
    assert [(h["adjust_no"], h["revision"], h["mode"]) for h in hist] == [
        ("A1", 1, "reversal"), ("A2", 2, "reversal")]


def test_inplace_twice_in_one_batch(client):
    """未封账月批内两次原地修改：仍只留一行原单，历史在 slip_revisions。"""
    add_month(client, "2026-12")
    post_csv(client, "/import/originals", "2026-12", ORIG_HEADER + "S1,CC-A,1000\n")
    resp = post_csv(client, "/import/adjustments", "2026-12",
                    ADJ_HEADER + "A1,S1,CC-B,1100\nA2,S1,CC-C,900\n")
    assert "生效 2 条" in resp.data.decode("utf-8")
    rows = postings(client.db_path, "2026-12")
    assert len(rows) == 1
    assert (rows[0]["cost_center"], rows[0]["amount_cents"], rows[0]["adjust_no"]) == (
        "CC-C", 900, "A2")
    hist = q(client.db_path,
             "SELECT adjust_no, mode FROM slip_revisions WHERE slip_no='S1' ORDER BY id")
    assert [(h["adjust_no"], h["mode"]) for h in hist] == [("A1", "inplace"), ("A2", "inplace")]


# ------------------------------------------- 准备期间账况变化：按最新账况重备

def test_reprepare_when_entry_month_closed_during_prepare(client, monkeypatch):
    """准备期间入账月被封账：检测到变化并按最新账况重备 → 业务冲突，整批不写。"""
    add_month(client, "2026-12")
    post_csv(client, "/import/originals", "2026-12", ORIG_HEADER + "S1,CC-A,1000\n")
    open_close(client, "2026-12")
    add_month(client, "2027-01")

    real_commit = appmod._commit_adjustments
    calls = {"n": 0}

    def interfering(conn, plan):
        calls["n"] += 1
        if calls["n"] == 1:
            other = appmod.connect(client.db_path)
            try:
                appmod.close_month(other, "2027-01")  # 准备完成后、入账前封账
            finally:
                other.close()
        return real_commit(conn, plan)

    monkeypatch.setattr(appmod, "_commit_adjustments", interfering)
    resp = post_csv(client, "/import/adjustments", "2027-01", ADJ_HEADER + "A1,S1,CC-B,1200\n")
    assert "已封账" in resp.data.decode("utf-8")
    assert calls["n"] == 1  # 首次入账发现过期，重备时即冲突，不再入账
    assert postings(client.db_path, "2027-01") == []
    assert q(client.db_path, "SELECT * FROM adjustments") == []


def test_reprepare_when_slip_changed_during_prepare(client, monkeypatch):
    """准备期间另一请求改了同一薪资单：按最新生效值重新计算冲回。"""
    add_month(client, "2026-12")
    post_csv(client, "/import/originals", "2026-12", ORIG_HEADER + "S1,CC-A,1000\n")
    open_close(client, "2026-12")
    add_month(client, "2027-01")

    real_commit = appmod._commit_adjustments
    calls = {"n": 0, "busy": False}

    def interfering(conn, plan):
        if calls["busy"]:  # 干扰动作自身的入账直接放行，不计数也不再干扰
            return real_commit(conn, plan)
        calls["n"] += 1
        if calls["n"] == 1:
            calls["busy"] = True
            try:
                other = appmod.connect(client.db_path)
                try:
                    appmod.ingest_adjustments(other, "2027-01", ADJ_HEADER + "A0,S1,CC-B,1200\n")
                finally:
                    other.close()
            finally:
                calls["busy"] = False
        return real_commit(conn, plan)

    monkeypatch.setattr(appmod, "_commit_adjustments", interfering)
    resp = post_csv(client, "/import/adjustments", "2027-01", ADJ_HEADER + "A1,S1,CC-C,900\n")
    assert "生效 1 条" in resp.data.decode("utf-8")
    assert calls["n"] == 2  # 首次过期重备，第二次入账成功
    jan = postings(client.db_path, "2027-01")
    assert [(p["kind"], p["cost_center"], p["amount_cents"], p["adjust_no"]) for p in jan] == [
        ("reverse", "CC-A", -1000, "A0"), ("rebook", "CC-B", 1200, "A0"),
        ("reverse", "CC-B", -1200, "A1"), ("rebook", "CC-C", 900, "A1")]
    s = slip(client.db_path, "S1")
    assert (s["cost_center"], s["amount_cents"]) == ("CC-C", 900)


def test_reprepare_when_same_adjust_no_processed_during_prepare(client, monkeypatch):
    """准备期间同号勘误被另一请求处理：重备后按幂等规则跳过，不重复入账。"""
    add_month(client, "2026-12")
    post_csv(client, "/import/originals", "2026-12", ORIG_HEADER + "S1,CC-A,1000\n")
    open_close(client, "2026-12")
    add_month(client, "2027-01")

    real_commit = appmod._commit_adjustments
    calls = {"n": 0}

    def interfering(conn, plan):
        calls["n"] += 1
        if calls["n"] == 1:
            other = appmod.connect(client.db_path)
            try:
                appmod.ingest_adjustments(other, "2027-01", ADJ_HEADER + "A1,S1,CC-B,1200\n")
            finally:
                other.close()
        return real_commit(conn, plan)

    monkeypatch.setattr(appmod, "_commit_adjustments", interfering)
    resp = post_csv(client, "/import/adjustments", "2027-01", ADJ_HEADER + "A1,S1,CC-B,1200\n")
    assert "重复跳过 1 条" in resp.data.decode("utf-8")
    assert len(postings(client.db_path, "2027-01")) == 2
    assert len(q(client.db_path, "SELECT * FROM adjustments")) == 1


def test_reprepare_when_conflicting_adjust_no_processed_during_prepare(client, monkeypatch):
    """准备期间同号异内容被另一请求处理：重备后按原规则整批拒绝。"""
    add_month(client, "2026-12")
    post_csv(client, "/import/originals", "2026-12", ORIG_HEADER + "S1,CC-A,1000\n")
    open_close(client, "2026-12")
    add_month(client, "2027-01")

    real_commit = appmod._commit_adjustments
    calls = {"n": 0}

    def interfering(conn, plan):
        calls["n"] += 1
        if calls["n"] == 1:
            other = appmod.connect(client.db_path)
            try:
                appmod.ingest_adjustments(other, "2027-01", ADJ_HEADER + "A1,S1,CC-B,1200\n")
            finally:
                other.close()
        return real_commit(conn, plan)

    monkeypatch.setattr(appmod, "_commit_adjustments", interfering)
    resp = post_csv(client, "/import/adjustments", "2027-01", ADJ_HEADER + "A1,S1,CC-C,900\n")
    assert "不同内容" in resp.data.decode("utf-8")
    # 只有另一请求的 A1 入账；本批未写任何行
    assert len(postings(client.db_path, "2027-01")) == 2
    assert len(q(client.db_path, "SELECT * FROM adjustments")) == 1
    assert slip(client.db_path, "S1")["cost_center"] == "CC-B"


def test_unrelated_month_activity_does_not_reprepare(client, monkeypatch):
    """无关月份的入账不影响准备结果：不触发重备，一次入账成功。"""
    add_month(client, "2026-12")
    post_csv(client, "/import/originals", "2026-12", ORIG_HEADER + "S1,CC-A,1000\n")
    open_close(client, "2026-12")
    add_month(client, "2027-01")
    add_month(client, "2027-02")

    real_prepare = appmod._prepare_adjustments
    real_commit = appmod._commit_adjustments
    calls = {"prepare": 0, "interfered": False}

    def counting_prepare(conn, entry_month, rows):
        calls["prepare"] += 1
        return real_prepare(conn, entry_month, rows)

    def interfering(conn, plan):
        if not calls["interfered"]:
            calls["interfered"] = True
            other = appmod.connect(client.db_path)
            try:
                # 无关月份入账 + 无关薪资单：与本批核对范围无交集
                appmod.ingest_originals(other, "2027-02", ORIG_HEADER + "S9,CC-Z,5\n")
            finally:
                other.close()
        return real_commit(conn, plan)

    monkeypatch.setattr(appmod, "_prepare_adjustments", counting_prepare)
    monkeypatch.setattr(appmod, "_commit_adjustments", interfering)
    resp = post_csv(client, "/import/adjustments", "2027-01", ADJ_HEADER + "A1,S1,CC-B,1200\n")
    assert "生效 1 条" in resp.data.decode("utf-8")
    assert calls["prepare"] == 1  # 没有重备
    assert len(postings(client.db_path, "2027-01")) == 2


def test_persistent_interference_gives_up(client, monkeypatch):
    """账况持续变化：重备次数用完后明确报错，本批不落任何行。"""
    add_month(client, "2026-12")
    post_csv(client, "/import/originals", "2026-12", ORIG_HEADER + "S1,CC-A,1000\n")
    open_close(client, "2026-12")
    add_month(client, "2027-01")

    real_commit = appmod._commit_adjustments
    counter = {"adj": 0, "busy": False}

    def interfering(conn, plan):
        if counter["busy"]:  # 干扰动作自身的入账直接放行
            return real_commit(conn, plan)
        counter["busy"] = True
        try:
            counter["adj"] += 1
            other = appmod.connect(client.db_path)
            try:
                appmod.ingest_adjustments(
                    other, "2027-01",
                    ADJ_HEADER + f"AX{counter['adj']},S1,CC-B,{1200 + counter['adj']}\n")
            finally:
                other.close()
        finally:
            counter["busy"] = False
        return real_commit(conn, plan)

    monkeypatch.setattr(appmod, "_commit_adjustments", interfering)
    resp = post_csv(client, "/import/adjustments", "2027-01", ADJ_HEADER + "A1,S1,CC-C,900\n")
    assert "持续变化" in resp.data.decode("utf-8")
    # A1 从未入账；只有干扰请求的 AX* 落账
    assert q(client.db_path, "SELECT * FROM adjustments WHERE adjust_no='A1'") == []
    assert len(q(client.db_path, "SELECT * FROM adjustments")) == appmod.MAX_PREPARE_ATTEMPTS


def test_prepare_does_not_block_other_writers(tmp_path, monkeypatch):
    """准备阶段在写事务之外：准备进行时其他请求可正常封账/入账。"""
    import threading

    db = str(tmp_path / "locks.db")
    appmod.init_db(db)
    conn = appmod.connect(db)
    appmod.open_month(conn, "2026-12")
    appmod.ingest_originals(conn, "2026-12", ORIG_HEADER + "S1,CC-A,1000\n")
    appmod.close_month(conn, "2026-12")
    appmod.open_month(conn, "2027-01")
    appmod.open_month(conn, "2027-02")
    conn.close()

    real_load = appmod._load_state
    entered = threading.Event()
    release = threading.Event()

    def slow_load(c, entry_month, rows):
        state = real_load(c, entry_month, rows)
        entered.set()          # 准备已开始（尚未进入写事务）
        release.wait(5)        # 模拟大文件推演的耗时
        return state

    monkeypatch.setattr(appmod, "_load_state", slow_load)

    outcome = {}

    def worker():
        c = appmod.connect(db)
        try:
            outcome["result"] = appmod.ingest_adjustments(
                c, "2027-01", ADJ_HEADER + "A1,S1,CC-B,1200\n")
        finally:
            c.close()

    t = threading.Thread(target=worker)
    t.start()
    assert entered.wait(5)

    # 准备期间另一请求封账：若准备持有写锁，这里会阻塞到 busy_timeout
    other = appmod.connect(db)
    try:
        appmod.close_month(other, "2027-02")
    finally:
        other.close()
    release.set()
    t.join(10)
    assert not t.is_alive()

    counts, _ = outcome["result"]
    assert counts["applied"] == 1
    assert q(db, "SELECT status FROM months WHERE month='2027-02'")[0]["status"] == "closed"
    assert len(postings(db, "2027-01")) == 2
