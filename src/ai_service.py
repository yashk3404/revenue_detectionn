import json
import os
import time
from dotenv import load_dotenv

load_dotenv()


def _get_gemini_client():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None

    try:
        from google import genai

        return genai.Client(api_key=api_key)
    except Exception:
        return None


gemini_client = _get_gemini_client()


def _call_gemini(prompt: str, max_retries: int | None = None) -> str | None:
    if gemini_client is None:
        return None

    effective_retries = max(1, int(max_retries or os.getenv("AI_MAX_RETRIES", "1")))
    retry_delay = max(0.0, float(os.getenv("AI_RETRY_DELAY_SECONDS", "0")))

    for attempt in range(1, effective_retries + 1):
        try:
            response = gemini_client.models.generate_content(
                model="gemini-2.5-flash-lite",
                contents=prompt,
            )
            return response.text.strip()
        except Exception as e:
            print(f"[Gemini call failed] attempt {attempt}/{effective_retries}: {type(e).__name__}: {e}")
            if attempt == effective_retries:
                return None
            if retry_delay > 0:
                time.sleep(retry_delay)
    return None


def _fallback_summary(gap: dict, reason: str = "AI summary unavailable") -> str:
    developer = gap.get("developer_id", "UNKNOWN")
    date = gap.get("date", "UNKNOWN")
    return f"{reason} for {developer} on {date}."


def generate_ai_summary(gap: dict, max_retries: int = 3) -> str:
    """Takes one gap document (dict from MongoDB) and returns an AI-written summary."""
    result = _call_gemini(
        f"""You are reviewing developer activity to detect unbilled work hours.

Developer: {gap.get('developer_id', 'UNKNOWN')}
Date: {gap.get('date', 'UNKNOWN')}
GitHub commits: {gap.get('github_count', 0)}
Slack messages: {gap.get('slack_count', 0)}
Jira updates: {gap.get('jira_count', 0)}
Hours logged in timesheet: {gap.get('hours_logged', 0)}
Reason flagged: {gap.get('reason', 'unknown')}

Write exactly 1-2 sentences summarizing this discrepancy in plain, professional language.
Do not speculate about cause - just state the facts clearly.""",
        max_retries=max_retries,
    )

    return result or _fallback_summary(gap)


def generate_gap_priority(gap: dict, max_retries: int = 3) -> str:
    prompt = f"""You are an expert reviewer of developer activity and timesheet data.

Developer: {gap.get('developer_id', 'UNKNOWN')}
Date: {gap.get('date', 'UNKNOWN')}
GitHub commits: {gap.get('github_count', 0)}
Slack messages: {gap.get('slack_count', 0)}
Jira updates: {gap.get('jira_count', 0)}
Hours logged: {gap.get('hours_logged', 0)}
Reason flagged: {gap.get('reason', 'unknown')}

Classify this gap as one of: High, Medium, or Low priority.
Then write one sentence explaining why.
Return only the classification and the explanation."""

    result = _call_gemini(prompt, max_retries=max_retries)
    if result is None:
        developer = gap.get("developer_id", "UNKNOWN")
        date = gap.get("date", "UNKNOWN")
        return f"Low priority: AI unavailable for {developer} on {date}."
    return result


def suggest_timesheet_entry(activity: dict, max_retries: int = 3) -> dict[str, str | float]:
    prompt = f"""You are an AI assistant that writes suggested timesheet entries from developer activity.

Developer: {activity.get('developer_id', 'UNKNOWN')}
Date: {activity.get('date', 'UNKNOWN')}
GitHub commits: {activity.get('github_count', 0)}
Slack messages: {activity.get('slack_count', 0)}
Jira updates: {activity.get('jira_count', 0)}
Hours logged: {activity.get('hours_logged', 0)}
Activity summary: {activity.get('activity_summary', 'No summary provided.')}

Suggest a timesheet entry with:
- hours
- project or task name
- a short note

Write the answer in JSON with keys: hours, project, note."""

    result = _call_gemini(prompt, max_retries=max_retries)
    if not result:
        return {
            "hours": 0,
            "project": "Unknown",
            "note": "AI suggestion unavailable.",
        }

    try:
        parsed = json.loads(result)
        if isinstance(parsed, dict):
            parsed["hours"] = float(parsed.get("hours", 0))
            parsed["project"] = str(parsed.get("project", "Unknown"))
            parsed["note"] = str(parsed.get("note", "AI suggestion returned invalid format."))
            return parsed
    except Exception:
        pass

    return {
        "hours": 0,
        "project": "Unknown",
        "note": result,
    }


def match_activity_to_project(activity: dict, max_retries: int = 3) -> str:
    prompt = f"""You are an AI system that matches developer activity to projects.

Developer: {activity.get('developer_id', 'UNKNOWN')}
Date: {activity.get('date', 'UNKNOWN')}
Commit messages: {activity.get('commit_messages', 'None')}
Slack messages: {activity.get('slack_messages', 'None')}
Jira issues: {activity.get('jira_issues', 'None')}
Current project labels: {activity.get('current_projects', 'None')}

Suggest the most likely project or task for this activity.
Return a single project name and one sentence explaining your choice."""

    result = _call_gemini(prompt, max_retries=max_retries)
    if result is None:
        return "Unknown project: AI unavailable."
    return result


def answer_gap_question(details: dict, question: str, max_retries: int = 3) -> str:
    prompt = f"""You are an AI analyst for developer billing and timesheet gaps.

Data:
- Developer: {details.get('developer_id', 'UNKNOWN')}
- Date: {details.get('date', 'UNKNOWN')}
- GitHub commits: {details.get('github_count', 0)}
- Slack messages: {details.get('slack_count', 0)}
- Jira updates: {details.get('jira_count', 0)}
- Hours logged: {details.get('hours_logged', 0)}
- Gap reason: {details.get('reason', 'unknown')}
- Any other relevant activity details: {details.get('details', 'None')}

User question:
{question}

Answer in plain business language, using the data above.
If the question cannot be answered from these details, say "Not enough information."""

    result = _call_gemini(prompt, max_retries=max_retries)
    if result is None:
        return "Not enough information."
    return result


if __name__ == "__main__":
    fake_gap = {
        "developer_id": "YUVRAJ SADANA",
        "date": "2026-06-29",
        "github_count": 5,
        "slack_count": 0,
        "jira_count": 0,
        "hours_logged": 0,
        "reason": "Commit exists but no timesheet",
    }
    print(generate_ai_summary(fake_gap))
 