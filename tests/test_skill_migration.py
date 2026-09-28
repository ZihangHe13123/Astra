import asyncio
import json
import sqlite3

from agent.runtime.learning import LearningStore
from agent.runtime.skill_curation import SkillCurator
from agent.runtime.skill_learning import LearnedSkills
from agent.runtime.skill_migration import migrate_legacy
from agent.runtime.skills import SkillStore
from test_skill_origins import skill
from tests.learning_fixtures import stage_legacy_proposal


def test_migration_preserves_user_files_history_and_supports_repeat_and_undo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = LearningStore(tmp_path / "learning.db")
    learned = LearnedSkills(SkillStore(tmp_path / "skills"))
    learned.skills.create("manual", skill("manual", "Original instruction"), "operations")
    learned.skills.write_file("manual", "references/source.md", "Original supporting file")
    original = learned.skills.view("manual")
    patch = stage_legacy_proposal(store, "prior", "skill_patch", {"name": "manual", "old_string": "Original instruction\n",
                         "new_string": "Improved instruction\n", "file_path": "SKILL.md"}, "Prior summary")
    # Patch must match exactly once, including the body rather than description.
    with store._connection() as db:
        payload = patch["payload"]
        payload["old_string"] = "\n\nOriginal instruction\n"
        payload["new_string"] = "\n\nImproved instruction\n"
        db.execute("UPDATE learning_proposals SET payload_json=? WHERE id=?", (json.dumps(payload), patch["id"]))
    created = stage_legacy_proposal(store, "prior", "skill_create", {"name": "new-skill", "content": skill("new-skill")}, "Prior summary")
    observation = stage_legacy_proposal(store, "prior", "observation", {"content": "One old machine observation"}, "History")
    rows = store.list()
    result = migrate_legacy(store, learned)
    assert (result["imported"], result["history_only"], result["remaining"]) == (2, 1, 0)
    assert not result["blocked"]
    assert store.list() == rows and learned.skills.view("manual") == original
    ledger = learned._state()["legacy_migrations"]
    alias = ledger[patch["id"]]["name"]
    assert alias != "manual" and "Improved instruction" in learned.skills.view(alias)
    assert learned.skills.view(alias, "references/source.md") == "Original supporting file"
    assert ledger[observation["id"]]["destination"] == "history"
    assert ledger[created["id"]]["destination"] == "auto"
    with sqlite3.connect(result["backup"]) as db:
        assert db.execute("SELECT count(*) FROM learning_proposals WHERE status='pending'").fetchone()[0] == 3
    assert migrate_legacy(store, learned)["run_id"] is None
    learned.undo(result["run_id"])
    assert learned._state()["skills"] == {}
    assert learned._state()["legacy_migrations"] == {}
    assert learned.skills.view("manual") == original and store.list() == rows
    assert migrate_legacy(store, learned)["imported"] == 2


def test_nonmatching_patch_stays_unmigrated_and_original_is_safe(tmp_path):
    store = LearningStore(tmp_path / "learning.db")
    learned = LearnedSkills(SkillStore(tmp_path / "skills"))
    learned.skills.create("manual", skill("manual"))
    row = stage_legacy_proposal(store, "prior", "skill_patch", {"name": "manual", "old_string": "No longer exists", "new_string": "Dangerous overwrite"}, "Prior summary")
    before = learned.skills.view("manual")
    result = migrate_legacy(store, learned)
    assert result["imported"] == 0 and result["remaining"] == 1
    assert result["blocked"][0]["candidate"] == row["id"]
    assert row["id"] not in learned._state()["legacy_migrations"]
    assert learned.skills.view("manual") == before


def test_archived_migrated_skill_is_not_reimported(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = LearningStore(tmp_path / "learning.db")
    learned = LearnedSkills(SkillStore(tmp_path / "skills"))
    stage_legacy_proposal(store, "prior", "skill_create", {"name": "old", "content": skill("old")}, "Prior summary")
    migrate_legacy(store, learned)

    class Reviewer:
        async def chat_limited(self, messages, **kwargs):
            return {"content": json.dumps({"actions": [{"action": "archive", "names": ["old"], "reason": "No longer useful"}]})}

    asyncio.run(SkillCurator(Reviewer(), learned).review())
    assert migrate_legacy(store, learned)["run_id"] is None
    assert learned._state()["skills"] == {}
