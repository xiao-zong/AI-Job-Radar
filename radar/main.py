"""Cloud Run job entry point. Deployment is fail-closed until acceptance is recorded."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import sys

from .engine import Engine
from .model import Gemini
from .sources import fetch
from .store import FirestoreStore
from .telegram import Telegram


def config_check(config, profile, sources, now=None):
    now = now or datetime.now(timezone.utc)
    if config.get("activation_authorized") is not True:
        raise ValueError("Production activation has not been authorized.")
    required = ("credit_coverage_verified", "cost_controls_verified", "independent_monitoring_verified",
                "source_samples_reviewed", "telegram_test_verified", "model_region_test_verified")
    if any(config.get(key) is not True for key in required):
        raise ValueError("Production acceptance checks are incomplete.")
    stop = datetime.fromisoformat(config["stop_before_utc"].replace("Z", "+00:00"))
    if stop.tzinfo is None or now >= stop:
        raise ValueError("Credit safety stop date reached or invalid.")
    if not isinstance(config.get("chat_id"), int) or config["chat_id"] >= 0:
        raise ValueError("Private channel destination is missing.")
    if not any(s.get("enabled") and s.get("validation_status") == "approved" for s in sources):
        raise ValueError("No approved production sources.")
    for key, maximum in (("ordinary_daily_limit", 5), ("model_calls_per_run_limit", 30),
                         ("model_calls_per_day_limit", 30)):
        if type(profile.get(key)) is not int or not 1 <= profile[key] <= maximum:
            raise ValueError("Application allowance outside initial safety range.")
    evidence = profile.get("verified_experience")
    if not isinstance(evidence, list):
        raise ValueError("Candidate evidence list is missing.")
    ids = set()
    for item in evidence:
        if not all(isinstance(item.get(k), str) and item[k].strip()
                   for k in ("evidence_id", "description", "evidence_source")) or item["evidence_id"] in ids:
            raise ValueError("Candidate evidence must have unique IDs and sources.")
        ids.add(item["evidence_id"])
    return stop


def main():
    config_dir = Path(os.environ.get("RADAR_CONFIG_DIR", "/app/config"))
    try:
        config = json.loads(Path(os.environ.get("RADAR_DEPLOYMENT_FILE", config_dir / "deployment.json")).read_text())
        profile = json.loads(Path(os.environ.get("RADAR_PROFILE_FILE", config_dir / "profile.private.json")).read_text())
        sources = json.loads(Path(os.environ.get("RADAR_SOURCES_FILE", config_dir / "sources.json")).read_text())
        stop = config_check(config, profile, sources)
        remaining = min(480, int((stop - datetime.now(timezone.utc)).total_seconds()))
        if remaining < 90:
            raise ValueError("Too close to safety stop date to start a run.")

        def stop_now(signum, frame):
            raise SystemExit(2)
        signal.signal(signal.SIGALRM, stop_now)
        signal.signal(signal.SIGTERM, stop_now)
        signal.alarm(remaining)
        model = Gemini(config["project_id"], config["model_location"], config["model_id"])
        engine = Engine(FirestoreStore(config["project_id"]), model,
                        Telegram(os.environ["TELEGRAM_BOT_TOKEN"]), config["chat_id"], profile,
                        soft_seconds=min(450, remaining - 30))
        summary, code = engine.run(sources, fetch)
        severity = "ERROR" if code else "WARNING" if summary.get("warnings") else "INFO"
        print(json.dumps({"severity": severity, "event": "radar_run", **summary}))
        return code
    except Exception as error:
        print(json.dumps({"severity": "ERROR", "event": "radar_failed", "error_type": type(error).__name__}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
