"""Manual Cloud Shell audit; only the dedicated folder, no storage or AI calls."""
from email import policy
from email.parser import BytesParser
import json
import re
import subprocess
import time

from radar.icloud_reader import ICloudReader
from radar.mail_sources import parse_alert, Resolver

PROJECT = "your-gcp-project-id"


def main():
    result = subprocess.run(
        ["gcloud", "secrets", "versions", "access", "1", "--secret=radar-mail-icloud",
         "--project=" + PROJECT], capture_output=True)
    if result.returncode:
        raise RuntimeError("Credential read failed.")
    credentials = json.loads(result.stdout)
    for sid in ("seek_email", "linkedin_email"):
        rows = ICloudReader(credentials).messages(sid, "AI Job Radar", time.time() - 7 * 86400)
        print(json.dumps({"source": sid, "messages_last_7d": len(rows)}), flush=True)
        for row in rows[:2]:
            mail = BytesParser(policy=policy.default).parsebytes(row["raw"])
            auth = []
            for header in mail.get_all("Authentication-Results", []):
                auth.extend(re.findall(r"(?:dkim|dmarc|spf)=(?:pass|fail|none|neutral|softfail)", header))
                auth.extend(re.findall(r"header\.(?:d|from)=[A-Za-z0-9.-]+", header))
            print(json.dumps({"source": sid, "authentication": auth,
                              "received_at": row["received_at"]}), flush=True)
            try:
                jobs = parse_alert(row["raw"], {"id": sid, "channel": "Industry_MEL"},
                                   row["received_at"], Resolver(seconds=25))
                print(json.dumps({"source": sid, "jobs": len(jobs),
                                  "samples": [{k: j[k] for k in
                                               ("source_id", "title", "location", "url")}
                                              for j in jobs[:2]]}, ensure_ascii=False), flush=True)
            except Exception as error:
                print(json.dumps({"source": sid, "parse_error_type": type(error).__name__}), flush=True)


if __name__ == "__main__":
    main()
