# Idea to Insight

**Current version:** `v0.0.1`  
**Skill slug:** `idea-to-insight`

A portable Agent Skill for exploring a personal idea through focused dialogue, testing its value for readers, drafting and compressing an essay, and learning from user-approved writing decisions.

It is not an automatic long-form expander. The process is: explore the idea, identify the reader value, build the argument, draft, compress, check clarity, and save only a user-confirmed final article.

## What it does

- Accepts multi-part idea dumps without prematurely summarizing or interrogating the user.
- Uses one high-impact question at a time to clarify the core judgment.
- Tests reader value before drafting; an insight can be valuable without an action checklist.
- Produces an argument outline before a thinking draft, then presents each full compression round for feedback.
- Saves the confirmed final article and an auditable learning event. Candidate writing preferences require user approval before becoming active rules.
- Keeps the writing workflow independent of `ai-write-flow`.

## Install in Hermes

The repository root is the skill directory. Clone it into the Hermes skills directory:

```bash
git clone https://github.com/guage1029/idea-to-insight.git ~/.hermes/skills/idea-to-insight
```

For an existing Git checkout, update it with:

```bash
git -C ~/.hermes/skills/idea-to-insight pull --ff-only
```

For another Agent Skills-compatible host, copy this repository directory to that host's skills location. Runtime support for Python scripts and local files depends on the host; if those capabilities are unavailable, the skill must not claim that it persisted data.

## Requirements and data

- Python 3 and its standard library for the local persistence script and tests; no third-party Python packages are required.
- The database is created outside the skill directory. Set `WRITING_SKILL_DATA_DIR` to choose a location. Defaults:
  - macOS: `~/Library/Application Support/idea-to-insight/memory.sqlite3`
  - Linux: `$XDG_DATA_HOME/idea-to-insight/memory.sqlite3`, or `~/.local/share/idea-to-insight/memory.sqlite3`
  - Windows: `%LOCALAPPDATA%/idea-to-insight/memory.sqlite3`
- The database stores checkpoints, confirmed final article text, evidence, candidates, and approved profile versions. It does not store full conversations or every draft.
- SQLite and JSON exports are not encrypted. POSIX permissions are restricted by the script; Windows ACLs are not configured by the script. Treat exports as private and never commit user data to this repository.
- Multiple Agents on one device may share a database sequentially. Multi-device concurrent writes are not supported.

See [`references/storage-protocol.md`](references/storage-protocol.md) for commands, migration, and recovery details.

## Version policy

`VERSION` is the canonical version source and contains the numeric value without a `v` prefix. The required `SKILL.md` frontmatter version and the README display are synchronized copies; the version checker rejects mismatches.

The initial public baseline is **v0.0.1**. Each subsequent update increments the patch component by exactly `0.0.1` (for example, `0.0.1` → `0.0.2`). To prepare the next update:

1. Make and test the code/content changes.
2. Run `python3 scripts/bump_version.py bump` once; it increments the patch version and synchronizes `VERSION`, `SKILL.md`, and this README.
3. Add the release notes under the matching version in [`CHANGELOG.md`](CHANGELOG.md).
4. Run the checks below, then commit and tag the release as `v<version>` (for example, `v0.0.2`).

Use `python3 scripts/bump_version.py check` to verify version consistency. Do not hand-edit only one copy of the version.

## Development checks

From the repository root:

```bash
python3 scripts/bump_version.py check
python3 evals/test_memory_store.py
python3 evals/test_versioning.py
```

`evals/behavior-cases.json` and `evals/trigger-queries.json` specify behavior/trigger cases; they are evaluation inputs, not proof that cross-Agent behavior has been tested.

## Current limits

- Candidate preferences are never promoted automatically; user approval is required.
- The skill does not publish articles or execute actions described in them.
- Cross-Agent runtime behavior and Windows ACL handling have not been verified.
- No license has been declared yet.

## Source

[github.com/guage1029/idea-to-insight](https://github.com/guage1029/idea-to-insight)
