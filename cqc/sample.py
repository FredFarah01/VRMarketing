"""Fictional sample records shaped exactly like CQC provider_id / location_id documents.

Used to demo and test the platform before a CQC subscription key is configured. All IDs start with
``SAMPLE-`` and rows are stored with ``source = 'SAMPLE'`` so they are never mistaken for CQC data.
"""
import random
from datetime import date, timedelta

from .mapping import upsert_location, upsert_provider
from .rules import reclassify

PLACES = [
    ("Leeds", "West Yorkshire", "Yorkshire and The Humber", "Leeds", "LS1 4AP", 53.7997, -1.5492,
     "QWO", "NHS West Yorkshire Integrated Care Board", "Leeds Central and Headingley"),
    ("Manchester", "Greater Manchester", "North West", "Manchester", "M1 2WD", 53.4794, -2.2453,
     "QOP", "NHS Greater Manchester Integrated Care Board", "Manchester Central"),
    ("Birmingham", "West Midlands", "West Midlands", "Birmingham", "B3 2PP", 52.4814, -1.8998,
     "QHL", "NHS Birmingham and Solihull Integrated Care Board", "Birmingham Ladywood"),
    ("Bristol", "Bristol", "South West", "Bristol, City of", "BS1 5TR", 51.4545, -2.5879,
     "QUY", "NHS Bristol, North Somerset and South Gloucestershire Integrated Care Board", "Bristol Central"),
    ("Croydon", "Greater London", "London", "Croydon", "CR0 1NX", 51.3762, -0.0982,
     "QWE", "NHS South West London Integrated Care Board", "Croydon West"),
    ("Norwich", "Norfolk", "East", "Norwich", "NR2 1NH", 52.6309, 1.2974,
     "QMM", "NHS Norfolk and Waveney Integrated Care Board", "Norwich South"),
    ("Brighton", "East Sussex", "South East", "Brighton and Hove", "BN1 1UB", 50.8225, -0.1372,
     "QNX", "NHS Sussex Integrated Care Board", "Brighton Pavilion"),
    ("Newcastle upon Tyne", "Tyne and Wear", "North East", "Newcastle upon Tyne", "NE1 7RU", 54.9783, -1.6178,
     "QHM", "NHS North East and North Cumbria Integrated Care Board", "Newcastle upon Tyne Central and West"),
    ("Nottingham", "Nottinghamshire", "East Midlands", "Nottingham", "NG1 5FS", 52.9548, -1.1581,
     "QT1", "NHS Nottingham and Nottinghamshire Integrated Care Board", "Nottingham East"),
]

SERVICE = {
    "dom": ("Homecare agencies", "Domiciliary care service", "Community based adult social care services",
            "Personal care", "Homecare agency", None),
    "sl": ("Supported living", "Supported living service", "Community based adult social care services",
           "Personal care", "Supported living service", None),
    "res": ("Care home service without nursing", "Care home service without nursing", "Residential social care",
            "Accommodation for persons who require nursing or personal care", "Social Care Org", "Y"),
    "nur": ("Care home service with nursing", "Care home service with nursing", "Residential social care",
            "Accommodation for persons who require nursing or personal care", "Social Care Org", "Y"),
    "xc": ("Extra Care housing services", "Extra care housing service", "Community based adult social care services",
           "Personal care", "Social Care Org", None),
    "gp": ("Doctors/GPs", "Doctors consultation service", "GP Practices",
           "Treatment of disease, disorder or injury", "Primary Medical Services", None),
}
SPECIALISMS = ["Dementia", "Learning disabilities", "Mental health conditions", "Physical disabilities",
               "Caring for adults over 65 yrs", "Caring for adults under 65 yrs", "Sensory impairments"]

GROUPS = [
    ("Sample Brightwater Care Group Ltd", ["dom"] * 14 + ["sl"] * 6, "Limited Company"),
    ("Sample Meadowbank Homes Ltd", ["res"] * 4 + ["nur"] * 3, "Limited Company"),
    ("Sample Harbour Supported Living CIC", ["sl"] * 3, "Organisation"),
    ("Sample Oakleaf Home Care Ltd", ["dom"], "Limited Company"),
    ("Sample Northern Complex Care Ltd", ["dom", "dom"], "Limited Company"),
    ("Sample Willow Live-in Care Ltd", ["dom"], "Limited Company"),
    ("Sample Riverside Nursing Homes plc", ["nur"] * 12, "Limited Company"),
    ("Sample Evergreen Extra Care Trust", ["xc", "xc"], "Charity"),
    ("Sample Hillview Medical Practice", ["gp"], "Partnership"),
    ("Sample Kindred Autism Services Ltd", ["sl", "res"], "Limited Company"),
    ("Sample Rosewood Care Home", ["res"], "Individual"),
    ("Sample Cedar Home Care (Closed) Ltd", ["dom"], "Limited Company"),
]


def build_documents(today: date | None = None) -> tuple[list[dict], list[dict]]:
    rnd = random.Random(2026)
    today = today or date.today()
    providers, locations = [], []
    lnum = 0
    for pnum, (pname, services, ownership) in enumerate(GROUPS, start=1):
        pid = f"SAMPLE-P-{pnum:04d}"
        home = PLACES[pnum % len(PLACES)]
        reg = today - timedelta(days=rnd.choice([20, 45, 75, 200, 800, 2400, 4100]))
        inactive = "Closed" in pname
        loc_ids = []
        for svc in services:
            lnum += 1
            lid = f"SAMPLE-L-{lnum:04d}"
            loc_ids.append(lid)
            gac, gac_desc, cat, ra, ltype, care_home = SERVICE[svc]
            town, county, region, la, pc, lat, lon, icb, icbname, const = PLACES[lnum % len(PLACES)]
            lname = pname.replace(" Ltd", "").replace(" plc", "").replace(" CIC", "").replace(" Trust", "")
            if "Complex" in pname:
                lname += " - Complex Care Team"
            lname += f" - {town}"
            specs = rnd.sample(SPECIALISMS, 3)
            if "Autism" in pname:
                specs = ["Learning disabilities", "Caring for adults under 65 yrs"]
            if svc == "gp":
                specs = []
            lreg = reg + timedelta(days=rnd.randint(0, max(1, (today - reg).days)))
            rating = rnd.choice(["Good", "Good", "Outstanding", "Requires improvement", None])
            report = today - timedelta(days=rnd.randint(10, 700))
            doc = {
                "locationId": lid, "providerId": pid, "organisationType": "Location",
                "type": ltype, "name": lname, "registrationStatus": "Deregistered" if inactive else "Registered",
                "registrationDate": lreg.isoformat(),
                "postalAddressLine1": f"{rnd.randint(1, 200)} Sample Street", "postalAddressTownCity": town,
                "postalAddressCounty": county, "region": region, "postalCode": pc,
                "onspdLatitude": round(lat + rnd.uniform(-.05, .05), 5),
                "onspdLongitude": round(lon + rnd.uniform(-.05, .05), 5),
                "onspdIcbCode": icb, "onspdIcbName": icbname, "localAuthority": la, "constituency": const,
                "careHome": care_home or "N", "inspectionDirectorate":
                    "Primary medical services" if svc == "gp" else "Adult social care",
                "mainPhoneNumber": f"0{rnd.randint(1000000000, 1999999999)}",
                "website": None if pnum % 4 == 0 else f"www.{pname.lower().split()[1]}-sample.example",
                "lastInspection": {"date": (report - timedelta(days=30)).isoformat()},
                "lastReport": {"publicationDate": report.isoformat()},
                "locationTypes": [{"type": "Social Care Org" if svc != "gp" else "Primary Medical Services"}],
                "regulatedActivities": [{"name": ra, "code": "RA2" if svc in ("res", "nur") else "RA1",
                                         "contacts": [{"personTitle": "Ms", "personGivenName": "Sample",
                                                       "personFamilyName": "Manager",
                                                       "personRoles": ["Registered Manager"]}]}],
                "gacServiceTypes": [{"name": gac, "description": gac_desc}],
                "inspectionCategories": [{"code": "S1" if cat != "GP Practices" else "P2", "primary": "true",
                                          "name": cat}],
                "specialisms": [{"name": s} for s in specs],
                "relationships": [],
                "reports": [{"linkId": f"sample-report-{lid}", "reportDate": report.isoformat(),
                             "reportUri": f"/reports/sample-report-{lid}", "reportType": "Location",
                             "firstVisitDate": (report - timedelta(days=30)).isoformat()}],
            }
            if svc in ("res", "nur"):
                doc["numberOfBeds"] = rnd.choice([18, 32, 45, 60, 72, 110])
            if rating:
                doc["currentRatings"] = {"overall": {
                    "rating": rating, "reportDate": report.isoformat(), "reportLinkId": f"sample-report-{lid}",
                    "keyQuestionRatings": [{"name": q, "rating": rating, "reportDate": report.isoformat(),
                                            "reportLinkId": f"sample-report-{lid}"}
                                           for q in ("Safe", "Effective", "Caring", "Responsive", "Well-led")]},
                    "reportDate": report.isoformat()}
                doc["historicRatings"] = [{"reportLinkId": f"sample-hist-{lid}", "reportDate":
                                           (report - timedelta(days=900)).isoformat(),
                                           "overall": {"rating": "Requires improvement", "keyQuestionRatings": []}}]
            locations.append(doc)
        town, county, region, la, pc, lat, lon, icb, icbname, const = home
        providers.append({
            "providerId": pid, "locationIds": loc_ids, "organisationType": "Provider",
            "ownershipType": ownership, "type": "Social Care Org" if "Medical" not in pname else "Primary Medical Services",
            "companiesHouseNumber": None if ownership in ("Individual", "Partnership") else
            ("SAMPLE0001" if pnum in (4, 6) else f"SAMPLE{pnum:04d}"),
            "name": pname, "registrationStatus": "Deregistered" if inactive else "Registered",
            "registrationDate": reg.isoformat(),
            "deregistrationDate": (today - timedelta(days=60)).isoformat() if inactive else None,
            "website": None if pnum % 4 == 0 else f"www.{pname.lower().split()[1]}-sample.example",
            "postalAddressLine1": "1 Sample House", "postalAddressTownCity": town, "postalAddressCounty": county,
            "region": region, "postalCode": pc, "onspdLatitude": lat, "onspdLongitude": lon,
            "onspdIcbCode": icb, "onspdIcbName": icbname, "mainPhoneNumber": f"0{rnd.randint(1000000000, 1999999999)}",
            "inspectionDirectorate": "Primary medical services" if "Medical" in pname else "Adult social care",
            "constituency": const, "localAuthority": la,
            "lastReport": {"publicationDate": (today - timedelta(days=40)).isoformat()},
            "contacts": [], "relationships": [],
            "regulatedActivities": [{"name": "Personal care", "code": "RA1",
                                     "nominatedIndividual": {"personTitle": "Mr", "personGivenName": "Sample",
                                                             "personFamilyName": "Nominee"}}],
            "inspectionCategories": [{"code": "S1", "primary": "true",
                                      "name": "Community based adult social care services"}],
        })
    return providers, locations


def load_sample(db) -> int:
    providers, locations = build_documents()
    for p in providers:
        upsert_provider(db, p, source="SAMPLE")
    for loc in locations:
        upsert_location(db, loc, source="SAMPLE")
    db.commit()
    reclassify(db, [p["providerId"] for p in providers])
    return len(providers) + len(locations)


def clear_sample(db) -> None:
    like = "SAMPLE-%"
    for table, col in (("cqc_providers", "provider_id"), ("cqc_locations", "location_id"),
                       ("cqc_provider_locations", "provider_id"), ("cqc_service_types", "location_id"),
                       ("cqc_specialisms", "location_id"), ("lead_list_members", "provider_id")):
        db.execute(f"DELETE FROM {table} WHERE {col} LIKE ?", (like,))
    for table in ("cqc_regulated_activities", "cqc_ratings", "cqc_reports", "cqc_relationships",
                  "cqc_classifications", "cqc_sync_failures"):
        db.execute(f"DELETE FROM {table} WHERE entity_id LIKE ?", (like,))
    accounts = [r[0] for r in db.execute("SELECT account_id FROM lead_accounts WHERE provider_id LIKE ?", (like,))]
    for acc in accounts:
        for table in ("lead_contacts", "lead_notes", "lead_activities"):
            db.execute(f"DELETE FROM {table} WHERE account_id = ?", (acc,))
    db.execute("DELETE FROM lead_accounts WHERE provider_id LIKE ?", (like,))
    db.commit()
