"""Dummy script: POST a single study_iuid + rad_id to the n8n test webhook."""

import json
import requests

WEBHOOK_URL = "https://groot.5cn.co.in/webhook-test/68e2d1e9-4ed4-46e8-a653-2fee0bb76329"
AUTH_TOKEN = "to5y7HyOAx3Q1"

STUDY_IUID = "2.25.852453662325405270204939472370010849887"
RAD_ID = 499
MODALITIES = ["CT"]


def main():
    payload = {
        "event": "start-reporting",
        "rad_id": RAD_ID,
        "study_iuid": STUDY_IUID,
        "modalities": MODALITIES,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": AUTH_TOKEN,
    }

    print(f"POST {WEBHOOK_URL}")
    print(f"Payload: {json.dumps(payload)}")

    resp = requests.post(WEBHOOK_URL, headers=headers, json=payload, timeout=30)

    print(f"\nStatus: {resp.status_code}")
    print("Response:")
    try:
        print(json.dumps(resp.json(), indent=2))
    except ValueError:
        print(resp.text)


if __name__ == "__main__":
    main()
