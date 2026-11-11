import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import requests


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("success", "failure"):
        raise SystemExit("Usage: daily_monitor_notification.py success|failure")

    status = sys.argv[1]
    topic = os.getenv("NTFY_TOPIC", "").strip()

    if not topic:
        raise SystemExit("NTFY_TOPIC is not configured")

    if topic.startswith("https://"):
        url = topic
    elif "://" in topic:
        raise SystemExit("Only HTTPS notification URLs are supported")
    else:
        url = f"https://ntfy.sh/{topic}"

    now = datetime.now(ZoneInfo("Europe/London"))
    date = now.strftime("%d %B %Y, %H:%M UK time")

    if status == "success":
        title = "Goyal Mortgages - Daily check successful"
        message = (
            f"Nationwide monitoring completed successfully.\n"
            f"Date: {date}\n"
            f"Purchase and remortgage checks: Completed\n"
            f"Rate-change alerts: Sent separately when changes are detected."
        )
        priority = "default"
    else:
        title = "Goyal Mortgages - Monitoring FAILED"
        message = (
            f"Nationwide monitoring did not complete successfully.\n"
            f"Date: {date}\n"
            f"Please investigate the GitHub Actions workflow."
        )
        priority = "high"

    response = requests.post(
        url,
        data=message.encode("utf-8"),
        headers={
            "Title": title,
            "Priority": priority,
            "Tags": "white_check_mark" if status == "success" else "warning",
        },
        timeout=20,
    )
    response.raise_for_status()
    print(f"Admin monitoring notification delivered: {status}")


if __name__ == "__main__":
    main()
