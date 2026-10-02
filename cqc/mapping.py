"""Map CQC Syndication API documents (provider_id / location_id schemas) onto database rows.

Only ``cqc_*`` tables are written here; CRM tables are never touched by a sync.
"""
import json
from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def piped(values) -> str | None:
    vals = [str(v).strip() for v in values if v and str(v).strip()]
    return "|" + "|".join(dict.fromkeys(vals)) + "|" if vals else None


def split_piped(value: str | None) -> list[str]:
    return [v for v in (value or "").split("|") if v]


def _date(value):
    return value[:10] if isinstance(value, str) and value else None


def _common(doc: dict) -> dict:
    ratings = doc.get("currentRatings") or {}
    overall = ratings.get("overall") or {}
    return {
        "name": doc.get("name"),
        "also_known_as": doc.get("alsoKnownAs"),
        "organisation_type": doc.get("organisationType"),
        "type": doc.get("type"),
        "brand_id": doc.get("brandId"),
        "brand_name": doc.get("brandName"),
        "uprn": doc.get("uprn"),
        "ods_code": doc.get("odsCode"),
        "registration_status": doc.get("registrationStatus"),
        "registration_date": _date(doc.get("registrationDate")),
        "deregistration_date": _date(doc.get("deregistrationDate")),
        "website": doc.get("website"),
        "main_phone_number": doc.get("mainPhoneNumber"),
        "address_line_1": doc.get("postalAddressLine1"),
        "address_line_2": doc.get("postalAddressLine2"),
        "town_city": doc.get("postalAddressTownCity"),
        "county": doc.get("postalAddressCounty"),
        "region": doc.get("region"),
        "postal_code": doc.get("postalCode"),
        "latitude": doc.get("onspdLatitude"),
        "longitude": doc.get("onspdLongitude"),
        "icb_code": doc.get("onspdIcbCode"),
        "icb_name": doc.get("onspdIcbName"),
        "inspection_directorate": doc.get("inspectionDirectorate"),
        "constituency": doc.get("constituency"),
        "local_authority": doc.get("localAuthority"),
        "last_inspection_date": _date((doc.get("lastInspection") or {}).get("date")),
        "last_report_date": _date((doc.get("lastReport") or {}).get("publicationDate")),
        "regulated_activities": piped(a.get("name") for a in doc.get("regulatedActivities") or []),
        "inspection_categories": piped(c.get("name") for c in doc.get("inspectionCategories") or []),
        "current_rating": overall.get("rating"),
        "current_rating_date": _date(overall.get("reportDate") or ratings.get("reportDate")),
        "raw_json": json.dumps(doc, separators=(",", ":")),
    }


def provider_row(doc: dict) -> dict:
    row = {"provider_id": doc["providerId"], **_common(doc)}
    row.update({
        "ownership_type": doc.get("ownershipType"),
        "companies_house_number": doc.get("companiesHouseNumber"),
        "charity_number": doc.get("charityNumber"),
        "location_ids": json.dumps(doc.get("locationIds") or []),
        "inspection_areas": piped(a.get("inspectionAreaName") for a in doc.get("inspectionAreas") or []),
        "contacts_json": json.dumps(doc.get("contacts") or []),
    })
    return row


def location_row(doc: dict) -> dict:
    row = {"location_id": doc["locationId"], "provider_id": doc.get("providerId"), **_common(doc)}
    beds = doc.get("numberOfBeds")
    row.update({
        "dormancy": doc.get("dormancy"),
        "dormancy_start_date": _date(doc.get("dormancyStartDate")),
        "dormancy_end_date": _date(doc.get("dormancyEndDate")),
        "number_of_beds": int(beds) if isinstance(beds, (int, float)) or (isinstance(beds, str) and beds.isdigit()) else None,
        "registered_manager_absent_date": _date(doc.get("registeredManagerAbsentDate")),
        "care_home": doc.get("careHome"),
        "ccg_code": doc.get("onspdCcgCode") or doc.get("odsCcgCode"),
        "ccg_name": doc.get("onspdCcgName") or doc.get("odsCcgName"),
        "location_types": piped(t.get("type") for t in doc.get("locationTypes") or []),
        "service_types": piped(s.get("name") for s in doc.get("gacServiceTypes") or []),
        "specialisms": piped(s.get("name") for s in doc.get("specialisms") or []),
    })
    return row


def _upsert(db, table: str, key: str, row: dict, source: str) -> None:
    ts = now_iso()
    row = {**row, "source": source, "cqc_synced_at": ts}
    cols = list(row)
    insert_cols = cols + ["first_seen_at"]
    placeholders = ", ".join("?" for _ in insert_cols)
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != key)
    db.execute(
        f"INSERT INTO {table} ({', '.join(insert_cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({key}) DO UPDATE SET {updates}",
        [row[c] for c in cols] + [ts],
    )


def _replace_children(db, entity_type: str, entity_id: str, doc: dict) -> None:
    for table in ("cqc_regulated_activities", "cqc_ratings", "cqc_reports", "cqc_relationships"):
        db.execute(f"DELETE FROM {table} WHERE entity_type = ? AND entity_id = ?", (entity_type, entity_id))
    for act in doc.get("regulatedActivities") or []:
        if act.get("code") or act.get("name"):
            contacts = act.get("contacts") or ([act["nominatedIndividual"]] if act.get("nominatedIndividual") else [])
            db.execute("INSERT INTO cqc_regulated_activities VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                       (entity_type, entity_id, act.get("code") or act.get("name"), act.get("name"),
                        json.dumps(contacts)))
    current = doc.get("currentRatings") or {}
    overall = current.get("overall") or {}
    if overall.get("rating"):
        db.execute("INSERT INTO cqc_ratings VALUES (?, ?, ?, 1, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                   (entity_type, entity_id, overall.get("reportLinkId") or "current",
                    _date(overall.get("reportDate") or current.get("reportDate")), overall.get("rating"),
                    json.dumps(overall.get("keyQuestionRatings") or []),
                    json.dumps(current.get("serviceRatings") or [])))
    for hist in doc.get("historicRatings") or []:
        ho = hist.get("overall") or {}
        db.execute("INSERT INTO cqc_ratings VALUES (?, ?, ?, 0, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                   (entity_type, entity_id, hist.get("reportLinkId") or hist.get("reportDate") or "historic",
                    _date(hist.get("reportDate")), ho.get("rating"),
                    json.dumps(ho.get("keyQuestionRatings") or []),
                    json.dumps(hist.get("serviceRatings") or [])))
    for rep in doc.get("reports") or []:
        if rep.get("linkId"):
            db.execute("INSERT INTO cqc_reports VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                       (entity_type, entity_id, rep["linkId"], _date(rep.get("reportDate")),
                        rep.get("reportUri"), rep.get("reportType"), _date(rep.get("firstVisitDate"))))
    id_key, name_key = (("relatedProviderId", "relatedProviderName") if entity_type == "provider"
                        else ("relatedLocationId", "relatedLocationName"))
    for rel in doc.get("relationships") or []:
        if rel.get(id_key):
            db.execute("INSERT INTO cqc_relationships VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                       (entity_type, entity_id, rel[id_key], rel.get(name_key), rel.get("type") or "",
                        rel.get("reason")))


def upsert_provider(db, doc: dict, source: str = "CQC") -> str:
    row = provider_row(doc)
    pid = row["provider_id"]
    _upsert(db, "cqc_providers", "provider_id", row, source)
    _replace_children(db, "provider", pid, doc)
    for lid in doc.get("locationIds") or []:
        db.execute("INSERT INTO cqc_provider_locations VALUES (?, ?) ON CONFLICT DO NOTHING", (pid, lid))
    return pid


def upsert_location(db, doc: dict, source: str = "CQC") -> str:
    row = location_row(doc)
    lid = row["location_id"]
    _upsert(db, "cqc_locations", "location_id", row, source)
    _replace_children(db, "location", lid, doc)
    db.execute("DELETE FROM cqc_service_types WHERE location_id = ?", (lid,))
    db.execute("DELETE FROM cqc_specialisms WHERE location_id = ?", (lid,))
    for st in doc.get("gacServiceTypes") or []:
        if st.get("name"):
            db.execute("INSERT INTO cqc_service_types VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                       (lid, st["name"], st.get("description")))
    for sp in doc.get("specialisms") or []:
        if sp.get("name"):
            db.execute("INSERT INTO cqc_specialisms VALUES (?, ?) ON CONFLICT DO NOTHING", (lid, sp["name"]))
    if row["provider_id"]:
        db.execute("INSERT INTO cqc_provider_locations VALUES (?, ?) ON CONFLICT DO NOTHING",
                   (row["provider_id"], lid))
    return lid
