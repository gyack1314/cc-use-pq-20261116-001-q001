"""来源版本更正：保留原始事实，重算责任期限，对无法接续的办理记录留争议。

更正不是重新受理：受理事件不重放、案件不重复计数，已发生的回应/转派/结案
保持不可变；更正以独立的来源版本链保存，业务时点重建（metrics.reconstruct）
据此在“当时已知(as_known)”与“后来事实(current)”两种口径下分别回放。
"""
from __future__ import annotations

import json
from datetime import datetime

from .contracts import CorrectionInput, DomainError, utc
from .routing import RoutingService
from .store import Store, canonical, digest


class CorrectionService:
    def __init__(self, store: Store):
        self.store = store

    @staticmethod
    def _effective_values(case: dict, correction: CorrectionInput) -> dict:
        return {
            "grid_id": correction.grid_id if correction.grid_id is not None else case["grid_id"],
            "category": correction.category if correction.category is not None else case["category"],
            "accepted_at": (utc(correction.accepted_at) if correction.accepted_at is not None
                            else case["accepted_at"]),
        }

    @staticmethod
    def _check_event(event: dict, state: dict) -> tuple[str, str] | None:
        """按更正后的时间线与责任部门，判断一条既存办理记录能否接续。"""
        kind = event["kind"]
        at = event["occurred_at"]
        if at < state["accepted_at"]:
            return ("TIME_BEFORE_ACCEPTANCE",
                    f"{kind} at {at} predates corrected acceptance {state['accepted_at']}")
        if kind == "assigned":
            if event["department"] != state["department"]:
                return ("RESPONSIBILITY_MISMATCH",
                        f"assignment to {event['department']} does not follow corrected "
                        f"owner {state['department']}")
            state["assigned_actors"].add(event["actor"])
            state["status"] = "assigned"
            return None
        if kind in {"responded", "explained"}:
            if state["status"] not in {"accepted", "assigned", "responded",
                                       "explained", "reopened"}:
                return ("FLOW_GAP",
                        f"{kind} cannot follow status {state['status']} on corrected timeline")
            owner = event["department"] or event["actor"]
            if owner != state["department"] and owner not in state["assigned_actors"]:
                return ("RESPONSIBILITY_MISMATCH",
                        f"{kind} owner {owner} is outside corrected owner "
                        f"{state['department']}")
            state["status"] = kind
            if state["first_response_at"] is None:
                state["first_response_at"] = at
            return None
        if kind == "acknowledged":
            if state["status"] not in {"responded", "explained"}:
                return ("FLOW_GAP", "acknowledgment has no preceding response on corrected timeline")
            state["status"] = "acknowledged"
            return None
        if kind == "closed":
            if state["status"] not in {"responded", "explained", "acknowledged"}:
                return ("FLOW_GAP", "closure has no preceding response on corrected timeline")
            if event["resolution"] == "solved" and state["status"] == "explained":
                return ("CLOSURE_MISMATCH",
                        "solved closure follows explanation-only handling on corrected timeline")
            state["status"] = "closed"
            state["resolution"] = event["resolution"]
            return None
        if kind == "reopened":
            if state["status"] != "closed":
                return ("FLOW_GAP", "reopening requires a prior closure on corrected timeline")
            state["status"] = "reopened"
            state["resolution"] = None
            return None
        return ("FLOW_GAP", f"unknown event kind {kind}")

    def _continuity(self, case: dict, route, events: list[dict]) -> list[dict]:
        # state.department 始终是更正后的适用责任部门；只有转派给该部门的办理人
        # 才进入 assigned_actors。原责任部门（如 team_a）在责任改判后不在其中，
        # 其回应/结案即判定为责任不接续。
        state = {"accepted_at": route.accepted_at, "department": route.department,
                 "status": "accepted", "resolution": None, "first_response_at": None,
                 "assigned_actors": set()}
        findings: list[dict] = []
        for event in events:
            problem = self._check_event(event, state)
            if problem is not None:
                reason, detail = problem
                findings.append({"event_id": event["event_id"], "reason": reason, "detail": detail})
        return findings

    def submit(self, case_id: int, correction: CorrectionInput) -> dict:
        payload = correction.model_dump(mode="json", exclude={"expected_version"})
        fingerprint = digest(payload)
        submitted = utc(correction.submitted_at)
        effective = utc(correction.effective_at)
        if effective > submitted:
            raise DomainError("FUTURE_CORRECTION",
                              "Correction cannot take effect before it is submitted", 422)
        with self.store.transaction() as db:
            case = self.store.case(db, case_id)
            prior = self.store.correction_by_key(db, case_id, correction.correction_key)
            if prior is not None:
                # 重复更正：同键同文幂等重放；同键异文冲突，绝不覆盖较新事实。
                if prior["digest"] != fingerprint:
                    raise DomainError("CORRECTION_KEY_CONFLICT",
                                      "Correction key already holds different content", 409)
                return {"case": self.store.case(db, case_id), "correction": prior,
                        "replayed": True, "disputes": self.store.disputes(db, case_id)}
            if case["version"] != correction.expected_version:
                raise DomainError("VERSION_CONFLICT", "Case version changed", 409,
                                  {"expected": correction.expected_version,
                                   "actual": case["version"]})
            latest = self.store.latest_correction(db, case_id)
            if latest is not None and latest["submitted_at"] > submitted:
                # 旧版本晚到：系统时点以提交登记为准，禁止回退已采用的事实。
                raise DomainError("STALE_CORRECTION",
                                  "A newer correction is already recorded", 409,
                                  {"latest_submitted_at": latest["submitted_at"]})
            values = self._effective_values(case, correction)
            if all(values[key] == case[key] for key in values):
                raise DomainError("NO_CORRECTION_CHANGE",
                                  "Correction repeats the already recorded case facts", 422)
            effective_at = datetime.fromisoformat(values["accepted_at"])
            # 按更正后的受理时间与类别重新确定适用责任与期限。
            route = RoutingService.resolve(db, values["grid_id"], values["category"],
                                           effective_at)
            old_snapshot = {key: case[key] for key in
                            ("grid_id", "district", "category", "accepted_at",
                             "department", "route_revision", "response_due", "resolution_due")}
            new_snapshot = {"grid_id": route.grid_id, "district": route.district,
                            "category": route.category, "accepted_at": values["accepted_at"],
                            "department": route.department, "route_revision": route.route_revision,
                            "response_due": route.response_due,
                            "resolution_due": route.resolution_due}
            events = self.store.events(db, case_id)
            findings = self._continuity(case, route, events)
            # 登记更正来源版本（journal），再写更正链。
            sequence = self.store.journal(db, "case_corrected", "case", str(case_id),
                                          {"key": correction.correction_key,
                                           "source": correction.source,
                                           "submitted_at": submitted, "effective_at": effective,
                                           "old": old_snapshot, "new": new_snapshot,
                                           "dispute_reasons": sorted({f["reason"] for f in findings})},
                                          submitted)
            cursor = db.execute(
                "INSERT INTO case_corrections(case_id,correction_key,expected_version,case_version,"
                "submitted_at,effective_at,source,old_json,new_json,route_revision,"
                "response_due,resolution_due,sequence,digest) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (case_id, correction.correction_key, correction.expected_version,
                 case["version"] + 1, submitted, effective, correction.source,
                 canonical(old_snapshot), canonical(new_snapshot), route.route_revision,
                 route.response_due, route.resolution_due, sequence, fingerprint))
            correction_id = int(cursor.lastrowid)
            for finding in findings:
                dispute_sequence = self.store.journal(
                    db, "case_dispute", "case", str(case_id),
                    {"correction_id": correction_id, **finding}, submitted)
                self.store.add_dispute(db, case_id, correction_id, finding["event_id"],
                                       finding["reason"], finding["detail"], dispute_sequence)
            # 更新案件当前事实（原始受理事件与办理事件均不可变、不重放）。
            db.execute(
                "UPDATE cases SET grid_id=?,district=?,category=?,accepted_at=?,department=?,"
                "initial_department=?,route_revision=?,response_due=?,resolution_due=?,"
                "version=version+1,correction_count=correction_count+1,corrected_at=?,"
                "dispute_count=(SELECT COUNT(DISTINCT event_id) FROM case_disputes WHERE case_id=?) "
                "WHERE case_id=?",
                (route.grid_id, route.district, route.category, values["accepted_at"],
                 route.department, route.department, route.route_revision,
                 route.response_due, route.resolution_due, effective, case_id, case_id))
            self._flag_reports(db, case_id, correction_id, sequence)
            current = self.store.case(db, case_id)
            stored = self.store.correction_by_key(db, case_id, correction.correction_key)
            return {"case": current, "correction": stored, "replayed": False,
                    "disputes": self.store.disputes(db, case_id),
                    "new_disputes": findings}

    @staticmethod
    def _flag_reports(db, case_id: int, correction_id: int, sequence: int) -> None:
        """已归档报表保留原数字；凡口径覆盖该案件且水位早于更正的，标待复核。"""
        case = Store.case(db, case_id)
        correction = db.execute(
            "SELECT * FROM case_corrections WHERE correction_id=?",
            (correction_id,)).fetchone()
        old = json.loads(correction["old_json"])
        rows = db.execute(
            "SELECT report_key, watermark, as_of, district, category FROM reports").fetchall()
        for record in rows:
            if int(record["watermark"]) >= sequence:
                continue
            # 报表时点须覆盖该案件（更正前后任一受理时间早于 as_of）。
            if old["accepted_at"] > record["as_of"] and case["accepted_at"] > record["as_of"]:
                continue
            # 报表的区域/类别筛选须在更正前或更正后覆盖该案件。
            if record["district"] is not None and record["district"] not in (
                    old["district"], case["district"]):
                continue
            if record["category"] is not None and record["category"] not in (
                    old["category"], case["category"]):
                continue
            Store.mark_report_review(db, record["report_key"], case_id,
                                     correction_id, sequence)

    def list_corrections(self, case_id: int) -> dict:
        with self.store.connection() as db:
            self.store.case(db, case_id)
            return {"case_id": case_id,
                    "items": self.store.corrections(db, case_id)}

    def list_disputes(self, case_id: int | None = None) -> dict:
        with self.store.connection() as db:
            if case_id is None:
                rows = [dict(item) for item in db.execute(
                    "SELECT * FROM case_disputes ORDER BY dispute_id")]
            else:
                self.store.case(db, case_id)
                rows = self.store.disputes(db, case_id)
            return {"items": rows, "count": len(rows),
                    "watermark": self.store.watermark(db)}

    def review_queue(self) -> dict:
        with self.store.connection() as db:
            reports = [dict(item) for item in db.execute(
                "SELECT report_key,as_of,watermark,group_by,district,category,created_at,"
                "needs_review,review_marked_sequence FROM reports WHERE needs_review=1 "
                "ORDER BY review_marked_sequence, report_key")]
            for report in reports:
                report["reviews"] = self.store.review_rows(db, report["report_key"])
            watermark = self.store.watermark(db)
            return {"watermark": watermark, "count": len(reports), "items": reports}

    def correction_chain(self, case_id: int) -> dict:
        with self.store.connection() as db:
            case = self.store.case(db, case_id)
            items = []
            for record in self.store.corrections(db, case_id):
                entry = dict(record)
                entry["old"] = json.loads(entry.pop("old_json"))
                entry["new"] = json.loads(entry.pop("new_json"))
                items.append(entry)
            return {"case_id": case_id, "source": case["source"],
                    "external_id": case["external_id"],
                    "correction_count": case["correction_count"],
                    "dispute_count": case["dispute_count"], "items": items}
