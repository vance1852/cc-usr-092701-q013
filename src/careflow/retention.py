"""按记录类别配置的保留期限、争议冻结与可复核清理。

清理对象限于八类业务记录；已签署病历（encounters/encounter_notes）、患者档案与
审计哈希链在任何情况下都不被清理删除。预览先行、冻结跳过、策略版本与范围指纹
双重校验，执行结果可安全重放。
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Any

from . import audit
from .db import Database, decode_json, encode_json
from .errors import Conflict, Forbidden, NotFound, ValidationError
from .ids import new_id, require_id
from .security import authorize, principal_for
from .validation import choice, integer, parsed_timestamp, text, timestamp

# 受保留策略管理的记录类别 -> (业务表, 归属患者的 JOIN, 诊所属地条件, 锚点时间列, 可清理状态)
# encounters / encounter_notes / audit_events / patients 刻意不在其中。
CATEGORY_SPECS: dict[str, dict[str, Any]] = {
    "consent": {
        "table": "consents", "anchor": "created_at",
        "states": ("withdrawn", "expired"),
        "state_column": "state",
    },
    "assessment": {
        "table": "assessments", "anchor": "captured_at",
        "states": ("draft", "superseded"),
        "state_column": "status",
    },
    "plan": {
        "table": "plans", "anchor": "created_at",
        "states": ("cancelled",),
        "state_column": "state",
    },
    "appointment": {
        "table": "appointments", "anchor": "starts_at",
        "states": ("cancelled", "no_show"),
        "state_column": "state",
    },
    "followup": {
        "table": "followups", "anchor": "due_at",
        "states": ("cancelled",),
        "state_column": "state",
    },
    "observation": {
        "table": "observations", "anchor": "observed_at",
        "states": None,  # 观察值无终态；是否可删由引用关系决定
        "state_column": "state",
    },
    "clinical_flag": {
        "table": "clinical_flags", "anchor": "created_at",
        "states": ("resolved",),
        "state_column": "state",
    },
    "incident": {
        "table": "incidents", "anchor": "reported_at",
        "states": ("closed",),
        "state_column": "state",
    },
}
CATEGORIES = set(CATEGORY_SPECS)

# 外部引用：被这些在线记录引用时不能删除（病历、不可变流水、合并/更正链等）。
CATEGORY_REFERENCES: dict[str, list[str]] = {
    "consent": ["SELECT 1 FROM plans WHERE consent_id=?",
                "SELECT 1 FROM consents WHERE supersedes=?"],
    "assessment": ["SELECT 1 FROM plans WHERE assessment_id=?",
                   "SELECT 1 FROM assessments WHERE supersedes=?"],
    "plan": ["SELECT 1 FROM appointments WHERE plan_id=?",
             "SELECT 1 FROM followups WHERE plan_id=?",
             "SELECT 1 FROM observations WHERE plan_id=?",
             "SELECT 1 FROM incidents WHERE plan_id=?"],
    "appointment": ["SELECT 1 FROM encounters WHERE appointment_id=?",
                    "SELECT 1 FROM stock_reservations WHERE appointment_id=?",
                    "SELECT 1 FROM stock_movements WHERE appointment_id=?"],
    "followup": [],
    "observation": ["SELECT 1 FROM observations WHERE correction_of=?"],
    "clinical_flag": [],
    "incident": [],
}

# 聚合私有子记录：随父记录一并删除（顺序先于父记录），审计哈希链不在其中。
CATEGORY_CHILD_DELETE: dict[str, list[str]] = {
    "plan": [
        "DELETE FROM milestone_events WHERE milestone_id IN (SELECT id FROM plan_milestones WHERE plan_id=?)",
        "DELETE FROM plan_milestones WHERE plan_id=?",
        "DELETE FROM plan_revisions WHERE plan_id=?",
    ],
    "incident": ["DELETE FROM incident_events WHERE incident_id=?"],
}

MAX_RETENTION_DAYS = 36500
MAX_ASSOCIATED = 200


def merged_patient_set(connection, clinic_id: str, patient_id: str) -> set[str]:
    """患者本人及经合并链并入该档案的全部来源档案。"""
    ids = {patient_id}
    frontier = [patient_id]
    while frontier:
        rows = connection.execute(
            "SELECT id FROM patients WHERE clinic_id=? AND state='merged' AND merged_into=?",
            (clinic_id, frontier.pop())).fetchall()
        for row in rows:
            if row["id"] not in ids:
                ids.add(row["id"])
                frontier.append(row["id"])
    return ids


class FreezeIndex:
    """活动冻结的内存索引，支持按患者/日期区间/显式关联三种范围命中。"""

    def __init__(self):
        self.explicit: dict[tuple[str, str], list[str]] = {}
        self.windows: list[tuple[str, set[str] | None, str | None, str | None]] = []

    def match(self, category: str, record_id: str, patient_id: str, anchor: str) -> list[str]:
        hit = list(self.explicit.get((category, record_id), ()))
        for freeze_id, patients, start, end in self.windows:
            if patients is not None and patient_id not in patients:
                continue
            if start is not None and anchor < start:
                continue
            if end is not None and anchor > end:
                continue
            if freeze_id not in hit:
                hit.append(freeze_id)
        return hit


def build_freeze_index(connection, clinic_id: str) -> FreezeIndex:
    index = FreezeIndex()
    freezes = connection.execute(
        "SELECT * FROM record_freezes WHERE clinic_id=? AND state='active'", (clinic_id,)).fetchall()
    for freeze in freezes:
        if freeze["patient_id"] is None and freeze["scope_start"] is None and freeze["scope_end"] is None:
            # 仅含显式关联档案的冻结不形成范围窗口，避免误伤其他记录。
            continue
        if freeze["patient_id"] is not None:
            patients = merged_patient_set(connection, clinic_id, freeze["patient_id"])
        else:
            patients = None  # 无患者限定：日期区间适用于全诊所
        index.windows.append((freeze["id"], patients, freeze["scope_start"], freeze["scope_end"]))
    items = connection.execute(
        "SELECT i.* FROM record_freeze_items i JOIN record_freezes f ON f.id=i.freeze_id "
        "WHERE f.clinic_id=? AND f.state='active'", (clinic_id,)).fetchall()
    for item in items:
        index.explicit.setdefault((item["record_category"], item["record_id"]), []).append(item["freeze_id"])
    return index


# 导出章节 -> 保留域记录类别；profile 对应患者档案，本身永不删除。
EXPORT_SECTION_CATEGORY = {
    "consents": "consent", "assessments": "assessment", "plans": "plan",
    "observations": "observation", "appointments": "appointment",
    "followups": "followup", "incidents": "incident",
}
_SECTION_BY_CATEGORY = {category: section for section, category in EXPORT_SECTION_CATEGORY.items()}


def frozen_export_requirements(connection, clinic_id: str, patient_id: str) -> dict[str, list[str]]:
    """返回该患者自身记录在活动冻结下必须出现在导出中的记录，按章节分组。

    患者级、日期区间冻结通过记录锚点匹配；显式关联档案单独并入。任何命中的章节
    都必须包含在导出所选章节中，否则视为从导出中遗漏。合并来源档案属于各自的
    患者导出，不在本患者导出范围内。
    """
    index = build_freeze_index(connection, clinic_id)
    patient_ids = {patient_id}
    required: dict[str, set[str]] = {}

    def add(category: str, record_id: str) -> None:
        required.setdefault(_SECTION_BY_CATEGORY[category], set()).add(record_id)

    for category, spec in CATEGORY_SPECS.items():
        if category not in _SECTION_BY_CATEGORY:
            continue
        anchor = spec["anchor"]
        rows = connection.execute(
            f"SELECT id,patient_id,{anchor} AS anchor FROM {spec['table']} WHERE patient_id IN ({','.join('?' for _ in patient_ids)})",
            tuple(patient_ids)).fetchall()
        for row in rows:
            if index.match(category, row["id"], row["patient_id"], row["anchor"]):
                add(category, row["id"])
    items = connection.execute(
        "SELECT i.record_category,i.record_id FROM record_freeze_items i "
        "JOIN record_freezes f ON f.id=i.freeze_id WHERE f.clinic_id=? AND f.state='active'",
        (clinic_id,)).fetchall()
    for row in items:
        category = row["record_category"]
        section = _SECTION_BY_CATEGORY.get(category)
        if section is None:
            continue
        owner = connection.execute(f"SELECT patient_id FROM {CATEGORY_SPECS[category]['table']} WHERE id=?",
                                   (row["record_id"],)).fetchone()
        if owner and owner["patient_id"] in patient_ids:
            add(category, row["record_id"])
    return {section: sorted(ids) for section, ids in sorted(required.items())}


class RetentionService:
    """保留策略与争议冻结的应用服务。"""

    def __init__(self, database: Database, clock):
        self.db = database
        self.clock = clock

    def now(self) -> str:
        return timestamp(self.clock.now())

    # -- 争议冻结 -----------------------------------------------------------

    def create_freeze(self, clinic_id: str, actor_id: str, *, reason: str, notice_ref: str,
                      patient_id: str | None = None, scope_start: str | None = None,
                      scope_end: str | None = None,
                      associated_records: list[dict[str, str]] | None = None) -> dict[str, Any]:
        reason = text(reason, "冻结原因", maximum=800)
        notice_ref = text(notice_ref, "争议通知编号", maximum=200)
        start = timestamp(scope_start, "区间起始") if scope_start else None
        end = timestamp(scope_end, "区间截止") if scope_end else None
        if start and end and start > end:
            raise ValidationError("冻结日期区间起始不能晚于截止")
        records = self._normalize_associated(associated_records or [])
        if patient_id is None and start is None and end is None and not records:
            raise ValidationError("冻结范围必须至少指定患者、日期区间或关联档案之一")
        freeze_id = new_id("frz")
        now = self.now()
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "records:manage", clinic_id=clinic_id)
            if patient_id is not None:
                patient = connection.execute(
                    "SELECT id FROM patients WHERE id=? AND clinic_id=?", (patient_id, clinic_id)).fetchone()
                if patient is None:
                    raise NotFound("患者不存在")
            checked = self._check_associated(connection, clinic_id, records)
            connection.execute(
                "INSERT INTO record_freezes(id,clinic_id,patient_id,scope_start,scope_end,reason,notice_ref,"
                "state,created_by,created_at) VALUES(?,?,?,?,?,?,?,'active',?,?)",
                (freeze_id, clinic_id, patient_id, start, end, reason, notice_ref, actor_id, now))
            for category, record_id in checked:
                connection.execute(
                    "INSERT INTO record_freeze_items(freeze_id,record_category,record_id,created_at) VALUES(?,?,?,?)",
                    (freeze_id, category, record_id, now))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=patient_id,
                               aggregate_type="record_freeze", aggregate_id=freeze_id, action="freeze.created",
                               occurred_at=now, payload={"reason": reason, "notice_ref": notice_ref,
                                                         "scope_start": start, "scope_end": end,
                                                         "associated": [{"category": c, "id": r} for c, r in checked]})
        return {
            "id": freeze_id, "clinic_id": clinic_id, "patient_id": patient_id,
            "scope_start": start, "scope_end": end, "reason": reason, "notice_ref": notice_ref,
            "state": "active", "created_by": actor_id, "created_at": now,
            "released_by": None, "released_at": None, "release_external_ref": None,
            "release_doc_digest": None, "release_note": None,
            "associated_records": [{"category": c, "id": r} for c, r in checked],
            "associated_count": len(checked),
        }

    @staticmethod
    def _normalize_associated(value: Any) -> list[tuple[str, str]]:
        if not isinstance(value, list) or not value:
            return []
        if len(value) > MAX_ASSOCIATED:
            raise ValidationError(f"关联档案数量不得超过 {MAX_ASSOCIATED}")
        records: list[tuple[str, str]] = []
        for item in value:
            if not isinstance(item, dict):
                raise ValidationError("关联档案必须包含类别与编号")
            category = choice(item.get("category"), "关联档案类别", CATEGORIES | {"encounter"})
            record_id = require_id(item.get("id", ""), "关联档案编号")
            pair = (category, record_id)
            if pair in records:
                raise ValidationError("关联档案不能重复")
            records.append(pair)
        return records

    @staticmethod
    def _check_associated(connection, clinic_id: str, records: list[tuple[str, str]]) -> list[tuple[str, str]]:
        for category, record_id in records:
            if category == "encounter":
                row = connection.execute(
                    "SELECT e.id FROM encounters e JOIN appointments a ON a.id=e.appointment_id "
                    "WHERE e.id=? AND a.clinic_id=?", (record_id, clinic_id)).fetchone()
            else:
                spec = CATEGORY_SPECS[category]
                row = connection.execute(
                    f"SELECT t.id FROM {spec['table']} t JOIN patients p ON p.id=t.patient_id "
                    "WHERE t.id=? AND p.clinic_id=?", (record_id, clinic_id)).fetchone()
            if row is None:
                raise NotFound("关联档案不存在", details={"category": category, "id": record_id})
        return records

    def release_freeze(self, clinic_id: str, actor_id: str, freeze_id: str, *,
                       external_ref: str, doc_digest: str | None = None, note: str | None = None) -> dict[str, Any]:
        """解除冻结必须由设置人之外的负责人执行，并记录外部通知依据。"""
        external_ref = text(external_ref, "外部解除依据", maximum=200)
        if note is not None:
            note = text(note, "解除说明", minimum=0, maximum=800)
        if doc_digest is not None:
            if not isinstance(doc_digest, str) or len(doc_digest) != 64 or any(c not in "0123456789abcdef" for c in doc_digest):
                raise ValidationError("外部文书摘要必须为 SHA-256")
        now = self.now()
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "records:manage", clinic_id=clinic_id)
            freeze = connection.execute("SELECT * FROM record_freezes WHERE id=? AND clinic_id=?",
                                        (freeze_id, clinic_id)).fetchone()
            if freeze is None:
                raise NotFound("冻结记录不存在")
            if freeze["state"] != "active":
                raise Conflict("冻结已解除，不能重复解除")
            if freeze["created_by"] == actor_id:
                raise Forbidden("冻结的设置人与解除人必须分离")
            connection.execute(
                "UPDATE record_freezes SET state='released',released_by=?,released_at=?,"
                "release_external_ref=?,release_doc_digest=?,release_note=? WHERE id=?",
                (actor_id, now, external_ref, doc_digest, note, freeze_id))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id,
                               patient_id=freeze["patient_id"], aggregate_type="record_freeze",
                               aggregate_id=freeze_id, action="freeze.released", occurred_at=now,
                               payload={"external_ref": external_ref, "doc_digest": doc_digest,
                                        "note": note, "set_by": freeze["created_by"]})
        return self.get_freeze(clinic_id, actor_id, freeze_id)

    def get_freeze(self, clinic_id: str, actor_id: str, freeze_id: str) -> dict[str, Any]:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "records:read", clinic_id=clinic_id)
            freeze = connection.execute("SELECT * FROM record_freezes WHERE id=? AND clinic_id=?",
                                        (freeze_id, clinic_id)).fetchone()
            if freeze is None:
                raise NotFound("冻结记录不存在")
            items = connection.execute("SELECT record_category,record_id FROM record_freeze_items WHERE freeze_id=?",
                                       (freeze_id,)).fetchall()
            return self._freeze_dict(freeze, [(row["record_category"], row["record_id"]) for row in items])

    def list_freezes(self, clinic_id: str, actor_id: str, *, state: str | None = None) -> dict[str, Any]:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "records:read", clinic_id=clinic_id)
            if state is None:
                rows = connection.execute("SELECT * FROM record_freezes WHERE clinic_id=? ORDER BY created_at,id",
                                          (clinic_id,)).fetchall()
            else:
                state = choice(state, "冻结状态", {"active", "released"})
                rows = connection.execute("SELECT * FROM record_freezes WHERE clinic_id=? AND state=? ORDER BY created_at,id",
                                          (clinic_id, state)).fetchall()
            return {"items": [self._freeze_dict(row, None) for row in rows]}

    @staticmethod
    def _freeze_dict(freeze, associated: list[tuple[str, str]] | None) -> dict[str, Any]:
        result = {
            "id": freeze["id"], "clinic_id": freeze["clinic_id"], "patient_id": freeze["patient_id"],
            "scope_start": freeze["scope_start"], "scope_end": freeze["scope_end"],
            "reason": freeze["reason"], "notice_ref": freeze["notice_ref"], "state": freeze["state"],
            "created_by": freeze["created_by"], "created_at": freeze["created_at"],
            "released_by": freeze["released_by"], "released_at": freeze["released_at"],
            "release_external_ref": freeze["release_external_ref"],
            "release_doc_digest": freeze["release_doc_digest"], "release_note": freeze["release_note"],
        }
        if associated is not None:
            result["associated_records"] = [{"category": c, "id": r} for c, r in associated]
            result["associated_count"] = len(associated)
        return result

    # -- 保留策略 -----------------------------------------------------------

    def set_policy(self, clinic_id: str, actor_id: str, rules: Any) -> dict[str, Any]:
        normalized = self._normalize_rules(rules)
        now = self.now()
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "records:manage", clinic_id=clinic_id)
            row = connection.execute("SELECT COALESCE(MAX(version),0) AS v FROM retention_policies WHERE clinic_id=?",
                                     (clinic_id,)).fetchone()
            version = row["v"] + 1
            if row["v"]:
                connection.execute("UPDATE retention_policies SET state='superseded' WHERE clinic_id=? AND state='active'",
                                   (clinic_id,))
            connection.execute(
                "INSERT INTO retention_policies(clinic_id,version,rules_json,state,created_by,created_at) "
                "VALUES(?,?,?,'active',?,?)", (clinic_id, version, encode_json(normalized), actor_id, now))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="retention_policy", aggregate_id=f"{clinic_id}:{version}",
                               action="retention.policy_set", occurred_at=now,
                               payload={"version": version, "rules": normalized})
        return {"clinic_id": clinic_id, "version": version, "rules": normalized, "state": "active", "created_at": now}

    @staticmethod
    def _normalize_rules(rules: Any) -> list[dict[str, int]]:
        if not isinstance(rules, list) or not rules:
            raise ValidationError("至少配置一条保留规则")
        if len(rules) > len(CATEGORIES):
            raise ValidationError("保留规则数量超出记录类别数")
        normalized: list[dict[str, int]] = []
        seen: set[str] = set()
        for item in rules:
            if not isinstance(item, dict):
                raise ValidationError("保留规则必须包含记录类别与保留天数")
            category = choice(item.get("category"), "记录类别", CATEGORIES)
            if category in seen:
                raise ValidationError("同一记录类别只能配置一条规则", details={"category": category})
            seen.add(category)
            days = integer(item.get("retention_days"), "保留天数", minimum=1, maximum=MAX_RETENTION_DAYS)
            normalized.append({"category": category, "retention_days": days})
        normalized.sort(key=lambda rule: rule["category"])
        return normalized

    def get_policy(self, clinic_id: str, actor_id: str) -> dict[str, Any]:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "records:read", clinic_id=clinic_id)
            row = connection.execute(
                "SELECT * FROM retention_policies WHERE clinic_id=? AND state='active' ORDER BY version DESC LIMIT 1",
                (clinic_id,)).fetchone()
            if row is None:
                raise NotFound("诊所尚未配置保留策略")
            return {"clinic_id": clinic_id, "version": row["version"], "rules": decode_json(row["rules_json"]),
                    "state": "active", "created_by": row["created_by"], "created_at": row["created_at"]}

    def _active_policy(self, connection, clinic_id: str):
        row = connection.execute(
            "SELECT * FROM retention_policies WHERE clinic_id=? AND state='active' ORDER BY version DESC LIMIT 1",
            (clinic_id,)).fetchone()
        if row is None:
            raise Conflict("诊所尚未配置生效的保留策略")
        return row

    # -- 预览与执行 ---------------------------------------------------------

    def create_preview(self, clinic_id: str, actor_id: str) -> dict[str, Any]:
        now = self.now()
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "records:manage", clinic_id=clinic_id)
            policy = self._active_policy(connection, clinic_id)
            rules = decode_json(policy["rules_json"])
            snapshot = self._compute_scope(connection, clinic_id, rules, now)
            preview_id = new_id("prv")
            connection.execute(
                "INSERT INTO retention_previews(id,clinic_id,policy_version,rules_json,cutoff_json,status,"
                "fingerprint,candidate_json,skipped_json,counts_json,created_by,created_at) "
                "VALUES(?,?,?,?,?,'ready',?,?,?,?,?,?)",
                (preview_id, clinic_id, policy["version"], encode_json(rules), encode_json(snapshot["cutoffs"]),
                 snapshot["fingerprint"], encode_json(snapshot["candidates"]),
                 encode_json(snapshot["skipped"]), encode_json(snapshot["counts"]), actor_id, now))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="retention_preview", aggregate_id=preview_id,
                               action="retention.preview_created", occurred_at=now,
                               payload={"policy_version": policy["version"], "fingerprint": snapshot["fingerprint"],
                                        "counts": snapshot["counts"]})
        return self.get_preview(clinic_id, actor_id, preview_id)

    def refresh_preview(self, clinic_id: str, actor_id: str, preview_id: str) -> dict[str, Any]:
        """以当前时间重算范围；策略版本变更后不允许刷新，只能基于新策略生成新预览。"""
        now = self.now()
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "records:manage", clinic_id=clinic_id)
            preview = self._load_preview(connection, clinic_id, preview_id)
            if preview["status"] != "ready":
                raise Conflict("已执行的预览不能刷新")
            policy = self._active_policy(connection, clinic_id)
            if policy["version"] != preview["policy_version"]:
                raise Conflict("保留策略已发布新版本，旧预览不能刷新或执行，请生成新预览",
                               details={"preview_policy_version": preview["policy_version"],
                                        "current_policy_version": policy["version"]})
            rules = decode_json(preview["rules_json"])
            snapshot = self._compute_scope(connection, clinic_id, rules, now)
            connection.execute(
                "UPDATE retention_previews SET cutoff_json=?,fingerprint=?,candidate_json=?,skipped_json=?,"
                "counts_json=?,created_at=? WHERE id=?",
                (encode_json(snapshot["cutoffs"]), snapshot["fingerprint"], encode_json(snapshot["candidates"]),
                 encode_json(snapshot["skipped"]), encode_json(snapshot["counts"]), now, preview_id))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="retention_preview", aggregate_id=preview_id,
                               action="retention.preview_refreshed", occurred_at=now,
                               payload={"fingerprint": snapshot["fingerprint"], "counts": snapshot["counts"]})
        return self.get_preview(clinic_id, actor_id, preview_id)

    def get_preview(self, clinic_id: str, actor_id: str, preview_id: str) -> dict[str, Any]:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "records:read", clinic_id=clinic_id)
            preview = self._load_preview(connection, clinic_id, preview_id)
            execution = connection.execute("SELECT * FROM retention_executions WHERE preview_id=?",
                                           (preview_id,)).fetchone()
            result = {
                "id": preview["id"], "clinic_id": clinic_id, "policy_version": preview["policy_version"],
                "rules": decode_json(preview["rules_json"]), "cutoffs": decode_json(preview["cutoff_json"]),
                "status": preview["status"], "fingerprint": preview["fingerprint"],
                "counts": decode_json(preview["counts_json"]),
                "frozen_skipped": decode_json(preview["skipped_json"]),
                "candidates": decode_json(preview["candidate_json"]),
                "created_by": preview["created_by"], "created_at": preview["created_at"],
                "executed_at": preview["executed_at"],
            }
            if execution:
                outcome = decode_json(execution["result_json"])
                result["execution"] = {k: outcome[k] for k in ("deleted", "totals", "executed_at")}
            return result

    def execute_preview(self, clinic_id: str, actor_id: str, preview_id: str) -> dict[str, Any]:
        now = self.now()
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "records:manage", clinic_id=clinic_id)
            preview = self._load_preview(connection, clinic_id, preview_id)
            prior = connection.execute("SELECT * FROM retention_executions WHERE preview_id=?",
                                       (preview_id,)).fetchone()
            if prior:
                # 安全重放：不重复删除、不追加审计，返回首次执行结果。
                return {**decode_json(prior["result_json"]), "replayed": True}
            if preview["status"] != "ready":
                raise Conflict("预览状态不允许执行")
            policy = self._active_policy(connection, clinic_id)
            if policy["version"] != preview["policy_version"]:
                raise Conflict("保留策略已发布新版本，旧预览不能执行，请生成新预览",
                               details={"preview_policy_version": preview["policy_version"],
                                        "current_policy_version": policy["version"]})
            stored_rules = decode_json(preview["rules_json"])
            stored_cutoffs = decode_json(preview["cutoff_json"])
            current = self._compute_scope(connection, clinic_id, stored_rules, now, cutoffs=stored_cutoffs)
            if current["fingerprint"] != preview["fingerprint"]:
                raise Conflict("预览范围已变化（有新增、合并或冻结变更），请刷新预览复核后再执行",
                               details={"stored_fingerprint": preview["fingerprint"],
                                        "current_fingerprint": current["fingerprint"]})
            deleted: dict[str, int] = {}
            for category, entries in current["candidates"].items():
                removable = [item["id"] for item in entries if not item["freeze_ids"] and not item["referenced"]]
                if removable:
                    for statement in CATEGORY_CHILD_DELETE.get(category, []):
                        connection.executemany(statement, [(record_id,) for record_id in removable])
                    table = CATEGORY_SPECS[category]["table"]
                    connection.executemany(
                        f"DELETE FROM {table} WHERE id=?", [(record_id,) for record_id in removable])
                deleted[category] = len(removable)
            totals = {"deleted": sum(deleted.values()),
                      "frozen_skipped": current["counts"]["totals"]["frozen_skipped"],
                      "referenced_skipped": current["counts"]["totals"]["referenced_skipped"]}
            execution_id = new_id("exe")
            outcome = {"id": execution_id, "preview_id": preview_id, "clinic_id": clinic_id,
                       "policy_version": preview["policy_version"], "executed_by": actor_id,
                       "executed_at": now, "deleted": deleted, "totals": totals, "replayed": False}
            connection.execute("UPDATE retention_previews SET status='executed',executed_at=? WHERE id=?",
                               (now, preview_id))
            connection.execute(
                "INSERT INTO retention_executions(id,preview_id,clinic_id,policy_version,result_json,"
                "executed_by,executed_at) VALUES(?,?,?,?,?,?,?)",
                (execution_id, preview_id, clinic_id, preview["policy_version"], encode_json(outcome), actor_id, now))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="retention_execution", aggregate_id=execution_id,
                               action="retention.executed", occurred_at=now,
                               payload={"preview_id": preview_id, "policy_version": preview["policy_version"],
                                        "fingerprint": preview["fingerprint"], "deleted": deleted, "totals": totals})
            return dict(outcome)

    @staticmethod
    def _load_preview(connection, clinic_id: str, preview_id: str):
        preview = connection.execute("SELECT * FROM retention_previews WHERE id=? AND clinic_id=?",
                                     (preview_id, clinic_id)).fetchone()
        if preview is None:
            raise NotFound("保留预览不存在")
        return preview

    def _compute_scope(self, connection, clinic_id: str, rules: list[dict[str, int]], as_of: str,
                       *, cutoffs: dict[str, str] | None = None) -> dict[str, Any]:
        """按当前数据计算候选、冻结跳过与引用跳过；cutoffs 可固定为重放校验。"""
        rules_by_category = {item["category"]: item["retention_days"] for item in rules}
        effective_cutoffs: dict[str, str] = {}
        for category, days in rules_by_category.items():
            if cutoffs and category in cutoffs:
                effective_cutoffs[category] = cutoffs[category]
            else:
                effective_cutoffs[category] = timestamp(parsed_timestamp(as_of) - timedelta(days=days))
        index = build_freeze_index(connection, clinic_id)
        candidates: dict[str, list[dict[str, Any]]] = {}
        frozen_skipped: list[dict[str, Any]] = []
        by_category_counts: dict[str, dict[str, int]] = {}
        for category in sorted(rules_by_category):
            spec = CATEGORY_SPECS[category]
            table, anchor, states = spec["table"], spec["anchor"], spec["states"]
            clauses = ["p.clinic_id=?", f"t.{anchor}<=?"]
            params: list[Any] = [clinic_id, effective_cutoffs[category]]
            if states is not None:
                placeholders = ",".join("?" for _ in states)
                clauses.append(f"t.{spec['state_column']} IN ({placeholders})")
                params.extend(states)
            rows = connection.execute(
                f"SELECT t.id AS id,t.patient_id AS patient_id,t.{anchor} AS anchor FROM {table} t "
                f"JOIN patients p ON p.id=t.patient_id WHERE {' AND '.join(clauses)} ORDER BY t.id",
                params).fetchall()
            entries = []
            frozen_count = referenced_count = removable_count = 0
            reference_checks = CATEGORY_REFERENCES.get(category, [])
            for row in rows:
                freeze_ids = index.match(category, row["id"], row["patient_id"], row["anchor"])
                referenced = False
                for check in reference_checks:
                    if connection.execute(check, (row["id"],)).fetchone():
                        referenced = True
                        break
                item = {"id": row["id"], "patient_id": row["patient_id"], "anchor": row["anchor"],
                        "freeze_ids": freeze_ids, "referenced": referenced}
                entries.append(item)
                # 冻结优先归类；被引用仅统计未冻结部分，保证三类之和等于候选数。
                if freeze_ids:
                    frozen_count += 1
                elif referenced:
                    referenced_count += 1
                else:
                    removable_count += 1
                if freeze_ids:
                    frozen_skipped.append({"category": category, "record_id": row["id"], "freeze_ids": freeze_ids})
            candidates[category] = entries
            by_category_counts[category] = {
                "candidates": len(entries), "frozen_skipped": frozen_count,
                "referenced_skipped": referenced_count,
                "removable": removable_count,
            }
        totals = {
            "candidates": sum(item["candidates"] for item in by_category_counts.values()),
            "frozen_skipped": sum(item["frozen_skipped"] for item in by_category_counts.values()),
            "referenced_skipped": sum(item["referenced_skipped"] for item in by_category_counts.values()),
            "removable": sum(item["removable"] for item in by_category_counts.values()),
        }
        counts = {"by_category": by_category_counts, "totals": totals}
        fingerprint_body = {
            "rules": rules, "cutoffs": effective_cutoffs,
            "candidates": {category: [item["id"] for item in entries]
                           for category, entries in candidates.items()},
            "frozen": frozen_skipped,
            "referenced": {category: [item["id"] for item in entries if item["referenced"]]
                           for category, entries in candidates.items()},
        }
        fingerprint = hashlib.sha256(encode_json(fingerprint_body).encode("utf-8")).hexdigest()
        return {"cutoffs": effective_cutoffs, "candidates": candidates,
                "skipped": frozen_skipped, "counts": counts, "fingerprint": fingerprint}
