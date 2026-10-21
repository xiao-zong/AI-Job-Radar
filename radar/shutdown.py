"""Pause only this pilot's schedules ahead of the promotional-credit safety stop."""
from datetime import datetime, timezone
import json
import sys

PROJECT = "your-gcp-project-id"
REGION = "australia-southeast1"
STOP = datetime(2027, 1, 14, tzinfo=timezone.utc)
SCHEDULES = ("radar-daily", "radar-credit-stop")


def run(session, now=None, check_only=False):
    now = now or datetime.now(timezone.utc)
    if not check_only and now < STOP:
        raise ValueError("Shutdown cannot pause schedules before the agreed safety date.")
    states = {}
    for job in SCHEDULES:
        url = f"https://cloudscheduler.googleapis.com/v1/projects/{PROJECT}/locations/{REGION}/jobs/{job}"
        response = session.get(url, timeout=(5, 20), allow_redirects=False)
        if response.status_code != 200:
            raise RuntimeError("Unable to verify a pilot schedule.")
        state = response.json().get("state")
        if not check_only and state != "PAUSED":
            response = session.post(url + ":pause", json={}, timeout=(5, 20), allow_redirects=False)
            if response.status_code != 200 or response.json().get("state") != "PAUSED":
                raise RuntimeError("Schedule pause was not confirmed.")
            state = "PAUSED"
        states[job] = state
    return states


def main():
    try:
        if sys.argv[1:] not in ([], ["--check"]):
            raise ValueError("Unexpected shutdown arguments.")
        check_only = sys.argv[1:] == ["--check"]
        if not check_only and datetime.now(timezone.utc) < STOP:
            raise ValueError("Shutdown safety date has not arrived.")
        import google.auth
        from google.auth.transport.requests import AuthorizedSession
        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        session = AuthorizedSession(credentials, max_refresh_attempts=0)
        states = run(session, check_only=check_only)
        print(json.dumps({"severity": "INFO", "event": "radar_schedule_check" if check_only else "radar_shutdown",
                          "states": states}))
        return 0
    except Exception as error:
        print(json.dumps({"severity": "ERROR", "event": "radar_shutdown_failed",
                          "error_type": type(error).__name__}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
