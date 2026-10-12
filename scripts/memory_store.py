#!/usr/bin/env python3
"""Portable local memory store for the idea-to-insight Agent Skill.

The database stores checkpoints, confirmed final article text, evidence, and approved learning metadata; it does not store full chats or every draft.
Use --db for tests/imports or WRITING_SKILL_DATA_DIR to share one store across local agents.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import sqlite3
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1
ACTIVE_STAGES = {
    "collecting", "dialogue", "value_gate", "drafting", "compression",
    "clarity", "awaiting_decision",
}
TOPIC_STATUSES = ACTIVE_STAGES | {"suspended", "abandoned", "finalized"}
SCOPES = {"article", "genre", "global"}


class StoreError(Exception):
    """Expected store, validation, or state-transition error."""


class NotFoundError(StoreError):
    pass


class ConflictError(StoreError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _uid() -> str:
    return str(uuid.uuid4())


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _from_json(value: str | None, fallback: Any = None) -> Any:
    if value is None:
        return {} if fallback is None else fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise StoreError("Stored JSON is invalid; no state was changed.") from exc


def default_db_path() -> Path:
    configured = os.environ.get("WRITING_SKILL_DATA_DIR", "").strip()
    if configured:
        root = Path(configured).expanduser()
    elif platform.system() == "Darwin":
        root = Path.home() / "Library" / "Application Support" / "idea-to-insight"
    elif platform.system() == "Windows":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "idea-to-insight"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "idea-to-insight"
    return root / "memory.sqlite3"


def _path(path: str | Path | None) -> Path:
    return Path(path).expanduser() if path is not None else default_db_path()


def _secure_mode(path: Path, mode: int) -> None:
    if os.name != "nt":
        try:
            path.chmod(mode)
        except OSError as exc:
            raise StoreError(f"Could not set secure permissions on {path}: {exc}") from exc


def _connect(path: str | Path | None) -> sqlite3.Connection:
    db = _path(path)
    if not db.exists():
        init_db(db)
    conn = sqlite3.connect(db, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    schema = conn.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
    if schema is None or int(schema["value"]) != SCHEMA_VERSION:
        conn.close()
        raise StoreError("Database schema version is missing or unsupported; no operation was performed.")
    _secure_mode(db.parent, 0o700)
    _secure_mode(db, 0o600)
    return conn


def init_db(path: str | Path | None = None) -> Path:
    db = _path(path)
    db.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _secure_mode(db.parent, 0o700)
    conn = sqlite3.connect(db, timeout=15.0)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute("PRAGMA synchronous = FULL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS topics (
                topic_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                direction TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL CHECK(status IN (
                    'collecting','dialogue','value_gate','drafting','compression',
                    'clarity','awaiting_decision','suspended','abandoned','finalized'
                )),
                last_stage TEXT NOT NULL,
                resume_status TEXT,
                checkpoint_json TEXT NOT NULL DEFAULT '{}',
                pause_reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                abandoned_at TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                topic_id TEXT REFERENCES topics(topic_id),
                event_type TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                article_hash TEXT,
                payload_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_final_event_per_hash
                ON events(topic_id, article_hash)
                WHERE event_type = 'article_finalized' AND article_hash IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS one_no_rule_event_per_final
                ON events(topic_id, article_hash)
                WHERE event_type = 'no_new_rule' AND article_hash IS NOT NULL;
            CREATE TABLE IF NOT EXISTS final_articles (
                article_id TEXT PRIMARY KEY,
                topic_id TEXT NOT NULL REFERENCES topics(topic_id),
                event_id TEXT NOT NULL REFERENCES events(event_id),
                article_hash TEXT NOT NULL,
                article_text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(topic_id, article_hash)
            );
            CREATE TABLE IF NOT EXISTS evidence (
                evidence_id TEXT PRIMARY KEY,
                topic_id TEXT NOT NULL REFERENCES topics(topic_id),
                event_id TEXT REFERENCES events(event_id),
                source_type TEXT NOT NULL,
                excerpt TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS learning_candidates (
                candidate_id TEXT PRIMARY KEY,
                topic_id TEXT NOT NULL REFERENCES topics(topic_id),
                rule TEXT NOT NULL,
                scope TEXT NOT NULL CHECK(scope IN ('article','genre','global')),
                scope_key TEXT NOT NULL DEFAULT '',
                trigger_text TEXT NOT NULL,
                evidence_ids_json TEXT NOT NULL,
                exception_text TEXT NOT NULL DEFAULT '',
                final_article_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','active','rejected','duplicate')),
                created_at TEXT NOT NULL,
                reviewed_at TEXT,
                profile_version INTEGER,
                UNIQUE(topic_id,final_article_hash,rule,scope,scope_key,trigger_text),
                FOREIGN KEY(topic_id,final_article_hash) REFERENCES final_articles(topic_id,article_hash)
            );
            CREATE TABLE IF NOT EXISTS profile_versions (
                version INTEGER PRIMARY KEY,
                parent_version INTEGER,
                rules_json TEXT NOT NULL,
                source_candidate_id TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS eval_cases (
                case_id TEXT PRIMARY KEY,
                rule_id TEXT,
                prompt TEXT NOT NULL,
                expected_behavior TEXT NOT NULL,
                source_event_id TEXT,
                created_at TEXT NOT NULL
            );
        """)
        with conn:
            conn.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
            conn.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES('active_profile_version','0')")
            conn.execute(
                "INSERT OR IGNORE INTO profile_versions(version,parent_version,rules_json,source_candidate_id,created_at) "
                "VALUES(0,NULL,'[]',NULL,?)", (_now(),)
            )
        conn.execute("PRAGMA optimize")
    finally:
        conn.close()
    _secure_mode(db, 0o600)
    return db


def _record_event(conn: sqlite3.Connection, topic_id: str | None, event_type: str,
                  payload: dict[str, Any] | None = None, article_hash: str | None = None) -> str:
    event_id = _uid()
    conn.execute(
        "INSERT INTO events(event_id,topic_id,event_type,occurred_at,article_hash,payload_json) VALUES(?,?,?,?,?,?)",
        (event_id, topic_id, event_type, _now(), article_hash, _json(payload or {})),
    )
    return event_id


def _topic(conn: sqlite3.Connection, topic_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM topics WHERE topic_id=?", (topic_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"Topic not found: {topic_id}")
    return row


def _topic_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["checkpoint"] = _from_json(result.pop("checkpoint_json"))
    return result


def create_topic(path: str | Path | None, title: str, direction: str = "") -> dict[str, Any]:
    title = title.strip()
    if not title:
        raise StoreError("Topic title cannot be empty.")
    now, topic_id = _now(), _uid()
    with _connect(path) as conn:
        conn.execute(
            "INSERT INTO topics(topic_id,title,direction,status,last_stage,created_at,updated_at) VALUES(?,?,?,'collecting','collecting',?,?)",
            (topic_id, title, direction.strip(), now, now),
        )
        _record_event(conn, topic_id, "topic_created", {"title": title, "direction": direction.strip()})
    return {"topic_id": topic_id, "title": title, "direction": direction.strip(), "status": "collecting"}


def save_checkpoint(path: str | Path | None, topic_id: str, stage: str,
                    checkpoint: dict[str, Any]) -> dict[str, Any]:
    if stage not in ACTIVE_STAGES:
        raise StoreError(f"Invalid active stage: {stage}")
    if not isinstance(checkpoint, dict):
        raise StoreError("Checkpoint must be a JSON object.")
    with _connect(path) as conn:
        row = _topic(conn, topic_id)
        if row["status"] in {"suspended", "abandoned", "finalized"}:
            raise StoreError(f"Cannot update a topic in status '{row['status']}'. Resume it or create a new topic.")
        now = _now()
        conn.execute("UPDATE topics SET status=?,last_stage=?,checkpoint_json=?,updated_at=? WHERE topic_id=?",
                     (stage, stage, _json(checkpoint), now, topic_id))
        _record_event(conn, topic_id, "checkpoint_saved", {"stage": stage})
        return _topic_dict(_topic(conn, topic_id))


def suspend_topic(path: str | Path | None, topic_id: str, reason: str, stage: str,
                  checkpoint: dict[str, Any]) -> dict[str, Any]:
    if stage not in ACTIVE_STAGES:
        raise StoreError(f"Invalid suspension stage: {stage}")
    if not isinstance(checkpoint, dict):
        raise StoreError("Checkpoint must be a JSON object.")
    with _connect(path) as conn:
        row = _topic(conn, topic_id)
        if row["status"] in {"abandoned", "finalized", "suspended"}:
            raise StoreError(f"Cannot suspend a topic in status '{row['status']}'.")
        now = _now()
        conn.execute(
            "UPDATE topics SET status='suspended',last_stage=?,resume_status=?,checkpoint_json=?,pause_reason=?,updated_at=? WHERE topic_id=?",
            (stage, stage, _json(checkpoint), reason.strip(), now, topic_id),
        )
        _record_event(conn, topic_id, "topic_suspended", {"reason": reason.strip(), "stage": stage})
        return _topic_dict(_topic(conn, topic_id))


def abandon_topic(path: str | Path | None, topic_id: str, reason: str = "") -> dict[str, Any]:
    with _connect(path) as conn:
        row = _topic(conn, topic_id)
        if row["status"] == "finalized":
            raise StoreError("A finalized topic cannot be abandoned; create a follow-up topic if needed.")
        if row["status"] == "abandoned":
            return _topic_dict(_topic(conn, topic_id))
        now = _now()
        conn.execute("UPDATE topics SET status='abandoned',pause_reason=?,abandoned_at=?,updated_at=? WHERE topic_id=?",
                     (reason.strip(), now, now, topic_id))
        _record_event(conn, topic_id, "topic_abandoned", {"reason": reason.strip(), "archived": True})
        return _topic_dict(_topic(conn, topic_id))


def list_suspended(path: str | Path | None) -> list[dict[str, Any]]:
    with _connect(path) as conn:
        rows = conn.execute("SELECT * FROM topics WHERE status='suspended' ORDER BY updated_at DESC,topic_id").fetchall()
        return [_topic_dict(row) for row in rows]


def list_pending_candidates(path: str | Path | None) -> list[dict[str, Any]]:
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT c.*,t.title AS topic_title FROM learning_candidates c "
            "JOIN topics t ON t.topic_id=c.topic_id WHERE c.status='pending' "
            "ORDER BY c.created_at,c.candidate_id"
        ).fetchall()
        result = []
        for row in rows:
            candidate = dict(row)
            evidence_ids = _from_json(candidate.pop("evidence_ids_json"), [])
            candidate["evidence_ids"] = evidence_ids
            candidate["evidence"] = []
            for evidence_id in evidence_ids:
                evidence = conn.execute(
                    "SELECT excerpt,note FROM evidence WHERE evidence_id=? AND topic_id=?",
                    (evidence_id, candidate["topic_id"]),
                ).fetchone()
                if evidence:
                    candidate["evidence"].append({"excerpt": evidence["excerpt"], "note": evidence["note"]})
            result.append(candidate)
        return result


def resume_topic(path: str | Path | None, topic_id: str) -> dict[str, Any]:
    with _connect(path) as conn:
        row = _topic(conn, topic_id)
        if row["status"] != "suspended":
            raise StoreError(f"Only suspended topics can be resumed; current status is '{row['status']}'.")
        stage = row["resume_status"] or row["last_stage"]
        now = _now()
        conn.execute("UPDATE topics SET status=?,resume_status=NULL,pause_reason='',updated_at=? WHERE topic_id=?",
                     (stage, now, topic_id))
        _record_event(conn, topic_id, "topic_resumed", {"stage": stage})
        return _topic_dict(_topic(conn, topic_id))


def finalize_topic(path: str | Path | None, topic_id: str, article_hash: str,
                   article_text: str) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{64}", article_hash):
        raise StoreError("Article hash must be a lowercase SHA-256 hex digest (64 characters).")
    if not isinstance(article_text, str) or not article_text.strip():
        raise StoreError("The confirmed final article text is required; drafts are not finalized.")
    calculated_hash = hashlib.sha256(article_text.encode("utf-8")).hexdigest()
    if calculated_hash != article_hash:
        raise StoreError("Article text does not match its SHA-256 hash; no state was changed.")
    with _connect(path) as conn:
        row = _topic(conn, topic_id)
        if row["status"] in {"suspended", "abandoned"}:
            raise StoreError(f"Cannot finalize a topic in status '{row['status']}'. Resume it first or start a new topic.")
        existing = conn.execute(
            "SELECT event_id,occurred_at FROM events WHERE topic_id=? AND event_type='article_finalized' AND article_hash=?",
            (topic_id, article_hash),
        ).fetchone()
        if existing:
            stored = conn.execute("SELECT 1 FROM final_articles WHERE topic_id=? AND article_hash=?",
                                  (topic_id, article_hash)).fetchone()
            if not stored:
                raise StoreError("Finalization event exists without its article artifact; integrity check required.")
            return {"topic_id": topic_id, "event_id": existing["event_id"], "article_hash": article_hash,
                    "duplicate": True, "stored_article": True}
        event_id = _record_event(conn, topic_id, "article_finalized", {"article_hash": article_hash}, article_hash)
        now = _now()
        conn.execute("INSERT INTO final_articles(article_id,topic_id,event_id,article_hash,article_text,created_at) VALUES(?,?,?,?,?,?)",
                     (_uid(), topic_id, event_id, article_hash, article_text, now))
        conn.execute("UPDATE topics SET status='finalized',last_stage='finalized',updated_at=? WHERE topic_id=?", (now, topic_id))
        return {"topic_id": topic_id, "event_id": event_id, "article_hash": article_hash,
                "duplicate": False, "stored_article": True}


def get_final_article(path: str | Path | None, topic_id: str,
                      article_hash: str | None = None) -> dict[str, Any]:
    with _connect(path) as conn:
        if article_hash:
            row = conn.execute("SELECT article_hash,article_text,created_at FROM final_articles WHERE topic_id=? AND article_hash=?",
                               (topic_id, article_hash)).fetchone()
        else:
            row = conn.execute("SELECT article_hash,article_text,created_at FROM final_articles WHERE topic_id=? ORDER BY created_at DESC LIMIT 1",
                               (topic_id,)).fetchone()
        if row is None:
            raise NotFoundError("No saved final article exists for this topic.")
        return {"topic_id": topic_id, "article_hash": row["article_hash"],
                "article_text": row["article_text"], "created_at": row["created_at"]}


def record_no_new_rule(path: str | Path | None, topic_id: str,
                       article_hash: str, reason: str) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{64}", article_hash):
        raise StoreError("Article hash must be a lowercase SHA-256 hex digest (64 characters).")
    reason = reason.strip()
    if not reason:
        raise StoreError("Give a brief reason why this article produced no reliable new rule.")
    with _connect(path) as conn:
        article = conn.execute("SELECT 1 FROM final_articles WHERE topic_id=? AND article_hash=?",
                               (topic_id, article_hash)).fetchone()
        if not article:
            raise NotFoundError("No confirmed final article matches this topic and hash.")
        candidates = conn.execute(
            "SELECT status FROM learning_candidates WHERE topic_id=? AND final_article_hash=?",
            (topic_id, article_hash),
        ).fetchall()
        if any(row["status"] in {"pending", "active", "duplicate"} for row in candidates):
            raise StoreError("This article has a pending or active/duplicate candidate; resolve pending review first. An active rule means this article cannot be recorded as no_new_rule.")
        existing = conn.execute(
            "SELECT event_id FROM events WHERE topic_id=? AND event_type='no_new_rule' AND article_hash=?",
            (topic_id, article_hash),
        ).fetchone()
        if existing:
            return {"event_id": existing["event_id"], "topic_id": topic_id,
                    "article_hash": article_hash, "duplicate": True}
        event_id = _record_event(conn, topic_id, "no_new_rule",
                                 {"reason": reason}, article_hash=article_hash)
        return {"event_id": event_id, "topic_id": topic_id,
                "article_hash": article_hash, "duplicate": False}


def add_evidence(path: str | Path | None, topic_id: str, source_type: str,
                 excerpt: str, note: str = "", event_id: str | None = None) -> str:
    excerpt = excerpt.strip()
    if not excerpt:
        raise StoreError("Evidence excerpt cannot be empty.")
    if len(excerpt) > 1200:
        raise StoreError("Evidence excerpt is too long; store only the minimal necessary snippet (max 1200 characters).")
    evidence_id = _uid()
    with _connect(path) as conn:
        _topic(conn, topic_id)
        if event_id:
            exists = conn.execute("SELECT 1 FROM events WHERE event_id=? AND topic_id=?", (event_id, topic_id)).fetchone()
            if not exists:
                raise NotFoundError("Evidence event does not belong to this topic.")
        conn.execute("INSERT INTO evidence(evidence_id,topic_id,event_id,source_type,excerpt,note,created_at) VALUES(?,?,?,?,?,?,?)",
                     (evidence_id, topic_id, event_id, source_type.strip(), excerpt, note.strip(), _now()))
        _record_event(conn, topic_id, "evidence_recorded", {"evidence_id": evidence_id, "source_type": source_type.strip()})
    return evidence_id


def propose_candidate(path: str | Path | None, topic_id: str, final_article_hash: str,
                      rule: str, scope: str,
                      trigger: str, evidence: Iterable[str], exception: str = "",
                      scope_key: str = "") -> dict[str, Any]:
    rule, trigger = rule.strip(), trigger.strip()
    if isinstance(evidence, (str, bytes)):
        raise StoreError("Evidence must be a list of concise snippets, not one string.")
    snippets = [str(item).strip() for item in evidence if str(item).strip()]
    if not rule or not trigger:
        raise StoreError("Candidate rule and trigger are required.")
    if not snippets:
        raise StoreError("A learning candidate needs at least one concise evidence snippet.")
    if scope not in SCOPES:
        raise StoreError("Scope must be article, genre, or global.")
    if scope == "genre" and not scope_key.strip():
        raise StoreError("Genre-scoped candidates require a scope key, such as '技术评论'.")
    if scope == "article" and not scope_key.strip():
        scope_key = topic_id
    if scope == "global":
        scope_key = ""
    if any(len(item) > 1200 for item in snippets):
        raise StoreError("Each evidence snippet must be 1200 characters or fewer.")
    if len(snippets) > 10:
        raise StoreError("Keep at most 10 concise evidence snippets per candidate.")
    with _connect(path) as conn:
        _topic(conn, topic_id)
        finalized = conn.execute("SELECT 1 FROM final_articles WHERE topic_id=? AND article_hash=?",
                                 (topic_id, final_article_hash)).fetchone()
        if not finalized:
            raise StoreError("Candidate must reference a confirmed final article hash for this topic.")
        existing = conn.execute(
            "SELECT * FROM learning_candidates WHERE topic_id=? AND final_article_hash=? AND rule=? AND scope=? AND scope_key=? AND trigger_text=?",
            (topic_id, final_article_hash, rule, scope, scope_key.strip(), trigger),
        ).fetchone()
        if existing:
            return {"candidate_id": existing["candidate_id"], "topic_id": topic_id,
                    "final_article_hash": final_article_hash, "rule": rule, "scope": scope,
                    "scope_key": scope_key.strip(), "trigger": trigger,
                    "evidence_ids": _from_json(existing["evidence_ids_json"], []),
                    "exception": existing["exception_text"], "status": existing["status"], "duplicate": True}
        evidence_ids = []
        for snippet in snippets:
            evidence_id = _uid()
            conn.execute("INSERT INTO evidence(evidence_id,topic_id,source_type,excerpt,created_at) VALUES(?,?,?,?,?)",
                         (evidence_id, topic_id, "learning_candidate", snippet, _now()))
            evidence_ids.append(evidence_id)
        candidate_id = _uid()
        now = _now()
        conn.execute(
            "INSERT INTO learning_candidates(candidate_id,topic_id,rule,scope,scope_key,trigger_text,evidence_ids_json,exception_text,final_article_hash,status,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?, 'pending',?)",
            (candidate_id, topic_id, rule, scope, scope_key.strip(), trigger, _json(evidence_ids), exception.strip(), final_article_hash, now),
        )
        _record_event(conn, topic_id, "learning_candidate_proposed",
                      {"candidate_id": candidate_id, "scope": scope, "article_hash": final_article_hash})
    return {"candidate_id": candidate_id, "topic_id": topic_id, "final_article_hash": final_article_hash,
            "rule": rule, "scope": scope,
            "scope_key": scope_key.strip(), "trigger": trigger, "evidence_ids": evidence_ids,
            "exception": exception.strip(), "status": "pending", "duplicate": False}


def _active_profile(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT value FROM metadata WHERE key='active_profile_version'").fetchone()
    if not row:
        raise StoreError("Active profile version is missing.")
    version = int(row["value"])
    profile = conn.execute("SELECT rules_json FROM profile_versions WHERE version=?", (version,)).fetchone()
    if not profile:
        raise StoreError("Active profile version does not exist.")
    return {"version": version, "rules": _from_json(profile["rules_json"], [])}


def get_active_profile(path: str | Path | None) -> dict[str, Any]:
    with _connect(path) as conn:
        return _active_profile(conn)


def review_candidate(path: str | Path | None, candidate_id: str, decision: str) -> dict[str, Any]:
    if decision not in {"approve", "reject"}:
        raise StoreError("Decision must be approve or reject.")
    with _connect(path) as conn:
        candidate = conn.execute("SELECT * FROM learning_candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
        if candidate is None:
            raise NotFoundError(f"Learning candidate not found: {candidate_id}")
        if candidate["status"] != "pending":
            raise StoreError(f"Candidate is already '{candidate['status']}'.")
        now = _now()
        if decision == "reject":
            conn.execute("UPDATE learning_candidates SET status='rejected',reviewed_at=? WHERE candidate_id=?", (now, candidate_id))
            _record_event(conn, candidate["topic_id"], "learning_candidate_rejected", {"candidate_id": candidate_id})
            return {"candidate_id": candidate_id, "status": "rejected"}

        profile = _active_profile(conn)
        scope, scope_key, trigger = candidate["scope"], candidate["scope_key"], candidate["trigger_text"]
        matching = [r for r in profile["rules"] if r["scope"] == scope and r["scope_key"] == scope_key and r["trigger"] == trigger]
        if matching:
            if matching[0]["rule"] == candidate["rule"]:
                conn.execute("UPDATE learning_candidates SET status='duplicate',reviewed_at=? WHERE candidate_id=?", (now, candidate_id))
                _record_event(conn, candidate["topic_id"], "learning_candidate_duplicate", {"candidate_id": candidate_id})
                return {"candidate_id": candidate_id, "status": "duplicate", "profile_version": profile["version"]}
            raise ConflictError("A different active rule has the same scope and trigger; candidate remains pending for user resolution.")

        rule = {
            "rule_id": _uid(), "rule": candidate["rule"], "scope": scope, "scope_key": scope_key,
            "trigger": trigger, "evidence_ids": _from_json(candidate["evidence_ids_json"], []),
            "exception": candidate["exception_text"], "source_candidate_id": candidate_id,
            "approved_at": now,
        }
        new_version = profile["version"] + 1
        rules = [*profile["rules"], rule]
        conn.execute("INSERT INTO profile_versions(version,parent_version,rules_json,source_candidate_id,created_at) VALUES(?,?,?,?,?)",
                     (new_version, profile["version"], _json(rules), candidate_id, now))
        conn.execute("UPDATE metadata SET value=? WHERE key='active_profile_version'", (str(new_version),))
        conn.execute("UPDATE learning_candidates SET status='active',reviewed_at=?,profile_version=? WHERE candidate_id=?",
                     (now, new_version, candidate_id))
        _record_event(conn, candidate["topic_id"], "learning_candidate_activated",
                      {"candidate_id": candidate_id, "profile_version": new_version})
        return {"candidate_id": candidate_id, "status": "active", "profile_version": new_version, "rule_id": rule["rule_id"]}


def rollback_profile(path: str | Path | None, version: int) -> dict[str, Any]:
    with _connect(path) as conn:
        exists = conn.execute("SELECT 1 FROM profile_versions WHERE version=?", (version,)).fetchone()
        if not exists:
            raise NotFoundError(f"Profile version not found: {version}")
        current = _active_profile(conn)["version"]
        conn.execute("UPDATE metadata SET value=? WHERE key='active_profile_version'", (str(version),))
        _record_event(conn, None, "profile_rollback", {"from_version": current, "to_version": version})
        selected = conn.execute("SELECT rules_json FROM profile_versions WHERE version=?", (version,)).fetchone()
        return {"version": version, "rules": _from_json(selected["rules_json"], [])}


def _table_rows(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()]


def export_bundle(path: str | Path | None, destination: str | Path) -> dict[str, Any]:
    tables = ("metadata", "topics", "events", "final_articles", "evidence", "learning_candidates", "profile_versions", "eval_cases")
    with _connect(path) as conn:
        bundle = {"format": "idea-to-insight-memory", "schema_version": SCHEMA_VERSION,
                  "exported_at": _now(), "tables": {table: _table_rows(conn, table) for table in tables}}
    dest = Path(destination).expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _secure_mode(dest.parent, 0o700)
    payload = (_json(bundle) + "\n").encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=f".{dest.name}.", dir=str(dest.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, dest)
        _secure_mode(dest, 0o600)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return {"path": str(dest), "schema_version": SCHEMA_VERSION,
            "counts": {table: len(rows) for table, rows in bundle["tables"].items()}}


def _validate_bundle(bundle: Any) -> dict[str, Any]:
    if not isinstance(bundle, dict) or bundle.get("format") != "idea-to-insight-memory":
        raise StoreError("Not an idea-to-insight memory bundle.")
    if bundle.get("schema_version") != SCHEMA_VERSION:
        raise StoreError(f"Unsupported bundle schema version: {bundle.get('schema_version')}")
    tables = bundle.get("tables")
    required = {"metadata", "topics", "events", "final_articles", "evidence", "learning_candidates", "profile_versions", "eval_cases"}
    if not isinstance(tables, dict) or set(tables) != required:
        raise StoreError("Bundle tables are incomplete or unknown.")
    if any(not isinstance(tables[name], list) for name in required):
        raise StoreError("Each bundle table must be a list.")
    return tables


def _import_into_database(dest: Path, tables: dict[str, Any], apply: bool) -> dict[str, Any]:
    primary_keys = {
        "metadata": "key", "topics": "topic_id", "events": "event_id", "final_articles": "article_id", "evidence": "evidence_id",
        "learning_candidates": "candidate_id", "profile_versions": "version", "eval_cases": "case_id",
    }
    counts: dict[str, int] = {}
    import_order = ("metadata", "topics", "events", "final_articles", "evidence", "learning_candidates", "profile_versions", "eval_cases")
    with _connect(dest) as conn:
        schema_columns = {
            table: {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for table in import_order
        }
        for table in import_order:
            rows = tables[table]
            pk = primary_keys[table]
            inserted = 0
            for row in rows:
                if not isinstance(row, dict) or set(row) != schema_columns[table] or pk not in row:
                    raise StoreError(f"Invalid row in {table}: columns do not match this schema.")
                existing = conn.execute(f"SELECT * FROM {table} WHERE {pk}=?", (row[pk],)).fetchone()
                if existing is not None:
                    if table == "metadata" and row.get("key") == "active_profile_version":
                        counts.setdefault(table, 0)
                        if dict(existing).get("value") != row.get("value"):
                            counts[table] += 1
                        continue
                    existing_dict = dict(existing)
                    same = existing_dict == row
                    # Every fresh database has its own empty root profile; its timestamp is local metadata.
                    if table == "profile_versions" and row.get("version") == 0:
                        ignored = {"created_at"}
                        same = {k: v for k, v in existing_dict.items() if k not in ignored} == {k: v for k, v in row.items() if k not in ignored}
                    if not same:
                        raise ConflictError(f"Import conflicts with existing {table} record {row[pk]}.")
                    continue
                if apply:
                    columns = sorted(schema_columns[table])
                    placeholders = ",".join("?" for _ in columns)
                    names = ",".join(f'"{column}"' for column in columns)
                    conn.execute(f"INSERT INTO {table}({names}) VALUES({placeholders})", tuple(row[c] for c in columns))
                inserted += 1
            counts[table] = inserted
        if apply:
            source_active = next((r["value"] for r in tables["metadata"] if r["key"] == "active_profile_version"), "0")
            active_version = conn.execute("SELECT 1 FROM profile_versions WHERE version=?", (int(source_active),)).fetchone()
            if not active_version:
                raise StoreError("Imported active profile version is missing; transaction was rolled back.")
            conn.execute("UPDATE metadata SET value=? WHERE key='active_profile_version'", (str(source_active),))
    return {"counts": counts,
            "topics_imported": counts.get("topics", 0), "schema_version": SCHEMA_VERSION}


def import_bundle(path: str | Path | None, source: str | Path, dry_run: bool = True) -> dict[str, Any]:
    try:
        bundle = json.loads(Path(source).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StoreError(f"Could not read a valid export bundle: {exc}") from exc
    tables = _validate_bundle(bundle)
    target = _path(path)
    if dry_run:
        # Validate foreign keys and the complete import in an isolated temporary database.
        with tempfile.TemporaryDirectory(prefix="idea-to-insight-import-", dir=os.environ.get("TMPDIR") or None) as temp_dir:
            simulated = init_db(Path(temp_dir) / "memory.sqlite3")
            preview = _import_into_database(simulated, tables, apply=True)
            if target.exists():
                preview = _import_into_database(target, tables, apply=False)
        preview["dry_run"] = True
        return preview
    dest = init_db(target)
    result = _import_into_database(dest, tables, apply=True)
    result["dry_run"] = False
    return result


def integrity_check(path: str | Path | None) -> dict[str, Any]:
    with _connect(path) as conn:
        sqlite_result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        profile = _active_profile(conn)
        schema_row = conn.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        if sqlite_result != "ok" or foreign_errors:
            raise StoreError(f"Database integrity failure: {sqlite_result}; foreign key errors={len(foreign_errors)}")
        if int(schema_row["value"]) != SCHEMA_VERSION:
            raise StoreError("Database schema version mismatch; migration required.")
        return {"ok": True, "schema_version": SCHEMA_VERSION,
                "active_profile_version": profile["version"], "foreign_key_errors": 0}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage persistent idea-to-insight state.")
    parser.add_argument("--db", help="SQLite database path; defaults to WRITING_SKILL_DATA_DIR.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("list-suspended")
    sub.add_parser("list-pending-candidates")
    p = sub.add_parser("create-topic"); p.add_argument("--title", required=True); p.add_argument("--direction", default="")
    p = sub.add_parser("checkpoint"); p.add_argument("topic_id"); p.add_argument("--stage", required=True); p.add_argument("--json", dest="payload", default="{}")
    p = sub.add_parser("suspend"); p.add_argument("topic_id"); p.add_argument("--reason", required=True); p.add_argument("--stage", required=True); p.add_argument("--json", dest="payload", default="{}")
    p = sub.add_parser("resume"); p.add_argument("topic_id")
    p = sub.add_parser("abandon"); p.add_argument("topic_id"); p.add_argument("--reason", default="")
    p = sub.add_parser("finalize"); p.add_argument("topic_id"); p.add_argument("--article-file", required=True)
    p = sub.add_parser("propose-candidate"); p.add_argument("topic_id"); p.add_argument("--article-sha256", required=True); p.add_argument("--rule", required=True); p.add_argument("--scope", choices=sorted(SCOPES), required=True); p.add_argument("--scope-key", default=""); p.add_argument("--trigger", required=True); p.add_argument("--evidence-json", default="[]"); p.add_argument("--exception", default="")
    p = sub.add_parser("no-new-rule"); p.add_argument("topic_id"); p.add_argument("--article-sha256", required=True); p.add_argument("--reason", required=True)
    p = sub.add_parser("review-candidate"); p.add_argument("candidate_id"); p.add_argument("--decision", choices=("approve", "reject"), required=True)
    sub.add_parser("profile")
    p = sub.add_parser("rollback"); p.add_argument("version", type=int)
    p = sub.add_parser("export"); p.add_argument("destination")
    p = sub.add_parser("import"); p.add_argument("source"); p.add_argument("--apply", action="store_true", help="Apply after review; default is dry-run.")
    sub.add_parser("integrity-check")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    db = args.db
    try:
        if args.command == "init": result = {"database": str(init_db(db))}
        elif args.command == "list-suspended": result = list_suspended(db)
        elif args.command == "list-pending-candidates": result = list_pending_candidates(db)
        elif args.command == "create-topic": result = create_topic(db, args.title, args.direction)
        elif args.command == "checkpoint": result = save_checkpoint(db, args.topic_id, args.stage, json.loads(args.payload))
        elif args.command == "suspend": result = suspend_topic(db, args.topic_id, args.reason, args.stage, json.loads(args.payload))
        elif args.command == "resume": result = resume_topic(db, args.topic_id)
        elif args.command == "abandon": result = abandon_topic(db, args.topic_id, args.reason)
        elif args.command == "finalize":
            article_text = Path(args.article_file).expanduser().read_text(encoding="utf-8")
            article_hash = hashlib.sha256(article_text.encode("utf-8")).hexdigest()
            result = finalize_topic(db, args.topic_id, article_hash, article_text)
        elif args.command == "propose-candidate": result = propose_candidate(db, args.topic_id, args.article_sha256, args.rule, args.scope, args.trigger, json.loads(args.evidence_json), args.exception, args.scope_key)
        elif args.command == "no-new-rule": result = record_no_new_rule(db, args.topic_id, args.article_sha256, args.reason)
        elif args.command == "review-candidate": result = review_candidate(db, args.candidate_id, args.decision)
        elif args.command == "profile": result = get_active_profile(db)
        elif args.command == "rollback": result = rollback_profile(db, args.version)
        elif args.command == "export": result = export_bundle(db, args.destination)
        elif args.command == "import": result = import_bundle(db, args.source, dry_run=not args.apply)
        elif args.command == "integrity-check": result = integrity_check(db)
        else: raise StoreError(f"Unknown command: {args.command}")
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (StoreError, sqlite3.Error, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
