"""按业务时点重建案件并计算回应指标。

双时间轴口径：
- as_known（当时已知）：仅采用在 as_of 之前已登记（journal 水位内）的更正，
  日报可重现当时掌握的情况；
- current（后来事实）：采用全部已登记更正，按最新事实重新分析。
两种口径都不会重放受理事件，案件始终只计一次。
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from typing import Iterable

from .contracts import DomainError, parse_time, utc
from .store import Store

VIEWS = ("as_known", "current")


def _apply_correction(item: dict, correction: dict) -> None:
    new = correction["new_json"] if "new_json" in correction else correction.get("new")
    if isinstance(new, str):
        new = json.loads(new)
    for field in ("grid_id", "district", "category", "accepted_at", "department",
                  "initial_department", "route_revision", "response_due",
                  "resolution_due"):
        if field in new:
            item[field] = new[field]
    item["corrected"] = True
    item["last_correction_sequence"] = correction["sequence"]


def _restore_baseline(base: dict, all_corrections: list[dict],
                      visible_corrections: list[dict]) -> dict:
    """cases 表保存当前事实；按水位回放时，先从更正链还原该水位之前的基准。"""
    item = dict(base)
    hidden = [c for c in all_corrections if c not in visible_corrections]
    if not hidden:
        return item
    # 最早一条不可见更正的 old_json 即水位时刻已知的案件事实。
    old = hidden[0]["old_json"]
    if isinstance(old, str):
        old = json.loads(old)
    for field in ("grid_id", "district", "category", "accepted_at", "department",
                  "initial_department", "route_revision", "response_due",
                  "resolution_due"):
        if field in old:
            item[field] = old[field]
    return item


def reconstruct(base: dict, events: Iterable[dict], as_of: str,
                corrections: Iterable[dict] | None = None,
                disputes: Iterable[dict] | None = None,
                all_corrections: Iterable[dict] | None = None) -> dict:
    corrections = list(corrections or ())
    all_corrections = list(all_corrections or ())
    item = _restore_baseline(base, all_corrections, corrections)
    item["status"] = "accepted"
    item["department"] = item["initial_department"]
    item["resolution"] = None
    item["first_response_at"] = None
    item["version"] = 1
    item["acknowledged"] = False
    item["explanation_count"] = 0
    item["response_count"] = 0
    item["reopen_count"] = 0
    item["last_event_at"] = item["accepted_at"]
    item["corrected"] = False
    item["disputed"] = False
    item["dispute_reasons"] = []
    for correction in corrections or ():
        _apply_correction(item, correction)
    disputed_event_ids = {row["event_id"] for row in (disputes or ())}
    item["disputed_event_ids"] = sorted(disputed_event_ids)
    item["disputed"] = bool(disputed_event_ids)
    item["dispute_reasons"] = sorted({row["reason"] for row in (disputes or ())})
    for event in events:
        if event["occurred_at"] > as_of:
            continue
        kind = event["kind"]
        item["last_event_at"] = event["occurred_at"]
        item["version"] = event["case_version"]
        if kind == "assigned":
            item["department"] = event["department"]
            item["status"] = "assigned"
        elif kind in {"responded", "explained"}:
            item["status"] = kind
            item["response_count"] += 1
            if kind == "explained":
                item["explanation_count"] += 1
            if item["first_response_at"] is None:
                item["first_response_at"] = event["occurred_at"]
        elif kind == "acknowledged":
            item["status"] = "acknowledged"
            item["acknowledged"] = True
        elif kind == "closed":
            item["status"] = "closed"
            item["resolution"] = event["resolution"]
        elif kind == "reopened":
            item["status"] = "reopened"
            item["resolution"] = None
            item["reopen_count"] += 1
        else:
            raise DomainError("CORRUPT_EVENT", "Unknown stored event kind", 500)
    return item

def derived(case: dict, as_of: str) -> dict:
    response_due = case["response_due"]
    resolution_due = case["resolution_due"]
    response_at = case["first_response_at"]
    responded = response_at is not None
    closed = case["status"] == "closed"
    return {
        "response_on_time": responded and response_at <= response_due,
        "response_overdue": (not responded and as_of > response_due) or
                            (responded and response_at > response_due),
        "resolution_overdue": (not closed and as_of > resolution_due) or
                              (closed and case["last_event_at"] > resolution_due),
        "responded": responded,
        "closed": closed,
        "solved": closed and case["resolution"] == "solved",
        "explained_close": closed and case["resolution"] == "explained",
        "out_of_scope": closed and case["resolution"] == "out_of_scope",
        "acknowledged": case["acknowledged"],
        "reopened": case["reopen_count"] > 0,
        "explained": case["explanation_count"] > 0,
        "corrected": case.get("corrected", False),
        "disputed": case.get("disputed", False),
    }

COUNTERS = (
    "accepted", "open", "responded", "response_on_time", "response_overdue",
    "resolution_overdue", "closed", "solved", "explained_close", "out_of_scope",
    "acknowledged", "reopened", "explained", "priority_one",
    "corrected", "disputed",
)

def empty_counters() -> dict[str, int]:
    return {key: 0 for key in COUNTERS}

def accrue(counters: dict, case: dict, flags: dict) -> None:
    counters["accepted"] += 1
    counters["open"] += int(not flags["closed"])
    for field in ("responded", "response_on_time", "response_overdue",
                  "resolution_overdue", "closed", "solved", "explained_close",
                  "out_of_scope", "acknowledged", "reopened", "explained",
                  "corrected", "disputed"):
        counters[field] += int(flags[field])
    counters["priority_one"] += int(case["priority"] == 1)

def ratios(counters: dict) -> dict:
    accepted = counters["accepted"]
    responded = counters["responded"]
    closed = counters["closed"]
    return {
        "response_rate": round(responded / accepted, 4) if accepted else None,
        "on_time_response_rate": round(counters["response_on_time"] / responded, 4)
                                 if responded else None,
        "closure_rate": round(closed / accepted, 4) if accepted else None,
        "solution_share_of_closed": round(counters["solved"] / closed, 4) if closed else None,
        "acknowledgment_rate": round(counters["acknowledged"] / responded, 4)
                               if responded else None,
    }

class MetricsService:
    def __init__(self, store: Store):
        self.store = store

    @staticmethod
    def _group(case: dict, key: str) -> str:
        return str(case[{"district": "district", "grid": "grid_id", "category": "category",
                         "department": "department"}[key]])

    def calculate(self, db, *, as_of: datetime, group_by: str,
                  district: str | None = None, category: str | None = None,
                  watermark: int | None = None, include_cases: bool = False,
                  view: str = "as_known") -> dict:
        stamp = utc(as_of)
        if group_by not in {"district", "grid", "category", "department"}:
            raise DomainError("BAD_GROUP", "Unsupported grouping")
        if view not in VIEWS:
            raise DomainError("BAD_VIEW", "View must be as_known or current")
        if watermark is None:
            watermark = self.store.watermark(db)
        correction_watermark = watermark if view == "as_known" else None
        dispute_watermark = watermark if view == "as_known" else None
        # 行级过滤只用水位；区域/类别/受理时间都可能被更正改写，
        # 统一在还原口径事实后于 Python 侧判定，避免当前 cases 行污染历史口径。
        filters = ["created_sequence<=?"]
        params: list[object] = [watermark]
        query = "SELECT * FROM cases WHERE " + " AND ".join(filters) + " ORDER BY case_id"
        rows = [dict(value) for value in db.execute(query, params)]
        totals = empty_counters()
        grouped: dict[str, dict] = defaultdict(empty_counters)
        details = []
        for base in rows:
            all_corrections = self.store.corrections(db, base["case_id"])
            corrections = (all_corrections if view == "current"
                           else self.store.corrections(db, base["case_id"], correction_watermark))
            effective_accepted = base["accepted_at"]
            if corrections:
                effective_accepted = json.loads(corrections[-1]["new_json"])["accepted_at"]
            elif all_corrections:
                effective_accepted = json.loads(all_corrections[0]["old_json"])["accepted_at"]
            if effective_accepted > stamp:
                continue
            events = self.store.events(db, base["case_id"], watermark)
            disputes = self.store.disputes(db, base["case_id"], dispute_watermark)
            case = reconstruct(base, events, stamp, corrections, disputes,
                               all_corrections=all_corrections)
            # 显式 district/category 筛选需按还原（口径内）后的事实重新判定。
            if district is not None and case["district"] != district:
                continue
            if category is not None and case["category"] != category:
                continue
            flags = derived(case, stamp)
            accrue(totals, case, flags)
            accrue(grouped[self._group(case, group_by)], case, flags)
            if include_cases:
                details.append({"case_id": case["case_id"], "status": case["status"],
                                "version": case["version"], "department": case["department"],
                                "district": case["district"], "flags": flags,
                                "dispute_reasons": case["dispute_reasons"],
                                "disputed_event_ids": case["disputed_event_ids"]})
        return {
            "as_of": stamp, "watermark": watermark, "group_by": group_by,
            "view": view,
            "filters": {"district": district, "category": category},
            "totals": {**totals, **ratios(totals)},
            "groups": [{"key": key, **value, **ratios(value)} for key, value in sorted(grouped.items())],
            **({"cases": details} if include_cases else {}),
        }

    def preview(self, *, as_of: datetime, group_by: str = "district",
                district: str | None = None, category: str | None = None,
                include_cases: bool = False, view: str = "as_known") -> dict:
        with self.store.transaction() as db:
            return self.calculate(db, as_of=as_of, group_by=group_by,
                                  district=district, category=category,
                                  include_cases=include_cases, view=view)

    def case_at(self, case_id: int, as_of: datetime, view: str = "as_known") -> dict:
        with self.store.transaction() as db:
            base = self.store.case(db, case_id)
            stamp = utc(as_of)
            watermark = self.store.watermark(db)
            correction_watermark = watermark if view == "as_known" else None
            dispute_watermark = watermark if view == "as_known" else None
            all_corrections = self.store.corrections(db, case_id)
            corrections = (all_corrections if view == "current"
                           else self.store.corrections(db, case_id, correction_watermark))
            effective_accepted = base["accepted_at"]
            if corrections:
                effective_accepted = json.loads(corrections[-1]["new_json"])["accepted_at"]
            elif all_corrections:
                effective_accepted = json.loads(all_corrections[0]["old_json"])["accepted_at"]
            if effective_accepted > stamp:
                raise DomainError("CASE_NOT_YET_ACCEPTED", "Case was not accepted at time", 404)
            current = reconstruct(base, self.store.events(db, case_id, watermark), stamp,
                                  corrections,
                                  self.store.disputes(db, case_id, dispute_watermark),
                                  all_corrections=all_corrections)
            current["indicators"] = derived(current, stamp)
            current["watermark"] = watermark
            current["view"] = view
            return current
