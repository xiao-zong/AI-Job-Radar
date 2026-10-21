#!/usr/bin/env python3
"""Operator-only reconciliation after inspecting the private Telegram channel."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from radar.engine import Engine
from radar.store import FirestoreStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", help="Exact event ID shown by the list command")
    parser.add_argument("--resolution", choices=["sent", "not-sent"])
    parser.add_argument("--message-id", type=int)
    args = parser.parse_args()
    config = json.loads((ROOT / "config/deployment.json").read_text())
    store = FirestoreStore(config["project_id"])
    if not args.event:
        for value in store.scan("notifications").values():
            if value["state"] in {"unknown", "failed", "sending"}:
                print(json.dumps({key: value.get(key) for key in
                                  ("id", "state", "kind", "job_id", "attempted_at", "error")}))
        return
    if not sys.stdin.isatty() or not args.resolution:
        parser.error("An interactive terminal and explicit resolution are required.")
    if args.resolution == "sent" and (args.message_id is None or args.message_id <= 0):
        parser.error("Confirmed delivery requires its Telegram message ID.")
    if input("Inspect the channel first. Type RECONCILE " + args.event + ": ") != "RECONCILE " + args.event:
        raise SystemExit("Cancelled.")
    engine = Engine(store, None, None, None, {})
    if not engine.acquire():
        raise SystemExit("A run is active; reconcile after it finishes.")
    try:
        path = "notifications/" + args.event
        event = store.get(path)
        if not event or event["state"] not in {"unknown", "failed"}:
            raise ValueError("Only unknown/failed deliveries may be reconciled.")
        job_path = "jobs/" + event["job_id"] if event["kind"] == "ordinary" else None

        def resolve(values):
            current = values[path]
            current.update(state="sent" if args.resolution == "sent" else "pending",
                           reconciled_at=engine.now(), resolution=args.resolution,
                           message_id=args.message_id if args.resolution == "sent" else None)
            if job_path and args.resolution == "sent":
                values[job_path]["last_sent_material"] = current["material_hash"]
        engine.atomic([path, *([job_path] if job_path else [])], resolve)
        print("Resolution saved. No Telegram message was sent by this command.")
    finally:
        engine.release()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        raise SystemExit("Reconciliation failed: " + type(error).__name__) from None
