from pathlib import Path

import pymupdf

SECTIONS = [
    ("Candidate Application", ["Completed application form on file", "Position applied for and contract type recorded",
                               "Candidate declaration signed and dated"]),
    ("Identity & Contact Details", ["Proof of identity verified", "Recent photograph held on file",
                                    "Current address and contact details confirmed"]),
    ("Employment & Activity History", ["Full employment history provided", "Start and end dates (month/year) recorded",
                                       "Periods of education, caring or other activity included"]),
    ("Employment Gap Explanations", ["Gaps in employment identified", "Written explanation obtained for each gap",
                                     "Explanations reviewed and recorded"]),
    ("References", ["Reference requested from most recent employer",
                    "Evidence of conduct in previous health or social care roles sought",
                    "Reason previous care or children's work ended verified", "References reviewed and verified"]),
    ("DBS Checks", ["Appropriate level of DBS check identified", "DBS certificate number and issue date recorded",
                    "Update Service status checked where applicable", "Any disclosures risk-assessed"]),
    ("Right to Work", ["Right to Work check completed before employment starts",
                       "Check method recorded (manual, share code or IDSP)", "Copy of evidence retained securely",
                       "Follow-up date set for time-limited permission"]),
    ("Qualifications & Training", ["Relevant qualifications evidenced", "Professional registrations verified",
                                   "Mandatory training requirements identified"]),
    ("Safeguarding Assessment", ["Safeguarding questions completed", "Responses reviewed by an authorised person",
                                 "Any concerns documented and actioned"]),
    ("Interview & Suitability", ["Interview notes recorded", "Values and suitability assessed",
                                 "Health declaration reviewed where appropriate"]),
    ("Required Documentation", ["All required documents uploaded against the candidate record",
                                "Documents checked for validity and expiry"]),
    ("Final Recruitment Review", ["All checks reviewed as complete", "Outstanding actions resolved",
                                  "Approval decision made by an authorised user"]),
    ("Recruitment Audit Trail", ["Who checked what, and when, is recorded",
                                 "Decisions and supporting evidence retained", "File is retrievable for review"]),
]

CSS = """
* { font-family: sans-serif; }
body { color: #0e1a3a; font-size: 10pt; }
h1 { font-size: 22pt; color: #0e1a3a; margin: 0; }
.eyebrow { color: #18b389; font-size: 8pt; font-weight: bold; letter-spacing: 1pt; }
.lead { color: #5b6478; font-size: 10pt; }
h2 { font-size: 12pt; color: #0e1a3a; margin-top: 12pt; margin-bottom: 4pt; }
.num { color: #18b389; }
li { margin-bottom: 3pt; color: #33405c; }
.note { color: #5b6478; font-size: 8pt; margin-top: 14pt; }
"""


def build_checklist_pdf(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    parts = ['<p class="eyebrow">VERIDYN RECRUIT · FREE RESOURCE</p>',
             "<h1>2026 Care Recruitment Compliance Checklist</h1>",
             '<p class="lead">A practical recruitment checklist for UK care providers. Use it to review each '
             "candidate file and help ensure every new care worker goes through a structured recruitment and "
             "compliance process before being approved for work.</p>"]
    for i, (title, items) in enumerate(SECTIONS, start=1):
        parts.append(f'<h2><span class="num">{i:02d}</span> &#8212; {title}</h2><ul>')
        parts += [f"<li>&#9744; {it}</li>" for it in items]
        parts.append("</ul>")
    parts.append('<p class="note">This checklist is designed to support safer, more consistent recruitment '
                 "processes. It is not legal advice and does not guarantee regulatory compliance. Always refer to "
                 "current guidance from the relevant regulator and the Home Office. "
                 "&#169; Veridyn Recruit.</p>")
    story = pymupdf.Story(html="".join(parts), user_css=CSS)
    writer = pymupdf.DocumentWriter(str(path))
    mediabox = pymupdf.paper_rect("a4")
    where = mediabox + (48, 56, -48, -56)
    more = True
    while more:
        dev = writer.begin_page(mediabox)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()
