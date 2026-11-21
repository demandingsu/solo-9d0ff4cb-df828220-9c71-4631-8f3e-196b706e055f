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
    """模拟写库中途异常：第一行已写入也要整体回滚。"""
    add_month(client, "2026-12")
    post_csv(client, "/import/originals", "2026-12", ORIG_HEADER + "S1,CC-A,1000\nS2,CC-B,2000\n")
    open_close(client, "2026-12")
    add_month(client, "2027-01")

    original = appmod._next_revision
    calls = {"n": 0}

    def flaky(conn, slip_no):
        calls["n"] += 1
        if calls["n"] == 2:  # 第 1 行写入后，第 2 行刚开始就爆炸
            raise RuntimeError("simulated disk failure")
        return original(conn, slip_no)

    monkeypatch.setattr(appmod, "_next_revision", flaky)
    with pytest.raises(RuntimeError):
        body = ADJ_HEADER + "A1,S1,CC-C,1100\nA2,S2,CC-D,2100\n"
        with appmod.connect(client.db_path) as raw:
            appmod.ingest_adjustments(raw, "2027-01", body)

    monkeypatch.setattr(appmod, "_next_revision", original)
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
