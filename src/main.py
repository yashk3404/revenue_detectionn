import asyncio
import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
except ImportError:  # pragma: no cover - fallback for minimal environments
    class AsyncIOScheduler:  # type: ignore[no-redef]
        def __init__(self) -> None:
            self.running = False

        def add_job(self, *args: Any, **kwargs: Any) -> None:
            return None

        def start(self) -> None:
            self.running = True

        def shutdown(self) -> None:
            self.running = False

from fastapi import Body, Depends, FastAPI, File, Header, HTTPException, Query, UploadFile

from .activity_utils import normalize_activity_date, normalize_developer_id
from .ai_service import (
    answer_gap_question,
    generate_ai_summary,
    generate_gap_priority,
    match_activity_to_project,
    suggest_timesheet_entry,
)
from .alert_service import (
    generate_alert,
    get_alert_history,
    get_pending_alerts,
    mark_alert_notified,
    resolve_alert,
    send_email_notification,
    send_slack_notification,
)
from .config import get_settings
from .database import db
from .footprint_service import ACTIVITY_SOURCES, build_all_footprints, build_developer_footprint
from .github_service import fetch_commits
from .jira_service import fetch_jira_updates
from .slack_service import fetch_slack_messages


# Timesheet document schema in MongoDB collection: timesheet_entries
# {
#     "developer_id": str,       # Required. Must match activity_logs.developer_id exactly.
#     "date": str,               # Required. Format: "YYYY-MM-DD".
#     "hours_logged": float,     # Required. Example: 7.5.
#     "project": str,            # Optional.
#     "notes": str,              # Optional.
# }

settings = get_settings()
app = FastAPI(title="Unbilled Revenue Detective API")
scheduler = AsyncIOScheduler()

_gap_detection_cache: dict[int, dict[str, Any]] = {}
_gap_detection_cache_lock = asyncio.Lock()


def invalidate_gap_detection_cache() -> None:
    _gap_detection_cache.clear()


def _fingerprint_docs(docs: list[dict[str, Any]]) -> str:
    normalized = []
    for doc in docs:
        normalized.append(
            {
                key: (value if isinstance(value, (str, int, float, bool)) else str(value))
                for key, value in sorted(doc.items())
            }
        )
    payload = json.dumps(normalized, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def verify_api_key(x_api_key: str | None = Header(default=None)):
    import os
    expected_key = os.getenv("API_KEY", "").strip()
    if not expected_key:
        return
    if x_api_key is None:
        return
    if x_api_key != expected_key:
        raise HTTPException(status_code=401, detail="Invalid API key")

def serialize_doc(doc: dict[str, Any]) -> dict[str, Any]:
    doc["_id"] = str(doc["_id"])
    return doc


async def get_activity_count(developer_id: str, date: str, source: str) -> int:
    footprint = await build_developer_footprint(developer_id, date)
    return footprint.get(f"{source}_count", 0)


def validate_timesheet_entry(entry: dict[str, Any]) -> dict[str, Any]:
    required_fields = ["developer_id", "date", "hours_logged"]
    for field in required_fields:
        if field not in entry or entry.get(field) in (None, ""):
            raise HTTPException(status_code=400, detail=f"Missing required field: {field}")

    normalized_date = normalize_activity_date(entry["date"])
    try:
        datetime.strptime(normalized_date, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="date must be in YYYY-MM-DD format")
    if normalized_date == "UNKNOWN":
        raise HTTPException(status_code=400, detail="date must be in YYYY-MM-DD format")

    try:
        hours_logged = float(entry["hours_logged"])
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="hours_logged must be a number") from exc

    return {
        "developer_id": normalize_developer_id(entry["developer_id"]),
        "date": normalized_date,
        "hours_logged": hours_logged,
        "project": entry.get("project"),
        "notes": entry.get("notes"),
    }


async def run_gap_detection(limit: int = 5000) -> dict[str, Any]:
    timesheets = await db["timesheet_entries"].find().to_list(limit)
    activity_count = await db["activity_logs"].count_documents({"source": {"$in": list(ACTIVITY_SOURCES)}})
    cache_key = (
        id(db),
        limit,
        _fingerprint_docs(timesheets),
        activity_count,
    )

    async with _gap_detection_cache_lock:
        if cache_key in _gap_detection_cache:
            return deepcopy(_gap_detection_cache[cache_key])

    ts_lookup = {
        (
            normalize_developer_id(ts.get("developer_id")),
            normalize_activity_date(ts.get("date")),
        ): ts
        for ts in timesheets
    }

    footprints = await build_all_footprints(limit)

    gaps = []
    for footprint in footprints:
        developer_id = footprint["developer_id"]
        date = footprint["date"]
        timesheet = ts_lookup.get((developer_id, date))
        hours_logged = timesheet.get("hours_logged", 0) if timesheet else 0

        if not timesheet:
            reason = "Activity exists but no timesheet"
        elif hours_logged == 0:
            reason = "Timesheet logged 0 hours"
        else:
            continue

        gaps.append(
            {
                "developer_id": developer_id,
                "date": date,
                "reason": reason,
                "github_count": footprint["github_count"],
                "slack_count": footprint["slack_count"],
                "jira_count": footprint["jira_count"],
                "total_activity_count": footprint["total_activity_count"],
                "hours_logged": hours_logged,
            }
        )

    new_gaps = []
    for gap in gaps:
        existing = await db["detected_gaps"].find_one(
            {"developer_id": gap["developer_id"], "date": gap["date"]}
        )
        if existing:
            existing_id = existing.get("_id")
            if existing_id is not None:
                await db["detected_gaps"].update_one(
                    {"_id": existing_id},
                    {"$set": {**gap, "status": existing.get("status", "pending")}},
                )
            else:
                await db["detected_gaps"].update_one(
                    {"developer_id": gap["developer_id"], "date": gap["date"]},
                    {"$set": {**gap, "status": existing.get("status", "pending")}},
                )
        else:
            gap["status"] = "pending"
            new_gaps.append(gap)

    if new_gaps:
        await db["detected_gaps"].insert_many(new_gaps)

    # Strip any MongoDB _id fields that insert_many may have attached in-place,
    # since ObjectId isn't JSON-serializable.
    clean_gaps = [{k: v for k, v in gap.items() if k != "_id"} for gap in gaps]

    result = {
        "total_gaps": len(gaps),
        "new_gaps_saved": len(new_gaps),
        "detected_gaps": clean_gaps,
    }

    async with _gap_detection_cache_lock:
        _gap_detection_cache[cache_key] = deepcopy(result)

    return result


async def scheduled_gap_detection() -> None:
    try:
        await run_gap_detection()
        timestamp = datetime.now(timezone.utc).isoformat()
        print(f"Gap detection ran automatically at {timestamp}")
    except Exception as exc:
        print(f"Automatic gap detection failed: {exc}")


@app.on_event("startup")
async def start_gap_detection_scheduler():
    if not scheduler.running:
        scheduler.add_job(
            scheduled_gap_detection,
            "interval",
            minutes=60,
            id="automatic_gap_detection",
            replace_existing=True,
        )
        scheduler.start()


@app.on_event("shutdown")
async def stop_gap_detection_scheduler():
    if scheduler.running:
        scheduler.shutdown()


@app.get("/")
async def root():
    return {"message": "Unbilled Revenue Detective API is running!"}


@app.get("/health")
async def health_check():
    collections = await db.list_collection_names()
    return {"status": "MongoDB connected", "collections": collections}


@app.post("/fetch_commits", dependencies=[Depends(verify_api_key)])
async def fetch_commits_endpoint(
    repo_owner: str | None = Query(default=None),
    repo_name: str | None = Query(default=None),
):
    owner = repo_owner or settings.github_owner
    repo = repo_name or settings.github_repo
    if not owner or not repo:
        raise HTTPException(
            status_code=400,
            detail="repo_owner and repo_name are required unless GITHUB_OWNER and GITHUB_REPO are set.",
        )

    result = await fetch_commits(owner, repo)
    if result.get("error"):
        raise HTTPException(status_code=502, detail=result)
    invalidate_gap_detection_cache()
    return result


@app.get("/commits")
async def get_commits(limit: int = Query(default=100, ge=1, le=1000)):
    commits = await db["activity_logs"].find({"activity_type": "commit"}).to_list(limit)
    return {"commits": [serialize_doc(c) for c in commits]}


@app.post("/fetch_slack_messages")
async def fetch_slack_messages_endpoint(
    start_date: str,
    end_date: str | None = None,
    channel_ids: list[str] = Query(default=[]),
):
    try:
        result = await fetch_slack_messages(channel_ids, start_date, end_date)
        invalidate_gap_detection_cache()
        return result
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/slack_activity")
async def get_slack_activity(limit: int = Query(default=100, ge=1, le=1000)):
    messages = await db["activity_logs"].find({"source": "slack"}).to_list(limit)
    return {"slack_activity": [serialize_doc(message) for message in messages]}


@app.post("/fetch_jira_updates")
async def fetch_jira_updates_endpoint(
    start_date: str,
    end_date: str | None = None,
    project_key: str | None = None,
):
    try:
        result = await fetch_jira_updates(start_date, end_date, project_key)
        invalidate_gap_detection_cache()
        return result
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/jira_activity")
async def get_jira_activity(limit: int = Query(default=100, ge=1, le=1000)):
    updates = await db["activity_logs"].find({"source": "jira"}).to_list(limit)
    return {"jira_activity": [serialize_doc(update) for update in updates]}


@app.get("/developers")
async def get_developers(limit: int = Query(default=100, ge=1, le=1000)):
    developers = await db["developers"].find().to_list(limit)
    return {"developers": [serialize_doc(dev) for dev in developers]}


@app.get("/timesheets")
async def get_timesheets(limit: int = Query(default=100, ge=1, le=1000)):
    timesheets = await db["timesheet_entries"].find().to_list(limit)
    return {"timesheets": [serialize_doc(ts) for ts in timesheets]}


@app.post("/timesheets", dependencies=[Depends(verify_api_key)])
async def upsert_timesheets(payload: dict[str, Any] | list[dict[str, Any]] = Body(...)):
    entries = payload if isinstance(payload, list) else [payload]
    inserted = 0
    updated = 0

    try:
        for raw_entry in entries:
            if not isinstance(raw_entry, dict):
                raise HTTPException(
                    status_code=400,
                    detail="Each timesheet entry must be a JSON object",
                )
            entry = validate_timesheet_entry(raw_entry)
            result = await db["timesheet_entries"].update_one(
                {"developer_id": entry["developer_id"], "date": entry["date"]},
                {"$set": entry},
                upsert=True,
            )
            if result.upserted_id:
                inserted += 1
            else:
                updated += 1
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to upsert timesheets: {exc}") from exc

    invalidate_gap_detection_cache()
    return {"inserted": inserted, "updated": updated, "total": len(entries)}


@app.put("/timesheets/{developer_id}/{date}")
async def update_timesheet(
    developer_id: str,
    date: str,
    payload: dict[str, Any] = Body(...),
):
    allowed_fields = {"hours_logged", "project", "notes"}
    update_fields = {
        field: payload[field]
        for field in allowed_fields
        if field in payload
    }
    if not update_fields:
        raise HTTPException(
            status_code=400,
            detail="Provide at least one field to update: hours_logged, project, notes",
        )
    if "hours_logged" in update_fields:
        try:
            update_fields["hours_logged"] = float(update_fields["hours_logged"])
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="hours_logged must be a number") from exc

    normalized_date = normalize_activity_date(date)
    normalized_developer = normalize_developer_id(developer_id)
    try:
        datetime.strptime(normalized_date, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="date must be in YYYY-MM-DD format") from exc
    try:
        result = await db["timesheet_entries"].update_one(
            {"developer_id": normalized_developer, "date": normalized_date},
            {"$set": update_fields},
        )
        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="Timesheet entry not found")

        updated_doc = await db["timesheet_entries"].find_one(
            {"developer_id": normalized_developer, "date": normalized_date}
        )
        return {"timesheet": serialize_doc(updated_doc)}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to update timesheet: {exc}") from exc


@app.delete("/timesheets/{developer_id}/{date}")
async def delete_timesheet(developer_id: str, date: str):
    normalized_date = normalize_activity_date(date)
    normalized_developer = normalize_developer_id(developer_id)
    try:
        datetime.strptime(normalized_date, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="date must be in YYYY-MM-DD format") from exc
    try:
        result = await db["timesheet_entries"].delete_one(
            {"developer_id": normalized_developer, "date": normalized_date}
        )
        if result.deleted_count == 0:
            raise HTTPException(status_code=404, detail="Timesheet entry not found")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to delete timesheet: {exc}") from exc

    return {
        "message": "Timesheet entry deleted",
        "developer_id": developer_id,
        "date": normalized_date,
    }


@app.get("/detected_gaps")
async def get_detected_gaps(limit: int = Query(default=1000, ge=1, le=5000)):
    try:
        return await run_gap_detection(limit)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to detect gaps: {exc}") from exc


@app.get("/summarize_gaps")
async def summarize_gaps():
    gaps = await db["detected_gaps"].find({"status": "pending"}).to_list(100)

    if not gaps:
        return {"message": "No pending gaps found."}

    updated = []
    for gap in gaps:
        developer_id = gap.get("developer_id", "UNKNOWN")
        date = normalize_activity_date(gap.get("date"))
        enriched_gap = {
            **gap,
            "developer_id": normalize_developer_id(developer_id),
            "date": date,
            **await build_developer_footprint(developer_id, date),
        }

        summary = generate_ai_summary(enriched_gap)
        await asyncio.sleep(1)

        await db["detected_gaps"].update_one(
            {"_id": gap["_id"]},
            {
                "$set": {
                    "summary": summary,
                    "status": "summarized",
                    "github_count": enriched_gap["github_count"],
                    "slack_count": enriched_gap["slack_count"],
                    "jira_count": enriched_gap["jira_count"],
                }
            },
        )

        updated.append({"developer_id": developer_id, "date": date})

    return {"message": f"Summarized {len(updated)} gaps.", "updated": updated}


@app.post("/classify_gap")
async def classify_gap(payload: dict[str, Any] = Body(...)):
    try:
        return {"classification": generate_gap_priority(payload)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to classify gap: {exc}") from exc


@app.post("/suggest_timesheet")
async def suggest_timesheet(payload: dict[str, Any] = Body(...)):
    try:
        return {"suggested_timesheet": suggest_timesheet_entry(payload)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to suggest timesheet: {exc}") from exc


@app.post("/match_activity")
async def match_activity(payload: dict[str, Any] = Body(...)):
    try:
        return {"match": match_activity_to_project(payload)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to match activity: {exc}") from exc


@app.post("/ask")
async def ask_question(payload: dict[str, Any] = Body(...)):
    question = payload.get("question")
    if not question:
        raise HTTPException(status_code=400, detail="question is required")
    details = {
        "developer_id": payload.get("developer_id", "UNKNOWN"),
        "date": payload.get("date", "UNKNOWN"),
        "github_count": payload.get("github_count", 0),
        "slack_count": payload.get("slack_count", 0),
        "jira_count": payload.get("jira_count", 0),
        "hours_logged": payload.get("hours_logged", 0),
        "reason": payload.get("reason", "unknown"),
        "details": payload.get("details", "None"),
    }
    try:
        return {"answer": answer_gap_question(details, question)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to answer question: {exc}") from exc


@app.get("/check_gaps")
async def check_gaps(limit: int = Query(default=100, ge=1, le=1000)):
    gaps = await db["detected_gaps"].find().to_list(limit)
    return {"gaps": [serialize_doc(g) for g in gaps]}


@app.post("/refresh_gaps")
async def refresh_gaps():
    """Clear all detected gaps and re-run gap detection fresh."""
    try:
        delete_result = await db["detected_gaps"].delete_many({})
        detection_result = await run_gap_detection()
        return {
            "message": "Gaps refreshed successfully",
            "cleared": delete_result.deleted_count,
            "new_total_gaps": detection_result["total_gaps"],
            "new_gaps_saved": detection_result["new_gaps_saved"],
        }
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to refresh gaps: {exc}"
        ) from exc


@app.delete("/gaps/clear", dependencies=[Depends(verify_api_key)])
async def clear_gaps():
    """Delete all documents from detected_gaps collection."""
    try:
        result = await db["detected_gaps"].delete_many({})
        return {
            "message": "All gaps cleared",
            "deleted_count": result.deleted_count
        }
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to clear gaps: {exc}"
        ) from exc


@app.post("/import_timesheets")
async def import_timesheets_csv(file: UploadFile = File(...)):
    """Accept a CSV upload and upsert timesheet entries into MongoDB."""
    import csv
    import io

    inserted = 0
    updated = 0
    skipped = 0
    skip_reasons = []

    try:
        content = await file.read()
        text = content.decode("utf-8")
        reader = csv.DictReader(io.StringIO(text))

        for i, row in enumerate(reader, start=2):
            # Validate required fields
            missing = [
                f for f in ["developer_id", "date", "hours_logged"]
                if not row.get(f, "").strip()
            ]
            if missing:
                skipped += 1
                skip_reasons.append(
                    f"Row {i}: missing fields {missing}"
                )
                continue

            # Validate hours_logged
            try:
                hours = float(row["hours_logged"])
                if not (0 <= hours <= 24):
                    raise ValueError
            except ValueError:
                skipped += 1
                skip_reasons.append(
                    f"Row {i}: hours_logged must be a number between 0 and 24"
                )
                continue

            # Validate date format
            from .activity_utils import normalize_activity_date
            from datetime import datetime
            normalized_date = normalize_activity_date(row["date"].strip())
            try:
                datetime.strptime(normalized_date, "%Y-%m-%d")
            except ValueError:
                skipped += 1
                skip_reasons.append(
                    f"Row {i}: invalid date format '{row['date']}'"
                )
                continue

            entry = {
                "developer_id": normalize_developer_id(row["developer_id"]),
                "date": normalized_date,
                "hours_logged": hours,
                "project": row.get("project", "").strip() or None,
                "notes": row.get("notes", "").strip() or None,
            }

            result = await db["timesheet_entries"].update_one(
                {
                    "developer_id": entry["developer_id"],
                    "date": entry["date"]
                },
                {"$set": entry},
                upsert=True,
            )

            if result.upserted_id:
                inserted += 1
            else:
                updated += 1

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to import CSV: {exc}"
        ) from exc

    invalidate_gap_detection_cache()
    return {
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "skip_reasons": skip_reasons,
        "total_processed": inserted + updated + skipped,
    }

# Phase 4: AI Analysis, Alerts, and Triage
@app.post("/analyze_and_alert", dependencies=[Depends(verify_api_key)])
async def analyze_and_alert(payload: dict[str, Any] = Body(...)):
    """
    Full AI analysis of a gap and alert generation.

    Performs:
    - Gap classification (priority)
    - Timesheet suggestion
    - Alert generation and storage
    - Optional Slack notification
    - Optional email notification
    """
    gap_id = payload.get("gap_id")
    developer_id = payload.get("developer_id")
    date = payload.get("date")

    if not all([gap_id, developer_id, date]):
        raise HTTPException(
            status_code=400,
            detail="gap_id, developer_id, and date are required"
        )

    try:
        # Get AI classifications
        priority = generate_gap_priority(payload)
        timesheet_suggestion = suggest_timesheet_entry(payload)

        summary = payload.get("summary", "Gap analysis performed.")
        recommended_action = f"Review suggested timesheet: {timesheet_suggestion.get('hours', 0)}h on {timesheet_suggestion.get('project', 'Unknown')}"

        # Generate and store alert
        alert = await generate_alert(
            gap_id=gap_id,
            developer_id=developer_id,
            date=date,
            priority=priority,
            summary=summary,
            recommended_action=recommended_action,
        )

        # Send Slack notification if configured
        slack_sent = await send_slack_notification(alert)

        # Send email notification if a recipient email was provided
        email_sent = False
        recipient_email = payload.get("recipient_email")
        if recipient_email:
            email_sent = await send_email_notification(alert, recipient_email)

        return {
            "alert": alert,
            "priority": priority,
            "suggested_timesheet": timesheet_suggestion,
            "slack_sent": slack_sent,
            "email_sent": email_sent,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to analyze and alert: {exc}") from exc


@app.get("/alerts/pending")
async def get_pending_alerts_endpoint(limit: int = Query(default=100, ge=1, le=1000)):
    """Get all pending (unnotified) alerts."""
    try:
        alerts = await get_pending_alerts(limit)
        return {"pending_alerts": alerts, "count": len(alerts)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to fetch pending alerts: {exc}") from exc


@app.get("/alerts/history")
async def get_alert_history_endpoint(
    developer_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
):
    """Get alert history, optionally filtered by developer."""
    try:
        alerts = await get_alert_history(developer_id, limit)
        return {"alerts": alerts, "count": len(alerts)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to fetch alert history: {exc}") from exc


@app.post("/alerts/{alert_id}/mark_notified")
async def mark_alert_notified_endpoint(alert_id: str):
    """Mark an alert as notified."""
    try:
        success = await mark_alert_notified(alert_id)
        if not success:
            raise HTTPException(status_code=404, detail="Alert not found")
        return {"message": "Alert marked as notified"}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to mark alert: {exc}") from exc


@app.post("/alerts/{alert_id}/resolve")
async def resolve_alert_endpoint(
    alert_id: str,
    payload: dict[str, Any] = Body(...)
):
    """Resolve an alert with optional resolution note."""
    try:
        resolution_note = payload.get("resolution_note", "")
        success = await resolve_alert(alert_id, resolution_note)
        if not success:
            raise HTTPException(status_code=404, detail="Alert not found")
        return {"message": "Alert resolved"}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to resolve alert: {exc}") from exc