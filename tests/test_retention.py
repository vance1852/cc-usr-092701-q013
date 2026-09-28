from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from careflow.api import create_handler
from careflow.clock import FrozenClock
from careflow.db import Database
from careflow.errors import Conflict, Forbidden, NotFound, ValidationError
from careflow.service import Careflow


class RetentionCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "clinic.sqlite3")
        self.clock = FrozenClock(datetime(2026, 9, 28, 12, 0, tzinfo=UTC))
        self.app = Careflow(self.db, self.clock)
        initial = self.app.initialize_clinic("澄序门诊", "Asia/Shanghai", "档案负责人", "LongPassphrase!2026")
        self.clinic = initial["clinic_id"]
        self.owner = initial["owner_id"]
        self.archivist = self.app.create_staff(self.clinic, "档案员甲", "owner", actor_id=self.owner)["id"]
        self.releaser = self.app.create_staff(self.clinic, "档案员乙", "owner", actor_id=self.owner)["id"]
        self.clinician = self.app.create_staff(self.clinic, "临床医生", "clinician", actor_id=self.owner)["id"]
        self.coordinator = self.app.create_staff(self.clinic, "运营协调员", "coordinator", actor_id=self.owner)["id"]
        self.patient = self.app.create_patient(self.clinic, self.coordinator, "case-r1", "何女士")["id"]

    def tearDown(self):
        self.temp.cleanup()

    def cancelled_appointment(self, key, year):
        self.clock.set(datetime(year, 3, 2, 1, 50, tzinfo=UTC))
        apt = self.app.create_appointment(
            self.clinic, self.coordinator, self.patient, "旧预约",
            f"{year}-03-02T10:00:00+08:00", f"{year}-03-02T10:30:00+08:00", key, hold_minutes=60)
        self.app.transition_appointment(self.clinic, self.coordinator, apt["id"], 1, "book")
        self.app.transition_appointment(self.clinic, self.coordinator, apt["id"], 2, "cancel", reason="患者取消")
        return apt

    def set_clock_back(self):
        self.clock.set(datetime(2026, 9, 28, 12, 0, tzinfo=UTC))

    # -- 冻结 ---------------------------------------------------------------

    def test_freeze_requires_scope_and_validates_associated_records(self):
        with self.assertRaises(ValidationError):
            self.app.retention.create_freeze(self.clinic, self.archivist, reason="争议", notice_ref="D-1")
        with self.assertRaises(NotFound):
            self.app.retention.create_freeze(
                self.clinic, self.archivist, reason="争议", notice_ref="D-1",
                associated_records=[{"category": "observation", "id": "obs_missing"}])
        with self.assertRaises(ValidationError):
            self.app.retention.create_freeze(
                self.clinic, self.archivist, reason="争议", notice_ref="D-1", patient_id=self.patient,
                scope_start="2026-12-31T00:00:00Z", scope_end="2025-01-01T00:00:00Z")
        freeze = self.app.retention.create_freeze(
            self.clinic, self.archivist, reason="收到患者争议通知", notice_ref="DISPUTE-2026-001",
            patient_id=self.patient, scope_start="2025-01-01T00:00:00Z", scope_end="2025-12-31T23:59:59Z")
        self.assertEqual(freeze["state"], "active")
        self.assertIsNone(freeze["released_at"])

    def test_freeze_setter_and_releaser_must_differ_and_release_needs_external_basis(self):
        freeze = self.app.retention.create_freeze(
            self.clinic, self.archivist, reason="争议", notice_ref="D-1", patient_id=self.patient)
        with self.assertRaises(Forbidden):
            self.app.retention.release_freeze(self.clinic, self.archivist, freeze["id"], external_ref="外部结案-1")
        with self.assertRaises(ValidationError):
            self.app.retention.release_freeze(self.clinic, self.releaser, freeze["id"], external_ref="")
        released = self.app.retention.release_freeze(
            self.clinic, self.releaser, freeze["id"], external_ref="COURT-NOTICE-2026-77",
            doc_digest="a" * 64, note="外部机构通知争议程序结束")
        self.assertEqual(released["state"], "released")
        self.assertEqual(released["released_by"], self.releaser)
        self.assertEqual(released["release_external_ref"], "COURT-NOTICE-2026-77")
        with self.assertRaises(Conflict):
            self.app.retention.release_freeze(self.clinic, self.releaser, freeze["id"], external_ref="再次解除")

    def test_only_records_management_role_can_freeze_or_configure(self):
        with self.assertRaises(Forbidden):
            self.app.retention.create_freeze(
                self.clinic, self.coordinator, reason="争议", notice_ref="D-1", patient_id=self.patient)
        with self.assertRaises(Forbidden):
            self.app.retention.set_policy(
                self.clinic, self.coordinator, [{"category": "observation", "retention_days": 30}])
        # 审计岗只读：可查看不能操作。
        auditor = self.app.create_staff(self.clinic, "内审", "auditor", actor_id=self.owner)["id"]
        listing = self.app.retention.list_freezes(self.clinic, auditor)
        self.assertEqual(listing["items"], [])
        with self.assertRaises(Forbidden):
            self.app.retention.create_freeze(
                self.clinic, auditor, reason="争议", notice_ref="D-1", patient_id=self.patient)

    # -- 预览与执行 ---------------------------------------------------------

    def test_preview_counts_candidates_frozen_skips_and_execution_is_replayable(self):
        in_window = self.cancelled_appointment("visit-old", 2025)
        outside = self.cancelled_appointment("visit-older", 2024)
        self.app.retention.create_freeze(
            self.clinic, self.archivist, reason="争议", notice_ref="D-1", patient_id=self.patient,
            scope_start="2025-01-01T00:00:00Z", scope_end="2025-12-31T23:59:59Z")
        self.app.retention.set_policy(
            self.clinic, self.owner, [{"category": "appointment", "retention_days": 30}])
        self.set_clock_back()
        preview = self.app.retention.create_preview(self.clinic, self.owner)
        totals = preview["counts"]["totals"]
        self.assertEqual(totals["candidates"], 2)
        self.assertEqual(totals["frozen_skipped"], 1)
        self.assertEqual(totals["removable"], 1)
        skipped = {(item["category"], item["record_id"]) for item in preview["frozen_skipped"]}
        self.assertIn(("appointment", in_window["id"]), skipped)
        result = self.app.retention.execute_preview(self.clinic, self.owner, preview["id"])
        self.assertEqual(result["deleted"], {"appointment": 1})
        # 冻结记录保留、窗口外记录已清理。
        with self.db.transaction(write=False) as conn:
            kept = conn.execute("SELECT count(*) FROM appointments WHERE id=?", (in_window["id"],)).fetchone()[0]
            gone = conn.execute("SELECT count(*) FROM appointments WHERE id=?", (outside["id"],)).fetchone()[0]
        self.assertEqual(kept, 1)
        self.assertEqual(gone, 0)
        # 安全重放：不产生第二次删除。
        replay = self.app.retention.execute_preview(self.clinic, self.owner, preview["id"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["totals"]["deleted"], 1)

    def test_preview_rejects_execution_after_scope_change_and_refresh_recomputes(self):
        first = self.cancelled_appointment("visit-1", 2024)
        self.app.retention.set_policy(
            self.clinic, self.owner, [{"category": "appointment", "retention_days": 30}])
        self.set_clock_back()
        preview = self.app.retention.create_preview(self.clinic, self.owner)
        self.assertEqual(preview["counts"]["totals"]["removable"], 1)
        # 预览后新产生一条符合范围的记录。
        second = self.cancelled_appointment("visit-2", 2023)
        self.set_clock_back()
        with self.assertRaises(Conflict):
            self.app.retention.execute_preview(self.clinic, self.owner, preview["id"])
        refreshed = self.app.retention.refresh_preview(self.clinic, self.owner, preview["id"])
        self.assertEqual(refreshed["counts"]["totals"]["removable"], 2)
        executed = self.app.retention.execute_preview(self.clinic, self.owner, preview["id"])
        self.assertEqual(executed["deleted"]["appointment"], 2)

    def test_preview_is_bound_to_policy_version(self):
        self.cancelled_appointment("visit-1", 2024)
        self.app.retention.set_policy(
            self.clinic, self.owner, [{"category": "appointment", "retention_days": 30}])
        self.set_clock_back()
        preview = self.app.retention.create_preview(self.clinic, self.owner)
        self.app.retention.set_policy(
            self.clinic, self.owner, [{"category": "appointment", "retention_days": 14}])
        with self.assertRaises(Conflict):
            self.app.retention.execute_preview(self.clinic, self.owner, preview["id"])
        with self.assertRaises(Conflict):
            self.app.retention.refresh_preview(self.clinic, self.owner, preview["id"])
        new_preview = self.app.retention.create_preview(self.clinic, self.owner)
        self.assertEqual(new_preview["policy_version"], 2)
        self.app.retention.execute_preview(self.clinic, self.owner, new_preview["id"])

    def test_freeze_released_between_preview_and_execution_blocks_and_recounts(self):
        apt = self.cancelled_appointment("visit-1", 2024)
        freeze = self.app.retention.create_freeze(
            self.clinic, self.archivist, reason="争议", notice_ref="D-1", patient_id=self.patient)
        self.app.retention.set_policy(
            self.clinic, self.owner, [{"category": "appointment", "retention_days": 30}])
        self.set_clock_back()
        preview = self.app.retention.create_preview(self.clinic, self.owner)
        self.assertEqual(preview["counts"]["totals"]["frozen_skipped"], 1)
        self.app.retention.release_freeze(self.clinic, self.releaser, freeze["id"], external_ref="EXT-9")
        with self.assertRaises(Conflict):
            self.app.retention.execute_preview(self.clinic, self.owner, preview["id"])
        refreshed = self.app.retention.refresh_preview(self.clinic, self.owner, preview["id"])
        self.assertEqual(refreshed["counts"]["totals"]["frozen_skipped"], 0)
        self.assertEqual(refreshed["counts"]["totals"]["removable"], 1)

    def test_patient_merge_keeps_freeze_protection_on_source_records(self):
        duplicate = self.app.create_patient(self.clinic, self.coordinator, "case-r2", "何女士（重复）")["id"]
        self.clock.set(datetime(2024, 6, 1, 1, 50, tzinfo=UTC))
        apt = self.app.create_appointment(
            self.clinic, self.coordinator, duplicate, "旧预约",
            "2024-06-01T10:00:00+08:00", "2024-06-01T10:30:00+08:00", "dup-1", hold_minutes=60)
        self.app.transition_appointment(self.clinic, self.coordinator, apt["id"], 1, "book")
        self.app.transition_appointment(self.clinic, self.coordinator, apt["id"], 2, "cancel", reason="重复")
        self.app.retention.create_freeze(
            self.clinic, self.archivist, reason="争议", notice_ref="D-2", patient_id=duplicate)
        self.app.merge_patients(self.clinic, self.owner, duplicate, self.patient,
                                expected_source=1, expected_target=1, reason="同一患者重复建档")
        self.set_clock_back()
        self.app.retention.set_policy(
            self.clinic, self.owner, [{"category": "appointment", "retention_days": 30}])
        preview = self.app.retention.create_preview(self.clinic, self.owner)
        skipped = {item["record_id"] for item in preview["frozen_skipped"]}
        self.assertIn(apt["id"], skipped)

    def test_explicit_associated_freeze_protects_only_listed_record(self):
        protected = self.app.record_observation(
            self.clinic, self.clinician, self.patient, "weight_kg", 72.0, "2024-01-01T08:00:00+08:00")
        other = self.app.record_observation(
            self.clinic, self.clinician, self.patient, "weight_kg", 73.0, "2024-02-01T08:00:00+08:00")
        self.app.retention.create_freeze(
            self.clinic, self.archivist, reason="争议", notice_ref="D-3",
            associated_records=[{"category": "observation", "id": protected["id"]}])
        self.app.retention.set_policy(
            self.clinic, self.owner, [{"category": "observation", "retention_days": 30}])
        preview = self.app.retention.create_preview(self.clinic, self.owner)
        skipped = {item["record_id"] for item in preview["frozen_skipped"]}
        self.assertIn(protected["id"], skipped)
        self.assertNotIn(other["id"], skipped)

    def test_signed_encounters_patients_and_audit_chain_are_never_deleted(self):
        from careflow.retention import CATEGORIES
        self.assertNotIn("encounter", CATEGORIES)
        self.assertNotIn("encounter_note", CATEGORIES)
        self.assertNotIn("audit_event", CATEGORIES)
        self.assertNotIn("patient", CATEGORIES)
        self.cancelled_appointment("visit-1", 2024)
        self.app.retention.set_policy(
            self.clinic, self.owner, [{"category": "appointment", "retention_days": 30}])
        self.set_clock_back()
        preview = self.app.retention.create_preview(self.clinic, self.owner)
        self.app.retention.execute_preview(self.clinic, self.owner, preview["id"])
        # 患者档案仍在；审计链可校验且包含执行事件。
        self.assertEqual(self.app.get_patient(self.clinic, self.owner, self.patient)["state"], "active")
        verification = self.app.verify_audit(self.clinic, self.owner)
        self.assertTrue(verification["ok"])
        self.assertGreater(verification["events_checked"], 0)

    def test_cancelled_plan_with_revisions_is_removable_but_plan_linked_to_encounter_is_kept(self):
        digest = hashlib.sha256(b"weight-r1").hexdigest()
        self.clock.set(datetime(2024, 1, 1, tzinfo=UTC))
        consent = self.app.grant_consent(
            self.clinic, self.clinician, self.patient, "weight_program", 1, digest)
        plan = self.app.create_plan(
            self.clinic, self.clinician, self.patient, "weight", self.clinician,
            {"description": "旧计划"}, {}, "2024-01-01", consent_id=consent["id"])
        self.app.transition_plan(self.clinic, self.clinician, plan["id"], 1, "propose")
        self.app.transition_plan(self.clinic, self.clinician, plan["id"], 2, "activate")
        self.app.transition_plan(self.clinic, self.clinician, plan["id"], 3, "pause", reason="患者暂停")
        self.app.transition_plan(self.clinic, self.clinician, plan["id"], 4, "cancel", reason="患者放弃")
        self.app.withdraw_consent(self.clinic, self.clinician, consent["id"], "患者撤回授权")
        self.set_clock_back()
        self.app.retention.set_policy(
            self.clinic, self.owner, [{"category": "plan", "retention_days": 30},
                                      {"category": "consent", "retention_days": 30}])
        preview = self.app.retention.create_preview(self.clinic, self.owner)
        categories = preview["counts"]["by_category"]
        # 计划的修订与节点为聚合私有记录，随计划删除；授权被计划引用而保留。
        self.assertEqual(categories["plan"]["removable"], 1)
        self.assertGreaterEqual(categories["consent"]["referenced_skipped"], 1)
        self.app.retention.execute_preview(self.clinic, self.owner, preview["id"])
        with self.db.transaction(write=False) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM plans WHERE id=?", (plan["id"],)).fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM plan_revisions WHERE plan_id=?", (plan["id"],)).fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM consents WHERE id=?", (consent["id"],)).fetchone()[0], 1)
        self.assertTrue(self.app.verify_audit(self.clinic, self.owner)["ok"])

    # -- 导出联动 -----------------------------------------------------------

    def test_export_must_cover_sections_touched_by_active_freeze(self):
        observation = self.app.record_observation(
            self.clinic, self.clinician, self.patient, "weight_kg", 70.0, "2024-01-01T08:00:00+08:00")
        self.app.retention.create_freeze(
            self.clinic, self.archivist, reason="争议", notice_ref="D-5",
            associated_records=[{"category": "observation", "id": observation["id"]}])
        digest = hashlib.sha256(b"export-consent").hexdigest()
        self.app.grant_consent(self.clinic, self.owner, self.patient, "data_export", 1, digest)
        with self.assertRaises(Conflict):
            self.app.exports.export(self.clinic, self.owner, self.patient, ["profile"], "争议调证", "exp-1")
        full = self.app.exports.export(
            self.clinic, self.owner, self.patient, ["profile", "observations"], "争议调证", "exp-1")
        self.assertEqual(full["frozen_records"],
                         {"observations": [observation["id"]]})
        replay = self.app.exports.export(
            self.clinic, self.owner, self.patient, ["observations", "profile"], "争议调证", "exp-1")
        self.assertEqual(replay["sha256"], full["sha256"])

    # -- HTTP ---------------------------------------------------------------

    def test_http_freeze_and_preview_routes(self):
        import threading
        server = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(self.app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            token = login(base, self.clinic, self.owner, "LongPassphrase!2026")
            self.app.set_password(self.clinic, self.owner, self.releaser, "ReleaserPassphrase!2026")
            releaser_token = login(base, self.clinic, self.releaser, "ReleaserPassphrase!2026")
            headers = {"X-Clinic-ID": self.clinic, "Authorization": f"Bearer {token}",
                       "Content-Type": "application/json"}
            releaser_headers = {**headers, "Authorization": f"Bearer {releaser_token}"}
            body = json.dumps({"rules": [{"category": "appointment", "retention_days": 30}]}).encode()
            with urlopen(Request(base + "/retention/policy", data=body, method="POST", headers=headers), timeout=3) as resp:
                self.assertEqual(resp.status, 200)
                payload = json.loads(resp.read())
                self.assertEqual(payload["version"], 1)
            freeze_body = json.dumps({"reason": "争议通知", "notice_ref": "D-HTTP-1",
                                      "patient_id": self.patient}).encode()
            with urlopen(Request(base + "/records/freezes", data=freeze_body, method="POST",
                                 headers=headers), timeout=3) as resp:
                self.assertEqual(resp.status, 201)
                freeze = json.loads(resp.read())
            with urlopen(Request(base + "/retention/previews", data=b"", method="POST",
                                 headers=headers), timeout=3) as resp:
                preview = json.loads(resp.read())
            self.assertIn("counts", preview)
            release_body = json.dumps({"external_ref": "EXT-HTTP-1"}).encode()
            with urlopen(Request(base + f"/records/freezes/{freeze['id']}/release", data=release_body,
                                 method="POST", headers=releaser_headers), timeout=3) as resp:
                self.assertEqual(json.loads(resp.read())["state"], "released")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


def login(base, clinic, staff_id, password):
    body = json.dumps({"staff_id": staff_id, "password": password}).encode()
    request = Request(base + "/auth/token", data=body, method="POST",
                      headers={"X-Clinic-ID": clinic, "Content-Type": "application/json"})
    with urlopen(request, timeout=3) as response:
        return json.loads(response.read())["access_token"]


if __name__ == "__main__":
    unittest.main()
