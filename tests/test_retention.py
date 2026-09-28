from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
import json

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from careflow.api import create_handler
from careflow.clock import FrozenClock
from careflow.db import Database
from careflow.errors import Conflict, Forbidden, NotFound
from careflow.service import Careflow

ZERO_RULES = {"appointment_cancelled": 0, "followup_closed": 0, "assessment_draft": 0}


class RetentionCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "clinic.sqlite3")
        self.clock = FrozenClock(datetime(2026, 9, 27, 12, 0, tzinfo=UTC))
        self.app = Careflow(self.db, self.clock)
        initial = self.app.initialize_clinic("澄序门诊", "Asia/Shanghai", "诊所负责人", "LongPassphrase!2026")
        self.clinic = initial["clinic_id"]
        self.owner = initial["owner_id"]
        self.coordinator = self.app.create_staff(self.clinic, "运营协调员", "coordinator", actor_id=self.owner)["id"]
        self.archivist = self.app.create_staff(self.clinic, "档案管理员甲", "auditor", actor_id=self.owner)["id"]
        self.archivist2 = self.app.create_staff(self.clinic, "档案管理员乙", "auditor", actor_id=self.owner)["id"]
        self.patient = self.app.create_patient(self.clinic, self.coordinator, "case-r01", "林女士")
        self.pid = self.patient["id"]

    def tearDown(self):
        self.temp.cleanup()

    def advance(self, *parts):
        self.clock.set(datetime(*parts, tzinfo=UTC))

    def cancelled_appointment(self, key="visit-r", day=29):
        sequence = getattr(self, "_appt_seq", 0)
        self._appt_seq = sequence + 1
        self.advance(2026, 9, 27, 12, sequence * 15)
        appointment = self.app.create_appointment(
            self.clinic, self.coordinator, self.pid, "复诊",
            f"2026-09-{day}T10:00:00+08:00", f"2026-09-{day}T10:30:00+08:00", key)
        self.advance(2026, 9, 27, 12, sequence * 15 + 11)
        self.assertEqual(self.app.expire_holds(self.clinic)["expired"], 1)
        return appointment["id"]

    def done_followup(self, key="fup-r", due="2026-09-27T11:00:00Z"):
        self.app.schedule_followup(self.clinic, self.coordinator, self.pid, due, "复诊反馈", key)
        claimed = self.app.claim_followups(self.clinic, self.coordinator, lease_minutes=60)
        item = claimed[0]
        self.app.complete_followup(self.clinic, self.coordinator, item["id"], item["claim_token"], "已联系", item["version"])
        return item["id"]

    def draft_assessment(self):
        return self.app.create_assessment(self.clinic, self.owner, self.pid, "wellbeing", {}, {"sleep": "一般"})["id"]

    def freeze(self, **overrides):
        params = {"scope_start": "2026-09-29", "scope_end": "2026-09-29",
                  "reason": "收到患者争议通知，保全相关记录", "external_notice_ref": "DISPUTE-2026-0091"}
        params.update(overrides)
        return self.app.retention.create_freeze(self.clinic, self.archivist, self.pid, **params)

    def test_policy_requires_owner_and_versions_increment(self):
        with self.assertRaises(Forbidden):
            self.app.retention.create_policy(self.clinic, self.archivist, ZERO_RULES)
        with self.assertRaises(NotFound):
            self.app.retention.get_policy(self.clinic, self.owner)
        first = self.app.retention.create_policy(self.clinic, self.owner, ZERO_RULES)
        self.assertEqual(first["version"], 1)
        second = self.app.retention.create_policy(self.clinic, self.owner, {"followup_closed": 30})
        self.assertEqual(second["version"], 2)
        self.assertEqual(self.app.retention.get_policy(self.clinic, self.archivist)["version"], 2)

    def test_policy_rejects_unknown_category_and_non_cleanable_tables(self):
        with self.assertRaises(Exception):
            self.app.retention.create_policy(self.clinic, self.owner, {"encounters": 1})
        with self.assertRaises(Exception):
            self.app.retention.create_policy(self.clinic, self.owner, {"followup_closed": -1})

    def test_freeze_setter_cannot_release_and_release_requires_notice_basis(self):
        freeze = self.freeze()
        # 设置人与解除人必须分离。
        with self.assertRaises(Forbidden):
            self.app.retention.release_freeze(self.clinic, self.archivist, freeze["id"],
                                              "COURT-2026-7", "争议已撤回")
        with self.assertRaises(Exception):
            self.app.retention.release_freeze(self.clinic, self.archivist2, freeze["id"], "", "争议已撤回")
        released = self.app.retention.release_freeze(self.clinic, self.archivist2, freeze["id"],
                                                     "COURT-2026-77", "收到争议结案通知")
        self.assertEqual(released["state"], "released")
        self.assertEqual(released["released_by"], self.archivist2)
        self.assertEqual(released["release_notice_ref"], "COURT-2026-77")
        with self.assertRaises(Conflict):
            self.app.retention.release_freeze(self.clinic, self.owner, freeze["id"], "COURT-2026-78", "再次解除")

    def test_freeze_validates_scope_and_related_record_ownership(self):
        with self.assertRaises(Exception):
            self.freeze(scope_end="2026-09-20")
        appointment_id = self.cancelled_appointment()
        created = self.freeze(related_records=[{"record_category": "appointment_cancelled",
                                                "record_id": appointment_id}])
        self.assertEqual(created["related_records"][0]["record_id"], appointment_id)
        with self.assertRaises(NotFound):
            self.freeze(external_notice_ref="DISPUTE-2026-0092",
                        related_records=[{"record_category": "appointment_cancelled", "record_id": "apt_missing"}])

    def test_preview_counts_candidates_and_frozen_skips(self):
        self.app.retention.create_policy(self.clinic, self.owner, ZERO_RULES)
        appointment_id = self.cancelled_appointment()
        followup_id = self.done_followup()
        assessment_id = self.draft_assessment()
        self.advance(2026, 10, 1, 0, 0)
        self.freeze(related_records=[{"record_category": "appointment_cancelled", "record_id": appointment_id}])
        preview = self.app.retention.create_preview(self.clinic, self.archivist)
        self.assertEqual(preview["candidates_total"], 3)
        self.assertEqual(preview["to_clean_total"], 2)
        self.assertEqual(preview["frozen_skipped_total"], 1)
        skipped = preview["frozen_skipped"][0]
        self.assertEqual(skipped["record_id"], appointment_id)
        self.assertTrue(skipped["freeze_ids"])
        candidate_ids = {item["record_id"] for item in preview["candidates"]}
        self.assertEqual(candidate_ids, {appointment_id, followup_id, assessment_id})

    def test_frozen_records_are_not_cleaned_but_others_are_and_still_exported(self):
        self.app.retention.create_policy(self.clinic, self.owner, ZERO_RULES)
        frozen_appt = self.cancelled_appointment("visit-frozen", day=29)
        other_appt = self.cancelled_appointment("visit-other", day=30)
        self.advance(2026, 10, 1, 0, 0)
        self.freeze(related_records=[{"record_category": "appointment_cancelled", "record_id": frozen_appt}])
        preview = self.app.retention.create_preview(self.clinic, self.archivist)
        result = self.app.retention.execute_preview(self.clinic, self.owner, preview["id"], "exec-1")
        self.assertFalse(result["replayed"])
        self.assertEqual(result["cleaned_total"], 1)
        with self.db.transaction(write=False) as connection:
            self.assertIsNone(connection.execute("SELECT id FROM appointments WHERE id=?", (other_appt,)).fetchone())
            self.assertIsNotNone(connection.execute("SELECT id FROM appointments WHERE id=?", (frozen_appt,)).fetchone())
        # 被冻结档案仍出现在患者导出中，未冻结且已清理的档案不再出现。
        import hashlib
        digest = hashlib.sha256(b"data_export-r1").hexdigest()
        self.app.grant_consent(self.clinic, self.owner, self.pid, "data_export", 1, digest)
        exported = self.app.exports.export(self.clinic, self.owner, self.pid, ["appointments"], "争议核对", "exp-frz")
        ids = {item["id"] for item in exported["data"]["appointments"]}
        self.assertIn(frozen_appt, ids)
        self.assertNotIn(other_appt, ids)

    def test_old_preview_cannot_execute_after_policy_version_changes(self):
        self.app.retention.create_policy(self.clinic, self.owner, ZERO_RULES)
        self.cancelled_appointment()
        self.advance(2026, 10, 1, 0, 0)
        preview = self.app.retention.create_preview(self.clinic, self.archivist)
        self.app.retention.create_policy(self.clinic, self.owner, {"appointment_cancelled": 90})
        with self.assertRaises(Conflict):
            self.app.retention.execute_preview(self.clinic, self.owner, preview["id"], "exec-stale")

    def test_preview_must_recalculate_when_records_appear_between_preview_and_execute(self):
        self.app.retention.create_policy(self.clinic, self.owner, ZERO_RULES)
        self.cancelled_appointment()
        self.advance(2026, 10, 1, 0, 0)
        preview = self.app.retention.create_preview(self.clinic, self.archivist)
        # 预览之后新增一条已结束且到期的随访，候选范围发生变化。
        self.done_followup("fup-new")
        with self.assertRaises(Conflict):
            self.app.retention.execute_preview(self.clinic, self.owner, preview["id"], "exec-changed")
        fresh = self.app.retention.create_preview(self.clinic, self.archivist)
        self.assertEqual(fresh["candidates_total"], 2)
        executed = self.app.retention.execute_preview(self.clinic, self.owner, fresh["id"], "exec-fresh")
        self.assertEqual(executed["cleaned_total"], 2)

    def test_preview_must_recalculate_when_patient_is_merged(self):
        self.app.retention.create_policy(self.clinic, self.owner, ZERO_RULES)
        self.cancelled_appointment()
        self.advance(2026, 10, 1, 0, 0)
        preview = self.app.retention.create_preview(self.clinic, self.archivist)
        target = self.app.create_patient(self.clinic, self.coordinator, "case-r02", "黄女士")
        self.app.merge_patients(self.clinic, self.coordinator, self.pid, target["id"],
                                expected_source=1, expected_target=1, reason="重复建档")
        with self.assertRaises(Conflict):
            self.app.retention.execute_preview(self.clinic, self.owner, preview["id"], "exec-merge")

    def test_preview_stays_valid_when_only_time_passes_without_record_changes(self):
        self.app.retention.create_policy(self.clinic, self.owner, {"followup_closed": 30})
        self.advance(2026, 11, 15, 0, 0)
        followup_id = self.done_followup(due="2026-10-01T00:00:00Z")
        self.advance(2026, 11, 16, 0, 0)
        preview = self.app.retention.create_preview(self.clinic, self.archivist)
        self.assertEqual(preview["to_clean_total"], 1)
        # 仅时间继续流逝，没有任何记录新增、签署、合并或冻结变动，预览仍应可执行。
        self.advance(2026, 11, 20, 0, 0)
        executed = self.app.retention.execute_preview(self.clinic, self.owner, preview["id"], "exec-time")
        self.assertEqual(executed["cleaned_total"], 1)
        with self.db.transaction(write=False) as connection:
            self.assertIsNone(connection.execute("SELECT id FROM followups WHERE id=?", (followup_id,)).fetchone())

    def test_execute_is_safely_replayable_and_key_cannot_be_reused(self):
        self.app.retention.create_policy(self.clinic, self.owner, ZERO_RULES)
        self.done_followup()
        self.advance(2026, 10, 1, 0, 0)
        preview = self.app.retention.create_preview(self.clinic, self.archivist)
        first = self.app.retention.execute_preview(self.clinic, self.owner, preview["id"], "exec-replay")
        replay = self.app.retention.execute_preview(self.clinic, self.owner, preview["id"], "exec-replay")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["cleaned_total"], first["cleaned_total"])
        preview_two = self.app.retention.create_preview(self.clinic, self.archivist)
        self.assertEqual(preview_two["to_clean_total"], 0)
        with self.assertRaises(Conflict):
            self.app.retention.execute_preview(self.clinic, self.owner, preview_two["id"], "exec-replay")

    def test_signed_records_and_audit_chain_history_are_never_altered(self):
        self.app.retention.create_policy(self.clinic, self.owner, ZERO_RULES)
        # 一份签署过的评估草稿不存在；签署评估永不进入清理候选。
        assessment = self.app.create_assessment(self.clinic, self.owner, self.pid, "wellbeing", {}, {"q": "a"})
        self.app.sign_assessment(self.clinic, self.owner, assessment["id"], expected_version=1)
        # 完成一条预约并签署就诊记录。
        appointment = self.app.create_appointment(
            self.clinic, self.coordinator, self.pid, "复诊",
            "2026-09-29T10:00:00+08:00", "2026-09-29T10:30:00+08:00", "visit-signed")
        self.app.transition_appointment(self.clinic, self.coordinator, appointment["id"], 1, "book")
        self.app.transition_appointment(self.clinic, self.coordinator, appointment["id"], 2, "arrive")
        self.advance(2026, 9, 29, 2, 31)
        self.app.transition_appointment(self.clinic, self.coordinator, appointment["id"], 3, "start")
        encounter = self.app.encounter_for_appointment(self.clinic, self.owner, appointment["id"])
        for section in ("chief_complaint", "assessment", "plan"):
            self.app.add_encounter_note(self.clinic, self.owner, encounter["id"], section, f"原文-{section}",
                                        expected_version=encounter["version"])
            encounter = self.app.encounter_for_appointment(self.clinic, self.owner, appointment["id"])
        self.app.sign_encounter(self.clinic, self.owner, encounter["id"], encounter["version"])
        self.app.transition_appointment(self.clinic, self.coordinator, appointment["id"], 4, "complete")
        self.advance(2026, 10, 1, 0, 0)
        self.cancelled_appointment("visit-to-clean")
        self.advance(2026, 10, 1, 0, 5)
        preview = self.app.retention.create_preview(self.clinic, self.archivist)
        candidate_ids = {item["record_id"] for item in preview["candidates"]}
        self.assertNotIn(assessment["id"], candidate_ids)
        self.assertNotIn(appointment["id"], candidate_ids)
        self.app.retention.execute_preview(self.clinic, self.owner, preview["id"], "exec-signed")
        assessments = self.app.list_assessments(self.clinic, self.owner, self.pid)
        self.assertEqual([item["id"] for item in assessments], [assessment["id"]])
        signed = self.app.encounter_notes(self.clinic, self.owner,
                                          self.app.encounter_for_appointment(self.clinic, self.owner, appointment["id"])["id"])
        self.assertTrue(any("原文" in note["body"] for note in signed["notes"]))
        # 被清理预约的审计事件仍然保留，哈希链校验通过。
        history = self.app.audit_history(self.clinic, self.archivist2, limit=500)
        self.assertTrue(any(event["action"] == "appointment.hold_expired" for event in history))
        self.assertTrue(any(event["action"] == "retention.executed" for event in history))
        self.assertTrue(self.app.verify_audit(self.clinic, self.owner)["ok"])

    def test_freeze_follows_merged_patients_for_scope_and_export(self):
        source = self.app.create_patient(self.clinic, self.coordinator, "case-r03", "陈女士")
        appointment = self.app.create_appointment(
            self.clinic, self.coordinator, source["id"], "复诊",
            "2026-09-29T10:00:00+08:00", "2026-09-29T10:30:00+08:00", "visit-merge")
        self.advance(2026, 9, 27, 12, 11)
        self.app.expire_holds(self.clinic)
        # 合并之前先按来源档案设置争议冻结。
        freeze = self.app.retention.create_freeze(
            self.clinic, self.archivist, source["id"], "2026-09-29", "2026-09-29",
            "争议保全", "DISPUTE-2026-0100",
            related_records=[{"record_category": "appointment_cancelled", "record_id": appointment["id"]}])
        self.app.merge_patients(self.clinic, self.coordinator, source["id"], self.pid,
                                expected_source=1, expected_target=1, reason="重复建档")
        self.app.retention.create_policy(self.clinic, self.owner, ZERO_RULES)
        self.advance(2026, 10, 1, 0, 0)
        preview = self.app.retention.create_preview(self.clinic, self.archivist)
        skipped = {item["record_id"]: item for item in preview["frozen_skipped"]}
        self.assertIn(appointment["id"], skipped)
        self.assertEqual(skipped[appointment["id"]]["freeze_ids"], [freeze["id"]])
        # 解除冻结（由另一名档案管理员）后，重新预览即可清理。
        self.app.retention.release_freeze(self.clinic, self.archivist2, freeze["id"], "COURT-2026-90", "争议结案")
        re_preview = self.app.retention.create_preview(self.clinic, self.archivist)
        self.assertEqual(re_preview["frozen_skipped_total"], 0)
        self.assertEqual(re_preview["to_clean_total"], 1)
        # 合并来源的冻结档案在保留档案导出中不被遗漏。
        import hashlib
        digest = hashlib.sha256(b"data_export-r2").hexdigest()
        self.app.grant_consent(self.clinic, self.owner, self.pid, "data_export", 1, digest)
        exported = self.app.exports.export(self.clinic, self.owner, self.pid, ["appointments"], "争议核对", "exp-merge")
        self.assertIn(appointment["id"], {item["id"] for item in exported["data"]["appointments"]})

    def test_freeze_listing_and_run_history(self):
        self.freeze()
        listing = self.app.retention.list_freezes(self.clinic, self.archivist, patient_id=self.pid)
        self.assertEqual(len(listing["items"]), 1)
        self.assertEqual(listing["items"][0]["state"], "active")
        self.app.retention.create_policy(self.clinic, self.owner, ZERO_RULES)
        self.advance(2026, 10, 1, 0, 0)
        self.app.retention.create_preview(self.clinic, self.archivist)
        runs = self.app.retention.run_history(self.clinic, self.owner)
        self.assertEqual(runs["items"][0]["state"], "previewed")

    def test_http_retention_flow(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(self.app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"

        def call(method, path, token, payload=None, headers=None):
            data = json.dumps(payload).encode() if payload is not None else None
            request = Request(base + path, data=data, method=method, headers={
                "X-Clinic-ID": self.clinic, "Authorization": f"Bearer {token}",
                "Content-Type": "application/json", **(headers or {})})
            with urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())

        def login(staff_id, password="LongPassphrase!2026"):
            request = Request(base + "/auth/token", data=json.dumps(
                {"staff_id": staff_id, "password": password}).encode(), method="POST",
                headers={"X-Clinic-ID": self.clinic, "Content-Type": "application/json"})
            with urlopen(request, timeout=3) as response:
                return json.loads(response.read())["access_token"]

        try:
            owner_token = login(self.owner)
            self.app.set_password(self.clinic, self.owner, self.archivist, "ArchivistPass!2026")
            archivist_token = login(self.archivist, "ArchivistPass!2026")
            status, policy = call("POST", "/retention/policies", owner_token, {"rules": ZERO_RULES})
            self.assertEqual(status, 201)
            self.assertEqual(policy["version"], 1)
            status, freeze = call("POST", f"/patients/{self.pid}/retention-freezes", archivist_token, {
                "scope_start": "2026-09-29", "scope_end": "2026-09-29",
                "reason": "争议保全", "external_notice_ref": "DISPUTE-HTTP-1"})
            self.assertEqual(status, 201)
            self.assertEqual(freeze["state"], "active")
            status, preview = call("POST", "/retention/previews", archivist_token, {})
            self.assertEqual(status, 201)
            self.assertEqual(preview["candidates_total"], 0)
            status, fetched = call("GET", f"/retention/freezes/{freeze['id']}", archivist_token)
            self.assertEqual(status, 200)
            self.assertEqual(fetched["external_notice_ref"], "DISPUTE-HTTP-1")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
