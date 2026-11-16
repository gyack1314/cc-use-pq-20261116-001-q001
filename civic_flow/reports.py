"""按请求参数与日志水位固定不可变指标快照。

报表冻结时的口径（as_known/current）与水位一起固化；归档报表的数字永不改变，
更正只能让受影响报表带上 needs_review 待复核标记。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .contracts import DomainError, ReportInput, utc
from .metrics import MetricsService
from .store import Store, canonical, digest

METRIC_KEYS = ("accepted", "open", "responded", "response_overdue",
               "resolution_overdue", "closed", "solved", "explained_close",
               "acknowledged", "reopened")


class ReportService:
    def __init__(self, store: Store):
        self.store = store
        self.metrics = MetricsService(store)

    @staticmethod
    def _parameters(request: ReportInput) -> dict:
        return request.model_dump(mode="json", exclude={"report_key"})

    @staticmethod
    def _unpack(record: dict, replayed: bool = False) -> dict:
        return {
            "report_key": record["report_key"], "as_of": record["as_of"],
            "watermark": record["watermark"], "created_at": record["created_at"],
            "view": record.get("view", "as_known"),
            "needs_review": bool(record.get("needs_review", 0)),
            "review_marked_sequence": record.get("review_marked_sequence"),
            "body": json.loads(record["body_json"]), "replayed": replayed,
        }

    def create(self, request: ReportInput) -> dict:
        parameters = self._parameters(request)
        fingerprint = digest(parameters)
        with self.store.transaction() as db:
            old = self.store.report(db, request.report_key)
            if old is not None:
                if old["request_digest"] != fingerprint:
                    raise DomainError("REPORT_KEY_CONFLICT", "Report key has different parameters", 409)
                return self._unpack(old, replayed=True)
            watermark = self.store.watermark(db)
            body = self.metrics.calculate(db, as_of=request.as_of, group_by=request.group_by,
                                          district=request.district, category=request.category,
                                          watermark=watermark, view=request.view)
            now = utc(datetime.now(timezone.utc))
            db.execute("INSERT INTO reports(report_key,request_digest,as_of,watermark,group_by,"
                       "district,category,body_json,created_at,view,needs_review) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,0)",
                       (request.report_key, fingerprint, utc(request.as_of), watermark,
                        request.group_by, request.district, request.category,
                        canonical(body), now, request.view))
            return self._unpack(self.store.require_report(db, request.report_key))

    def get(self, report_key: str) -> dict:
        with self.store.connection() as db:
            return self._unpack(self.store.require_report(db, report_key))

    def compare(self, first_key: str, second_key: str) -> dict:
        with self.store.connection() as db:
            first = self.store.require_report(db, first_key)
            second = self.store.require_report(db, second_key)
            if (first["group_by"] != second["group_by"]
                    or first["district"] != second["district"]
                    or first["category"] != second["category"]):
                raise DomainError("REPORT_SCOPE_MISMATCH", "Reports must use identical grouping and filters", 409)
            a = json.loads(first["body_json"])
            b = json.loads(second["body_json"])
            keys = ("accepted", "open", "responded", "response_overdue", "resolution_overdue",
                    "closed", "solved", "explained_close", "acknowledged", "reopened")
            deltas = {key: b["totals"][key] - a["totals"][key] for key in keys}
            ga = {group["key"]: group for group in a["groups"]}
            gb = {group["key"]: group for group in b["groups"]}
            groups = [{"key": key, "delta": {metric: gb.get(key, {}).get(metric, 0)
                         - ga.get(key, {}).get(metric, 0) for metric in keys}}
                      for key in sorted(set(ga) | set(gb))]
            return {"from": first_key, "to": second_key, "from_watermark": first["watermark"],
                    "to_watermark": second["watermark"], "totals_delta": deltas, "groups": groups}

    def list(self, after: str = "", limit: int = 50) -> dict:
        if not 1 <= limit <= 200:
            raise DomainError("BAD_PAGE", "Invalid report page size")
        with self.store.connection() as db:
            rows = [dict(item) for item in db.execute(
                "SELECT report_key,as_of,watermark,group_by,district,category,created_at,"
                "needs_review,review_marked_sequence FROM reports "
                "WHERE report_key>? ORDER BY report_key LIMIT ?", (after, limit))]
            for record in rows:
                record["needs_review"] = bool(record["needs_review"])
            return {"items": rows, "next_cursor": rows[-1]["report_key"] if rows else None}

    def verify(self, report_key: str) -> dict:
        with self.store.connection() as db:
            record = self.store.require_report(db, report_key)
            view = record.get("view", "as_known")
            # 仅按归档水位与归档口径重算，后期更正不得改动已归档数字。
            actual = self.metrics.calculate(db, as_of=datetime.fromisoformat(record["as_of"]),
                                            group_by=record["group_by"], district=record["district"],
                                            category=record["category"], watermark=record["watermark"],
                                            view=view)
            saved = json.loads(record["body_json"])
            return {"report_key": report_key, "matches": actual == saved,
                    "watermark": record["watermark"], "saved_digest": digest(saved),
                    "recomputed_digest": digest(actual),
                    "needs_review": bool(record.get("needs_review", 0))}

    def reanalyze(self, report_key: str) -> dict:
        """按后来掌握的事实，用同一时点/范围/分组重新分析，并说明变化。"""
        with self.store.transaction() as db:
            record = self.store.require_report(db, report_key)
            as_of = datetime.fromisoformat(record["as_of"])
            current = self.metrics.calculate(db, as_of=as_of, group_by=record["group_by"],
                                              district=record["district"],
                                              category=record["category"], view="current")
            archived = json.loads(record["body_json"])
            changed_totals = {key: current["totals"][key] - archived["totals"][key]
                              for key in METRIC_KEYS
                              if current["totals"][key] != archived["totals"][key]}
            ga = {group["key"]: group for group in archived["groups"]}
            gb = {group["key"]: group for group in current["groups"]}
            changed_groups = []
            for key in sorted(set(ga) | set(gb)):
                before, after = ga.get(key, {}), gb.get(key, {})
                delta = {metric: after.get(metric, 0) - before.get(metric, 0)
                         for metric in METRIC_KEYS
                         if after.get(metric, 0) != before.get(metric, 0)}
                if delta:
                    changed_groups.append({"key": key, "status": ("added" if key not in ga
                                          else "removed" if key not in gb else "changed"),
                                           "delta": delta})
            return {"report_key": report_key, "as_of": record["as_of"],
                    "archived_watermark": record["watermark"],
                    "current_watermark": current["watermark"],
                    "archived_view": archived.get("view", "as_known"),
                    "needs_review": bool(record.get("needs_review", 0)),
                    "changed_totals": changed_totals,
                    "changed_groups": changed_groups,
                    "archived_totals": {key: archived["totals"][key] for key in METRIC_KEYS},
                    "current_totals": {key: current["totals"][key] for key in METRIC_KEYS}}
