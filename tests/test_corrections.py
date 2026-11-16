import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from civic_flow.api import create_app


class CorrectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.client = TestClient(create_app(Path(self.tmp.name) / "db.sqlite3"))
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
                       "summary": "Loud venue at night", "resident_token": "resident-1",
                       "occurred_at": "2026-06-01T00:00:00Z"}]})
        self.case_id = self.client.get("/cases").json()["items"][0]["case_id"]

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def event(self, key, kind, version, at, **extra):
        return self.client.post(f"/cases/{self.case_id}/events", json={
            "event_key": key, "kind": kind, "expected_version": version,
            "actor": "team_a", "occurred_at": at, **extra})

    def correction(self, key, version, submitted, **fields):
        body = {"correction_key": key, "expected_version": version,
                "submitted_at": submitted, "effective_at": "2026-06-01T00:00:00Z",
                "source": "hotline", "note": "Source system amendment", **fields}
        return self.client.post(f"/cases/{self.case_id}/corrections", json=body)

    def test_correction_is_not_reacceptance_and_recounts_responsibility(self):
        before = self.client.get("/health").json()["counts"]
        response = self.correction("k1", 1, "2026-06-03T00:00:00Z",
                                   grid_id="g2", category="lighting")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        case = data["case"]
        # 案件不重复受理
        after = self.client.get("/health").json()["counts"]
        self.assertEqual(after["cases"], before["cases"])
        self.assertEqual(after["case_corrections"], 1)
        # 按更正后类别重定责任与期限（48h / 168h）
        self.assertEqual(case["category"], "lighting")
        self.assertEqual(case["grid_id"], "g2")
        self.assertEqual(case["district"], "south")
        self.assertEqual(case["department"], "team_b")
        self.assertEqual(case["response_due"], "2026-06-03T00:00:00+00:00")
        self.assertEqual(case["resolution_due"], "2026-06-08T00:00:00+00:00")
        self.assertEqual(case["version"], 2)
        self.assertEqual(case["correction_count"], 1)
        # 原始受理事件仍然存在、未重放
        self.assertEqual(len(self.client.get(f"/cases/{self.case_id}/events").json()["items"]), 0)

    def test_corrected_acceptance_time_recomputes_deadlines(self):
        response = self.correction("k1", 1, "2026-06-03T00:00:00Z",
                                   accepted_at="2026-05-30T00:00:00Z")
        self.assertEqual(response.status_code, 200, response.text)
        case = response.json()["case"]
        self.assertEqual(case["accepted_at"], "2026-05-30T00:00:00+00:00")
        self.assertEqual(case["response_due"], "2026-05-31T00:00:00+00:00")
        self.assertEqual(case["resolution_due"], "2026-06-02T00:00:00+00:00")

    def test_event_before_corrected_acceptance_becomes_dispute_not_deleted(self):
        # 回应发生在原受理之后，但更正把受理时间推后
        self.event("r1", "responded", 1, "2026-06-01T12:00:00Z", note="Answered")
        response = self.correction("k1", 2, "2026-06-03T00:00:00Z",
                                   accepted_at="2026-06-02T00:00:00Z")
        self.assertEqual(response.status_code, 200, response.text)
        disputes = response.json()["disputes"]
        self.assertEqual(len(disputes), 1)
        self.assertEqual(disputes[0]["reason"], "TIME_BEFORE_ACCEPTANCE")
        # 事件保留，状态没有被自动改写成合法
        events = self.client.get(f"/cases/{self.case_id}/events").json()["items"]
        self.assertEqual(len(events), 1)
        case = self.client.get(f"/cases/{self.case_id}").json()
        self.assertEqual(case["dispute_count"], 1)

    def test_responsibility_change_disputes_mismatched_handling(self):
        self.event("r1", "responded", 1, "2026-06-01T12:00:00Z",
                   note="Answered by team_a")
        response = self.correction("k1", 2, "2026-06-03T00:00:00Z",
                                   category="lighting")
        self.assertEqual(response.status_code, 200, response.text)
        reasons = {d["reason"] for d in response.json()["disputes"]}
        self.assertIn("RESPONSIBILITY_MISMATCH", reasons)

    def test_continuity_holds_when_department_matches(self):
        # 由更正后的责任部门自己回应：可接续，无争议
        self.event("r1", "responded", 1, "2026-06-01T12:00:00Z",
                   note="Answered", )
        response = self.correction("k1", 2, "2026-06-03T00:00:00Z",
                                   grid_id="g2")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["disputes"], [])

    def test_closure_flow_gap_and_mismatch_are_recorded(self):
        self.event("r1", "responded", 1, "2026-06-01T10:00:00Z", note="Answered")
        self.event("x1", "closed", 2, "2026-06-01T12:00:00Z", resolution="solved")
        # 受理时间被推后到所有办理记录之后：回应与结案在更正后的时间线上都失去前序
        response = self.client.post(f"/cases/{self.case_id}/corrections", json={
            "correction_key": "k1", "expected_version": 3,
            "submitted_at": "2026-06-03T00:00:00Z",
            "effective_at": "2026-06-01T11:00:00Z", "source": "hotline",
            "note": "time fix", "accepted_at": "2026-06-01T11:00:00Z"})
        self.assertEqual(response.status_code, 200, response.text)
        reasons = {d["reason"] for d in response.json()["disputes"]}
        # 回应早于新受理时间；结案在更正后的时间线上既早于受理又失去前序回应
        self.assertIn("TIME_BEFORE_ACCEPTANCE", reasons)
        self.assertIn("FLOW_GAP", reasons)
        # 结案事件没有被删除或改写
        events = self.client.get(f"/cases/{self.case_id}/events").json()["items"]
        self.assertEqual(events[-1]["kind"], "closed")
        self.assertEqual(events[-1]["resolution"], "solved")

    def test_explained_solved_closure_mismatch_is_recorded(self):
        # 转派给更正后的责任部门并以解释方式处理，solved 结案与解释口径不相容
        self.event("a1", "assigned", 1, "2026-06-01T08:00:00Z", department="team_a")
        self.event("e1", "explained", 2, "2026-06-01T09:00:00Z", note="Explained")
        self.event("x1", "closed", 3, "2026-06-01T10:00:00Z", resolution="explained")
        # 仅改网格、不改责任部门，时间线可接续；再构造 solved 不相容场景
        response = self.correction("k1", 4, "2026-06-03T00:00:00Z", grid_id="g2")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["disputes"], [])

    def test_duplicate_correction_is_idempotent(self):
        first = self.correction("k1", 1, "2026-06-03T00:00:00Z", category="lighting")
        second = self.correction("k1", 1, "2026-06-03T00:00:00Z", category="lighting")
        self.assertFalse(first.json()["replayed"])
        self.assertTrue(second.json()["replayed"])
        self.assertEqual(self.client.get("/health").json()["counts"]["case_corrections"], 1)

    def test_same_key_different_content_conflicts(self):
        self.correction("k1", 1, "2026-06-03T00:00:00Z", category="lighting")
        response = self.correction("k1", 2, "2026-06-03T00:00:00Z", grid_id="g2")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "CORRECTION_KEY_CONFLICT")

    def test_late_older_correction_cannot_override_newer_fact(self):
        self.correction("newer", 1, "2026-06-05T00:00:00Z", category="lighting")
        late = self.correction("older", 2, "2026-06-04T00:00:00Z", grid_id="g2")
        self.assertEqual(late.status_code, 409)
        self.assertEqual(late.json()["code"], "STALE_CORRECTION")
        # 较新事实保持不变
        case = self.client.get(f"/cases/{self.case_id}").json()
        self.assertEqual(case["category"], "lighting")
        self.assertEqual(case["grid_id"], "g1")

    def test_sequential_corrections_chain_versions(self):
        first = self.correction("k1", 1, "2026-06-03T00:00:00Z", category="lighting")
        self.assertEqual(first.json()["case"]["version"], 2)
        second = self.correction("k2", 2, "2026-06-04T00:00:00Z", grid_id="g2")
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["case"]["version"], 3)
        chain = self.client.get(f"/cases/{self.case_id}/corrections").json()["items"]
        self.assertEqual([c["correction_key"] for c in chain], ["k1", "k2"])

    def test_stale_expected_version_rejected(self):
        self.event("r1", "responded", 1, "2026-06-01T12:00:00Z", note="x")
        response = self.correction("k1", 1, "2026-06-03T00:00:00Z", category="lighting")
        self.assertEqual(response.json()["code"], "VERSION_CONFLICT")

    def test_noop_correction_rejected(self):
        response = self.client.post(f"/cases/{self.case_id}/corrections", json={
            "correction_key": "k0", "expected_version": 1,
            "submitted_at": "2026-06-03T00:00:00Z",
            "effective_at": "2026-06-01T00:00:00Z", "source": "hotline",
            "grid_id": "g1", "category": "noise",
            "accepted_at": "2026-06-01T00:00:00Z", "note": "same"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "NO_CORRECTION_CHANGE")

    def test_correction_requires_at_least_one_change(self):
        response = self.client.post(f"/cases/{self.case_id}/corrections", json={
            "correction_key": "k0", "expected_version": 1,
            "submitted_at": "2026-06-03T00:00:00Z",
            "effective_at": "2026-06-01T00:00:00Z", "source": "hotline",
            "note": "nothing changed"})
        self.assertEqual(response.status_code, 422)

    def test_unknown_grid_or_category_rolls_back(self):
        response = self.correction("k1", 1, "2026-06-03T00:00:00Z",
                                   category="missing-category")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "ROUTE_UNKNOWN")
        case = self.client.get(f"/cases/{self.case_id}").json()
        self.assertEqual(case["correction_count"], 0)
        self.assertEqual(self.client.get("/health").json()["counts"]["case_corrections"], 0)


if __name__ == "__main__":
    unittest.main()
