#!/usr/bin/env python3
"""Prepare a bounded Cloud Build in the selected pilot project."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "your-gcp-project-id"
REGION = "australia-southeast1"
BUCKET = "gs://radar-build-123456789012"
BUILDER = f"radar-builder@{PROJECT}.iam.gserviceaccount.com"


def command(args):
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
    if result.returncode:
        print(result.stderr[-2500:])
        raise RuntimeError("Command failed: " + " ".join(args[:4]))
    return result.stdout


def gcloud(*args):
    output = command(["gcloud", *args, "--project=" + PROJECT, "--format=json"])
    return json.loads(output) if output.strip() else None


def main():
    os.environ["CLOUDSDK_CORE_DISABLE_PROMPTS"] = "1"
    os.chdir(ROOT)
    budget_file = ROOT / "work/budgets-verified.json"
    budgets = json.loads(budget_file.read_text()).get("budgets", [])
    if len([b for b in budgets if b.get("budgetFilter", {}).get("projects") ==
            ["projects/123456789012"]]) != 3:
        raise RuntimeError("Expected installed pilot budgets were not recorded.")
    wheels = ROOT / "work/build-wheels"
    wheels.mkdir(parents=True, exist_ok=True)
    command([str(ROOT / ".venv/bin/python"), "-m", "pip", "download",
             "--python-version=3.12", "--only-binary=:all:", "--no-deps",
             "-r", "requirements.lock", "-d", str(wheels)])
    hashes = {}
    for wheel in wheels.glob("*.whl"):
        parts = wheel.name.split("-")
        key = parts[0].replace("_", "-").lower() + "==" + parts[1]
        hashes.setdefault(key, []).append(hashlib.sha256(wheel.read_bytes()).hexdigest())
    locked = []
    for line in (ROOT / "requirements.lock").read_text().splitlines():
        package = line.split(" --hash=")[0].strip()
        if not package or package.startswith("#"):
            continue
        key = package.replace("_", "-").lower()
        if key not in hashes:
            raise RuntimeError("No downloaded wheel for " + package)
        locked.append(package + "".join(" --hash=sha256:" + h for h in sorted(hashes[key])))
    (ROOT / "requirements.lock").write_text("\n".join(locked) + "\n")
    command(["docker", "pull", "python:3.12-slim"])
    base = json.loads(command(["docker", "image", "inspect", "python:3.12-slim"]))[0]["RepoDigests"][0]
    if not base.startswith("python@sha256:"):
        raise RuntimeError("Unexpected base image digest.")
    dockerfile = ROOT / "Dockerfile"
    lines = dockerfile.read_text().splitlines()
    lines[0] = "FROM " + base
    dockerfile.write_text("\n".join(lines).replace(
        "pip install --no-cache-dir -r", "pip install --no-cache-dir --require-hashes -r") + "\n")
    build_config = ROOT / "cloudbuild.yaml"
    build_config.write_text(build_config.read_text().replace("name: python:3.12-slim", "name: " + base))
    command([str(ROOT / ".venv/bin/python"), "-m", "unittest", "discover", "-s", "tests"])
    print("TESTS_AND_DEPENDENCY_HASHES_VERIFIED", flush=True)
    buckets = gcloud("storage", "buckets", "list")
    if not any(b.get("name") in {BUCKET, BUCKET[5:], BUCKET + "/"} for b in buckets):
        gcloud("storage", "buckets", "create", BUCKET, "--location=" + REGION,
               "--uniform-bucket-level-access", "--public-access-prevention")
    # The bucket is dedicated to temporary source archives, not runtime data.
    policy = ROOT / "work/build-bucket-lifecycle.json"
    policy.write_text(json.dumps({"rule": [{"action": {"type": "Delete"},
                                          "condition": {"age": 7}}]}))
    gcloud("storage", "buckets", "update", BUCKET, "--lifecycle-file=" + str(policy))
    gcloud("storage", "buckets", "add-iam-policy-binding", BUCKET,
           "--member=serviceAccount:" + BUILDER, "--role=roles/storage.objectViewer")
    command(["git", "add", "Dockerfile", "requirements.lock", "cloudbuild.yaml",
             "scripts/prepare_cloud_build.py"])
    changed = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode
    if changed:
        command(["git", "-c", "user.name=AI Job Radar setup",
                 "-c", "user.email=ai-job-radar@localhost", "commit",
                 "-m", "Pin build base image and dependency wheel hashes"])
    revision = command(["git", "rev-parse", "--short=12", "HEAD"]).strip()
    image = f"{REGION}-docker.pkg.dev/{PROJECT}/radar/ai-job-radar:{revision}"
    result = gcloud("builds", "submit", ".", "--config=cloudbuild.yaml",
                    "--region=" + REGION, "--gcs-source-staging-dir=" + BUCKET + "/source",
                    "--service-account=projects/" + PROJECT + "/serviceAccounts/" + BUILDER,
                    "--substitutions=_IMAGE=" + image, "--async")
    (ROOT / "work/build-submission.json").write_text(json.dumps(result, indent=2))
    print(json.dumps({"build_id": result["id"], "status": result["status"],
                      "image": image, "source_commit": revision}), flush=True)


if __name__ == "__main__":
    main()
