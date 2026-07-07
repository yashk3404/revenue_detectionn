import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")
os.environ.setdefault("MONGO_DB_NAME", "unbilled_detective")
os.environ.setdefault("GITHUB_TOKEN", "")
os.environ.setdefault("SLACK_BOT_TOKEN", "")
os.environ.setdefault("JIRA_EMAIL", "")
os.environ.setdefault("JIRA_API_TOKEN", "")
os.environ.setdefault("JIRA_BASE_URL", "")

from src import ai_service
from src.main import run_gap_detection, validate_timesheet_entry


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def find(self, *args, **kwargs):
        return self

    async def to_list(self, limit):
        return self.docs[:limit]

    async def count_documents(self, query):
        return len(self.docs)

    async def find_one(self, query):
        for doc in self.docs:
            if all(doc.get(k) == v for k, v in query.items()):
                return doc
        return None

    async def update_one(self, query, update, upsert=False):
        existing = None
        for index, doc in enumerate(self.docs):
            if all(doc.get(k) == v for k, v in query.items()):
                existing = index
                break

        if existing is None:
            new_doc = {**query, **update.get("$set", {})}
            self.docs.append(new_doc)
            return type("Result", (), {"upserted_id": "new", "modified_count": 0, "matched_count": 0})()

        self.docs[existing] = {**self.docs[existing], **update.get("$set", {})}
        return type("Result", (), {"upserted_id": None, "modified_count": 1, "matched_count": 1})()

    async def insert_many(self, docs):
        self.docs.extend(docs)
        return type("InsertResult", (), {"inserted_ids": list(range(len(docs)))})()


class FakeDb:
    def __init__(self, timesheets=None):
        self.collections = {
            "timesheet_entries": FakeCollection(timesheets or []),
            "activity_logs": FakeCollection([]),
            "detected_gaps": FakeCollection([]),
        }

    def __getitem__(self, key):
        return self.collections[key]


class Phase3Tests(unittest.IsolatedAsyncioTestCase):
    async def test_validate_timesheet_entry_normalizes_developer_id(self):
        entry = validate_timesheet_entry(
            {
                "developer_id": "john doe",
                "date": "2026-07-01",
                "hours_logged": "7.5",
                "project": "demo",
                "notes": "ok",
            }
        )

        self.assertEqual(entry["developer_id"], "JOHN DOE")
        self.assertEqual(entry["date"], "2026-07-01")
        self.assertEqual(entry["hours_logged"], 7.5)

    async def test_run_gap_detection_records_missing_and_zero_hour_gaps(self):
        fake_db = FakeDb(timesheets=[{"developer_id": "JOHN DOE", "date": "2026-07-01", "hours_logged": 0}])

        footprints = [
            {
                "developer_id": "JOHN DOE",
                "date": "2026-07-01",
                "github_count": 1,
                "slack_count": 0,
                "jira_count": 0,
                "total_activity_count": 1,
            },
            {
                "developer_id": "JANE DOE",
                "date": "2026-07-01",
                "github_count": 1,
                "slack_count": 0,
                "jira_count": 0,
                "total_activity_count": 1,
            },
        ]

        with patch("src.main.db", fake_db), patch("src.main.build_all_footprints", new=AsyncMock(return_value=footprints)):
            result = await run_gap_detection(10)

        self.assertEqual(result["total_gaps"], 2)
        self.assertEqual(result["new_gaps_saved"], 2)
        self.assertEqual(result["detected_gaps"][0]["reason"], "Timesheet logged 0 hours")
        self.assertEqual(result["detected_gaps"][1]["reason"], "Activity exists but no timesheet")

    async def test_run_gap_detection_uses_cached_result_when_available(self):
        fake_db = FakeDb(timesheets=[{"developer_id": "JOHN DOE", "date": "2026-07-01", "hours_logged": 0}])
        footprints = [
            {
                "developer_id": "JOHN DOE",
                "date": "2026-07-01",
                "github_count": 1,
                "slack_count": 0,
                "jira_count": 0,
                "total_activity_count": 1,
            }
        ]

        with patch("src.main.db", fake_db), patch("src.main.build_all_footprints", new=AsyncMock(return_value=footprints)) as build_mock:
            first = await run_gap_detection(10)
            second = await run_gap_detection(10)

        self.assertEqual(first["total_gaps"], 1)
        self.assertEqual(second["total_gaps"], 1)
        self.assertEqual(build_mock.await_count, 1)

    async def test_generate_ai_summary_returns_fallback_when_client_fails(self):
        class FailingClient:
            @property
            def models(self):
                raise RuntimeError("missing client")

        with patch.object(ai_service, "gemini_client", FailingClient()):
            summary = ai_service.generate_ai_summary(
                {
                    "developer_id": "JOHN DOE",
                    "date": "2026-07-01",
                    "github_count": 1,
                    "slack_count": 0,
                    "jira_count": 0,
                    "hours_logged": 0,
                    "reason": "Commit exists but no timesheet",
                }
            )

        self.assertIn("AI summary unavailable", summary)

    async def test_generate_gap_priority_returns_fallback_when_ai_unavailable(self):
        with patch.object(ai_service, "gemini_client", None):
            result = ai_service.generate_gap_priority(
                {
                    "developer_id": "JANE DOE",
                    "date": "2026-07-01",
                    "github_count": 2,
                    "slack_count": 3,
                    "jira_count": 1,
                    "hours_logged": 0,
                    "reason": "Missing timesheet",
                }
            )

        self.assertIn("AI unavailable", result)

    async def test_suggest_timesheet_entry_returns_fallback_when_ai_unavailable(self):
        with patch.object(ai_service, "gemini_client", None):
            result = ai_service.suggest_timesheet_entry(
                {
                    "developer_id": "JANE DOE",
                    "date": "2026-07-01",
                    "github_count": 2,
                    "slack_count": 3,
                    "jira_count": 1,
                    "hours_logged": 0,
                    "activity_summary": "Fixed bug and answered code review comments.",
                }
            )

        self.assertEqual(result["project"], "Unknown")
        self.assertEqual(result["hours"], 0)

    async def test_match_activity_to_project_returns_fallback_when_ai_unavailable(self):
        with patch.object(ai_service, "gemini_client", None):
            result = ai_service.match_activity_to_project(
                {
                    "developer_id": "JANE DOE",
                    "date": "2026-07-01",
                    "commit_messages": "Fixed bug",
                    "slack_messages": "Reviewed PR",
                    "jira_issues": "BUG-123",
                    "current_projects": "Unknown",
                }
            )

        self.assertEqual(result, "Unknown project: AI unavailable.")

    async def test_answer_gap_question_returns_not_enough_information_when_ai_unavailable(self):
        with patch.object(ai_service, "gemini_client", None):
            result = ai_service.answer_gap_question(
                {
                    "developer_id": "JANE DOE",
                    "date": "2026-07-01",
                    "github_count": 2,
                    "slack_count": 0,
                    "jira_count": 0,
                    "hours_logged": 0,
                    "reason": "Missing timesheet",
                },
                "Can you explain why this gap exists?",
            )

        self.assertEqual(result, "Not enough information.")


if __name__ == "__main__":
    unittest.main()
