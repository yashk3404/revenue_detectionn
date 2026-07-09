"""
Unbilled Revenue Detective — Full Streamlit Dashboard
Covers every endpoint in the FastAPI backend (src/main.py).

Setup:
    pip install streamlit requests pandas

Run:
    1) uvicorn src.main:app --reload --port 8001   (in one terminal)
    2) streamlit run dashboard.py                   (in another terminal)
"""

import requests
import streamlit as st
import pandas as pd
from datetime import datetime

st.set_page_config(
    page_title="Unbilled Revenue Detective",
    page_icon="🕵️",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Sidebar — connection settings + nav
# ---------------------------------------------------------------------------
st.sidebar.title("🕵️ Unbilled Revenue Detective")
base_url = st.sidebar.text_input("Backend URL", value="http://127.0.0.1:8001")
api_key = st.sidebar.text_input(
    "API Key (x-api-key)", type="password",
    help="Needed for protected endpoints: add timesheets, clear gaps, analyze & alert, fetch commits."
)

page = st.sidebar.radio(
    "Navigate",
    ["Overview", "Data Sync", "Timesheets", "Gaps", "Developers", "Alerts", "Ask AI", "System Health"],
)

st.sidebar.divider()
st.sidebar.caption("Start your FastAPI backend before using this dashboard.")

# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------
def headers():
    h = {"Content-Type": "application/json"}
    if api_key:
        h["x-api-key"] = api_key
    return h


def api_get(path, params=None):
    try:
        r = requests.get(f"{base_url}{path}", headers=headers(), params=params, timeout=200)
        if r.status_code >= 400:
            st.error(f"GET {path} failed ({r.status_code}): {r.text}")
            return None
        return r.json()
    except requests.exceptions.RequestException as e:
        st.error(f"Could not reach backend at {base_url}. Is the server running?\n\n{e}")
        return None


def api_post(path, json=None, files=None, timeout=30):
    try:
        if files:
            r = requests.post(f"{base_url}{path}",
                               headers={k: v for k, v in headers().items() if k != "Content-Type"},
                               files=files, timeout=90)
        else:
            r = requests.post(f"{base_url}{path}", headers=headers(), json=json, timeout=timeout)
        if r.status_code >= 400:
            st.error(f"POST {path} failed ({r.status_code}): {r.text}")
            return None
        return r.json()
    except requests.exceptions.RequestException as e:
        st.error(f"Could not reach backend at {base_url}. Is the server running?\n\n{e}")
        return None


def api_put(path, json=None):
    try:
        r = requests.put(f"{base_url}{path}", headers=headers(), json=json, timeout=20)
        if r.status_code >= 400:
            st.error(f"PUT {path} failed ({r.status_code}): {r.text}")
            return None
        return r.json()
    except requests.exceptions.RequestException as e:
        st.error(f"Could not reach backend at {base_url}. Is the server running?\n\n{e}")
        return None


def api_delete(path):
    try:
        r = requests.delete(f"{base_url}{path}", headers=headers(), timeout=20)
        if r.status_code >= 400:
            st.error(f"DELETE {path} failed ({r.status_code}): {r.text}")
            return None
        return r.json()
    except requests.exceptions.RequestException as e:
        st.error(f"Could not reach backend at {base_url}. Is the server running?\n\n{e}")
        return None


PRIORITY_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}


def priority_badge(text):
    if not text:
        return ""
    t = str(text).lower()
    for key, emoji in PRIORITY_EMOJI.items():
        if key in t:
            return f"{emoji} {text}"
    return text


def df_or_none(records, drop_id=True):
    if not records:
        return None
    df = pd.DataFrame(records)
    if drop_id and "_id" in df.columns:
        df = df.drop(columns=["_id"])
    return df


# ===========================================================================
# OVERVIEW
# ===========================================================================
if page == "Overview":
    st.title("Overview")
    st.caption("Quick health check across data sources, gaps, and alerts.")

    gaps_data = api_get("/detected_gaps")
    devs_data = api_get("/developers")
    alerts_data = api_get("/alerts/pending")

    gaps = gaps_data.get("detected_gaps", []) if gaps_data else []
    devs = devs_data.get("developers", []) if devs_data else []
    alerts = alerts_data if isinstance(alerts_data, list) else (alerts_data.get("alerts", []) if alerts_data else [])

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Gaps", len(gaps))
    c2.metric("High Priority", sum(1 for g in gaps if "high" in str(g.get("priority", "")).lower()))
    c3.metric("Developers Tracked", len(devs))
    c4.metric("Pending Alerts", len(alerts))

    st.divider()

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("🔄 Refresh gap detection")
        st.write("Re-compares real activity against timesheets.")
        if st.button("Refresh Gaps", use_container_width=True):
            result = api_post("/refresh_gaps")
            if result:
                st.success(f"{result.get('new_total_gaps', '?')} total gaps, {result.get('new_gaps_saved', '?')} new.")
                st.rerun()

    with col2:
        st.subheader("🤖 Summarize pending gaps")
        st.write("Runs Gemini over all pending gaps.")
        if st.button("Summarize Pending Gaps", use_container_width=True):
            result = api_get("/summarize_gaps")
            if result:
                st.success(result.get("message", "Done."))

    if gaps:
        st.divider()
        st.subheader("Gaps by reason")
        df = pd.DataFrame(gaps)
        if "reason" in df.columns:
            st.bar_chart(df["reason"].value_counts())

# ===========================================================================
# DATA SYNC (GitHub / Slack / Jira ingestion)
# ===========================================================================
elif page == "Data Sync":
    st.title("Data Sync")
    st.caption("Pull fresh activity from GitHub, Slack, and Jira, and view what's stored.")

    tab_gh, tab_slack, tab_jira = st.tabs(["GitHub", "Slack", "Jira"])

    with tab_gh:
        st.subheader("GitHub Commits")
        if st.button("⬇️ Fetch latest commits"):
            result = api_post("/fetch_commits")
            if result:
                st.success(result)
        commits = api_get("/commits")
        df = df_or_none(commits.get("commits", commits) if isinstance(commits, dict) else commits)
        if df is not None:
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info("No commits stored yet.")

    with tab_slack:
        st.subheader("Slack Activity")
        if st.button("⬇️ Fetch latest Slack messages"):
            result = api_post("/fetch_slack_messages")
            if result:
                st.success(result)
        slack = api_get("/slack_activity")
        df = df_or_none(slack.get("slack_activity", slack) if isinstance(slack, dict) else slack)
        if df is not None:
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info("No Slack activity stored yet.")

    with tab_jira:
        st.subheader("Jira Updates")
        if st.button("⬇️ Fetch latest Jira updates"):
            result = api_post("/fetch_jira_updates")
            if result:
                st.success(result)
        jira = api_get("/jira_activity")
        df = df_or_none(jira.get("jira_activity", jira) if isinstance(jira, dict) else jira)
        if df is not None:
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info("No Jira activity stored yet.")

# ===========================================================================
# TIMESHEETS
# ===========================================================================
elif page == "Timesheets":
    st.title("Timesheets")
    st.caption("View, add, edit, delete, and bulk-import timesheet entries.")

    ts_data = api_get("/timesheets")
    timesheets = ts_data.get("timesheets", []) if ts_data else []
    df = df_or_none(timesheets, drop_id=False)
    if df is not None:
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.info("No timesheet entries yet.")

    st.divider()
    add_tab, edit_tab, import_tab = st.tabs(["➕ Add / Update", "✏️ Edit or Delete One", "📁 Bulk Import CSV"])

    with add_tab:
        with st.form("add_ts"):
            f1, f2, f3, f4 = st.columns(4)
            dev_id = f1.text_input("Developer ID")
            date_val = f2.date_input("Date", value=datetime.today())
            hours = f3.number_input("Hours logged", min_value=0.0, step=0.5)
            project = f4.text_input("Project (optional)")
            notes = st.text_input("Notes (optional)")
            if st.form_submit_button("Save entry"):
                payload = {
                    "developer_id": dev_id,
                    "date": date_val.strftime("%Y-%m-%d"),
                    "hours_logged": hours,
                }
                if project:
                    payload["project"] = project
                if notes:
                    payload["notes"] = notes
                result = api_post("/timesheets", json=payload)
                if result:
                    st.success(f"Inserted: {result.get('inserted')}, Updated: {result.get('updated')}")

    with edit_tab:
        e1, e2 = st.columns(2)
        edit_dev = e1.text_input("Developer ID", key="edit_dev")
        edit_date = e2.text_input("Date (YYYY-MM-DD)", key="edit_date")

        st.write("**Update fields** (leave blank to skip a field)")
        u1, u2, u3 = st.columns(3)
        new_hours = u1.text_input("New hours_logged")
        new_project = u2.text_input("New project")
        new_notes = u3.text_input("New notes")

        b1, b2 = st.columns(2)
        if b1.button("Update entry"):
            if not edit_dev or not edit_date:
                st.warning("Developer ID and date are required.")
            else:
                payload = {}
                if new_hours:
                    payload["hours_logged"] = float(new_hours)
                if new_project:
                    payload["project"] = new_project
                if new_notes:
                    payload["notes"] = new_notes
                if not payload:
                    st.warning("Provide at least one field to update.")
                else:
                    result = api_put(f"/timesheets/{edit_dev}/{edit_date}", json=payload)
                    if result:
                        st.success("Updated.")
                        st.json(result)

        if b2.button("🗑️ Delete entry", type="secondary"):
            if not edit_dev or not edit_date:
                st.warning("Developer ID and date are required.")
            else:
                result = api_delete(f"/timesheets/{edit_dev}/{edit_date}")
                if result:
                    st.success(result.get("message", "Deleted."))

    with import_tab:
        st.write("Upload a CSV with columns like `developer_id, date, hours_logged, project, notes`.")
        uploaded = st.file_uploader("Choose CSV file", type="csv")
        if uploaded and st.button("Import CSV"):
            files = {"file": (uploaded.name, uploaded.getvalue(), "text/csv")}
            result = api_post("/import_timesheets", files=files)
            if result:
                st.success(result)

# ===========================================================================
# GAPS
# ===========================================================================
elif page == "Gaps":
    st.title("Detected Gaps")
    st.caption("Days a developer was active but didn't log matching hours.")

    top1, top2, top3 = st.columns([1, 1, 2])
    with top1:
        if st.button("🔄 Refresh gap detection"):
            result = api_post("/refresh_gaps")
            if result:
                st.rerun()
    with top2:
        if st.button("🗑️ Clear all gaps"):
            result = api_delete("/gaps/clear")
            if result:
                st.rerun()

    check_result = api_get("/check_gaps")
    gaps_data = api_get("/detected_gaps")
    gaps = gaps_data.get("detected_gaps", []) if gaps_data else []

    if not gaps:
        st.info("No gaps found. Try 'Refresh gap detection' above.")
    else:
        df = pd.DataFrame(gaps)
        display_cols = [c for c in ["developer_id", "date", "reason", "hours_logged",
                                     "github_count", "slack_count", "jira_count",
                                     "total_activity_count", "status", "priority", "summary"]
                         if c in df.columns]
        st.dataframe(df[display_cols], use_container_width=True, hide_index=True)

        st.divider()
        st.subheader("Inspect a gap with AI")
        options = [f"{g.get('developer_id')} — {g.get('date')}" for g in gaps]
        choice = st.selectbox("Pick a gap", options)
        gap = gaps[options.index(choice)]
        st.json(gap, expanded=False)

        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("📋 Classify Priority"):
                result = api_post("/classify_gap", json=gap, timeout=90)
                if result:
                    st.info(priority_badge(result.get("classification")))
        with c2:
            if st.button("✍️ Suggest Timesheet Entry"):
                result = api_post("/suggest_timesheet", json=gap, timeout=90)
                if result:
                    st.info(result.get("suggested_timesheet"))
        with c3:
            if st.button("🔗 Match to Project"):
                result = api_post("/match_activity", json=gap, timeout=90)
                if result:
                    st.info(result.get("match"))

        st.divider()
        st.subheader("Send an alert for this gap")
        recipient_email = st.text_input("Recipient email (optional — blank skips email, Slack always fires)")
        if st.button("🚨 Analyze & Alert"):
            payload = {
                "gap_id": gap.get("_id", f"{gap.get('developer_id')}_{gap.get('date')}"),
                "developer_id": gap.get("developer_id"),
                "date": gap.get("date"),
                "summary": gap.get("summary", gap.get("reason", "")),
            }
            if recipient_email:
                payload["recipient_email"] = recipient_email
            result = api_post("/analyze_and_alert", json=payload)
            if result:
                st.success(result)

# ===========================================================================
# DEVELOPERS
# ===========================================================================
elif page == "Developers":
    st.title("Developers")
    st.caption("Activity footprint vs. logged timesheet, side by side.")

    devs_data = api_get("/developers")
    devs = devs_data.get("developers", []) if devs_data else []

    if not devs:
        st.info("No developers found yet.")
    else:
        dev_ids = [d.get("developer_id", d.get("_id", "unknown")) for d in devs]
        chosen = st.selectbox("Pick a developer", dev_ids)

        ts_data = api_get("/timesheets")
        timesheets = ts_data.get("timesheets", []) if ts_data else []
        dev_ts = [t for t in timesheets if t.get("developer_id") == chosen]

        gaps_data = api_get("/detected_gaps")
        gaps = gaps_data.get("detected_gaps", []) if gaps_data else []
        dev_gaps = [g for g in gaps if g.get("developer_id") == chosen]

        col1, col2 = st.columns(2)
        with col1:
            st.subheader("Logged Timesheets")
            df = df_or_none(dev_ts)
            if df is not None:
                st.dataframe(df, use_container_width=True, hide_index=True)
            else:
                st.write("No entries.")
        with col2:
            st.subheader("Detected Gaps")
            df = df_or_none(dev_gaps)
            if df is not None:
                st.dataframe(df, use_container_width=True, hide_index=True)
            else:
                st.write("No gaps. 🎉")

# ===========================================================================
# ALERTS
# ===========================================================================
elif page == "Alerts":
    st.title("Alerts")
    st.caption("Notifications sent for significant gaps (Slack + email).")

    tab1, tab2 = st.tabs(["Pending", "History"])

    with tab1:
        pending = api_get("/alerts/pending")
        pending_list = pending if isinstance(pending, list) else (pending.get("alerts", []) if pending else [])
        if not pending_list:
            st.info("No pending alerts.")
        else:
            for alert in pending_list:
                with st.container(border=True):
                    st.write(alert)
                    c1, c2 = st.columns(2)
                    alert_id = alert.get("_id") or alert.get("id")
                    if c1.button("✅ Mark Notified", key=f"notify_{alert_id}"):
                        result = api_post(f"/alerts/{alert_id}/mark_notified")
                        if result:
                            st.rerun()
                    if c2.button("☑️ Resolve", key=f"resolve_{alert_id}"):
                        result = api_post(f"/alerts/{alert_id}/resolve")
                        if result:
                            st.rerun()

    with tab2:
        history = api_get("/alerts/history")
        history_list = history if isinstance(history, list) else (history.get("alerts", []) if history else [])
        df = df_or_none(history_list)
        if df is not None:
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info("No alert history yet.")

# ===========================================================================
# ASK AI
# ===========================================================================
elif page == "Ask AI":
    st.title("Ask AI About a Gap")
    st.caption("Free-form Q&A over a specific gap, powered by Gemini.")

    gaps_data = api_get("/detected_gaps")
    gaps = gaps_data.get("detected_gaps", []) if gaps_data else []

    if not gaps:
        st.info("No gaps available yet — refresh gap detection first.")
    else:
        options = [f"{g.get('developer_id')} — {g.get('date')}" for g in gaps]
        choice = st.selectbox("Pick a gap to ask about", options)
        gap = gaps[options.index(choice)]

        question = st.text_area("Your question", placeholder="Why does this gap look suspicious?")
        if st.button("Ask"):
            if not question:
                st.warning("Type a question first.")
            else:
                payload = {**gap, "question": question}
                result = api_post("/ask", json=payload, timeout=90)
                if result:
                    st.success(result.get("answer"))

# ===========================================================================
# SYSTEM HEALTH
# ===========================================================================
elif page == "System Health":
    st.title("System Health")
    st.caption("Basic connectivity check for the backend API.")

    c1, c2 = st.columns(2)
    with c1:
        if st.button("Check root (/)"):
            st.json(api_get("/"))
    with c2:
        if st.button("Check /health"):
            st.json(api_get("/health"))

    st.divider()
    st.write("**Connected to:**", base_url)
    st.write("**API key set:**", "Yes" if api_key else "No (some endpoints will fail with 401)")