"""使用本地合成数据运行离线验收。"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from .api import create_app

def required(response, expected: int = 200) -> dict:
    if response.status_code != expected:
        raise RuntimeError(f"Expected HTTP {expected}, got {response.status_code}: {response.text}")
    return response.json()

def run() -> dict:
    with tempfile.TemporaryDirectory() as folder:
        client = TestClient(create_app(Path(folder) / "demo.sqlite3"))
        required(client.put("/grids/g-1?actor=analyst",
                            json={"grid_id": "g-1", "district": "north", "active": True}))
        required(client.put("/grids/g-2?actor=analyst",
                            json={"grid_id": "g-2", "district": "south", "active": True}))
        required(client.put("/routes/lighting?actor=analyst", json={
            "category": "lighting", "department": "public_works", "priority": 5,
            "response_hours": 24, "resolution_hours": 168}))
        required(client.put("/routes/fitness?actor=analyst", json={
            "category": "fitness", "department": "parks", "priority": 6,
            "response_hours": 48, "resolution_hours": 240}))
        batch = {"batch_key": "demo-001", "submitted_at": "2026-01-02T12:00:00Z", "cases": [
            {"external_id": "n-1", "grid_id": "g-1", "category": "lighting",
             "summary": "Street lamp has failed", "occurred_at": "2026-01-01T08:00:00Z",
             "resident_token": "synthetic-person-1", "priority": 1},
            {"external_id": "s-1", "grid_id": "g-2", "category": "fitness",
             "summary": "Outdoor exercise equipment is damaged",
             "occurred_at": "2026-01-01T09:00:00Z",
             "resident_token": "synthetic-person-2", "priority": 2},
        ]}
        imported = required(client.post("/batches", json=batch))
        if imported["imported_count"] != 2:
            raise RuntimeError("Synthetic batch count changed")
        again = required(client.post("/batches", json=batch))
        if not again["replayed"]:
            raise RuntimeError("Batch replay was not idempotent")
        items = required(client.get("/cases"))["items"]
        first = items[0]["case_id"]
        event = {"event_key": "answer-1", "kind": "responded",
                 "occurred_at": "2026-01-02T08:00:00Z", "actor": "public_works",
                 "expected_version": 1, "note": "Repair team scheduled"}
        required(client.post(f"/cases/{first}/events", json=event))
        required(client.post(f"/cases/{first}/events", json={
            "event_key": "close-1", "kind": "closed", "actor": "public_works",
            "occurred_at": "2026-01-03T08:00:00Z", "expected_version": 2,
            "resolution": "solved"}))
        before = required(client.post("/reports", json={
            "report_key": "before", "as_of": "2026-01-02T07:00:00Z",
            "group_by": "district"}))
        after = required(client.post("/reports", json={
            "report_key": "after", "as_of": "2026-01-04T00:00:00Z",
            "group_by": "district"}))
        if before["body"]["totals"]["closed"] != 0:
            raise RuntimeError("Historic snapshot contains future closure")
        if after["body"]["totals"]["closed"] != 1:
            raise RuntimeError("Closure missing from later snapshot")
        quality = required(client.get("/quality/inspect", params={
            "as_of": "2026-01-04T00:00:00Z"}))
        if quality["issue_count"]:
            raise RuntimeError("Synthetic data failed reconciliation")
        check = required(client.get("/reports/after/verify"))
        if not check["matches"]:
            raise RuntimeError("Frozen report verification failed")
        # 来源版本更正：类别改判，责任与期限重算，原办理记录进入争议。
        required(client.put("/routes/noise?actor=analyst", json={
            "category": "noise", "department": "noise_team", "priority": 7,
            "response_hours": 12, "resolution_hours": 48}))
        correction = required(client.post(f"/cases/{first}/corrections", json={
            "correction_key": "fix-1", "expected_version": 3,
            "submitted_at": "2026-01-05T00:00:00Z",
            "effective_at": "2026-01-01T08:00:00Z", "source": "hotline",
            "category": "noise", "note": "Source reclassified the appeal"}))
        if correction["case"]["department"] != "noise_team":
            raise RuntimeError("Correction did not re-determine responsibility")
        if not correction["disputes"]:
            raise RuntimeError("Prior handling under another owner must open a dispute")
        archived = required(client.get("/reports/after"))
        if not archived["needs_review"]:
            raise RuntimeError("Archived report affected by correction must need review")
        if required(client.get("/reports/after/verify"))["matches"] is not True:
            raise RuntimeError("Archived figures must remain reproducible")
        reanalysis = required(client.get("/reports/after/reanalyze"))
        if reanalysis["current_totals"]["accepted"] != 2:
            raise RuntimeError("Correction must not double-count the case")
        if not reanalysis["changed_groups"]:
            raise RuntimeError("Reanalysis must explain the grouping change")
        duplicate = required(client.post(f"/cases/{first}/corrections", json={
            "correction_key": "fix-1", "expected_version": 3,
            "submitted_at": "2026-01-05T00:00:00Z",
            "effective_at": "2026-01-01T08:00:00Z", "source": "hotline",
            "category": "noise", "note": "Source reclassified the appeal"}))
        if not duplicate["replayed"]:
            raise RuntimeError("Duplicate correction must be idempotent")
        return {"accepted": imported["imported_count"],
                "closed_as_of_later_report": after["body"]["totals"]["closed"],
                "historic_closed": before["body"]["totals"]["closed"],
                "quality_issues": quality["issue_count"], "report_verified": check["matches"],
                "correction_disputes": len(correction["disputes"]),
                "review_queue": required(client.get("/reports/review-queue"))["count"],
                "reanalysis_group_changes": len(reanalysis["changed_groups"])}

if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, sort_keys=True))
