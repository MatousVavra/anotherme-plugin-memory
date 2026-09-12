import asyncio
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def client(make_client):
    return make_client()


def _get_api():
    import src.main
    return src.main.plugin_manager.get_registry().get_api("memory")


# --- 1. Plugin listed ---

def test_memory_plugin_listed(client):
    names = {p["name"] for p in client.get("/plugins").json()}
    assert "memory" in names


# --- 2. Tables created ---

def test_memory_tables_created(client):
    import src.database
    conn = src.database.get_db()
    tables = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "memory_facts" in tables
    assert "memory_people" in tables


# --- 3. Facts CRUD ---

def test_memory_facts_crud(client):
    api = _get_api()
    api.save_fact("test-main", "hobby", "coding")

    resp = client.get("/plugins/memory/facts")
    assert resp.status_code == 200
    facts = resp.json()
    assert any(f["key"] == "hobby" and f["value"] == "coding" for f in facts)

    fact_id = next(f["id"] for f in facts if f["key"] == "hobby")

    resp = client.put(f"/plugins/memory/facts/{fact_id}", json={"value": "programming"})
    assert resp.status_code == 200

    resp = client.get("/plugins/memory/facts")
    updated = next(f for f in resp.json() if f["id"] == fact_id)
    assert updated["value"] == "programming"

    resp = client.delete(f"/plugins/memory/facts/{fact_id}")
    assert resp.status_code == 200

    resp = client.get("/plugins/memory/facts")
    assert not any(f["id"] == fact_id for f in resp.json())

    assert client.put("/plugins/memory/facts/99999", json={"value": "x"}).status_code == 404
    assert client.delete("/plugins/memory/facts/99999").status_code == 404


# --- 4. People list and update ---

def test_memory_people_list_and_update(client):
    api = _get_api()
    api.save_person("test-main", "Alice", "sister", "lives in Prague")

    resp = client.get("/plugins/memory/people")
    assert resp.status_code == 200
    people = resp.json()
    assert any(p["name"] == "Alice" for p in people)

    resp = client.put("/plugins/memory/people/Alice", json={"pronunciation_hint": "AH-liss"})
    assert resp.status_code == 200

    resp = client.get("/plugins/memory/people")
    alice = next(p for p in resp.json() if p["name"] == "Alice")
    assert alice["pronunciation_hint"] == "AH-liss"

    assert client.put("/plugins/memory/people/Unknown", json={"pronunciation_hint": "x"}).status_code == 404


# --- 5. Projects list ---

def test_memory_projects_list(client):
    api = _get_api()
    api.save_project("test-main", "Alpha", "desc", "active")

    resp = client.get("/plugins/memory/projects")
    assert resp.status_code == 200
    projects = resp.json()
    assert any(p["title"] == "Alpha" and p["status"] == "active" for p in projects)


# --- 6. API registered ---

def test_memory_api_registered(client):
    api = _get_api()
    assert api is not None
    assert hasattr(api, "extract_facts")
    assert hasattr(api, "save_fact")
    assert hasattr(api, "get_facts")
    assert hasattr(api, "save_person")
    assert hasattr(api, "save_project")
    assert hasattr(api, "get_people")


# --- 7. Tools registered ---

def test_memory_tools_registered(client):
    import src.main
    registry = src.main.plugin_manager.get_registry()
    tool_names = {t["function"]["name"] for t in registry.get_tools()}
    assert "memory.save_fact" in tool_names
    assert "memory.get_facts" in tool_names
    assert "memory.save_person" in tool_names
    assert "memory.save_project" in tool_names


# --- 8. save_fact tool ---

@pytest.mark.asyncio
async def test_memory_save_fact_tool(client):
    from src import tools as tools_module
    result = await tools_module.execute_tool(
        "memory_save_fact", '{"key": "city", "value": "Berlin"}', None, None
    )
    assert "Fact saved" in result
    api = _get_api()
    facts = api.get_facts("test-main")
    assert any(f["key"] == "city" and f["value"] == "Berlin" for f in facts)


# --- 9. get_facts tool ---

@pytest.mark.asyncio
async def test_memory_get_facts_tool(client):
    api = _get_api()
    api.save_fact("test-main", "lang", "Python")
    from src import tools as tools_module
    result = await tools_module.execute_tool("memory_get_facts", "{}", None, None)
    assert "Python" in result


# --- 10. save_person tool ---

@pytest.mark.asyncio
async def test_memory_save_person_tool(client):
    from src import tools as tools_module
    result = await tools_module.execute_tool(
        "memory_save_person",
        '{"name": "Bob", "relationship": "colleague", "notes": "works remotely"}',
        None, None,
    )
    assert "Person saved" in result
    api = _get_api()
    people = api.get_people("test-main")
    assert any(p["name"] == "Bob" for p in people)
    vault = Path(os.environ["VAULTS_DIR"]) / "test-main"
    assert (vault / "People" / "Bob.md").is_file()


# --- 11. save_project tool ---

@pytest.mark.asyncio
async def test_memory_save_project_tool(client):
    from src import tools as tools_module
    result = await tools_module.execute_tool(
        "memory_save_project",
        '{"title": "Gamma", "description": "test proj", "status": "planning"}',
        None, None,
    )
    assert "Project saved" in result
    vault = Path(os.environ["VAULTS_DIR"]) / "test-main"
    assert (vault / "Projects" / "Gamma.md").is_file()


# --- 12. extract_facts mocked ---

def test_memory_extract_facts_mocked(client):
    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock(message=MagicMock(content='{"hometown": "Prague"}'))]
    mock_client.chat.completions.create.return_value = mock_resp

    with patch("src.llm.get_client", return_value=mock_client):
        api = _get_api()
        result = api.extract_facts("test-main", [{"role": "user", "content": "I live in Prague"}])

    assert result == {"hometown": "Prague"}
    resp = client.get("/plugins/memory/facts")
    facts = resp.json()
    assert any(f["key"] == "hometown" and f["value"] == "Prague" for f in facts)


# --- 13. Prompt fragment ---

def test_memory_prompt_fragment(client):
    import src.main
    registry = src.main.plugin_manager.get_registry()
    fragments = registry.get_prompt_fragments("chat")
    assert any("memory.save_fact" in frag for frag in fragments)


# --- 14. Old endpoint removed ---

# (Old /memory endpoint removed in Phase 7)


# --- 15. Empty results on fresh DB ---

def test_memory_facts_empty(client):
    assert client.get("/plugins/memory/facts").json() == []


def test_memory_people_empty(client):
    assert client.get("/plugins/memory/people").json() == []


def test_memory_projects_empty(client):
    assert client.get("/plugins/memory/projects").json() == []


# --- 16. Data isolation: plugin writes don't appear on old endpoints ---

# (Old /memory endpoint removed in Phase 7 — data isolation is inherent)


# --- 17. extract_facts error path ---

def test_memory_extract_facts_error(client):
    import src.main
    api = src.main.plugin_manager.get_registry().get_api("memory")
    with patch("src.llm.get_client", side_effect=Exception("network down")):
        result = api.extract_facts("test-main", [{"role": "user", "content": "hi"}])
    assert result == {}


# --- 18. Tool input validation ---

@pytest.mark.asyncio
async def test_memory_save_fact_tool_missing_key(client):
    from src import tools as tools_module
    result = await tools_module.execute_tool(
        "memory_save_fact", '{"value": "x"}', None, None
    )
    assert "Error" in result


# --- 19. Fact deduplication: same key + value is no-op ---

def test_memory_fact_dedup_same_value(client):
    api = _get_api()
    api.save_fact("test-main", "city", "Berlin")
    api.save_fact("test-main", "city", "Berlin")
    facts = api.get_facts("test-main")
    city_facts = [f for f in facts if f["key"] == "city"]
    assert len(city_facts) == 1


# --- 20. Fact contradiction: same key, different value ---

def test_memory_fact_contradiction(client):
    api = _get_api()
    api.save_fact("test-main", "city", "Berlin")
    api.save_fact("test-main", "city", "Prague")
    facts = api.get_facts("test-main")
    city_facts = [f for f in facts if f["key"] == "city"]
    assert len(city_facts) == 2
    contradicted = [f for f in city_facts if f["contradicted"]]
    assert len(contradicted) == 1
    assert contradicted[0]["value"] == "Berlin"
    current = [f for f in city_facts if not f["contradicted"]]
    assert len(current) == 1
    assert current[0]["value"] == "Prague"
    assert current[0]["confidence"] == 0.5


# --- 21. GET /contradictions returns contradicted facts ---

def test_memory_contradictions_route(client):
    api = _get_api()
    api.save_fact("test-main", "city", "Berlin")
    api.save_fact("test-main", "city", "Prague")
    resp = client.get("/plugins/memory/contradictions")
    assert resp.status_code == 200
    contradictions = resp.json()
    assert len(contradictions) == 1
    assert contradictions[0]["key"] == "city"
    assert contradictions[0]["value"] == "Berlin"
    assert contradictions[0]["contradicted"] is True


# --- 22. Resolve contradiction with "confirm" ---

def test_memory_resolve_confirm(client):
    api = _get_api()
    api.save_fact("test-main", "city", "Berlin")
    api.save_fact("test-main", "city", "Prague")
    contradictions = client.get("/plugins/memory/contradictions").json()
    fact_id = contradictions[0]["id"]
    resp = client.post(f"/plugins/memory/facts/{fact_id}/resolve?action=confirm")
    assert resp.status_code == 200
    facts = client.get("/plugins/memory/facts").json()
    resolved = next(f for f in facts if f["id"] == fact_id)
    assert resolved["contradicted"] is False
    assert resolved["confidence"] == 1.0


# --- 23. Resolve contradiction with "dismiss" ---

def test_memory_resolve_dismiss(client):
    api = _get_api()
    api.save_fact("test-main", "city", "Berlin")
    api.save_fact("test-main", "city", "Prague")
    contradictions = client.get("/plugins/memory/contradictions").json()
    fact_id = contradictions[0]["id"]
    resp = client.post(f"/plugins/memory/facts/{fact_id}/resolve?action=dismiss")
    assert resp.status_code == 200
    facts = client.get("/plugins/memory/facts").json()
    assert not any(f["id"] == fact_id for f in facts)


# --- 24. Resolve non-existent fact returns 404 ---

def test_memory_resolve_not_found(client):
    resp = client.post("/plugins/memory/facts/99999/resolve?action=confirm")
    assert resp.status_code == 404


# --- 25. extract_facts passes source="chat" ---

def test_memory_extract_facts_source(client):
    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock(message=MagicMock(content='{"city": "Paris"}'))]
    mock_client.chat.completions.create.return_value = mock_resp

    with patch("src.llm.get_client", return_value=mock_client):
        api = _get_api()
        api.extract_facts("test-main", [{"role": "user", "content": "I live in Paris"}])

    facts = client.get("/plugins/memory/facts").json()
    fact = next(f for f in facts if f["key"] == "city")
    assert fact["source"] == "chat"
