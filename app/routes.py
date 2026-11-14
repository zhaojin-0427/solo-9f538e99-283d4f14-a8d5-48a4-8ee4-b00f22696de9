"""HTTP routes for the admin console and the public viewer."""

from __future__ import annotations

import hmac
import secrets
import sqlite3
from datetime import date
from pathlib import Path

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from . import services, storage
from .db import get_db
from .validation import parse_correction_value, validate_file

public_bp = Blueprint("public", __name__)
admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def _parse_dataset_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


def _csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def _check_csrf() -> None:
    if not current_app.config.get("CSRF_ENABLED", True):
        return
    expected = session.get("_csrf_token", "")
    supplied = request.form.get("csrf_token", "")
    if not expected or not hmac.compare_digest(expected, supplied):
        abort(400, description="表单已过期，请刷新页面后重试。")


@public_bp.get("/healthz")
def healthz():
    conn = get_db()
    conn.execute("SELECT 1").fetchone()
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Public viewer
# ---------------------------------------------------------------------------


@public_bp.get("/")
def index():
    conn = get_db()
    versions = services.list_published(conn)
    return render_template("public/index.html", versions=versions)


@public_bp.get("/datasets/<dataset_date>")
def dataset(dataset_date: str):
    conn = get_db()
    version = services.get_current_published(conn, dataset_date)
    if version is None:
        abort(404)
    return redirect(url_for("public.version_detail", version_id=version["id"]))


@public_bp.get("/versions/<int:version_id>")
def version_detail(version_id: int):
    conn = get_db()
    version = services.get_version(conn, version_id)
    if version is None or version["status"] != "published":
        abort(404)
    site = request.args.get("site", "").strip()
    metric = request.args.get("metric", "").strip()
    page = services.parse_page(request.args.get("page"))
    detail = services.fetch_observations(
        conn,
        version_id,
        site=site,
        metric=metric,
        page=page,
        per_page=current_app.config["PER_PAGE"],
    )
    rows = services.observations_with_corrections(conn, version_id, detail)
    summary = services.version_correction_summary(conn, version_id)
    return render_template(
        "public/version_detail.html",
        version=version,
        detail=detail,
        rows=rows,
        summary=summary,
    )


@public_bp.get("/versions/<int:version_id>/download")
def download(version_id: int):
    conn = get_db()
    version = services.get_version(conn, version_id)
    if version is None or version["status"] != "published":
        abort(404)
    try:
        path = storage.verify_file(
            current_app.config["FILES_DIR"],
            version["stored_path"],
            version["sha256"],
        )
    except storage.StorageError:
        abort(503, description="原始文件暂时不可用，请稍后重试。")
    return send_file(
        path,
        mimetype="text/csv",
        as_attachment=True,
        download_name=version["original_filename"],
        conditional=True,
    )


# ---------------------------------------------------------------------------
# Admin console
# ---------------------------------------------------------------------------


@admin_bp.get("/")
def dashboard():
    conn = get_db()
    versions = services.list_versions(conn)
    groups = services.build_admin_groups(versions)
    default_date = current_app.config.get("ADMIN_DATASET_DATE") or date.today().isoformat()
    return render_template(
        "admin/dashboard.html", groups=groups, default_date=default_date
    )


@admin_bp.post("/upload")
def upload():
    _check_csrf()
    dataset_date = _parse_dataset_date(request.form.get("dataset_date"))
    if dataset_date is None:
        flash("请选择有效的发布日期。", "error")
        return redirect(url_for("admin.dashboard"))

    upload_file = request.files.get("file")
    if upload_file is None or not upload_file.filename:
        flash("请选择要上传的 CSV 文件。", "error")
        return redirect(url_for("admin.dashboard"))

    original_name = Path(upload_file.filename).name
    # Keep the display/download name bounded and free of control characters.
    original_name = "".join(
        ch for ch in original_name if ch.isprintable() and ch not in "\\/"
    )[:200] or "observations.csv"
    if not original_name.lower().endswith((".csv", ".txt")):
        flash("仅支持 CSV 文件。", "error")
        return redirect(url_for("admin.dashboard"))

    files_dir = current_app.config["FILES_DIR"]
    temp_path = storage.new_temp_path(files_dir)
    try:
        upload_file.save(temp_path)
    except OSError:
        storage.discard_temp(temp_path)
        flash("上传文件保存失败，请重试。", "error")
        return redirect(url_for("admin.dashboard"))

    result = validate_file(temp_path, dataset_date)
    if not result.ok:
        storage.discard_temp(temp_path)
        return (
            render_template(
                "admin/upload_result.html",
                accepted=False,
                dataset_date=dataset_date.isoformat(),
                filename=original_name,
                result=result,
            ),
            422,
        )

    try:
        stored = storage.store_file(
            temp_path, files_dir, dataset_date, original_name
        )
    except storage.StorageError as exc:
        storage.discard_temp(temp_path)
        flash(str(exc), "error")
        return redirect(url_for("admin.dashboard"))

    conn = get_db()
    try:
        version_id = services.create_candidate(
            conn,
            dataset_date,
            original_name,
            stored,
            result.observations,
        )
    except sqlite3.Error:
        current_app.logger.exception("candidate insert failed")
        flash("数据库写入失败，候选未创建；已发布版本不受影响。", "error")
        return redirect(url_for("admin.dashboard"))

    flash(
        f"校验通过：{len(result.observations)} 条明细，已形成待发布候选。",
        "success",
    )
    return redirect(url_for("admin.candidate_detail", version_id=version_id))


@admin_bp.get("/versions/<int:version_id>")
def candidate_detail(version_id: int):
    conn = get_db()
    version = services.get_version(conn, version_id)
    if version is None:
        abort(404)
    page = services.parse_page(request.args.get("page"))
    detail = services.fetch_observations(
        conn,
        version_id,
        page=page,
        per_page=current_app.config["PER_PAGE"],
    )
    latest = conn.execute(
        """
        SELECT MAX(version_no) AS latest_no FROM versions WHERE dataset_date = ?
        """,
        (version["dataset_date"],),
    ).fetchone()["latest_no"]
    newer = conn.execute(
        "SELECT * FROM versions WHERE dataset_date = ? ORDER BY version_no DESC LIMIT 1",
        (version["dataset_date"],),
    ).fetchone()
    current_published = services.get_current_published(conn, version["dataset_date"])
    conflict = latest is not None and int(latest) > version["version_no"]
    rows = services.observations_with_corrections(conn, version_id, detail)
    summary = services.version_correction_summary(conn, version_id)
    return render_template(
        "admin/candidate_detail.html",
        version=version,
        detail=detail,
        rows=rows,
        summary=summary,
        conflict=conflict,
        newer=newer,
        current_published=current_published,
    )


@admin_bp.post("/versions/<int:version_id>/publish")
def publish(version_id: int):
    _check_csrf()
    conn = get_db()
    outcome = services.publish_candidate(
        conn, current_app.config["FILES_DIR"], version_id
    )
    if outcome.status == "published":
        flash(outcome.message, "success")
        return redirect(
            url_for("public.version_detail", version_id=version_id)
        )
    if outcome.status == "conflict":
        flash(
            f"{outcome.message}：已存在版本 v{outcome.newer['version_no']}，"
            "请改为发布该日期的最新候选。",
            "error",
        )
        return redirect(url_for("admin.candidate_detail", version_id=version_id))
    if outcome.status == "already_published":
        flash(outcome.message, "info")
        return redirect(url_for("public.version_detail", version_id=version_id))
    if outcome.status == "not_found":
        abort(404)

    flash(outcome.message or "发布失败。", "error")
    return redirect(url_for("admin.candidate_detail", version_id=version_id))


@admin_bp.get("/versions/<int:version_id>/download")
def admin_download(version_id: int):
    conn = get_db()
    version = services.get_version(conn, version_id)
    if version is None:
        abort(404)
    try:
        path = storage.verify_file(
            current_app.config["FILES_DIR"],
            version["stored_path"],
            version["sha256"],
        )
    except storage.StorageError as exc:
        abort(503, description=str(exc))
    return send_file(
        path,
        mimetype="text/csv",
        as_attachment=True,
        download_name=version["original_filename"],
        conditional=True,
    )


# ---------------------------------------------------------------------------
# Corrections (admin)
# ---------------------------------------------------------------------------


def _correction_payload(conn, version, observation_id: int | None):
    """Build the context for the correction page/form."""
    options = services.correction_form_options(conn, version)
    observations = options["observations"]
    selected = options["selected"]
    if observation_id is not None:
        selected = next(
            (row for row in observations if int(row["id"]) == int(observation_id)),
            None,
        )
    latest = services.latest_corrections(conn, int(version["id"]))
    history = services.correction_history(conn, int(version["id"]))
    rows = []
    for row in observations:
        key = int(row["id"])
        correction = latest.get(key)
        value, value_text = services.current_value(row, correction)
        rows.append(
            {
                "row": row,
                "current_text": value_text,
                "revision_no": services.effective_revision_no(row, correction),
                "history": history.get(key, []),
            }
        )
    selected_row = None
    if selected is not None:
        key = int(selected["id"])
        correction = latest.get(key)
        current_value, current_text = services.current_value(selected, correction)
        selected_row = {
            "row": selected,
            "current_value": current_value,
            "current_text": current_text,
            "revision_no": services.effective_revision_no(selected, correction),
            "history": history.get(key, []),
        }
    return {"observations": rows, "selected": selected_row}


@admin_bp.get("/versions/<int:version_id>/correct")
def correction_page(version_id: int):
    conn = get_db()
    version = services.get_version(conn, version_id)
    if version is None:
        abort(404)
    if version["status"] != "published":
        current = services.get_current_published(conn, version["dataset_date"])
        flash(
            "该版本已不是当前发布版本，可能该日期已重新发布；"
            "请在最新发布版本上更正数值。",
            "error",
        )
        if current is None:
            abort(404)
        return redirect(
            url_for("admin.correction_page", version_id=current["id"])
        )
    observation_id = request.args.get("observation_id", type=int)
    context = _correction_payload(conn, version, observation_id)
    return render_template(
        "admin/correct.html",
        version=version,
        **context,
        conflict=None,
        value_input=request.args.get("value", ""),
        reason_input="",
    )


@admin_bp.post("/versions/<int:version_id>/correct")
def correct(version_id: int):
    _check_csrf()
    conn = get_db()
    version = services.get_version(conn, version_id)
    if version is None:
        abort(404)

    observation_id = request.form.get("observation_id", type=int)
    reason_raw = request.form.get("reason", "")
    value_raw = request.form.get("value", "")
    try:
        expected_revision_no = int(request.form.get("expected_revision_no", "0"))
    except ValueError:
        expected_revision_no = 0

    def render_form(status: int, *, notice=None, conflict=None):
        context = _correction_payload(conn, version, observation_id)
        return (
            render_template(
                "admin/correct.html",
                version=version,
                **context,
                conflict=conflict,
                notice=notice,
                value_input=value_raw,
                reason_input=reason_raw,
            ),
            status,
        )

    if version["status"] != "published":
        current = services.get_current_published(conn, version["dataset_date"])
        conflict = {
            "kind": "version",
            "message": (
                "该版本已不再是对外发布版本：该日期可能已在别处重新发布。"
                "本次提交未更改任何数据。"
            ),
            "current": current,
        }
        return render_form(409, conflict=conflict)

    if observation_id is None:
        return render_form(400, notice="请从该发布版本中选择一条观测。")

    try:
        new_value, new_value_text = parse_correction_value(value_raw)
    except ValueError as exc:
        return render_form(400, notice=f"新数值无效：{exc}")

    context = _correction_payload(conn, version, observation_id)
    if context["selected"] is None:
        return render_form(
            409,
            conflict={
                "kind": "observation",
                "message": "所选观测不属于该发布版本，本次提交未更改任何数据。",
                "current": None,
            },
        )
    # Only report "unchanged" when the form is still current; otherwise a
    # stale form must surface the conflict even if its value now matches.
    if (
        expected_revision_no == context["selected"]["revision_no"]
        and new_value == context["selected"]["current_value"]
    ):
        return render_form(400, notice="新数值与现用值相同，无需更正。")

    outcome = services.record_correction(
        conn,
        version_id=version_id,
        observation_id=observation_id,
        expected_revision_no=expected_revision_no,
        new_value=new_value,
        new_value_text=new_value_text,
        reason=reason_raw,
    )

    if outcome.status == "corrected":
        flash(
            "更正已保存：公开明细的现用值已更新，原值、更正记录与原始文件均保留。",
            "success",
        )
        return redirect(
            url_for("public.version_detail", version_id=version_id)
        )
    if outcome.status == "not_found":
        abort(404)
    if outcome.status == "db_error":
        current_app.logger.error(
            "correction commit failed", exc_info=outcome.error
        )
        return render_form(
            500,
            notice=(
                "更正写入失败，本次提交没有更改任何数据：现用值与更正记录均保持原状，"
                "原始文件不受影响。请稍后重试。"
            ),
        )
    if outcome.status == "unchanged":
        return render_form(400, notice=outcome.message)
    if outcome.status == "conflict":
        current_version = services.get_current_published(
            conn, version["dataset_date"]
        )
        return render_form(
            409,
            conflict={
                "kind": outcome.conflict_kind,
                "message": outcome.message + "本次提交未更改任何数据。",
                "current": current_version,
                "current_revision_no": outcome.current_revision_no,
            },
        )
    return render_form(400, notice=outcome.message or "更正未保存。")

