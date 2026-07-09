"""
Unbilled Revenue Detective - one-click launcher (cross-platform)

Run this instead of starting uvicorn and streamlit separately:
    python run_app.py

It starts both the FastAPI backend and the Streamlit dashboard as
subprocesses, streams their output to this terminal, and shuts both
down together when you press Ctrl+C.
"""

import signal
import subprocess
import sys
import time

API_CMD = [
    sys.executable, "-m", "uvicorn",
    "src.main:app", "--reload", "--host", "0.0.0.0", "--port", "8001",
]
DASHBOARD_CMD = [
    sys.executable, "-m", "streamlit", "run", "src/dashboard.py",
]

processes = []


def shutdown(*_args):
    print("\nShutting down...")
    for p in processes:
        if p.poll() is None:
            p.terminate()
    for p in processes:
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
    sys.exit(0)


def main():
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print("Starting FastAPI backend on http://127.0.0.1:8001 ...")
    api_process = subprocess.Popen(API_CMD)
    processes.append(api_process)

    time.sleep(4)  # give the API a moment to boot before the dashboard connects

    print("Starting Streamlit dashboard ...")
    dashboard_process = subprocess.Popen(DASHBOARD_CMD)
    processes.append(dashboard_process)

    print("\nBoth services are running. Press Ctrl+C to stop both.\n")

    # Wait on either process; if one dies unexpectedly, shut down the other too.
    while True:
        for p in processes:
            if p.poll() is not None:
                print(f"Process {p.args} exited unexpectedly. Shutting down.")
                shutdown()
        time.sleep(1)


if __name__ == "__main__":
    main()