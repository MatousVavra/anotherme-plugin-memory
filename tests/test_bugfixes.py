"""Unit tests for verified bug fixes (person merge, extract_facts guard, contradiction resolve)."""
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from conftest import load_plugin_module


class _FakeVault:
    def __init__(self, root):
        self.root = Path(root)

    def vault_path(self, vault_name):
        return self.root / vault_name

    def create_note(self, vault_name, title, content, tags, note_type, meta=None, subfolder=None):
        d = self.root / vault_name / (subfolder or "")
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{title}.md"
        p.write_text(content, encoding="utf-8")
        return p


class _FakeCompletions:
    def __init__(self, content):
        self._content = content

    def create(self, **kwargs):
        message = SimpleNamespace(content=self._content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _FakeLLM:
    LLM_MODEL = "fake-model"

    def __init__(self, content):
        self._client = SimpleNamespace(
            chat=SimpleNamespace(completions=_FakeCompletions(content))
        )

    def get_client(self):
        return self._client


def test_create_person_merges_existing_note(tmp_path):
    mod = load_plugin_module()
    vault = _FakeVault(tmp_path)
    api = mod.MemoryApi(db_module=None, vault_manager=vault, llm_client=None)
    api.create_person("main", "Alice", "friend", "likes tea")
    note = tmp_path / "main" / "People" / "Alice.md"
    text = note.read_text(encoding="utf-8")
    assert "# Alice" in text and "friend" in text and "likes tea" in text

    note.write_text(text + "\n## Watch For\n\n- mood\n", encoding="utf-8")

    api.create_person("main", "Alice", "colleague", "plays chess")
    merged = note.read_text(encoding="utf-8")
    assert "## Watch For" in merged, "existing content was overwritten"
    assert "friend" in merged and "likes tea" in merged
    assert "colleague" in merged and "plays chess" in merged


def test_create_person_merge_is_idempotent(tmp_path):
    mod = load_plugin_module()
    vault = _FakeVault(tmp_path)
    api = mod.MemoryApi(db_module=None, vault_manager=vault, llm_client=None)
    api.create_person("main", "Alice", "friend", "likes tea")
    api.create_person("main", "Alice", "friend", "likes tea")
    text = (tmp_path / "main" / "People" / "Alice.md").read_text(encoding="utf-8")
    assert text.count("## Relationship") == 1
    assert text.count("## Notes") == 1


def test_extract_facts_non_dict_json_returns_empty():
    mod = load_plugin_module()
    for payload in ('["a", "b"]', '"hello"', "42"):
        api = mod.MemoryApi(db_module=None, vault_manager=None, llm_client=_FakeLLM(payload))
        assert api.extract_facts("main", [{"role": "user", "content": "hi"}]) == {}


def _make_facts_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE memory_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vault_name TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            category TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            source TEXT DEFAULT 'manual',
            confidence REAL DEFAULT 1.0,
            contradicted INTEGER DEFAULT 0
        )
    """)
    return conn


def test_resolve_contradiction_confirm_deactivates_other_active_rows():
    mod = load_plugin_module()
    conn = _make_facts_db()
    conn.execute(
        'INSERT INTO memory_facts (vault_name, key, value, created_at, contradicted) VALUES (?, ?, ?, ?, 1)',
        ("main", "hometown", "Brno", "2026-01-01T00:00:00Z"),
    )
    conn.execute(
        'INSERT INTO memory_facts (vault_name, key, value, created_at, contradicted) VALUES (?, ?, ?, ?, 0)',
        ("main", "hometown", "Prague", "2026-01-02T00:00:00Z"),
    )
    old_id = conn.execute(
        "SELECT id FROM memory_facts WHERE value = 'Brno'"
    ).fetchone()["id"]

    assert mod._resolve_contradiction(conn, "main", old_id, "confirm")

    rows = conn.execute(
        "SELECT value, contradicted FROM memory_facts WHERE vault_name = 'main' AND key = 'hometown'"
    ).fetchall()
    actives = [r for r in rows if not r["contradicted"]]
    assert len(actives) == 1
    assert actives[0]["value"] == "Brno"


def test_resolve_contradiction_confirm_missing_fact_returns_false():
    mod = load_plugin_module()
    conn = _make_facts_db()
    assert not mod._resolve_contradiction(conn, "main", 12345, "confirm")
