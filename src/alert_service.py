"""
Alert and notification service for Phase 4: AI-driven gap analysis and escalation.
Handles alert generation, persistence, and notification routing.
"""

import os
from datetime import datetime, timezone
from typing import Any

from .database import db


class AlertSeverity:
    """Alert severity levels."""
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


def _get_slack_webhook() -> str | None:
    """Get Slack webhook URL from environment."""
    return os.getenv("SLACK_WEBHOOK_URL")


async def generate_alert(
    gap_id: str,
    developer_id: str,
    date: str,
    priority: str,
    summary: str,
    recommended_action: str,
) -> dict[str, Any]:
    """
    Generate and store an alert from a gap analysis.
    
    Args:
        gap_id: MongoDB ObjectId of the detected gap
        developer_id: Developer ID
        date: Gap date
        priority: Priority level (high/medium/low)
        summary: AI summary of the gap
        recommended_action: Recommended action from AI
        
    Returns:
        Alert document as dict
    """
    severity = AlertSeverity.HIGH if priority.lower() == "high" else (
        AlertSeverity.MEDIUM if priority.lower() == "medium" else AlertSeverity.LOW
    )
    
    alert = {
        "gap_id": gap_id,
        "developer_id": developer_id,
        "date": date,
        "severity": severity,
        "priority": priority,
        "summary": summary,
        "recommended_action": recommended_action,
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "notified_at": None,
        "resolved_at": None,
    }
    
    result = await db["alerts"].insert_one(alert)
    alert["_id"] = str(result.inserted_id)
    return alert


async def get_pending_alerts(limit: int = 100) -> list[dict[str, Any]]:
    """Get all pending (unnotified) alerts."""
    alerts = await db["alerts"].find({"status": "pending"}).to_list(limit)
    return [{"_id": str(a.get("_id")), **{k: v for k, v in a.items() if k != "_id"}} for a in alerts]


async def get_alert_history(developer_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """Get alert history, optionally filtered by developer."""
    query = {} if developer_id is None else {"developer_id": developer_id}
    alerts = await db["alerts"].find(query).to_list(limit)
    return [{"_id": str(a.get("_id")), **{k: v for k, v in a.items() if k != "_id"}} for a in alerts]


async def mark_alert_notified(alert_id: str) -> bool:
    """Mark an alert as notified."""
    from bson import ObjectId
    try:
        result = await db["alerts"].update_one(
            {"_id": ObjectId(alert_id)},
            {
                "$set": {
                    "status": "notified",
                    "notified_at": datetime.now(timezone.utc).isoformat(),
                }
            },
        )
        return result.matched_count > 0
    except Exception:
        return False


async def resolve_alert(alert_id: str, resolution_note: str = "") -> bool:
    """Mark an alert as resolved."""
    from bson import ObjectId
    try:
        result = await db["alerts"].update_one(
            {"_id": ObjectId(alert_id)},
            {
                "$set": {
                    "status": "resolved",
                    "resolved_at": datetime.now(timezone.utc).isoformat(),
                    "resolution_note": resolution_note,
                }
            },
        )
        return result.matched_count > 0
    except Exception:
        return False


async def send_slack_notification(alert: dict[str, Any]) -> bool:
    """
    Send alert notification to Slack.
    
    Args:
        alert: Alert document
        
    Returns:
        True if sent successfully, False otherwise
    """
    webhook_url = _get_slack_webhook()
    if not webhook_url:
        return False
    
    try:
        import httpx
        
        color="#FF0000" if alert["severity"] == AlertSeverity.HIGH else (
            "#FFA500" if alert["severity"] == AlertSeverity.MEDIUM else "#00FF00"
        )
        
        payload = {
            "attachments": [
                {
                    "color": color,
                    "title": f"Gap Alert: {alert['developer_id']} on {alert['date']}",
                    "fields": [
                        {
                            "title": "Severity",
                            "value": alert["severity"].upper(),
                            "short": True,
                        },
                        {
                            "title": "Summary",
                            "value": alert["summary"],
                            "short": False,
                        },
                        {
                            "title": "Recommended Action",
                            "value": alert["recommended_action"],
                            "short": False,
                        },
                    ],
                }
            ]
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.post(webhook_url, json=payload, timeout=10)
            return response.status_code in (200, 204)
    except Exception:
        return False


async def send_email_notification(alert: dict[str, Any], recipient: str) -> bool:
    """
    Send alert notification via email using Resend.

    Args:
        alert: Alert document
        recipient: Email address

    Returns:
        True if sent successfully, False otherwise
    """
    api_key = os.getenv("SENDGRID_API_KEY")
    email_from = os.getenv("EMAIL_FROM")

    if not api_key or not email_from:
        print("Email not configured: missing RESEND_API_KEY or EMAIL_FROM")
        return False

    subject = f"Unbilled Revenue Alert: {alert.get('developer_id', 'Unknown developer')}"
    body = alert.get("summary") or (
        f"A gap was detected for {alert.get('developer_id', 'Unknown')} "
        f"on {alert.get('date', 'Unknown date')}."
    )

    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail

        message = Mail(
            from_email=email_from,
            to_emails=recipient,
            subject=subject,
            plain_text_content=body,
        )
        sg = SendGridAPIClient(api_key)
        sg.send(message)
        return True
    except Exception as e:
        print(f"Failed to send email notification: {e}")
        return False
    

    # hushushudjdnssuh