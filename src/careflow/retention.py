"""按记录类别的保留期限、争议冻结与可复核的清理预览/执行。

清理对象仅限从未签署、从未进入临床或库存流水的运营记录：
- appointment_cancelled：已取消或未到诊、且没有就诊记录与库存预留的预约；
- followup_closed：已完成或已取消的随访任务；
- assessment_draft：未签署、且未被计划引用的评估草稿。

已签署病历、授权、计划、观察值、不良事件、库存流水与审计哈希链不属于
任何可清理类别，例行清理永远不会触及它们。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import audit
from .db import Database, decode_json, encode_json
from .errors import Conflict, Forbidden, NotFound, ValidationError
from .ids import new_id
from .security import authorize, principal_for
from .validation import calendar_date, choice, request_digest, text, timestamp

# 可配置保留期限的记录类别白名单。白名单之外的表不接受保留规则。
RETENTION_CATEGORIES = {"appointment_cancelled", "followup_closed", "assessment_draft"}

MAX_RETENTION_DAYS = 36_500


class RetentionService:
    def __init__(self, database: Database, clock):
        self.db = database
        self.clock = clock

    # -- 保留策略 ---------------------------------------------------------

    def create_policy(self, clinic_id: str, actor_id: str, rules: dict[str, int]) -> dict:
        normalized = self._normalize_rules(rules)
        now = timestamp(self.clock.now())
        policy_id = new_id("pol")
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "retention:policy", clinic_id=clinic_id)
            if connection.execute("SELECT 1 FROM clinics WHERE id=?", (clinic_id,)).fetchone() is None:
                raise NotFound("诊所不存在")
            previous = connection.execute(
                "SELECT id,version FROM retention_policies WHERE clinic_id=? ORDER BY version DESC LIMIT 1",
                (clinic_id,)).fetchone()
            version = (previous["version"] + 1) if previous else 1
            connection.execute(
                "INSERT INTO retention_policies(id,clinic_id,version,created_by,created_at,supersedes) VALUES(?,?,?,?,?,?)",
                (policy_id, clinic_id, version, actor_id, now, previous["id"] if previous else None))
            connection.executemany(
                "INSERT INTO retention_policy_rules(policy_id,record_category,retention_days) VALUES(?,?,?)",
                [(policy_id, category, days) for category, days in normalized])
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="retention_policy", aggregate_id=policy_id,
                               action="retention.policy_created", occurred_at=now,
                               payload={"version": version, "rules": dict(normalized),
                                        "supersedes": previous["id"] if previous else None})
        return {"id": policy_id, "version": version, "rules": dict(normalized),
                "supersedes": previous["id"] if previous else None, "created_at": now}

    @staticmethod
    def _normalize_rules(rules) -> list[tuple[str, int]]:
        if not isinstance(rules, dict) or not rules:
            raise ValidationError("保留策略必须至少配置一个记录类别")
        normalized: list[tuple[str, int]] = []
        for category, days in rules.items():
            category = choice(category, "记录类别", RETENTION_CATEGORIES)
            if isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= MAX_RETENTION_DAYS:
                raise ValidationError(f"{category} 的保留天数必须为 0 至 {MAX_RETENTION_DAYS} 的整数")
            normalized.append((category, days))
        normalized.sort()
        return normalized

    def current_policy(self, connection, clinic_id: str):
        policy = connection.execute(
            "SELECT * FROM retention_policies WHERE clinic_id=? ORDER BY version DESC LIMIT 1",
            (clinic_id,)).fetchone()
        if policy is None:
            return None
        rules = connection.execute(
            "SELECT record_category,retention_days FROM retention_policy_rules WHERE policy_id=? ORDER BY record_category",
            (policy["id"],)).fetchall()
        return {"id": policy["id"], "version": policy["version"], "created_by": policy["created_by"],
                "created_at": policy["created_at"],
                "rules": {row["record_category"]: row["retention_days"] for row in rules}}

    def get_policy(self, clinic_id: str, actor_id: str) -> dict:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "retention:read", clinic_id=clinic_id)
            policy = self.current_policy(connection, clinic_id)
            if policy is None:
                raise NotFound("诊所尚未配置保留策略")
            return policy

    # -- 争议冻结 ---------------------------------------------------------

    def create_freeze(self, clinic_id: str, actor_id: str, patient_id: str, scope_start: str,
                      scope_end: str, reason: str, external_notice_ref: str,
                      *, related_records: list[dict] | None = None) -> dict:
        start = calendar_date(scope_start, "冻结起始日期")
        end = calendar_date(scope_end, "冻结结束日期")
        if end < start:
            raise ValidationError("冻结结束日期不能早于起始日期")
        reason = text(reason, "争议说明", maximum=1000)
        notice = text(external_notice_ref, "外部争议通知编号", maximum=200)
        records = self._normalize_related(related_records or [])
        freeze_id = new_id("frz")
        now = timestamp(self.clock.now())
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "retention:freeze", clinic_id=clinic_id)
            patient = connection.execute("SELECT id FROM patients WHERE id=? AND clinic_id=?", (patient_id, clinic_id)).fetchone()
            if patient is None:
                raise NotFound("患者不存在")
            for category, record_id in records:
                if self._record_clinic(connection, category, record_id) != clinic_id:
                    raise NotFound(f"关联档案不存在：{category}/{record_id}")
            connection.execute(
                "INSERT INTO retention_freezes(id,clinic_id,patient_id,scope_start,scope_end,reason,"
                "external_notice_ref,state,created_by,created_at) VALUES(?,?,?,?,?,?,?,'active',?,?)",
                (freeze_id, clinic_id, patient_id, start, end, reason, notice, actor_id, now))
            connection.executemany(
                "INSERT INTO retention_freeze_records(freeze_id,record_category,record_id) VALUES(?,?,?)",
                [(freeze_id, category, record_id) for category, record_id in records])
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=patient_id,
                               aggregate_type="retention_freeze", aggregate_id=freeze_id,
                               action="retention.freeze_created", occurred_at=now,
                               payload={"scope_start": start, "scope_end": end,
                                        "external_notice_ref": notice,
                                        "related": [[c, r] for c, r in records]})
            result = self._freeze_dict(clinic_id, freeze_id, connection)
        return result

    @staticmethod
    def _normalize_related(related) -> list[tuple[str, str]]:
        if not isinstance(related, list) or len(related) > 500:
            raise ValidationError("关联档案必须为不超过 500 项的列表")
        normalized: list[tuple[str, str]] = []
        for item in related:
            if not isinstance(item, dict):
                raise ValidationError("关联档案必须包含 record_category 与 record_id")
            category = choice(item.get("record_category"), "记录类别", RETENTION_CATEGORIES)
            record_id = text(item.get("record_id"), "档案编号", maximum=80)
            normalized.append((category, record_id))
        return normalized

    @staticmethod
    def _record_clinic(connection, category: str, record_id: str) -> str | None:
        if category == "appointment_cancelled":
            row = connection.execute("SELECT clinic_id FROM appointments WHERE id=?", (record_id,)).fetchone()
        elif category == "followup_closed":
            row = connection.execute("SELECT p.clinic_id FROM followups f JOIN patients p ON p.id=f.patient_id WHERE f.id=?",
                                     (record_id,)).fetchone()
        else:
            row = connection.execute("SELECT clinic_id FROM assessments WHERE id=?", (record_id,)).fetchone()
        return row[0] if row else None

    def release_freeze(self, clinic_id: str, actor_id: str, freeze_id: str,
                       release_notice_ref: str, release_reason: str) -> dict:
        notice = text(release_notice_ref, "解除所依据的外部通知编号", maximum=200)
        reason = text(release_reason, "解除原因", maximum=1000)
        now = timestamp(self.clock.now())
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "retention:release", clinic_id=clinic_id)
            freeze = connection.execute("SELECT * FROM retention_freezes WHERE id=? AND clinic_id=?", (freeze_id, clinic_id)).fetchone()
            if freeze is None:
                raise NotFound("争议冻结不存在")
            if freeze["state"] != "active":
                raise Conflict("争议冻结已解除")
            # 设置人与解除人必须分离。
            if freeze["created_by"] == actor_id:
                raise Forbidden("冻结设置人不得自行解除冻结，须由其他授权人员办理")
            connection.execute(
                "UPDATE retention_freezes SET state='released',released_by=?,released_at=?,"
                "release_notice_ref=?,release_reason=?,version=version+1 WHERE id=?",
                (actor_id, now, notice, reason, freeze_id))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=freeze["patient_id"],
                               aggregate_type="retention_freeze", aggregate_id=freeze_id,
                               action="retention.freeze_released", occurred_at=now,
                               payload={"created_by": freeze["created_by"],
                                        "release_notice_ref": notice, "release_reason": reason})
            result = self._freeze_dict(clinic_id, freeze_id, connection)
        return result

    def get_freeze(self, clinic_id: str, actor_id: str, freeze_id: str) -> dict:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "retention:read", clinic_id=clinic_id)
            if connection.execute("SELECT 1 FROM retention_freezes WHERE id=? AND clinic_id=?", (freeze_id, clinic_id)).fetchone() is None:
                raise NotFound("争议冻结不存在")
            return self._freeze_dict(clinic_id, freeze_id, connection)

    def list_freezes(self, clinic_id: str, actor_id: str, *, patient_id: str | None = None,
                     state: str | None = None) -> dict:
        if state is not None:
            state = choice(state, "冻结状态", {"active", "released"})
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "retention:read", clinic_id=clinic_id)
            clauses, params = ["clinic_id=?"], [clinic_id]
            if patient_id:
                clauses.append("patient_id=?")
                params.append(patient_id)
            if state:
                clauses.append("state=?")
                params.append(state)
            rows = connection.execute(
                "SELECT id FROM retention_freezes WHERE " + " AND ".join(clauses) + " ORDER BY created_at,id",
                params).fetchall()
            return {"items": [self._freeze_dict(clinic_id, row["id"], connection) for row in rows]}

    def _freeze_dict(self, clinic_id: str, freeze_id: str, connection) -> dict:
        row = connection.execute("SELECT * FROM retention_freezes WHERE id=? AND clinic_id=?", (freeze_id, clinic_id)).fetchone()
        if row is None:
            raise NotFound("争议冻结不存在")
        records = connection.execute(
            "SELECT record_category,record_id FROM retention_freeze_records WHERE freeze_id=? ORDER BY record_category,record_id",
            (freeze_id,)).fetchall()
        return {"id": row["id"], "patient_id": row["patient_id"], "scope_start": row["scope_start"],
                "scope_end": row["scope_end"], "reason": row["reason"],
                "external_notice_ref": row["external_notice_ref"], "state": row["state"],
                "created_by": row["created_by"], "created_at": row["created_at"],
                "released_by": row["released_by"], "released_at": row["released_at"],
                "release_notice_ref": row["release_notice_ref"], "release_reason": row["release_reason"],
                "version": row["version"],
                "related_records": [{"record_category": r["record_category"], "record_id": r["record_id"]} for r in records]}

    # -- 预览与执行 -------------------------------------------------------

    def create_preview(self, clinic_id: str, actor_id: str) -> dict:
        now = timestamp(self.clock.now())
        run_id = new_id("run")
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "retention:preview", clinic_id=clinic_id)
            policy = self.current_policy(connection, clinic_id)
            if policy is None:
                raise Conflict("诊所尚未配置保留策略，无法生成清理预览")
            scope = self._compute_scope(connection, clinic_id, policy, now)
            connection.execute(
                "INSERT INTO retention_runs(id,clinic_id,policy_id,policy_version,rules_hash,state,created_by,"
                "created_at,candidates_total,to_clean_total,frozen_skipped_total,scope_as_of,scope_fingerprint,"
                "candidates_json,skipped_json) VALUES(?,?,?,?,?,'previewed',?,?,?,?,?,?,?,?,?)",
                (run_id, clinic_id, policy["id"], policy["version"], scope["rules_hash"], actor_id, now,
                 len(scope["candidates"]), len(scope["to_clean"]), len(scope["frozen"]),
                 scope["as_of"], scope["fingerprint"],
                 encode_json(scope["candidates"]), encode_json(scope["frozen"])))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="retention_run", aggregate_id=run_id,
                               action="retention.preview_created", occurred_at=now,
                               payload={"policy_version": policy["version"],
                                        "candidates": len(scope["candidates"]),
                                        "to_clean": len(scope["to_clean"]),
                                        "frozen_skipped": len(scope["frozen"])})
            result = self._run_dict(clinic_id, run_id, connection)
        return result

    def get_preview(self, clinic_id: str, actor_id: str, run_id: str) -> dict:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "retention:read", clinic_id=clinic_id)
            if connection.execute("SELECT 1 FROM retention_runs WHERE id=? AND clinic_id=?", (run_id, clinic_id)).fetchone() is None:
                raise NotFound("清理预览不存在")
            return self._run_dict(clinic_id, run_id, connection)

    def execute_preview(self, clinic_id: str, actor_id: str, run_id: str, idempotency_key: str) -> dict:
        key = idempotency_key.strip() if isinstance(idempotency_key, str) else ""
        if not key or len(key) > 160:
            raise ValidationError("执行操作必须提供不超过 160 个字符的幂等编号")
        now = timestamp(self.clock.now())
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "retention:execute", clinic_id=clinic_id)
            run = connection.execute("SELECT * FROM retention_runs WHERE id=? AND clinic_id=?", (run_id, clinic_id)).fetchone()
            if run is None:
                raise NotFound("清理预览不存在")
            if run["state"] == "executed":
                if run["idempotency_key"] != key:
                    raise Conflict("该预览已执行，且使用了不同的幂等编号")
                return {**self._run_dict(clinic_id, run_id, connection), "replayed": True}
            if run["state"] != "previewed":
                raise Conflict("该预览已失效，不能执行")
            # 安全重放：幂等编号全局唯一，防止跨预览复用。
            clash = connection.execute("SELECT id FROM retention_runs WHERE idempotency_key=? AND id<>?",
                                       (key, run_id)).fetchone()
            if clash:
                raise Conflict("幂等编号已被其他清理执行使用")
            # 策略版本变更后，旧预览不能直接执行。
            policy = self.current_policy(connection, clinic_id)
            if policy is None or policy["version"] != run["policy_version"] or policy["id"] != run["policy_id"]:
                raise Conflict("保留策略已变更，旧预览不能执行；请基于新版本重新生成预览",
                               details={"preview_policy_version": run["policy_version"],
                                        "current_policy_version": policy["version"] if policy else None})
            scope = self._compute_scope(connection, clinic_id, policy, run["scope_as_of"])
            if scope["fingerprint"] != run["scope_fingerprint"]:
                raise Conflict("预览生成后记录范围已变化（新增、签署、合并或冻结变动），请重新生成预览",
                               details={"preview_fingerprint": run["scope_fingerprint"],
                                        "current_fingerprint": scope["fingerprint"]})
            # 指纹一致意味着结构全集与预览时相同；以同一时点重算的待清理列表为准执行。
            to_clean = scope["to_clean"]
            cleaned: list[dict] = []
            for item in to_clean:
                self._delete_candidate(connection, item, clinic_id)
                cleaned_id = new_id("cln")
                connection.execute(
                    "INSERT INTO retention_cleaned(id,clinic_id,record_category,record_id,patient_id,"
                    "policy_id,policy_version,run_id,cleaned_by,cleaned_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (cleaned_id, clinic_id, item["record_category"], item["record_id"], item["patient_id"],
                     policy["id"], policy["version"], run_id, actor_id, now))
                cleaned.append({"record_category": item["record_category"], "record_id": item["record_id"],
                                "patient_id": item["patient_id"]})
            connection.execute(
                "UPDATE retention_runs SET state='executed',executed_by=?,executed_at=?,idempotency_key=?,"
                "cleaned_total=? WHERE id=?",
                (actor_id, now, key, len(cleaned), run_id))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="retention_run", aggregate_id=run_id,
                               action="retention.executed", occurred_at=now,
                               payload={"policy_version": policy["version"], "cleaned": len(cleaned),
                                        "frozen_skipped": run["frozen_skipped_total"],
                                        "idempotency_key": key, "records": cleaned})
            result = self._run_dict(clinic_id, run_id, connection)
        return {**result, "replayed": False}

    @staticmethod
    def _delete_candidate(connection, item: dict, clinic_id: str) -> None:
        category, record_id = item["record_category"], item["record_id"]
        # 删除条件与候选条件保持一致；即使范围计算后状态被改动也不会误删。
        if category == "appointment_cancelled":
            changed = connection.execute(
                "DELETE FROM appointments WHERE id=? AND clinic_id=? AND state IN ('cancelled','no_show') "
                "AND NOT EXISTS (SELECT 1 FROM encounters e WHERE e.appointment_id=appointments.id) "
                "AND NOT EXISTS (SELECT 1 FROM stock_reservations s WHERE s.appointment_id=appointments.id)",
                (record_id, clinic_id)).rowcount
        elif category == "followup_closed":
            changed = connection.execute(
                "DELETE FROM followups WHERE id=? AND state IN ('done','cancelled') AND patient_id IN "
                "(SELECT id FROM patients WHERE clinic_id=?)", (record_id, clinic_id)).rowcount
        else:
            changed = connection.execute(
                "DELETE FROM assessments WHERE id=? AND clinic_id=? AND status='draft' AND NOT EXISTS "
                "(SELECT 1 FROM plans p WHERE p.assessment_id=assessments.id)", (record_id, clinic_id)).rowcount
        if changed != 1:
            raise Conflict("清理范围在执行时发生变化，已整体回滚", details={"record_category": category,
                                                                      "record_id": record_id})

    # -- 范围计算 ---------------------------------------------------------

    def _compute_scope(self, connection, clinic_id: str, policy: dict, as_of: str) -> dict:
        rules = policy["rules"]
        rules_hash = request_digest({"policy_id": policy["id"], "version": policy["version"], "rules": rules})
        tz = connection.execute("SELECT timezone FROM clinics WHERE id=?", (clinic_id,)).fetchone()["timezone"]
        zone = ZoneInfo(tz)
        patients = {row["id"]: row for row in connection.execute(
            "SELECT id,state,merged_into,version,updated_at FROM patients WHERE clinic_id=?", (clinic_id,)).fetchall()}
        freezes = connection.execute(
            "SELECT * FROM retention_freezes WHERE clinic_id=? AND state='active' ORDER BY id", (clinic_id,)).fetchall()
        explicit: dict[tuple[str, str], list[str]] = {}
        rows = connection.execute(
            "SELECT fr.freeze_id,fr.record_category,fr.record_id FROM retention_freeze_records fr "
            "JOIN retention_freezes f ON f.id=fr.freeze_id WHERE f.clinic_id=? AND f.state='active'",
            (clinic_id,)).fetchall()
        for row in rows:
            explicit.setdefault((row["record_category"], row["record_id"]), []).append(row["freeze_id"])

        freeze_bounds = []
        freeze_meta = []
        for freeze in freezes:
            start_instant = datetime.combine(date.fromisoformat(freeze["scope_start"]),
                                             datetime.min.time(), zone).astimezone(UTC)
            end_instant = datetime.combine(date.fromisoformat(freeze["scope_end"]) + timedelta(days=1),
                                           datetime.min.time(), zone).astimezone(UTC)
            freeze_bounds.append((freeze["id"], timestamp(start_instant), timestamp(end_instant)))
            freeze_meta.append({"id": freeze["id"], "version": freeze["version"], "patient_id": freeze["patient_id"],
                                "scope_start": freeze["scope_start"], "scope_end": freeze["scope_end"]})

        as_of_dt = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        universe: list[dict] = []
        for category in sorted(rules):
            # 结构全集不受保留年龄限制：任何新产生、被签署、被引用或被合并的记录
            # 都会改变全集，从而使旧预览失效。
            for row in self._category_rows(connection, clinic_id, category, None):
                patient = patients.get(row["patient_id"])
                root_id = self._merge_root(patients, row["patient_id"])
                blocking = list(explicit.get((category, row["record_id"]), []))
                for freeze in freezes:
                    if freeze["id"] in blocking:
                        continue
                    if freeze["patient_id"] != row["patient_id"] and freeze["patient_id"] != root_id:
                        continue
                    bounds = next(item for item in freeze_bounds if item[0] == freeze["id"])
                    if bounds[1] <= row["business_date"] < bounds[2]:
                        blocking.append(freeze["id"])
                blocking.sort()
                universe.append({
                    "record_category": category,
                    "record_id": row["record_id"],
                    "patient_id": row["patient_id"],
                    "patient_state": patient["state"] if patient else None,
                    "merged_into": patient["merged_into"] if patient else None,
                    "patient_version": patient["version"] if patient else None,
                    "business_date": row["business_date"],
                    "record_state": row["state"],
                    "frozen": bool(blocking),
                    "freeze_ids": blocking,
                })
        universe.sort(key=lambda item: (item["record_category"], item["record_id"]))
        # 指纹只依赖结构全集、冻结与策略，不依赖当前时间，保证执行时可复核。
        fingerprint = request_digest(
            {"rules_hash": rules_hash,
             "freezes": sorted(freeze_meta, key=lambda item: item["id"]),
             "explicit": sorted([[k[0], k[1], sorted(v)] for k, v in explicit.items()]),
             "universe": universe})
        candidates = []
        for item in universe:
            days = rules[item["record_category"]]
            cutoff = timestamp(as_of_dt - timedelta(days=days))
            if item["business_date"] < cutoff:
                candidates.append(item)
        frozen = [item for item in candidates if item["frozen"]]
        return {"rules_hash": rules_hash, "fingerprint": fingerprint, "as_of": as_of,
                "candidates": candidates, "frozen": frozen, "to_clean": [c for c in candidates if not c["frozen"]]}

    @staticmethod
    def _merge_root(patients: dict, patient_id: str) -> str:
        seen: set[str] = set()
        current = patient_id
        while current and current in patients and patients[current]["merged_into"] and current not in seen:
            seen.add(current)
            current = patients[current]["merged_into"]
        return current

    @staticmethod
    def _category_rows(connection, clinic_id: str, category: str, cutoff: str | None):
        age = " AND {} < ?" if cutoff is not None else ""
        params: list = [clinic_id]
        if category == "appointment_cancelled":
            sql = (
                "SELECT a.id AS record_id,a.patient_id,a.starts_at AS business_date,a.state AS state "
                "FROM appointments a WHERE a.clinic_id=? AND a.state IN ('cancelled','no_show') "
                + age.format("a.starts_at") +
                " AND NOT EXISTS (SELECT 1 FROM encounters e WHERE e.appointment_id=a.id) "
                "AND NOT EXISTS (SELECT 1 FROM stock_reservations s WHERE s.appointment_id=a.id) "
                "ORDER BY a.id")
            if cutoff:
                params.append(cutoff)
            return connection.execute(sql, params).fetchall()
        if category == "followup_closed":
            sql = (
                "SELECT f.id AS record_id,f.patient_id,f.due_at AS business_date,f.state AS state "
                "FROM followups f JOIN patients p ON p.id=f.patient_id WHERE p.clinic_id=? "
                "AND f.state IN ('done','cancelled')" + age.format("f.due_at") + " ORDER BY f.id")
            if cutoff:
                params.append(cutoff)
            return connection.execute(sql, params).fetchall()
        sql = (
            "SELECT a.id AS record_id,a.patient_id,a.created_at AS business_date,a.status AS state "
            "FROM assessments a WHERE a.clinic_id=? AND a.status='draft'"
            + age.format("a.created_at") +
            " AND NOT EXISTS (SELECT 1 FROM plans p WHERE p.assessment_id=a.id) ORDER BY a.id")
        if cutoff:
            params.append(cutoff)
        return connection.execute(sql, params).fetchall()

    def _run_dict(self, clinic_id: str, run_id: str, connection) -> dict:
        row = connection.execute("SELECT * FROM retention_runs WHERE id=? AND clinic_id=?", (run_id, clinic_id)).fetchone()
        if row is None:
            raise NotFound("清理预览不存在")
        return {"id": row["id"], "policy_id": row["policy_id"], "policy_version": row["policy_version"],
                "state": row["state"], "created_by": row["created_by"], "created_at": row["created_at"],
                "executed_by": row["executed_by"], "executed_at": row["executed_at"],
                "candidates_total": row["candidates_total"], "to_clean_total": row["to_clean_total"],
                "frozen_skipped_total": row["frozen_skipped_total"], "cleaned_total": row["cleaned_total"],
                "candidates": decode_json(row["candidates_json"]),
                "frozen_skipped": decode_json(row["skipped_json"])}

    def run_history(self, clinic_id: str, actor_id: str, *, limit: int = 50) -> dict:
        if not 1 <= limit <= 200:
            raise ValidationError("查询数量须为 1 至 200")
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "retention:read", clinic_id=clinic_id)
            rows = connection.execute(
                "SELECT id,policy_version,state,created_at,executed_at,candidates_total,to_clean_total,"
                "frozen_skipped_total,cleaned_total FROM retention_runs WHERE clinic_id=? "
                "ORDER BY created_at DESC,id DESC LIMIT ?", (clinic_id, limit)).fetchall()
            return {"items": [dict(row) for row in rows]}


def merge_component_patient_ids(connection, clinic_id: str, patient_id: str) -> set[str]:
    """同一合并图内的全部患者编号（含自身），用于冻结跨合并追踪。"""
    rows = connection.execute("SELECT id,merged_into FROM patients WHERE clinic_id=?", (clinic_id,)).fetchall()
    patients = {row["id"]: row["merged_into"] for row in rows}

    def root_of(value: str) -> str:
        seen: set[str] = set()
        current = value
        while current in patients and patients[current] and current not in seen:
            seen.add(current)
            current = patients[current]
        return current

    if patient_id not in patients:
        return set()
    target_root = root_of(patient_id)
    return {other for other in patients if root_of(other) == target_root}


def active_freeze_record_ids(connection, clinic_id: str, patient_id: str, record_category: str) -> set[str]:
    """导出时不得遗漏的活动冻结显式关联档案编号。

    档案按其所属患者（含合并来源）归属；即使冻结登记在合并图内其他患者
    名下，只要该档案属于当前导出患者，就必须包含。
    """
    component = merge_component_patient_ids(connection, clinic_id, patient_id)
    if not component:
        return set()
    placeholders = ",".join("?" for _ in component)
    if record_category == "appointment_cancelled":
        owner_sql = f"SELECT a.id FROM appointments a WHERE a.patient_id IN ({placeholders})"
    elif record_category == "followup_closed":
        owner_sql = f"SELECT f.id FROM followups f WHERE f.patient_id IN ({placeholders})"
    else:
        owner_sql = f"SELECT a.id FROM assessments a WHERE a.patient_id IN ({placeholders})"
    rows = connection.execute(
        f"SELECT fr.record_id FROM retention_freeze_records fr "
        f"JOIN retention_freezes f ON f.id=fr.freeze_id "
        f"WHERE f.clinic_id=? AND f.state='active' AND fr.record_category=? "
        f"AND fr.record_id IN ({owner_sql})",
        [clinic_id, record_category, *sorted(component)]).fetchall()
    return {row["record_id"] for row in rows}
