"""Explicit, bounded Cloud Run acceptance checks; does not enable production.

Run with python -m radar.acceptance firestore|model|telegram after provisioning.
Requires acceptance_authorized=true and verified credit/cost controls in the
mounted deployment configuration. Every external attempt is reserved first and
never automatically retried after an unknown outcome.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import uuid

from .domain import normalize_job, validate_assessment
from .model import Gemini
from .store import FirestoreStore
from .telegram import Telegram


def check_config(config, now=None):
    now = now or datetime.now(timezone.utc)
    for flag in ("acceptance_authorized", "credit_coverage_verified", "cost_controls_verified"):
        if config.get(flag) is not True:
            raise ValueError("Cloud acceptance has not been authorized with cost controls.")
    stop = datetime.fromisoformat(config["stop_before_utc"].replace("Z", "+00:00"))
    if stop.tzinfo is None or (stop - now).total_seconds() < 600:
        raise ValueError("Acceptance is too close to the credit safety stop.")
    if config.get("project_id") != "your-gcp-project-id":
        raise ValueError("Unexpected acceptance project.")


def reserve(store, operation):
    limits = {"firestore": 3, "model": 2, "model_replay": 1, "telegram": 1}
    if operation not in limits:
        raise ValueError("Unknown acceptance operation.")
    path = "control/acceptance_attempts"
    def mutate(values):
        item = values[path] or {}
        used = item.get(operation, 0)
        if used >= limits[operation]:
            raise ValueError("Acceptance allowance exhausted; inspect prior outcomes first.")
        item[operation] = used + 1
        values[path] = item
    store.atomic([path], mutate)


def run(operation, config, profile, store):
    check_config(config)
    reserve(store, operation)
    run_id = uuid.uuid4().hex
    path = "acceptance/" + run_id
    started = {"operation": operation, "status": "started",
               "started_at": datetime.now(timezone.utc).isoformat()}
    store.atomic([path], lambda values: values.update({path: started}))
    result = {"operation": operation, "run_id": run_id}
    if operation == "firestore":
        if store.get(path) != started:
            raise ValueError("Firestore read-back differs from committed value.")
        # Exercise update-time preconditions through the production adapter.
        def update(values):
            values[path]["roundtrip"] = "verified"
        store.atomic([path], update)
        if store.get(path).get("roundtrip") != "verified":
            raise ValueError("Firestore update round-trip failed.")
        result["roundtrip"] = "verified"
    elif operation in {"model", "model_replay"}:
        source = {"id": "acceptance", "channel": "Industry_MEL", "employer": "Synthetic acceptance fixture"}
        body = ("Location: Melbourne, Australia. This synthetic Applied Scientist position requires "
                "experience in statistical modelling. Python programming is preferred. Full working "
                "rights in Australia are required. Salary and application closing date are not provided.")
        job = normalize_job(source, "synthetic", "Applied Scientist", "Melbourne",
                            "https://example.invalid/acceptance", body)
        if operation == "model_replay":
            record_id = config.get("model_replay_record", "")
            if len(record_id) != 32 or any(c not in "0123456789abcdef" for c in record_id):
                raise ValueError("An inspected acceptance record is required for replay.")
            prior = store.get("acceptance/" + record_id) or {}
            if prior.get("operation") != "model" or prior.get("status") != "validation_failed":
                raise ValueError("Replay source is not an inspected failed model response.")
            assessment, usage = prior["model_response"], prior["usage"]
            result.update(replayed_from=record_id, paid_model_call=False)
        else:
            model = Gemini(config["project_id"], config["model_location"], config["model_id"])
            assessment, usage = model.analyze(job, profile)
        # Persist the synthetic response before semantic validation, so a failed
        # check can be diagnosed without another paid model request.
        def record_response(values):
            values[path].update(model_response=assessment, usage=usage)
        store.atomic([path], record_response)
        try:
            validate_assessment(assessment, job, profile)
        except ValueError as error:
            def invalid(values):
                values[path].update(status="validation_failed", validation_error=str(error))
            store.atomic([path], invalid)
            raise
        result.update({"schema_and_evidence": "verified", "usage": usage,
                       "model_id": config["model_id"], "fixture": "synthetic"})
    else:
        chat_id = config.get("chat_id")
        if type(chat_id) is not int or chat_id >= 0:
            raise ValueError("Private channel destination is required.")
        result["message_id"] = Telegram(os.environ["TELEGRAM_BOT_TOKEN"]).send(
            chat_id, "AI Job Radar: Cloud Run delivery test succeeded. This is a setup test, not a job recommendation.")
    def complete(values):
        values[path].update(result)
        values[path]["status"] = "passed"
    store.atomic([path], complete)
    return result


def main():
    try:
        if len(sys.argv) != 2 or sys.argv[1] not in {"firestore", "model", "model_replay", "telegram"}:
            raise ValueError("Select exactly one acceptance operation.")
        config_dir = Path(os.environ.get("RADAR_CONFIG_DIR", "/runtime-config"))
        config = json.loads(Path(os.environ.get("RADAR_DEPLOYMENT_FILE", config_dir / "deployment.json")).read_text())
        profile = json.loads(Path(os.environ.get("RADAR_PROFILE_FILE", config_dir / "profile.private.json")).read_text())
        check_config(config)  # Before creating a cloud client or making a request.
        result = run(sys.argv[1], config, profile, FirestoreStore(config["project_id"]))
        print(json.dumps({"severity": "INFO", "event": "radar_acceptance", **result}))
        return 0
    except Exception as error:
        print(json.dumps({"severity": "ERROR", "event": "radar_acceptance_failed",
                          "error_type": type(error).__name__}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
