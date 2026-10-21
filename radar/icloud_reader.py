"""Bounded IMAP reads of the dedicated iCloud job-alert folder.

Apple app-specific passwords are not restricted to a folder or read-only access.
This application enforces EXAMINE/read-only selection and BODY.PEEK, and contains
no SMTP, STORE, COPY, MOVE, EXPUNGE or delete operations.
"""
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
import imaplib
import json
from pathlib import Path
import re
import ssl
import time

HOST, PORT, FOLDER = "imap.mail.me.com", 993, "AI Job Radar"
MAX_BYTES = 2_000_000
SENDERS = {
    "seek_email": ("jobmail@seek.com.au", "jobmail@s.seek.com.au", "noreply@s.seek.com.au"),
    "linkedin_email": ("jobs-noreply@linkedin.com", "jobalerts-noreply@linkedin.com"),
}


class MailAccessError(RuntimeError):
    """Safe error text without server response bodies or credentials."""


def sender_is_alert(mail, source):
    addresses = getaddresses(mail.get_all("From", []))
    if len(addresses) != 1 or addresses[0][1].lower() not in SENDERS[source]:
        return False
    subject = str(mail.get("Subject", "")).casefold()
    # SEEK's general noreply address is shared with other messages.
    sender = addresses[0][1].lower()
    if sender == "noreply@s.seek.com.au":
        return re.search(r"\bnew jobs\b", subject) is not None
    if sender == "jobs-noreply@linkedin.com":
        # Shared with application receipts; only explicit new-job alerts qualify.
        return re.search(r"\b(?:new jobs|job alert)\b", subject) is not None
    return True


def sender_search(source):
    terms = ['FROM "' + sender + '"' for sender in SENDERS[source]]
    query = terms[-1]
    for term in reversed(terms[:-1]):
        query = "OR " + term + " (" + query + ")"
    return "(" + query + ")"


class ICloudReader:
    def __init__(self, credential_file, factory=imaplib.IMAP4_SSL, now=time.time, seconds=90):
        self.credentials = (dict(credential_file) if isinstance(credential_file, dict)
                            else json.loads(Path(credential_file).read_text()))
        if not re.fullmatch(r"[^@\s]+@(?:icloud\.com|me\.com|mac\.com)", self.credentials["username"]):
            raise MailAccessError("An explicit iCloud mailbox is required.")
        if not re.fullmatch(r"[a-z]{4}(?:-[a-z]{4}){3}", self.credentials["app_password"]):
            raise MailAccessError("Use an Apple app-specific password.")
        self.factory, self.now = factory, now
        self.end = time.monotonic() + seconds

    def _check(self):
        if time.monotonic() + 2 >= self.end:
            raise MailAccessError("Mailbox read exceeded its time allowance.")

    def messages(self, source_id, folder, not_before):
        if source_id not in SENDERS or folder != FOLDER:
            raise MailAccessError("Unapproved mailbox query.")
        if not isinstance(not_before, (int, float)) or not_before <= 0:
            raise MailAccessError("Mail activation time is required.")
        cutoff = max(not_before, self.now() - 7 * 86400)
        # IMAP SINCE uses days. Exact INTERNALDATE is checked again before body fetch.
        since = datetime.fromtimestamp(cutoff - 86400, timezone.utc).strftime("%d-%b-%Y")
        connection = None
        try:
            self._check()
            context = ssl.create_default_context()
            connection = self.factory(HOST, PORT, ssl_context=context, timeout=12)
            connection.login(self.credentials["username"], self.credentials["app_password"])
            status, _ = connection.select('"' + FOLDER + '"', readonly=True)
            if status != "OK":
                raise MailAccessError("Dedicated iCloud folder was not found.")
            status, result = connection.uid("search", None,
                f'(SINCE "{since}" {sender_search(source_id)})')
            if status == "OK" and result == [None]:
                result = [b""]  # imaplib empty-search representation.
            if status != "OK" or len(result) != 1 or not isinstance(result[0], bytes):
                raise MailAccessError("Mailbox search failed.")
            ids = result[0].split()
            if len(ids) > 100:
                raise MailAccessError("Mail volume exceeds reviewed per-run allowance.")
            rows = []
            for uid in reversed(ids):
                self._check()
                if not re.fullmatch(rb"[1-9][0-9]{0,12}", uid):
                    raise MailAccessError("Unexpected IMAP identifier.")
                status, metadata = connection.uid("fetch", uid, "(UID INTERNALDATE RFC822.SIZE)")
                if status != "OK":
                    raise MailAccessError("Mail metadata read failed.")
                header = b" ".join(p for p in metadata if isinstance(p, bytes))
                size = re.search(rb"\bRFC822.SIZE (\d+)", header)
                date_tuple = imaplib.Internaldate2tuple(header)
                if not size or date_tuple is None:
                    raise MailAccessError("Required mail metadata is absent.")
                received = time.mktime(date_tuple)
                if not cutoff <= received <= self.now() + 300:
                    continue
                if int(size[1]) > MAX_BYTES:
                    raise MailAccessError("Mail exceeds the reviewed size allowance.")
                status, parts = connection.uid("fetch", uid, f"(BODY.PEEK[]<0.{MAX_BYTES + 1}>)")
                bodies = [p[1] for p in parts if isinstance(p, tuple) and isinstance(p[1], bytes)]
                if status != "OK" or len(bodies) != 1 or len(bodies[0]) > MAX_BYTES:
                    raise MailAccessError("Mail content is incomplete or oversized.")
                raw = bodies[0]
                if len(raw) != int(size[1]):
                    raise MailAccessError("Mail was truncated during retrieval.")
                mail = BytesParser(policy=policy.default).parsebytes(raw)
                if not sender_is_alert(mail, source_id):
                    continue
                rows.append({"raw": raw, "received_at": received})
            return sorted(rows, key=lambda row: row["received_at"], reverse=True)
        except MailAccessError:
            raise
        except Exception:
            raise MailAccessError("iCloud mailbox request failed.") from None
        finally:
            if connection:
                try:
                    # LOGOUT cannot expunge a read-only mailbox. Never use CLOSE.
                    connection.logout()
                except Exception:
                    pass
