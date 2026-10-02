"""Tests for the CQC client, mapping, classification and re-sync behaviour (SQLite)."""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

from cqc.client import CQCClient, CQCError, CQCNotFound
from cqc.mapping import location_row, provider_row, upsert_location, upsert_provider
from cqc.rules import DEFAULT_RULES, classify_record, reclassify, size_tier, validate_rules
from cqc.sample import build_documents
from cqc.schema import CQC_SCHEMA_SQL

ATTACH = Path("/home/ubuntu/attachments")


class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def make_client(responses):
    session = mock.Mock(spec=requests.Session)
    session.get.side_effect = responses
    client = CQCClient("secret-key-123", "https://example.test/public/v1", max_retries=3, request_delay=0,
                       session=session)
    return client, session


class ClientTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("cqc.client.time.sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def test_sends_subscription_key_header(self):
        client, session = make_client([FakeResponse(200, {"providerId": "1-1"})])
        client.get_provider("1-1")
        _, kwargs = session.get.call_args
        self.assertEqual(kwargs["headers"]["Ocp-Apim-Subscription-Key"], "secret-key-123")
        self.assertTrue(session.get.call_args[0][0].endswith("/providers/1-1"))

    def test_retries_429_honouring_retry_after(self):
        client, _ = make_client([FakeResponse(429, {}, {"Retry-After": "7"}), FakeResponse(200, {"ok": 1})])
        self.assertEqual(client.get_location("1-2"), {"ok": 1})
        self.sleep.assert_any_call(7.0)

    def test_retries_server_errors_then_fails(self):
        client, session = make_client([FakeResponse(502, {})] * 4)
        with self.assertRaises(CQCError):
            client.get_provider("1-1")
        self.assertEqual(session.get.call_count, 4)

    def test_auth_error_not_retried_and_key_not_leaked(self):
        client, session = make_client([FakeResponse(401, {"message": "bad"})])
        with self.assertRaises(CQCError) as ctx:
            client.get_provider("1-1")
        self.assertEqual(session.get.call_count, 1)
        self.assertNotIn("secret-key-123", str(ctx.exception))

    def test_not_found(self):
        client, _ = make_client([FakeResponse(404, {})])
        with self.assertRaises(CQCNotFound):
            client.get_provider("nope")

    def test_missing_key(self):
        with mock.patch.dict(os.environ, {"CQC_API_KEY": ""}):
            client = CQCClient(None)
        self.assertFalse(client.configured)
        with self.assertRaises(CQCError):
            client.get_provider("1-1")

    def test_changes_normalised_to_ids(self):
        client, _ = make_client([FakeResponse(200, {"changes": ["1-1", {"providerId": "1-2"}], "totalPages": 1})])
        self.assertEqual(list(client.iter_changes("provider", "a", "b")), ["1-1", "1-2"])


class MappingAndSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db = sqlite3.connect(self.tmp.name)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(CQC_SCHEMA_SQL)
        self.providers, self.locations = build_documents()

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp.name)

    def load(self):
        for doc in self.providers:
            upsert_provider(self.db, doc, "SAMPLE")
        for doc in self.locations:
            upsert_location(self.db, doc, "SAMPLE")
        self.db.commit()

    def test_mapped_columns_exist_in_schema(self):
        pcols = {r[1] for r in self.db.execute("PRAGMA table_info(cqc_providers)")}
        lcols = {r[1] for r in self.db.execute("PRAGMA table_info(cqc_locations)")}
        self.assertLessEqual(set(provider_row(self.providers[0])), pcols)
        self.assertLessEqual(set(location_row(self.locations[0])), lcols)

    def test_schema_fields_used_by_mapper_exist_in_supplied_schemas(self):
        files = {"provider": "Syndication_Provider_Id.json", "location": "Syndication_Location_Id.json"}
        found = {k: list(ATTACH.glob(f"*/{v}")) for k, v in files.items()}
        if not all(found.values()):
            self.skipTest("Supplied CQC schema files not available")
        for kind, docs in (("provider", self.providers), ("location", self.locations)):
            schema = json.loads(found[kind][0].read_text())
            props = set((schema.get("properties") or schema.get("items", {}).get("properties") or {}).keys())
            if not props:
                self.skipTest("Schema format not recognised")
            self.assertLessEqual(set(docs[0]) - {"lastSyncedNote"}, props, kind)

    def test_resync_creates_no_duplicates_and_keeps_crm(self):
        self.load()
        pid = self.providers[0]["providerId"]
        self.db.execute("INSERT INTO lead_accounts VALUES ('a1', ?, 'CONTACTED', 'Sam', NULL, 'x', 'x')", (pid,))
        self.db.execute("INSERT INTO lead_notes VALUES ('n1', 'a1', 'note', 'me', 'x')")
        self.db.commit()
        counts = [self.db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in
                  ("cqc_providers", "cqc_locations", "cqc_provider_locations", "cqc_ratings", "cqc_service_types")]
        self.load()
        again = [self.db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in
                 ("cqc_providers", "cqc_locations", "cqc_provider_locations", "cqc_ratings", "cqc_service_types")]
        self.assertEqual(counts, again)
        self.assertEqual(tuple(self.db.execute("SELECT status, sales_owner FROM lead_accounts").fetchone()),
                         ("CONTACTED", "Sam"))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM lead_notes").fetchone()[0], 1)

    def test_classification_and_tiers(self):
        self.load()
        reclassify(self.db)
        rows = {r["entity_id"]: r for r in self.db.execute("SELECT * FROM cqc_classifications WHERE entity_type='provider'")}
        self.assertEqual(len(rows), len(self.providers))
        names = {p["providerId"]: p["name"] for p in self.providers}
        medical = [pid for pid, n in names.items() if "Medical" in n]
        self.assertTrue(medical)
        self.assertEqual(rows[medical[0]]["primary_segment"], "NON-TARGET")
        self.assertEqual(rows[medical[0]]["is_target"], 0)
        group = max(rows.values(), key=lambda r: r["location_count"])
        self.assertIn("|MULTI-SITE CARE GROUP|", group["segments"])
        self.assertEqual(size_tier(1, DEFAULT_RULES), "Independent")
        self.assertEqual(size_tier(6, DEFAULT_RULES), "Growing Group")
        self.assertEqual(size_tier(60, DEFAULT_RULES), "Enterprise Group")

    def test_record_rules(self):
        rec = {"service_types": "|Homecare agencies|", "inspection_directorate": "Adult social care"}
        self.assertIn("DOMICILIARY CARE", classify_record(rec, DEFAULT_RULES))
        self.assertEqual(classify_record({"inspection_directorate": "Hospitals"}, DEFAULT_RULES), ["NON-TARGET"])
        self.assertEqual(validate_rules(DEFAULT_RULES), [])
        self.assertTrue(validate_rules({"segments": "bad"}))


if __name__ == "__main__":
    unittest.main()
