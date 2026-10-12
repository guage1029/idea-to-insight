"""Deterministic storage and learning-state tests for idea-to-insight."""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from memory_store import (  # noqa: E402
    ConflictError,
    abandon_topic,
    create_topic,
    export_bundle,
    finalize_topic,
    get_final_article,
    get_active_profile,
    import_bundle,
    init_db,
    list_suspended,
    list_pending_candidates,
    propose_candidate,
    resume_topic,
    review_candidate,
    rollback_profile,
    suspend_topic,
    integrity_check,
    record_no_new_rule,
)


class MemoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "memory.sqlite3"
        init_db(self.db)
        self.topic = create_topic(self.db, "写作工作流", "探索个人写作 skill")["topic_id"]

    def finalize_current(self) -> str:
        text = "最终定稿内容"
        import hashlib
        article_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        finalize_topic(self.db, self.topic, article_hash, text)
        return article_hash

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_suspended_is_listed_and_resumes_to_previous_stage(self) -> None:
        suspend_topic(self.db, self.topic, "价值点还没想清楚", stage="dialogue",
                      checkpoint={"open_question": "读者获得什么新判断？"})
        rows = list_suspended(self.db)
        self.assertEqual([row["topic_id"] for row in rows], [self.topic])
        resumed = resume_topic(self.db, self.topic)
        self.assertEqual(resumed["status"], "dialogue")
        self.assertEqual(resumed["checkpoint"]["open_question"], "读者获得什么新判断？")
        self.assertEqual(list_suspended(self.db), [])

    def test_abandoned_topic_is_not_offered_for_reopening(self) -> None:
        abandoned = abandon_topic(self.db, self.topic, "当前没有足够读者价值")
        self.assertEqual(abandoned["status"], "abandoned")
        self.assertEqual(list_suspended(self.db), [])

    def test_database_permissions_and_integrity(self) -> None:
        self.assertTrue(integrity_check(self.db)["ok"])
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.db.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.db.parent.stat().st_mode), 0o700)

    def test_finalization_is_idempotent_and_saves_confirmed_article(self) -> None:
        import hashlib
        text = "一篇用户确认的最终文章。"
        article_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        first = finalize_topic(self.db, self.topic, article_hash, text)
        second = finalize_topic(self.db, self.topic, article_hash, text)
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["article_hash"], article_hash)
        self.assertTrue(first["stored_article"])
        self.assertEqual(get_final_article(self.db, self.topic)["article_text"], text)

    def test_no_new_rule_is_explicit_and_idempotent(self) -> None:
        article_hash = self.finalize_current()
        first = record_no_new_rule(self.db, self.topic, article_hash, "本篇没有足够证据归纳长期偏好")
        second = record_no_new_rule(self.db, self.topic, article_hash, "重复记录不产生第二条事件")
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])

    def test_pending_candidate_does_not_change_profile_until_approved(self) -> None:
        article_hash = self.finalize_current()
        candidate = propose_candidate(
            self.db, self.topic, article_hash,
            rule="保留作者明确提出的反例，不用套话替代",
            scope="global",
            trigger="压缩涉及反例或边界时",
            evidence=["作者指出：真实张力不能被删掉"],
            exception="反例与核心判断无关时可移入旁支",
        )
        self.assertEqual(candidate["status"], "pending")
        before = get_active_profile(self.db)
        self.assertEqual(before["version"], 0)
        self.assertEqual(before["rules"], [])
        applied = review_candidate(self.db, candidate["candidate_id"], "approve")
        self.assertEqual(applied["status"], "active")
        after = get_active_profile(self.db)
        self.assertEqual(after["version"], 1)
        self.assertEqual(after["rules"][0]["scope"], "global")

    def test_pending_candidate_and_evidence_can_be_reviewed_later(self) -> None:
        article_hash = self.finalize_current()
        candidate = propose_candidate(self.db, self.topic, article_hash, "保留具体反例", "global", "写作时", ["用户明确要求保留"])
        pending = list_pending_candidates(self.db)
        self.assertEqual(pending[0]["candidate_id"], candidate["candidate_id"])
        self.assertEqual(pending[0]["evidence"][0]["excerpt"], "用户明确要求保留")
        review_candidate(self.db, candidate["candidate_id"], "reject")
        self.assertEqual(list_pending_candidates(self.db), [])

    def test_rejected_candidate_never_enters_profile(self) -> None:
        article_hash = self.finalize_current()
        candidate = propose_candidate(
            self.db, self.topic, article_hash, "用短句", "article", "这篇文章的开头", ["本篇修改反馈"]
        )
        rejected = review_candidate(self.db, candidate["candidate_id"], "reject")
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(get_active_profile(self.db)["rules"], [])

    def test_conflicting_rule_is_blocked_without_overwriting_active_profile(self) -> None:
        article_hash = self.finalize_current()
        first = propose_candidate(self.db, self.topic, article_hash, "保留限定条件", "global", "处理结论", ["证据 1"])
        review_candidate(self.db, first["candidate_id"], "approve")
        second = propose_candidate(self.db, self.topic, article_hash, "删去所有限定条件", "global", "处理结论", ["证据 2"])
        with self.assertRaises(ConflictError):
            review_candidate(self.db, second["candidate_id"], "approve")
        self.assertEqual(get_active_profile(self.db)["version"], 1)
        self.assertEqual(get_active_profile(self.db)["rules"][0]["rule"], "保留限定条件")

    def test_rollback_selects_a_prior_immutable_profile_version(self) -> None:
        article_hash = self.finalize_current()
        candidate = propose_candidate(self.db, self.topic, article_hash, "保留作者判断", "global", "压缩时", ["证据"])
        review_candidate(self.db, candidate["candidate_id"], "approve")
        restored = rollback_profile(self.db, 0)
        self.assertEqual(restored["version"], 0)
        self.assertEqual(restored["rules"], [])

    def test_export_import_round_trip_and_dry_run(self) -> None:
        article_hash = self.finalize_current()
        candidate = propose_candidate(self.db, self.topic, article_hash, "保留真实反例", "global", "压缩论证时", ["用户明确要求保留反例"])
        review_candidate(self.db, candidate["candidate_id"], "approve")
        suspended_topic = create_topic(self.db, "待补素材话题")["topic_id"]
        suspend_topic(self.db, suspended_topic, "等待补素材", stage="dialogue", checkpoint={"x": 1})
        out = self.root / "bundle.json"
        export_bundle(self.db, out)
        parsed = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(parsed["schema_version"], 1)
        target = self.root / "restored.sqlite3"
        init_db(target)
        preview = import_bundle(target, out, dry_run=True)
        self.assertTrue(preview["dry_run"])
        self.assertEqual(list_suspended(target), [])
        imported = import_bundle(target, out, dry_run=False)
        self.assertGreater(imported["topics_imported"], 0)
        self.assertEqual(list_suspended(target)[0]["topic_id"], suspended_topic)
        self.assertEqual(get_final_article(target, self.topic)["article_text"], "最终定稿内容")
        self.assertEqual(get_active_profile(target)["version"], 1)
        self.assertEqual(get_active_profile(target)["rules"][0]["rule"], "保留真实反例")

    def test_dry_run_does_not_create_missing_destination_database(self) -> None:
        out = self.root / "empty-bundle.json"
        export_bundle(self.db, out)
        target = self.root / "not-created.sqlite3"
        result = import_bundle(target, out, dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
