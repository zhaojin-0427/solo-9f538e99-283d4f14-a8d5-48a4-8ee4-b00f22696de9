from __future__ import annotations

import io
import sqlite3
import threading
from pathlib import Path

import pytest

from app import create_app
from app import services
from app.db import connect

ROWS = [
    ("2026-11-14", "东闸断面", "氨氮", "2026-11-14 08:00:00", "1.24"),
    ("2026-11-14", "东闸断面", "pH", "2026-11-14 08:00:00", "7.31"),
    ("2026-11-14", "西渡断面", "氨氮", "2026-11-14 14:00:00", "0.86"),
]


def csv_bytes(rows=ROWS):
    header = "dataset_date,site,metric,observed_at,value"
    return ("\n".join([header, *[",".join(r) for r in rows]]) + "\n").encode()


@pytest.fixture()
def app(tmp_path: Path):
    application = create_app(
        {
            "TESTING": True,
            "DATA_DIR": str(tmp_path),
            "DATABASE": str(tmp_path / "test.db"),
            "FILES_DIR": str(tmp_path / "files"),
            "SECRET_KEY": "test-secret",
            "CSRF_ENABLED": False,
        }
    )
    yield application


@pytest.fixture()
def client(app):
    return app.test_client()


def upload_and_publish(client, rows=ROWS, dataset_date="2026-11-14") -> int:
    response = client.post(
        "/admin/upload",
        data={
            "dataset_date": dataset_date,
            "file": (io.BytesIO(csv_bytes(rows)), "obs.csv"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    conn = connect(client.application.config["DATABASE"])
    try:
        version = conn.execute(
            "SELECT * FROM versions ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    publish = client.post(f"/admin/versions/{version['id']}/publish")
    assert publish.status_code == 302
    return int(version["id"])


def db_rows(app, sql, params=()):
    conn = connect(app.config["DATABASE"])
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def observation_id(app, version_id: int, site: str, metric: str) -> int:
    rows = db_rows(
        app,
        "SELECT id FROM observations WHERE version_id = ? AND site = ? AND metric = ?",
        (version_id, site, metric),
    )
    assert len(rows) == 1
    return int(rows[0]["id"])


def correct(client, version_id, observation_id, value, reason="录入有误", revision=0):
    return client.post(
        f"/admin/versions/{version_id}/correct",
        data={
            "observation_id": str(observation_id),
            "expected_revision_no": str(revision),
            "value": value,
            "reason": reason,
        },
        follow_redirects=False,
    )


# ---------------------------------------------------------------------------
# Correction flow
# ---------------------------------------------------------------------------


def test_correction_updates_public_value_and_keeps_original(client, app):
    version_id = upload_and_publish(client)
    oid = observation_id(app, version_id, "东闸断面", "氨氮")

    page = client.get(f"/admin/versions/{version_id}/correct?observation_id={oid}")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "原值" in body and "现用值" in body
    assert "1.24" in body

    response = correct(client, version_id, oid, "1.28")
    assert response.status_code == 302

    public = client.get(f"/versions/{version_id}")
    assert public.status_code == 200
    text = public.get_data(as_text=True)
    assert "1.24" in text  # original value still displayed
    assert "1.28" in text  # current value displayed to analysts
    assert "录入有误" in text
    assert "已更正" in text

    # The observation row (the original value) is untouched.
    row = db_rows(
        app, "SELECT * FROM observations WHERE id = ?", (oid,)
    )[0]
    assert row["value_text"] == "1.24"

    corrections = db_rows(
        app, "SELECT * FROM corrections WHERE observation_id = ?", (oid,)
    )
    assert len(corrections) == 1
    assert corrections[0]["revision_no"] == 1
    assert corrections[0]["from_value_text"] == "1.24"
    assert corrections[0]["to_value_text"] == "1.28"
    assert corrections[0]["reason"] == "录入有误"

    # The downloadable original file is byte-for-byte unchanged.
    download = client.get(f"/versions/{version_id}/download")
    assert download.status_code == 200
    assert download.data == csv_bytes()


def test_successive_corrections_chain_values(client, app):
    version_id = upload_and_publish(client)
    oid = observation_id(app, version_id, "东闸断面", "氨氮")

    assert correct(client, version_id, oid, "1.28").status_code == 302
    assert correct(client, version_id, oid, "1.30", "再次复核", revision=1).status_code == 302

    records = db_rows(
        app,
        "SELECT * FROM corrections WHERE observation_id = ? ORDER BY revision_no",
        (oid,),
    )
    assert [r["revision_no"] for r in records] == [1, 2]
    assert records[0]["from_value_text"] == "1.24"
    assert records[0]["to_value_text"] == "1.28"
    assert records[1]["from_value_text"] == "1.28"
    assert records[1]["to_value_text"] == "1.3"  # canonicalised

    body = client.get(f"/versions/{version_id}").get_data(as_text=True)
    assert "1.24" in body
    assert "1.3" in body
    assert "1.28" in body  # appears in the correction history
    assert "再次复核" in body


def test_correction_requires_published_version(client, app):
    client.post(
        "/admin/upload",
        data={
            "dataset_date": "2026-11-14",
            "file": (io.BytesIO(csv_bytes()), "obs.csv"),
        },
        content_type="multipart/form-data",
    )
    candidate = db_rows(app, "SELECT * FROM versions")[0]
    response = client.get(f"/admin/versions/{candidate['id']}/correct")
    assert response.status_code == 404
    response = client.post(
        f"/admin/versions/{candidate['id']}/correct",
        data={"observation_id": "1", "value": "1.5", "reason": "x"},
    )
    assert response.status_code == 409
    assert db_rows(app, "SELECT * FROM corrections") == []


def test_correction_rejects_invalid_input(client, app):
    version_id = upload_and_publish(client)
    oid = observation_id(app, version_id, "东闸断面", "氨氮")

    for value in ("", "abc", "NaN", "inf", "-inf"):
        response = correct(client, version_id, oid, value)
        assert response.status_code == 400
        assert "新数值无效" in response.get_data(as_text=True)

    response = correct(client, version_id, oid, "1.28", reason="   ")
    assert response.status_code == 400
    assert "更正原因" in response.get_data(as_text=True)

    assert db_rows(app, "SELECT * FROM corrections") == []


def test_correction_rejects_unchanged_value(client, app):
    version_id = upload_and_publish(client)
    oid = observation_id(app, version_id, "东闸断面", "氨氮")
    response = correct(client, version_id, oid, "1.2400")
    assert response.status_code == 400
    assert "相同" in response.get_data(as_text=True)
    assert db_rows(app, "SELECT * FROM corrections") == []


def test_unchanged_value_on_current_form_is_rejected(client, app):
    version_id = upload_and_publish(client)
    oid = observation_id(app, version_id, "东闸断面", "氨氮")
    assert correct(client, version_id, oid, "1.28").status_code == 302

    response = correct(client, version_id, oid, "1.280", revision=1)
    assert response.status_code == 400
    assert "相同" in response.get_data(as_text=True)
    assert len(db_rows(app, "SELECT * FROM corrections")) == 1


def test_correction_requires_csrf_when_enabled(tmp_path):
    application = create_app(
        {
            "TESTING": True,
            "DATA_DIR": str(tmp_path),
            "DATABASE": str(tmp_path / "t.db"),
            "FILES_DIR": str(tmp_path / "files"),
            "SECRET_KEY": "s",
            "CSRF_ENABLED": False,
        }
    )
    client = application.test_client()
    version_id = upload_and_publish(client)
    application.config["CSRF_ENABLED"] = True
    response = client.post(
        f"/admin/versions/{version_id}/correct",
        data={"observation_id": "1", "value": "1.5", "reason": "x"},
    )
    assert response.status_code == 400
    assert "表单已过期" in response.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Conflicts
# ---------------------------------------------------------------------------


def test_republished_day_makes_open_form_conflict(client, app):
    """The form was opened on v1; the day is re-published as v2 meanwhile."""
    first = upload_and_publish(client)
    oid = observation_id(app, first, "东闸断面", "氨氮")

    # Form page is opened (would embed v1 + its current revision).
    assert client.get(f"/admin/versions/{first}/correct?observation_id={oid}").status_code == 200

    new_rows = ROWS + [("2026-11-14", "西渡断面", "pH", "2026-11-14 14:00:00", "7.28")]
    second = upload_and_publish(client, ROWS + [
        ("2026-11-14", "西渡断面", "pH", "2026-11-14 14:00:00", "7.28")
    ])

    # Submitting against the old (now superseded) version must conflict.
    response = correct(client, first, oid, "1.28")
    assert response.status_code == 409
    body = response.get_data(as_text=True)
    assert "提交冲突" in body or "版本已变化" in body
    assert "未更改任何数据" in body

    assert db_rows(app, "SELECT * FROM corrections") == []
    old = db_rows(app, "SELECT * FROM versions WHERE id = ?", (first,))[0]
    assert old["status"] == "superseded"
    assert db_rows(app, "SELECT * FROM versions WHERE id = ?", (second,))[0]["status"] == "published"
    # Old version is no longer publicly visible; new version has no corrections.
    assert client.get(f"/versions/{first}").status_code == 404
    assert db_rows(app, "SELECT * FROM corrections WHERE version_id = ?", (second,)) == []


def test_correcting_stale_form_on_new_version_is_fine(client, app):
    """A form opened on the current version still works after a re-publish
    only if it targets the new version; the new version starts uncorrected."""
    first = upload_and_publish(client)
    upload_and_publish(client, ROWS + [
        ("2026-11-14", "西渡断面", "pH", "2026-11-14 14:00:00", "7.28")
    ])
    response = client.get(f"/admin/versions/{first}/correct")
    # GET redirects to the current version instead of offering a dead form.
    assert response.status_code == 302
    assert "/correct" in response.headers["Location"]
    assert db_rows(app, "SELECT * FROM corrections") == []


def test_same_observation_corrected_first_conflicts(client, app):
    version_id = upload_and_publish(client)
    oid = observation_id(app, version_id, "东闸断面", "氨氮")

    # Both users open the form at revision 0.
    first = correct(client, version_id, oid, "1.28")
    assert first.status_code == 302

    second = correct(client, version_id, oid, "1.26", "另一个用户")
    assert second.status_code == 409
    body = second.get_data(as_text=True)
    assert "提交冲突" in body
    assert "已被其他人更正" in body
    assert "未更改任何数据" in body

    records = db_rows(
        app, "SELECT * FROM corrections WHERE observation_id = ?", (oid,)
    )
    assert len(records) == 1
    assert records[0]["to_value_text"] == "1.28"
    public = client.get(f"/versions/{version_id}").get_data(as_text=True)
    assert "1.26" not in public


def test_failed_write_leaves_value_and_records_untouched(client, app, monkeypatch):
    version_id = upload_and_publish(client)
    oid = observation_id(app, version_id, "东闸断面", "氨氮")
    assert correct(client, version_id, oid, "1.28").status_code == 302

    from app import db as db_module

    class FailingCommitConnection:
        def __init__(self, inner):
            self._inner = inner

        def commit(self):
            raise sqlite3.OperationalError("simulated commit failure")

        def __getattr__(self, name):
            return getattr(self._inner, name)

    real_connect = db_module.connect

    def failing_connect(database):
        return FailingCommitConnection(real_connect(database))

    monkeypatch.setattr(db_module, "connect", failing_connect)
    response = correct(client, version_id, oid, "1.30", "复核", revision=1)
    monkeypatch.setattr(db_module, "connect", real_connect)

    assert response.status_code == 500
    body = response.get_data(as_text=True)
    assert "没有更改任何数据" in body

    records = db_rows(
        app,
        "SELECT * FROM corrections WHERE observation_id = ? ORDER BY revision_no",
        (oid,),
    )
    assert len(records) == 1
    assert records[0]["to_value_text"] == "1.28"

    public = client.get(f"/versions/{version_id}").get_data(as_text=True)
    assert "1.28" in public
    assert "<strong>1.30</strong>" not in public


def test_failed_first_correction_touches_nothing(client, app, monkeypatch):
    version_id = upload_and_publish(client)
    oid = observation_id(app, version_id, "东闸断面", "氨氮")

    from app import db as db_module

    class FailingCommitConnection:
        def __init__(self, inner):
            self._inner = inner

        def commit(self):
            raise sqlite3.OperationalError("simulated commit failure")

        def __getattr__(self, name):
            return getattr(self._inner, name)

    real_connect = db_module.connect

    def failing_connect(database):
        return FailingCommitConnection(real_connect(database))

    monkeypatch.setattr(db_module, "connect", failing_connect)
    response = correct(client, version_id, oid, "1.30")
    monkeypatch.setattr(db_module, "connect", real_connect)

    assert response.status_code == 500
    assert db_rows(app, "SELECT * FROM corrections") == []
    public = client.get(f"/versions/{version_id}").get_data(as_text=True)
    # Original value displayed, no corrected marker.
    assert "1.24" in public
    assert "1.30" not in public


# ---------------------------------------------------------------------------
# Corrections do not carry over to a re-published version
# ---------------------------------------------------------------------------


def test_republished_version_does_not_inherit_corrections(client, app):
    first = upload_and_publish(client)
    oid = observation_id(app, first, "东闸断面", "氨氮")
    assert correct(client, first, oid, "1.28").status_code == 302
    assert "1.28" in client.get(f"/versions/{first}").get_data(as_text=True)

    second = upload_and_publish(client)

    public = client.get(f"/versions/{second}").get_data(as_text=True)
    assert "1.24" in public
    assert "1.28" not in public
    assert "1.24" in client.get(f"/versions/{second}/download").data.decode("utf-8")
    assert db_rows(app, "SELECT * FROM corrections WHERE version_id = ?", (second,)) == []

    # The old version keeps its own correction record even though superseded.
    old_records = db_rows(
        app, "SELECT * FROM corrections WHERE version_id = ?", (first,)
    )
    assert len(old_records) == 1
    assert int(old_records[0]["observation_id"]) == oid


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_concurrent_corrections_only_one_wins(app):
    application = app
    version_id = upload_and_publish(application.test_client())
    oid = observation_id(application, version_id, "东闸断面", "氨氮")

    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def run(value: str):
        conn = connect(application.config["DATABASE"])
        try:
            barrier.wait()
            outcome = services.record_correction(
                conn,
                version_id=version_id,
                observation_id=oid,
                expected_revision_no=0,
                new_value=float(value),
                new_value_text=value,
                reason=f"并发 {value}",
            )
            with lock:
                outcomes.append(outcome.status)
        finally:
            conn.close()

    threads = [threading.Thread(target=run, args=(v,)) for v in ("1.28", "1.30")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["conflict", "corrected"]
    records = db_rows(
        application, "SELECT * FROM corrections WHERE observation_id = ?", (oid,)
    )
    assert len(records) == 1
    assert records[0]["revision_no"] == 1
    assert records[0]["from_value_text"] == "1.24"


def test_concurrent_correction_and_republish_never_mix(app):
    application = app
    version_id = upload_and_publish(application.test_client())

    conn = connect(application.config["DATABASE"])
    oid = int(
        conn.execute(
            "SELECT id FROM observations WHERE version_id = ? ORDER BY id LIMIT 1",
            (version_id,),
        ).fetchone()["id"]
    )
    conn.close()

    # Create the re-publish candidate first.
    client = application.test_client()
    client.post(
        "/admin/upload",
        data={
            "dataset_date": "2026-11-14",
            "file": (
                io.BytesIO(
                    csv_bytes(
                        ROWS
                        + [("2026-11-14", "西渡断面", "pH", "2026-11-14 14:00:00", "7.28")]
                    )
                ),
                "obs.csv",
            ),
        },
        content_type="multipart/form-data",
    )
    new_version = int(db_rows(application, "SELECT id FROM versions ORDER BY id DESC LIMIT 1")[0]["id"])

    barrier = threading.Barrier(2)
    results: list[str] = []
    lock = threading.Lock()

    def publish_new():
        conn = connect(application.config["DATABASE"])
        try:
            barrier.wait()
            outcome = services.publish_candidate(
                conn, application.config["FILES_DIR"], new_version
            )
            with lock:
                results.append("publish:" + outcome.status)
        finally:
            conn.close()

    def do_correct():
        conn = connect(application.config["DATABASE"])
        try:
            barrier.wait()
            outcome = services.record_correction(
                conn,
                version_id=version_id,
                observation_id=oid,
                expected_revision_no=0,
                new_value=1.28,
                new_value_text="1.28",
                reason="并发更正",
            )
            with lock:
                results.append("correct:" + outcome.status)
        finally:
            conn.close()

    threads = [threading.Thread(target=publish_new), threading.Thread(target=do_correct)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    old = db_rows(application, "SELECT * FROM versions WHERE id = ?", (version_id,))[0]
    new = db_rows(application, "SELECT * FROM versions WHERE id = ?", (new_version,))[0]
    assert new["status"] == "published"
    assert old["status"] == "superseded"

    # Whatever the interleaving, the new version never carries old corrections.
    assert db_rows(
        application, "SELECT * FROM corrections WHERE version_id = ?", (new_version,)
    ) == []
    # The correction either landed on the old version before it was
    # superseded, or was refused as a conflict once it was superseded.
    assert set(results) == {"publish:published", "correct:corrected"} or set(
        results
    ) == {"publish:published", "correct:conflict"}
    old_records = db_rows(
        application, "SELECT * FROM corrections WHERE version_id = ?", (version_id,)
    )
    if "correct:corrected" in results:
        assert len(old_records) == 1
    else:
        assert old_records == []
