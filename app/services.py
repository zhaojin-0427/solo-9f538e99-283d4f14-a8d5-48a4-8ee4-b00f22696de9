"""Application services: candidate creation, publishing and read models."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import storage
from .db import transaction
from .validation import Observation

PER_PAGE_DEFAULT = 100


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Conflict(Exception):
    def __init__(self, newer: sqlite3.Row) -> None:
        self.newer = newer
        super().__init__("该日期已存在更新版本")


@dataclass
class PublishOutcome:
    status: str
    message: str = ""
    version: sqlite3.Row | None = None
    newer: sqlite3.Row | None = None
    error: Exception | None = None


@dataclass
class CorrectionOutcome:
    """Result of a correction attempt.

    ``status`` is one of ``corrected``, ``conflict``, ``unchanged``,
    ``invalid``, ``not_found`` or ``db_error``.  ``conflict_kind``
    distinguishes a re-published day (``version``) from a correction
    submitted by someone else first (``observation``).
    """

    status: str
    message: str = ""
    conflict_kind: str = ""
    expected_revision_no: int = 0
    current_revision_no: int = 0
    correction: sqlite3.Row | None = None
    error: Exception | None = None


def create_candidate(
    conn: sqlite3.Connection,
    dataset_date: date,
    original_filename: str,
    stored: storage.StoredFile,
    observations: Sequence[Observation],
) -> int:
    """Insert a validated candidate version and its observations atomically."""
    now = utc_now()
    with transaction(conn):
        row = conn.execute(
            "SELECT COALESCE(MAX(version_no), 0) + 1 AS next_no "
            "FROM versions WHERE dataset_date = ?",
            (dataset_date.isoformat(),),
        ).fetchone()
        version_no = int(row["next_no"])
        cursor = conn.execute(
            """
            INSERT INTO versions (
                dataset_date, version_no, status, original_filename,
                stored_path, sha256, size_bytes, row_count, created_at
            ) VALUES (?, ?, 'candidate', ?, ?, ?, ?, ?, ?)
            """,
            (
                dataset_date.isoformat(),
                version_no,
                original_filename,
                stored.relative_path,
                stored.sha256,
                stored.size_bytes,
                len(observations),
                now,
            ),
        )
        version_id = int(cursor.lastrowid)
        conn.executemany(
            """
            INSERT INTO observations (
                version_id, row_no, site, metric, observed_at,
                observed_date, value, value_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    version_id,
                    item.row_no,
                    item.site,
                    item.metric,
                    item.observed_at,
                    item.observed_date,
                    item.value,
                    item.value_text,
                )
                for item in observations
            ],
        )
    return version_id


def publish_candidate(
    conn: sqlite3.Connection,
    files_dir: str | Path,
    version_id: int,
    *,
    now: str | None = None,
) -> PublishOutcome:
    """Publish a candidate unless a newer version for its date exists.

    The original file is verified *before* the transaction.  Files are
    content-addressed and immutable, so a verified file cannot change while
    the transaction replaces the current published version.  The update is
    one transaction: either the new version becomes the single published
    version for the date, or nothing changes and the previous published
    version stays fully usable.
    """
    candidate = conn.execute(
        "SELECT * FROM versions WHERE id = ?", (version_id,)
    ).fetchone()
    if candidate is None:
        return PublishOutcome("not_found", "版本不存在")

    if candidate["status"] == "published":
        return PublishOutcome("already_published", "该版本已是当前发布版本", candidate)
    if candidate["status"] != "candidate":
        return PublishOutcome("not_candidate", "该版本不是待发布候选", candidate)

    # A cheap pre-check gives the conflict message priority over file errors.
    newer = _latest_version_for_date(conn, candidate["dataset_date"])
    if newer is not None and newer["version_no"] > candidate["version_no"]:
        return PublishOutcome(
            "conflict",
            "该日期已存在更新版本，旧候选不能发布",
            candidate,
            newer,
        )

    try:
        storage.verify_file(
            files_dir, candidate["stored_path"], candidate["sha256"]
        )
    except storage.StorageError as exc:
        return PublishOutcome("file_error", str(exc), candidate, error=exc)

    timestamp = now or utc_now()
    try:
        with transaction(conn):
            # Re-read under the write lock: another request may have created
            # a newer version or published this candidate meanwhile.
            current = conn.execute(
                "SELECT * FROM versions WHERE id = ?", (version_id,)
            ).fetchone()
            if current is None:
                return PublishOutcome("not_found", "版本不存在")
            if current["status"] == "published":
                return PublishOutcome(
                    "already_published", "该版本已是当前发布版本", current
                )
            if current["status"] != "candidate":
                return PublishOutcome(
                    "not_candidate", "该版本不是待发布候选", current
                )
            latest = _latest_version_for_date(conn, current["dataset_date"])
            if latest is not None and latest["version_no"] > current["version_no"]:
                raise Conflict(latest)

            conn.execute(
                """
                UPDATE versions
                   SET status = 'superseded', superseded_at = ?
                 WHERE dataset_date = ? AND status = 'published'
                """,
                (timestamp, current["dataset_date"]),
            )
            conn.execute(
                """
                UPDATE versions
                   SET status = 'published', published_at = ?, superseded_at = NULL
                 WHERE id = ?
                """,
                (timestamp, version_id),
            )
    except Conflict as exc:
        return PublishOutcome(
            "conflict", "该日期已存在更新版本，旧候选不能发布", candidate, exc.newer
        )
    except sqlite3.Error as exc:
        return PublishOutcome(
            "db_error",
            "数据库提交失败，原有已发布版本保持不变",
            candidate,
            error=exc,
        )

    published = conn.execute(
        "SELECT * FROM versions WHERE id = ?", (version_id,)
    ).fetchone()
    return PublishOutcome(
        "published", "发布成功，版本已对外可见", published
    )


def _latest_version_for_date(
    conn: sqlite3.Connection, dataset_date: str
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM versions
         WHERE dataset_date = ?
         ORDER BY version_no DESC
         LIMIT 1
        """,
        (dataset_date,),
    ).fetchone()


def get_version(conn: sqlite3.Connection, version_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM versions WHERE id = ?", (version_id,)
    ).fetchone()


def get_current_published(
    conn: sqlite3.Connection, dataset_date: str
) -> sqlite3.Row | None:
    """Return the single externally visible version for a date."""
    return conn.execute(
        """
        SELECT * FROM versions
         WHERE dataset_date = ? AND status = 'published'
         ORDER BY version_no DESC
         LIMIT 1
        """,
        (dataset_date,),
    ).fetchone()


def list_published(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM versions
         WHERE status = 'published'
         ORDER BY dataset_date DESC
        """
    ).fetchall()


def list_versions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM versions
         ORDER BY dataset_date DESC, version_no DESC
        """
    ).fetchall()


def build_admin_groups(versions: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for version in versions:
        day = version["dataset_date"]
        group = groups.setdefault(
            day,
            {"dataset_date": day, "versions": [], "latest_no": 0, "published": None},
        )
        group["versions"].append(version)
        group["latest_no"] = max(group["latest_no"], version["version_no"])
        if version["status"] == "published":
            group["published"] = version
    return list(groups.values())


def fetch_observations(
    conn: sqlite3.Connection,
    version_id: int,
    *,
    site: str = "",
    metric: str = "",
    page: int = 1,
    per_page: int = PER_PAGE_DEFAULT,
) -> dict[str, Any]:
    conditions = ["version_id = ?"]
    params: list[Any] = [version_id]
    if site:
        conditions.append("site LIKE ?")
        params.append(f"%{site}%")
    if metric:
        conditions.append("metric LIKE ?")
        params.append(f"%{metric}%")
    where = " AND ".join(conditions)

    total = int(
        conn.execute(
            f"SELECT COUNT(*) FROM observations WHERE {where}", params
        ).fetchone()[0]
    )
    per_page = max(1, min(int(per_page), 500))
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(int(page), pages))
    rows = conn.execute(
        f"""
        SELECT * FROM observations
         WHERE {where}
         ORDER BY site, metric, observed_at, row_no
         LIMIT ? OFFSET ?
        """,
        [*params, per_page, (page - 1) * per_page],
    ).fetchall()
    return {
        "rows": rows,
        "total": total,
        "page": page,
        "pages": pages,
        "per_page": per_page,
        "site": site,
        "metric": metric,
    }


def parse_page(value: str | None) -> int:
    try:
        return max(1, int(value or "1"))
    except (TypeError, ValueError):
        return 1


# ---------------------------------------------------------------------------
# Corrections
# ---------------------------------------------------------------------------


def latest_corrections(
    conn: sqlite3.Connection,
    version_id: int,
    observation_ids: Sequence[int] | None = None,
) -> dict[int, sqlite3.Row]:
    """Return the newest correction for each observation in *version_id*."""
    conditions = ["c.version_id = ?"]
    params: list[Any] = [version_id]
    if observation_ids is not None:
        ids = [int(value) for value in observation_ids]
        if not ids:
            return {}
        conditions.append(
            "c.observation_id IN (%s)" % ", ".join("?" for _ in ids)
        )
        params.extend(ids)
    rows = conn.execute(
        f"""
        SELECT c.*
          FROM corrections c
          JOIN (
                SELECT observation_id, MAX(revision_no) AS revision_no
                  FROM corrections
                 WHERE version_id = ?
                 GROUP BY observation_id
               ) latest
            ON latest.observation_id = c.observation_id
           AND latest.revision_no = c.revision_no
         WHERE {' AND '.join(conditions)}
        """,
        [version_id, *params],
    ).fetchall()
    return {int(row["observation_id"]): row for row in rows}


def correction_history(
    conn: sqlite3.Connection, version_id: int
) -> dict[int, list[sqlite3.Row]]:
    """Return all corrections for a version, oldest revision first per row."""
    rows = conn.execute(
        """
        SELECT * FROM corrections
         WHERE version_id = ?
         ORDER BY observation_id, revision_no
        """,
        (version_id,),
    ).fetchall()
    history: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        history.setdefault(int(row["observation_id"]), []).append(row)
    return history


def current_value(observation: sqlite3.Row, correction: sqlite3.Row | None):
    """Return ``(effective_value, effective_value_text)`` for a row."""
    if correction is None:
        return observation["value"], observation["value_text"]
    return correction["to_value"], correction["to_value_text"]


def effective_revision_no(
    observation: sqlite3.Row, correction: sqlite3.Row | None
) -> int:
    return int(correction["revision_no"]) if correction is not None else 0


def correction_form_options(
    conn: sqlite3.Connection, version: sqlite3.Row
) -> dict[str, Any]:
    """Choose a deterministic default observation for the correction form.

    The first (by the public detail ordering) observation that has no
    correction yet is preferred, so repeated corrections walk through the
    dataset; otherwise the first observation is used.
    """
    observations = conn.execute(
        """
        SELECT * FROM observations
         WHERE version_id = ?
         ORDER BY site, metric, observed_at, row_no
        """,
        (version["id"],),
    ).fetchall()
    latest = latest_corrections(conn, int(version["id"]))
    selected = next(
        (row for row in observations if int(row["id"]) not in latest),
        observations[0] if observations else None,
    )
    return {"observations": observations, "selected": selected}


def record_correction(
    conn: sqlite3.Connection,
    *,
    version_id: int,
    observation_id: int,
    expected_revision_no: int,
    new_value: float,
    new_value_text: str,
    reason: str,
    now: str | None = None,
) -> CorrectionOutcome:
    """Append a correction to a published version atomically.

    Concurrency and re-publish guards are checked *inside* the write
    transaction:

    * the version must still be the single published version for its date
      (if the day was re-published meanwhile, the version is ``superseded``
      and the correction is refused);
    * the observation must still belong to that version;
    * the observation must not already have a newer correction than the
      revision the form showed (optimistic locking on ``revision_no``).

    The observation row — and therefore the original CSV value — is never
    modified.  The current effective value is always derived from the newest
    correction, so there is only one write: a failed insert leaves neither a
    new value nor a new record behind.
    """
    reason = (reason or "").strip()
    if not reason:
        return CorrectionOutcome("invalid", "请填写更正原因。")
    if len(reason) > 500:
        return CorrectionOutcome("invalid", "更正原因不能超过 500 个字符。")

    timestamp = now or utc_now()
    try:
        with transaction(conn):
            version = conn.execute(
                "SELECT * FROM versions WHERE id = ?", (version_id,)
            ).fetchone()
            if version is None:
                return CorrectionOutcome("not_found", "版本不存在。")
            if version["status"] != "published":
                return CorrectionOutcome(
                    "conflict",
                    "该版本已不再是对外发布版本，可能该日期已重新发布；"
                    "请在最新发布版本上重新更正。",
                    conflict_kind="version",
                )

            observation = conn.execute(
                "SELECT * FROM observations WHERE id = ? AND version_id = ?",
                (observation_id, version_id),
            ).fetchone()
            if observation is None:
                return CorrectionOutcome(
                    "conflict",
                    "所选观测不属于该发布版本；请刷新页面后重新选择。",
                    conflict_kind="observation",
                )

            # Timestamps are second-granularity; the unique
            # (observation_id, revision_no) key is the real guard against a
            # duplicate revision under concurrency.
            row = conn.execute(
                """
                SELECT COALESCE(MAX(revision_no), 0) AS revision_no
                  FROM corrections
                 WHERE observation_id = ?
                """,
                (observation_id,),
            ).fetchone()
            current_revision = int(row["revision_no"])
            if current_revision != int(expected_revision_no):
                return CorrectionOutcome(
                    "conflict",
                    "该观测已被其他人更正；请刷新页面确认后再提交。",
                    conflict_kind="observation",
                    expected_revision_no=int(expected_revision_no),
                    current_revision_no=current_revision,
                )

            previous = latest_corrections(
                conn, version_id, [observation_id]
            ).get(observation_id)
            from_value, from_value_text = current_value(observation, previous)
            if float(new_value) == float(from_value):
                return CorrectionOutcome(
                    "unchanged",
                    "新数值与现用值相同，无需更正。",
                    current_revision_no=current_revision,
                )

            cursor = conn.execute(
                """
                INSERT INTO corrections (
                    version_id, observation_id, revision_no,
                    from_value, from_value_text, to_value, to_value_text,
                    reason, corrected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    observation_id,
                    current_revision + 1,
                    from_value,
                    from_value_text,
                    new_value,
                    new_value_text,
                    reason,
                    timestamp,
                ),
            )
            correction = conn.execute(
                "SELECT * FROM corrections WHERE id = ?",
                (int(cursor.lastrowid),),
            ).fetchone()
    except sqlite3.Error as exc:
        return CorrectionOutcome(
            "db_error",
            "更正写入失败，未作任何修改；原值与已有更正记录保持不变。",
            error=exc,
        )

    return CorrectionOutcome(
        "corrected", "更正已保存，当前对外的现用值已更新。", correction=correction
    )


def observations_with_corrections(
    conn: sqlite3.Connection,
    version_id: int,
    detail: dict[str, Any],
) -> list[dict[str, Any]]:
    """Decorate a page of observations with effective values and history."""
    ids = [int(row["id"]) for row in detail["rows"]]
    latest = latest_corrections(conn, version_id, ids)
    history = correction_history(conn, version_id)
    decorated: list[dict[str, Any]] = []
    for row in detail["rows"]:
        key = int(row["id"])
        correction = latest.get(key)
        decorated.append(
            {
                "row": row,
                "original_text": row["value_text"],
                "current_text": (
                    correction["to_value_text"]
                    if correction is not None
                    else row["value_text"]
                ),
                "revision_no": (
                    int(correction["revision_no"]) if correction is not None else 0
                ),
                "corrected": correction is not None,
                "history": history.get(key, []),
            }
        )
    return decorated


def version_correction_summary(
    conn: sqlite3.Connection, version_id: int
) -> dict[str, Any]:
    """Return counts and the full correction ledger for a version."""
    records = conn.execute(
        """
        SELECT c.*, o.site, o.metric, o.observed_at, o.row_no
          FROM corrections c
          JOIN observations o ON o.id = c.observation_id
         WHERE c.version_id = ?
         ORDER BY c.corrected_at, c.id
        """,
        (version_id,),
    ).fetchall()
    affected = {int(item["observation_id"]) for item in records}
    return {
        "records": records,
        "record_count": len(records),
        "affected_count": len(affected),
    }
