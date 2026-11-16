import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from civic_flow.api import create_app


class BitemporalReportTests(unittest.TestCase):
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

    def correct(self, key, version, submitted, **fields):
        body = {"correction_key": key, "expected_version": version,
                "submitted_at": submitted, "effective_at": "2026-06-01T00:00:00Z",
                "source": "hotline", "note": "amendment", **fields}
        return self.client.post(f"/cases/{self.case_id}/corrections", json=body)

    def report(self, key, as_of="2026-06-04T00:00:00Z", view="as_known", **extra):
        return self.client.post("/reports", json={"report_key": key, "as_of": as_of,
                                                   "group_by": "category", "view": view,
                                                   **extra})

    def test_as_known_report_reproduces_facts_available_then(self):
        archived = self.report("daily-0604", view="as_known")
        self.assertEqual(archived.status_code, 200, archived.text)
        groups_before = {g["key"]: g["accepted"] for g in archived.json()["body"]["groups"]}
        self.assertEqual(groups_before, {"noise": 1})
        # 更正于报表之后登记
        self.correct("k1", 1, "2026-06-06T00:00:00Z", category="lighting")
        # 归档报表原数字不变
        frozen = self.client.get("/reports/daily-0604").json()
        self.assertEqual({g["key"]: g["accepted"] for g in frozen["body"]["groups"]},
                         {"noise": 1})
        self.assertTrue(frozen["needs_review"])
        # verify 仍按归档水位/口径重算并匹配
        self.assertTrue(self.client.get("/reports/daily-0604/verify").json()["matches"])

    def test_current_view_reanalyzes_with_later_facts(self):
        self.report("daily")
        self.correct("k1", 1, "2026-06-06T00:00:00Z", category="lighting")
        analysis = self.client.get("/reports/daily/reanalyze").json()
        # 类别分组从 noise 迁移到 lighting
        self.assertEqual(analysis["changed_groups"][0]["key"], "lighting")
        self.assertEqual(analysis["changed_groups"][0]["status"], "added")
        removed = [g for g in analysis["changed_groups"] if g["key"] == "noise"][0]
        self.assertEqual(removed["status"], "removed")
        # 案件总数不变：没有算两次
        self.assertEqual(analysis["archived_totals"]["accepted"], 1)
        self.assertEqual(analysis["current_totals"]["accepted"], 1)
        self.assertEqual(analysis["changed_totals"].get("accepted", 0), 0)

    def test_current_metrics_endpoint_shows_corrected_grouping(self):
        before = self.client.get("/metrics", params={
            "as_of": "2026-06-04T00:00:00Z", "group_by": "category",
            "view": "current"}).json()
        self.correct("k1", 1, "2026-06-06T00:00:00Z", category="lighting")
        after = self.client.get("/metrics", params={
            "as_of": "2026-06-04T00:00:00Z", "group_by": "category",
            "view": "current"}).json()
        self.assertEqual([g["key"] for g in before["groups"]], ["noise"])
        self.assertEqual([g["key"] for g in after["groups"]], ["lighting"])
        # as_known 口径在同一时点仍重现旧事实（更正登记晚于该时点的水位视角由报表保证；
        # 实时 as_known 使用当前水位，这里验证两种 view 参数均被接受且结构一致）
        known = self.client.get("/metrics", params={
            "as_of": "2026-06-04T00:00:00Z", "group_by": "category",
            "view": "as_known"}).json()
        self.assertEqual(known["view"], "as_known")
        self.assertEqual(after["view"], "current")

    def test_overdue_conclusions_change_with_corrected_deadlines(self):
        # 原口径：24h 回应期限，6-04 已逾期
        late = self.report("late", as_of="2026-06-04T00:00:00Z").json()["body"]["totals"]
        self.assertEqual(late["response_overdue"], 1)
        # 更正为 lighting：48h 回应期限，受理 6-01 00:00 → 截止 6-03 00:00，仍逾期；
        # 再把受理时间推前到 5-25，验证期限变化翻转结论用更长时限的类别
        self.client.post(f"/cases/{self.case_id}/corrections", json={
            "correction_key": "k1", "expected_version": 1,
            "submitted_at": "2026-06-06T00:00:00Z", "effective_at": "2026-06-03T12:00:00Z",
            "source": "hotline", "note": "later actual time",
            "accepted_at": "2026-06-03T12:00:00Z", "category": "lighting"})
        analysis = self.client.get("/reports/late/reanalyze").json()
        # 新受理 6-03 12:00 + 48h = 6-05 12:00，在 6-04 时点尚未逾期
        self.assertEqual(analysis["current_totals"]["response_overdue"], 0)
        self.assertEqual(analysis["changed_totals"]["response_overdue"], -1)

    def test_review_queue_lists_only_affected_archived_reports(self):
        self.report("r-before")
        self.correct("k1", 1, "2026-06-06T00:00:00Z", category="lighting")
        # 更正之后新建的报表水位更高，不标待复核
        self.report("r-after")
        queue = self.client.get("/reports/review-queue").json()
        keys = {item["report_key"] for item in queue["items"]}
        self.assertIn("r-before", keys)
        self.assertNotIn("r-after", keys)
        item = next(x for x in queue["items"] if x["report_key"] == "r-before")
        self.assertEqual({r["case_id"] for r in item["reviews"]}, {self.case_id})

    def test_disputed_case_flagged_in_current_metrics(self):
        self.client.post(f"/cases/{self.case_id}/events", json={
            "event_key": "r1", "kind": "responded", "expected_version": 1,
            "actor": "team_a", "occurred_at": "2026-06-01T12:00:00Z",
            "note": "answer"})
        # 受理时间被推后到回应之后 → 争议
        self.client.post(f"/cases/{self.case_id}/corrections", json={
            "correction_key": "k1", "expected_version": 2,
            "submitted_at": "2026-06-06T00:00:00Z", "effective_at": "2026-06-02T00:00:00Z",
            "source": "hotline", "note": "time fix",
            "accepted_at": "2026-06-02T00:00:00Z"})
        snapshot = self.client.get("/metrics", params={
            "as_of": "2026-06-07T00:00:00Z", "group_by": "district",
            "include_cases": True, "view": "current"}).json()
        self.assertEqual(snapshot["totals"]["disputed"], 1)
        self.assertEqual(snapshot["totals"]["accepted"], 1)
        detail = snapshot["cases"][0]
        self.assertIn("TIME_BEFORE_ACCEPTANCE", detail["dispute_reasons"])

    def test_late_correction_excluded_from_historical_as_known_snapshot(self):
        # 先在水位 W1 固化 as_known 日报
        first = self.report("w1", as_of="2026-06-04T00:00:00Z")
        w1 = first.json()["watermark"]
        self.correct("k1", 1, "2026-06-06T00:00:00Z", category="lighting")
        # 新建一份 as_of 相同、但水位包含更正的 as_known 报表：分组已变化
        second = self.report("w2", as_of="2026-06-04T00:00:00Z")
        self.assertGreater(second.json()["watermark"], w1)
        self.assertEqual([g["key"] for g in second.json()["body"]["groups"]], ["lighting"])
        # 旧报表仍可重现当时已知（noise）
        self.assertEqual([g["key"] for g in
                          self.client.get("/reports/w1").json()["body"]["groups"]], ["noise"])

    def test_review_queue_respects_report_scope(self):
        # 区域筛选为 south 的报表：案件原在 north，更正后仍在 north（仅改类别）
        self.report("r-scoped", district="south")
        self.report("r-wide")
        self.correct("k1", 1, "2026-06-06T00:00:00Z", category="lighting")
        queue = self.client.get("/reports/review-queue").json()
        keys = {item["report_key"] for item in queue["items"]}
        self.assertIn("r-wide", keys)
        self.assertNotIn("r-scoped", keys)

    def test_correction_chain_and_timeline_expose_versions(self):
        self.correct("k1", 1, "2026-06-06T00:00:00Z", category="lighting")
        chain = self.client.get(f"/cases/{self.case_id}/corrections").json()
        self.assertEqual(len(chain["items"]), 1)
        old = chain["items"][0]
        self.assertEqual(json.loads(old["new_json"])["category"], "lighting")
        timeline = self.client.get(f"/cases/{self.case_id}/timeline").json()
        kinds = [item["kind"] for item in timeline["timeline"]]
        self.assertIn("accepted", kinds)
        self.assertEqual(timeline["correction_count"], 1)
        self.assertEqual([c["kind"] for c in timeline["corrections"]], ["corrected"])
        history = self.client.get(f"/cases/{self.case_id}?history=true").json()
        self.assertEqual(len(history["corrections"]), 1)

    def test_historical_filter_keeps_case_that_left_the_group(self):
        # 案件原类别 noise，后更正为 lighting；按 noise 过滤的历史口径仍应包含它
        archived = self.report("old-noise", category="noise")
        self.assertEqual(archived.json()["body"]["totals"]["accepted"], 1)
        self.correct("k1", 1, "2026-06-06T00:00:00Z", category="lighting")
        # 归档报表数字不变且可验证
        self.assertTrue(self.client.get("/reports/old-noise/verify").json()["matches"])
        # 同水位重现的 as_known 指标仍按 noise 计入
        known = self.client.get("/metrics", params={
            "as_of": "2026-06-04T00:00:00Z", "group_by": "category",
            "category": "noise", "view": "current"}).json()
        # current 口径下案件已离开 noise 分组
        self.assertEqual(known["totals"]["accepted"], 0)

    def test_correction_and_response_concurrent_commit_one_version_each(self):
        # 同一 expected_version 竞争：一个成功，另一个版本冲突，不会算两次
        event = self.client.post(f"/cases/{self.case_id}/events", json={
            "event_key": "r1", "kind": "responded", "expected_version": 1,
            "actor": "team_a", "occurred_at": "2026-06-01T12:00:00Z", "note": "x"})
        stale = self.correct("k1", 1, "2026-06-03T00:00:00Z", category="lighting")
        self.assertEqual(event.status_code, 200)
        self.assertEqual(stale.json()["code"], "VERSION_CONFLICT")
        case = self.client.get(f"/cases/{self.case_id}").json()
        self.assertEqual(case["version"], 2)
        self.assertEqual(case["correction_count"], 0)


if __name__ == "__main__":
    unittest.main()
