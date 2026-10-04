"""Conservative public-business-contact enrichment for Veridyn sales accounts.

Only scans the provider's own public website. It does not guess email patterns and
does not mark discovered addresses as verified.
"""

import argparse
import html
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

from app import connect, migrate, now_iso

EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
PHONE_RE = re.compile(r"(?:(?:\+44\s?\d{2,4}|0\d{2,4})[\s().-]*\d{3,4}[\s.-]*\d{3,4})")
ROLE_PREFIXES = ("recruitment", "recruiting", "hr", "careers", "jobs", "enquiries", "enquiry", "info", "admin", "office", "contact", "hello")
PATHS = ("", "/contact", "/contact-us", "/about", "/about-us", "/team", "/recruitment", "/jobs", "/careers")
TIMEOUT = 12
MAX_BYTES = 1_000_000


def _normalise_site(url):
    if not url:
        return None
    value = url.strip()
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    parsed = urllib.parse.urlparse(value)
    if not parsed.hostname:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _same_domain(a, b):
    ha = (urllib.parse.urlparse(a).hostname or "").lower().removeprefix("www.")
    hb = (urllib.parse.urlparse(b).hostname or "").lower().removeprefix("www.")
    return bool(ha and ha == hb)


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "VeridynRecruit-ContactResearch/1.0", "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=ssl.create_default_context()) as response:
        ctype = response.headers.get("Content-Type", "")
        if "text/html" not in ctype.lower():
            return None, response.geturl()
        return response.read(MAX_BYTES).decode("utf-8", errors="replace"), response.geturl()


def _visible_text(source):
    source = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", source)
    source = re.sub(r"(?s)<[^>]+>", " ", source)
    return re.sub(r"\s+", " ", html.unescape(source))


def _title(source):
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", source)
    return re.sub(r"\s+", " ", html.unescape(m.group(1))).strip()[:250] if m else None


def _classify_email(email):
    local = email.split("@", 1)[0].lower()
    if local in ROLE_PREFIXES:
        return "Role inbox", "High"
    if any(local.startswith(prefix + ".") or local.startswith(prefix + "-") for prefix in ROLE_PREFIXES):
        return "Role inbox", "High"
    return "Named/business email", "Found"


def discover_for_account(db, account):
    site = _normalise_site(account["website"])
    if not site:
        return 0
    found = {}
    for path in PATHS:
        url = urllib.parse.urljoin(site + "/", path.lstrip("/"))
        try:
            source, final_url = _fetch(url)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
            continue
        if not source or not _same_domain(site, final_url):
            continue
        text = _visible_text(source)
        page_title = _title(source)
        for email in EMAIL_RE.findall(text):
            email = email.lower().strip(".,;:()[]<>")
            domain = email.rsplit("@", 1)[-1].removeprefix("www.")
            site_domain = (urllib.parse.urlparse(site).hostname or "").lower().removeprefix("www.")
            # Keep provider-domain addresses only; do not collect unrelated third-party emails.
            if domain != site_domain:
                continue
            contact_type, confidence = _classify_email(email)
            found[("email", email)] = {
                "email": email, "phone": None, "contact_type": contact_type, "confidence": confidence,
                "source_url": final_url, "source_page_title": page_title,
                "evidence": f"Publicly displayed on provider website: {final_url}",
            }
        for phone in PHONE_RE.findall(text):
            phone = re.sub(r"\s+", " ", phone).strip()
            found[("phone", phone)] = {
                "email": None, "phone": phone, "contact_type": "Business phone", "confidence": "Found",
                "source_url": final_url, "source_page_title": page_title,
                "evidence": f"Publicly displayed on provider website: {final_url}",
            }

    now = now_iso()
    for item in found.values():
        existing = db.execute(
            """SELECT candidate_id FROM sales_contact_candidates WHERE account_id=?
               AND COALESCE(email,'')=? AND COALESCE(phone,'')=?""",
            (account["account_id"], item["email"] or "", item["phone"] or ""),
        ).fetchone()
        if existing:
            db.execute("""UPDATE sales_contact_candidates SET source_url=?, source_page_title=?,
                          confidence=?, evidence=?, last_found_at=? WHERE candidate_id=?""",
                       (item["source_url"], item["source_page_title"], item["confidence"],
                        item["evidence"], now, existing["candidate_id"]))
        else:
            db.execute("""INSERT INTO sales_contact_candidates
                (candidate_id, account_id, email, phone, contact_type, source_url, source_page_title,
                 discovery_method, confidence, status, evidence, first_found_at, last_found_at)
                VALUES (?,?,?,?,?,?,?,'Provider website scan',?,'Review',?,?,?)""",
                (str(uuid.uuid4()), account["account_id"], item["email"], item["phone"], item["contact_type"],
                 item["source_url"], item["source_page_title"], item["confidence"], item["evidence"], now, now))
    return len(found)


def run(limit=25, priority=None):
    migrate()
    db = connect()
    sql = "SELECT * FROM sales_accounts WHERE website IS NOT NULL AND website <> ''"
    params = []
    if priority:
        sql += " AND priority=?"
        params.append(priority)
    sql += " ORDER BY account_score DESC LIMIT ?"
    params.append(limit)
    accounts = db.execute(sql, params).fetchall()
    total = 0
    for account in accounts:
        total += discover_for_account(db, account)
        db.commit()
    db.close()
    return {"accounts_scanned": len(accounts), "contact_candidates_found": total}


def main():
    parser = argparse.ArgumentParser(description="Find public business contacts on provider-owned websites.")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--priority", choices=("A1", "A2", "B", "C"))
    args = parser.parse_args()
    if args.limit < 1 or args.limit > 500:
        parser.error("--limit must be between 1 and 500")
    print(json.dumps(run(args.limit, args.priority), indent=2))


if __name__ == "__main__":
    main()
