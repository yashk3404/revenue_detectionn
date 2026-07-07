import os
import httpx
import asyncio
from dotenv import load_dotenv

load_dotenv()

WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")

async def send_test_alert():
    message = {
        "text": "Unbilled Revenue Detective - Slack test! Developer YUVRAJ made 4 commits on 2026-06-23 but logged 0 hours."
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(WEBHOOK_URL, json=message)
        if response.status_code == 200:
            print("Slack alert sent successfully!")
        else:
            print(f"Failed: {response.status_code} - {response.text}")

asyncio.run(send_test_alert())
