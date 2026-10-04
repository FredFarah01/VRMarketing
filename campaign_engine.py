"""Manual-first outbound campaign sequencing for Veridyn sales."""

import argparse
import json
import uuid
from datetime import datetime, timedelta, timezone

from app import connect, migrate, now_iso

CADENCE = [
    (1, 0, "Email", "Care recruitment compliance at {{company_name}}", "Problem-led introduction and offer the free compliance checklist."),
    (2, 1, "LinkedIn", None, "View/connect with the relevant decision-maker."),
    (3, 2, "Call", None, "Call the provider and identify the person responsible for recruitment/compliance."),
    (4, 4, "Email", "2026 care recruitment compliance checklist", "Send the useful checklist/resource; keep the CTA low-friction."),
    (5, 6, "Call", None, "Follow up on the checklist and current recruitment admin pain points."),
    (6, 9, "Email", "Reducing recruitment admin at {{company_name}}", "Focus on employment gaps, references, DBS and Right to Work visibility."),
    (7, 13, "LinkedIn", None, "Follow up with a short relevant message."),
    (8, 16, "Call", None, "Call to qualify fit and offer a short Veridyn Recruit demo."),
    (9, 20, "Email", "Should I close this out?", "Polite break-up email with opt-out and no-pressure demo invitation."),
]


def seed_campaign(name, segment=None):
    migrate()
    db = connect()
    existing = db.execute("SELECT campaign_id FROM sales_campaigns WHERE name=?", (name,)).fetchone()
    if existing:
        db.close()
        return existing["campaign_id"]
    cid = str(uuid.uuid4())
    now = now_iso()
    db.execute("""INSERT INTO sales_campaigns
        (campaign_id, name, target_segment, status, description, created_at, updated_at)
        VALUES (?,?,?,'Draft',?,?,?)""",
        (cid, name, segment, "Manual-first 21-day multi-channel Veridyn Recruit outreach.", now, now))
    for number, offset, channel, subject, task in CADENCE:
        db.execute("""INSERT INTO sales_campaign_steps
            (step_id, campaign_id, step_number, day_offset, channel, subject_template, body_template, task_title, created_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (str(uuid.uuid4()), cid, number, offset, channel, subject, None, task, now))
    db.commit()
    db.close()
    return cid


def _suppressed(db, account, contact):
    if contact and contact["do_not_contact"]:
        return True, "Contact marked do not contact"
    email = (contact["email"] if contact else None) or ""
    domain = (account["domain"] or "").lower()
    row = db.execute("""SELECT reason FROM sales_suppressions
        WHERE (email IS NOT NULL AND email <> '' AND email=?)
           OR (domain IS NOT NULL AND domain <> '' AND domain=?)
           OR (provider_id IS NOT NULL AND provider_id=?) LIMIT 1""",
        (email.lower(), domain, account["provider_id"])).fetchone()
    return (True, row["reason"]) if row else (False, None)


def enrol(campaign_id, priority=None, limit=50):
    migrate()
    db = connect()
    campaign = db.execute("SELECT * FROM sales_campaigns WHERE campaign_id=?", (campaign_id,)).fetchone()
    if not campaign:
        raise ValueError("Campaign not found")
    sql = "SELECT * FROM sales_accounts WHERE lifecycle_stage NOT IN ('Customer','Closed')"
    params = []
    if campaign["target_segment"]:
        sql += " AND target_segment=?"
        params.append(campaign["target_segment"])
    if priority:
        sql += " AND priority=?"
        params.append(priority)
    sql += " ORDER BY account_score DESC LIMIT ?"
    params.append(limit)
    accounts = db.execute(sql, params).fetchall()
    steps = db.execute("SELECT * FROM sales_campaign_steps WHERE campaign_id=? ORDER BY step_number", (campaign_id,)).fetchall()
    enrolled = skipped = 0
    base = datetime.now(timezone.utc)
    for account in accounts:
        contact = db.execute("""SELECT * FROM sales_contacts WHERE account_id=? AND do_not_contact=0
            ORDER BY is_decision_maker DESC, CASE WHEN email IS NOT NULL THEN 0 ELSE 1 END, created_at LIMIT 1""",
            (account["account_id"],)).fetchone()
        blocked, _ = _suppressed(db, account, contact)
        if blocked:
            skipped += 1
            continue
        existing = db.execute("""SELECT enrolment_id FROM sales_campaign_enrolments
            WHERE campaign_id=? AND account_id=? AND COALESCE(contact_id,'')=COALESCE(?, '')""",
            (campaign_id, account["account_id"], contact["contact_id"] if contact else None)).fetchone()
        if existing:
            skipped += 1
            continue
        eid = str(uuid.uuid4())
        db.execute("""INSERT INTO sales_campaign_enrolments
            (enrolment_id, campaign_id, account_id, contact_id, status, enrolled_at, current_step)
            VALUES (?,?,?,?,'Active',?,1)""",
            (eid, campaign_id, account["account_id"], contact["contact_id"] if contact else None, now_iso()))
        for step in steps:
            due = base + timedelta(days=step["day_offset"])
            db.execute("""INSERT INTO sales_campaign_actions
                (action_id, enrolment_id, step_id, due_at, status, created_at)
                VALUES (?,?,?,?, 'Due', ?)""",
                (str(uuid.uuid4()), eid, step["step_id"], due.isoformat(), now_iso()))
        enrolled += 1
    db.commit()
    db.close()
    return {"enrolled": enrolled, "skipped": skipped}


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    seed = sub.add_parser("seed")
    seed.add_argument("--name", default="Veridyn Recruit 21-Day Outreach")
    seed.add_argument("--segment")
    en = sub.add_parser("enrol")
    en.add_argument("campaign_id")
    en.add_argument("--priority", choices=("A1", "A2", "B", "C"))
    en.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    if args.command == "seed":
        print(json.dumps({"campaign_id": seed_campaign(args.name, args.segment)}, indent=2))
    else:
        print(json.dumps(enrol(args.campaign_id, args.priority, args.limit), indent=2))


if __name__ == "__main__":
    main()
