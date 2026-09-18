"""Unit tests for the PUT /people/{name} merge route."""
import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

from conftest import load_plugin_module
from fake_plugin_context import FakePluginContext


class _FakeDbModule:
    def __init__(self, conn):
        self._conn = conn

    def get_db(self):
        return self._conn


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


def _make_db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
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
    conn.execute("""
        CREATE TABLE memory_people (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vault_name TEXT NOT NULL,
            name TEXT NOT NULL,
            relationship TEXT,
            notes TEXT,
            tags TEXT DEFAULT '[]',
            pronunciation_hint TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(vault_name, name)
        )
    """)
    return conn


def _people_route(conn, tmp_path):
    ctx = FakePluginContext(
        db_module=_FakeDbModule(conn),
        vault_manager=_FakeVault(tmp_path),
        llm_client=MagicMock(),
    )
    mod = load_plugin_module()
    mod.Plugin().on_load(ctx)
    router = ctx.registry.routers[0][1]
    route = next(r for r in router.routes if r.path == "/people/{name}" and "PUT" in r.methods)
    return mod, route


def _put(mod, route, name, **fields):
    return asyncio.run(route.endpoint(name, mod.PersonUpdate(**fields)))


def test_update_person_creates_missing(tmp_path):
    conn = _make_db()
    mod, route = _people_route(conn, tmp_path)
    result = _put(mod, route, "Alice", relationship="friend", notes="likes tea", tags=["inner"])
    assert result.detail == "Person updated"
    person = mod._get_person(conn, "main", "Alice")
    assert person["relationship"] == "friend"
    assert person["notes"] == "likes tea"
    assert person["tags"] == ["inner"]
    assert (tmp_path / "main" / "People" / "Alice.md").is_file()


def test_update_person_merges_omitted_fields(tmp_path):
    conn = _make_db()
    mod, route = _people_route(conn, tmp_path)
    _put(mod, route, "Alice", relationship="friend", notes="likes tea", pronunciation_hint="AH-lice")
    _put(mod, route, "Alice", notes="plays chess")
    person = mod._get_person(conn, "main", "Alice")
    assert person["relationship"] == "friend"
    assert person["notes"] == "plays chess"
    assert person["pronunciation_hint"] == "AH-lice"


def test_update_person_hint_only_preserves_rest(tmp_path):
    conn = _make_db()
    mod, route = _people_route(conn, tmp_path)
    _put(mod, route, "Alice", relationship="friend", notes="likes tea", tags=["inner"])
    _put(mod, route, "Alice", pronunciation_hint="AH-lice")
    person = mod._get_person(conn, "main", "Alice")
    assert person["relationship"] == "friend"
    assert person["notes"] == "likes tea"
    assert person["tags"] == ["inner"]
    assert person["pronunciation_hint"] == "AH-lice"


def test_update_person_is_idempotent(tmp_path):
    conn = _make_db()
    mod, route = _people_route(conn, tmp_path)
    _put(mod, route, "Alice", relationship="friend", notes="likes tea")
    _put(mod, route, "Alice", relationship="friend", notes="likes tea")
    rows = conn.execute(
        "SELECT COUNT(*) AS c FROM memory_people WHERE vault_name = 'main' AND name = 'Alice'"
    ).fetchone()
    assert rows["c"] == 1
    text = (tmp_path / "main" / "People" / "Alice.md").read_text(encoding="utf-8")
    assert text.count("## Relationship") == 1
    assert text.count("## Notes") == 1
