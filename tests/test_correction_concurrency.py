import concurrent.futures
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from civic_flow.api import create_app


class CorrectionConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "db.sqlite3"
        self.client = TestClient(create_app(self.path))
        self.client.put("/grids/g1?actor=admin", json={"grid_id": "g1", "district": "north"})
        self.client.put("/grids/g2?actor=admin", json={"grid_id": "g2", "district": "south"})
        self.client.put("/routes/noise?actor=admin", json={
            "category": "noise", "department": "team_a", "response_hours": 24,
            "resolution_hours": 72})
        self.client.put("/routes/lighting?actor=admin", json={
            "category": "lighting", "department": "team_b", "response_hours": 48,
            "resolution_hours": 168})
        self.client.post("/batches", json={
            "batch_key": "b", "submitted_at": "2026-06-02T00:00:00Z",
            "cases": [{"external_id": "c1", "grid_id": "g1", "category": "noise",
                       "summary": "Synthetic appeal fixture", "resident_token": "resident-1",
                       "occurred_at": "2026-06-01T00:00:00Z"}]})
        self.case_id = self.client.get("/cases").json()["items"][0]["case_id"]

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def correction_payload(self, key):
        return {"correction_key": key, "expected_version": 1,
                "submitted_at": "2026-06-03T00:00:00Z",
                "effective_at": "2026-06-01T00:00:00Z", "source": "hotline",
                "category": "lighting", "note": "amendment " + key}

    def event_payload(self, key):
        return {"event_key": key, "kind": "responded", "expected_version": 1,
                "actor": "team_a", "occurred_at": "2026-06-01T12:00:00Z",
                "note": "response " + key}

    def test_correction_and_response_compete_for_same_version(self):
        def submit(kind):
            with TestClient(create_app(self.path)) as other:
                if kind == "correction":
                    resp = other.post(f"/cases/{self.case_id}/corrections",
                                      json=self.correction_payload("k-1"))
                else:
                    resp = other.post(f"/cases/{self.case_id}/events",
                                      json=self.event_payload("e-1"))
                return resp.status_code, resp.json()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(submit, ("correction", "event")))
        statuses = sorted(code for code, _ in results)
        self.assertEqual(statuses, [200, 409])
        case = self.client.get(f"/cases/{self.case_id}").json()
        self.assertEqual(case["version"], 2)
        # 案件只计一次，更正与事件至多各一条
        counts = self.client.get("/health").json()["counts"]
        self.assertEqual(counts["cases"], 1)
        self.assertLessEqual(counts["case_corrections"], 1)

    def test_concurrent_identical_corrections_commit_once(self):
        def submit(_):
            with TestClient(create_app(self.path)) as other:
                resp = other.post(f"/cases/{self.case_id}/corrections",
                                  json=self.correction_payload("same-key"))
                return resp.status_code, resp.json().get("replayed")
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(submit, range(4)))
        self.assertEqual([code for code, _ in results], [200] * 4)
        self.assertEqual(sum(1 for _, replayed in results if not replayed), 1)
        self.assertEqual(self.client.get("/health").json()["counts"]["case_corrections"], 1)

    def test_reopen_database_preserves_correction_and_disputes(self):
        self.client.post(f"/cases/{self.case_id}/events", json=self.event_payload("e-1"))
        self.client.post(f"/cases/{self.case_id}/corrections", json={
            "correction_key": "k-1", "expected_version": 2,
            "submitted_at": "2026-06-03T00:00:00Z",
            "effective_at": "2026-06-01T00:00:00Z", "source": "hotline",
            "category": "lighting", "note": "reclassify"})
        with TestClient(create_app(self.path)) as later:
            chain = later.get(f"/cases/{self.case_id}/corrections").json()
            self.assertEqual(len(chain["items"]), 1)
            disputes = later.get(f"/cases/{self.case_id}/disputes").json()
            self.assertGreaterEqual(disputes["count"], 1)
            case = later.get(f"/cases/{self.case_id}").json()
            self.assertEqual(case["category"], "lighting")
            self.assertEqual(case["dispute_count"], disputes["count"])


if __name__ == "__main__":
    unittest.main()
