import asyncio
import json
import logging
import re
from datetime import datetime, timezone

import yaml
from fastapi import APIRouter, HTTPException

from pydantic import BaseModel, Field


class MemoryFact(BaseModel):
    id: int
    key: str
    value: str
    category: str | None = None
    created_at: str
    updated_at: str
    source: str = "manual"
    confidence: float = 1.0
    contradicted: bool = False


class MemoryUpdate(BaseModel):
    value: str = Field(min_length=1)
    category: str | None = None


class Person(BaseModel):
    name: str
    relationship: str | None = None
    notes: str | None = None
    tags: list[str] = Field(default_factory=list)
    pronunciation_hint: str | None = None
    created_at: str
    updated_at: str


class PersonUpdate(BaseModel):
    pronunciation_hint: str | None = None


class PersonUpdateResponse(BaseModel):
    detail: str

logger = logging.getLogger(__name__)


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _categorize_fact(key: str) -> str:
    k = (key or "").lower()
    if re.search(r"family|wife|husband|son|daughter|mother|father|brother|sister|parent|child|friend|partner|spouse|cousin|relative", k):
        return "Family"
    if re.search(r"work|job|career|project|task|deadline|meeting|office|code|programming", k):
        return "Work"
    if re.search(r"health|exercise|sleep|diet|weight|doctor|medicine|therapy|wellness|gym", k):
        return "Health"
    return "Preferences"


def _save_fact(conn, vault_name, key, value, source="manual", confidence=1.0):
    now = _now_utc()
    existing = conn.execute(
        "SELECT id, value, confidence, contradicted FROM memory_facts WHERE vault_name = ? AND key = ? AND COALESCE(contradicted, 0) = 0",
        (vault_name, key),
    ).fetchone()

    if existing:
        if existing["value"] == value:
            return
        conn.execute(
            "UPDATE memory_facts SET contradicted = 1, updated_at = ? WHERE vault_name = ? AND key = ? AND COALESCE(contradicted, 0) = 0",
            (now, vault_name, key),
        )
        conn.execute(
            "INSERT INTO memory_facts (vault_name, key, value, created_at, updated_at, source, confidence, contradicted) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            (vault_name, key, value, now, now, source, 0.5),
        )
        conn.commit()
        return

    conn.execute(
        "INSERT INTO memory_facts (vault_name, key, value, created_at, updated_at, source, confidence, contradicted) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
        (vault_name, key, value, now, now, source, confidence),
    )
    conn.commit()


def _get_facts(conn, vault_name):
    rows = conn.execute(
        "SELECT id, key, value, category, created_at, updated_at, source, confidence, contradicted FROM memory_facts WHERE vault_name = ? ORDER BY created_at DESC",
        (vault_name,),
    ).fetchall()
    return [{"id": r["id"], "key": r["key"], "value": r["value"],
             "category": r["category"] or _categorize_fact(r["key"]),
             "created_at": r["created_at"],
             "updated_at": r["updated_at"] or r["created_at"],
             "source": r["source"] or "manual",
             "confidence": r["confidence"] if r["confidence"] is not None else 1.0,
             "contradicted": bool(r["contradicted"]) if r["contradicted"] is not None else False} for r in rows]


def _update_fact(conn, vault_name, fact_id, value, category=None):
    now = _now_utc()
    if category:
        cur = conn.execute(
            "UPDATE memory_facts SET value = ?, category = ?, updated_at = ? WHERE vault_name = ? AND id = ?",
            (value, category, now, vault_name, fact_id),
        )
    else:
        cur = conn.execute(
            "UPDATE memory_facts SET value = ?, updated_at = ? WHERE vault_name = ? AND id = ?",
            (value, now, vault_name, fact_id),
        )
    conn.commit()
    return cur.rowcount > 0


def _delete_fact(conn, vault_name, fact_id):
    cur = conn.execute("DELETE FROM memory_facts WHERE vault_name = ? AND id = ?", (vault_name, fact_id))
    conn.commit()
    return cur.rowcount > 0


def _ensure_columns(conn):
    schema_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_facts'"
    ).fetchone()
    if schema_row and "UNIQUE" in schema_row["sql"].upper():
        conn.execute("ALTER TABLE memory_facts RENAME TO memory_facts_old")
        conn.execute("""CREATE TABLE memory_facts (
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
        )""")
        conn.execute(
            "INSERT INTO memory_facts (id, vault_name, key, value, category, created_at, updated_at) "
            "SELECT id, vault_name, key, value, category, created_at, created_at "
            "FROM memory_facts_old"
        )
        conn.execute("DROP TABLE memory_facts_old")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(memory_facts)").fetchall()}
    for col, decl in [
        ("updated_at", "TEXT"),
        ("source", "TEXT DEFAULT 'manual'"),
        ("confidence", "REAL DEFAULT 1.0"),
        ("contradicted", "INTEGER DEFAULT 0"),
    ]:
        if col not in cols:
            conn.execute(f"ALTER TABLE memory_facts ADD COLUMN {col} {decl}")
    conn.commit()


def _get_contradictions(conn, vault_name):
    rows = conn.execute(
        "SELECT id, key, value, category, created_at, updated_at, source, confidence, contradicted FROM memory_facts WHERE vault_name = ? AND COALESCE(contradicted, 0) = 1 ORDER BY created_at DESC",
        (vault_name,),
    ).fetchall()
    return [{"id": r["id"], "key": r["key"], "value": r["value"],
             "category": r["category"] or _categorize_fact(r["key"]),
             "created_at": r["created_at"],
             "updated_at": r["updated_at"] or r["created_at"],
             "source": r["source"] or "manual",
             "confidence": r["confidence"] if r["confidence"] is not None else 1.0,
             "contradicted": bool(r["contradicted"]) if r["contradicted"] is not None else False} for r in rows]


def _resolve_contradiction(conn, vault_name, fact_id, action):
    if action == "confirm":
        cur = conn.execute(
            "UPDATE memory_facts SET contradicted = 0, confidence = 1.0, updated_at = ? WHERE vault_name = ? AND id = ?",
            (_now_utc(), vault_name, fact_id),
        )
        conn.commit()
        return cur.rowcount > 0
    elif action == "dismiss":
        cur = conn.execute(
            "DELETE FROM memory_facts WHERE vault_name = ? AND id = ?",
            (vault_name, fact_id),
        )
        conn.commit()
        return cur.rowcount > 0
    return False


def _save_person(conn, vault_name, name, relationship, notes, tags, pronunciation_hint=None):
    cur = conn.execute(
        """INSERT OR REPLACE INTO memory_people (vault_name, name, relationship, notes, tags, pronunciation_hint, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, COALESCE(?, (SELECT pronunciation_hint FROM memory_people WHERE vault_name=? AND name=?)),
                   COALESCE((SELECT created_at FROM memory_people WHERE vault_name=? AND name=?), ?), ?)""",
        (vault_name, name, relationship, notes, json.dumps(tags), pronunciation_hint,
         vault_name, name, vault_name, name, _now_utc(), _now_utc()),
    )
    conn.commit()
    return cur.lastrowid


def _get_people(conn, vault_name):
    rows = conn.execute(
        "SELECT name, relationship, notes, tags, pronunciation_hint, created_at, updated_at FROM memory_people WHERE vault_name = ? ORDER BY name",
        (vault_name,),
    ).fetchall()
    return [{"name": r["name"], "relationship": r["relationship"], "notes": r["notes"],
             "tags": json.loads(r["tags"]), "pronunciation_hint": r["pronunciation_hint"],
             "created_at": r["created_at"], "updated_at": r["updated_at"]} for r in rows]


# --- Scoring / keyword extraction (from src/context.py — owned by memory) ---

def extract_keywords(text: str) -> list[str]:
    text = re.sub(r"[^\w\s]", " ", text.lower())
    tokens = [t for t in text.split() if len(t) > 2]
    stop = {"the", "and", "for", "are", "but", "not", "you", "all", "can", "had", "her", "was", "went", "one", "our", "out", "day", "get", "has", "him", "his", "how", "its", "may", "new", "now", "old", "see", "two", "who", "boy", "did", "she", "use", "her", "way", "many", "oil", "sit", "set", "run", "eat", "far", "sea", "eye", "ask", "own", "say", "too", "any", "try", "let", "put", "end", "why", "all", "may", "say", "she", "try", "way", "own", "say", "too", "old", "tell", "very", "when", "much", "would", "there", "their", "what", "said", "have", "each", "which", "will", "about", "if", "up", "out", "many", "then", "them", "these", "so", "some", "her", "would", "make", "like", "into", "him", "time", "has", "two", "more", "very", "what", "know", "just", "first", "also", "after", "back", "other", "many", "than", "only", "those", "come", "day", "most", "us", "is", "it", "of", "to", "in", "a", "on", "at", "be", "as", "by", "or", "an", "do", "if", "no", "so", "up", "my"}
    return list(dict.fromkeys(t for t in tokens if t not in stop))[:20]


def score_relevance(items: list[dict], keywords: list[str], recency_weight: float = 0.3) -> list[tuple[float, dict]]:
    keywords = set(keywords)
    now = datetime.now(timezone.utc)
    scored = []
    for item in items:
        text = " ".join(str(item.get(k, "")) for k in ("key", "value", "content", "title", "name", "notes"))
        text_tokens = set(text.lower().split())
        overlap = len(keywords & text_tokens)
        try:
            created = datetime.fromisoformat(item.get("created_at", "2026-01-01T00:00:00Z").replace("Z", "+00:00"))
        except ValueError:
            created = datetime(2026, 1, 1, tzinfo=timezone.utc)
        days_old = max(0, (now - created).days)
        recency = max(0, 1 - (days_old / 365))
        score = overlap + recency_weight * recency
        scored.append((score, item))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored


def _update_person_hint(conn, vault_name, name, hint):
    cur = conn.execute(
        "UPDATE memory_people SET pronunciation_hint = ?, updated_at = ? WHERE vault_name = ? AND name = ?",
        (hint, _now_utc(), vault_name, name),
    )
    conn.commit()
    return cur.rowcount > 0


MEMORY_EXTRACT_SYSTEM = """You are a memory extraction agent. Given a conversation between a user and their AI assistant, extract any facts about the user that would be useful to remember for future conversations.

Rules:
- Only extract clear, factual statements about the user.
- Each fact should be a single key-value pair.
- Keys should be short and descriptive (e.g. "favorite_color", "job_title", "hometown").
- Do NOT extract facts about projects, notes content, or transient info.
- Focus on: preferences, personal details, recurring habits, important relationships.
- If there's nothing worth remembering, return an empty JSON object.

Respond ONLY with a JSON object: {"key": "value"}

Example: {"hometown": "Prague", "preferred_language": "TypeScript"}
"""


class MemoryApi:
    def __init__(self, db_module, vault_manager, llm_client):
        self._db = db_module
        self._vault = vault_manager
        self._llm = llm_client

    def extract_facts(self, vault_name, messages):
        """Sync — blocks on LLM call. Callers must wrap in asyncio.to_thread()."""
        try:
            client = self._llm.get_client()
            resp = client.chat.completions.create(
                model=self._llm.LLM_MODEL,
                messages=[
                    {"role": "system", "content": MEMORY_EXTRACT_SYSTEM},
                    {"role": "user", "content": str(messages)},
                ],
            )
            content = resp.choices[0].message.content or "{}"
            content = content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1].rsplit("\n", 1)[0]
                if content.startswith("json"):
                    content = content[4:].strip()
            facts = json.loads(content)
        except Exception as e:
            logger.debug("Fact extraction failed: %s", e)
            return {}

        conn = self._db.get_db()
        for key, value in facts.items():
            if isinstance(value, str) and key and value:
                _save_fact(conn, vault_name, key, value, source="chat")
        return facts

    def save_fact(self, vault_name, key, value):
        _save_fact(self._db.get_db(), vault_name, key, value)

    def get_facts(self, vault_name):
        return _get_facts(self._db.get_db(), vault_name)

    def save_person(self, vault_name, name, relationship=None, notes="", tags=None):
        tags = tags or []
        _save_person(self._db.get_db(), vault_name, name, relationship, notes, tags)
        self.create_person(vault_name, name, relationship, notes, tags)

    def save_project(self, vault_name, title, description="", status="active"):
        self.create_project(vault_name, title, description, status, [])

    def get_people(self, vault_name):
        return _get_people(self._db.get_db(), vault_name)

    # --- Vault domain methods (moved from src/vault.py) ---

    def create_person(self, vault_name, name, relationship=None, notes="", tags=None):
        parts = [f"# {name}", ""]
        if relationship:
            parts += ["## Relationship", "", relationship, ""]
        if notes:
            parts += ["## Notes", "", notes, ""]
        return self._vault.create_note(vault_name, name, "\n".join(parts), tags, "person",
                                       {"relationship": relationship} if relationship else {}, "People")

    def create_project(self, vault_name, title, description="", status="active", tags=None):
        parts = [f"# {title}", "", description, "", "## Status", "", status, "", "## Log", ""]
        return self._vault.create_note(vault_name, title, "\n".join(parts), tags, "project", {"status": status}, "Projects")

    def get_watch_fors(self, vault_name):
        """Watch For items across all People files. Used by chat/digest."""
        vault = self._vault.vault_path(vault_name)
        parts = []
        people_dir = vault / "People"
        if people_dir.is_dir():
            for pf in people_dir.glob("*.md"):
                try:
                    content = pf.read_text(encoding="utf-8")
                    watches = self._extract_watch_fors(content)
                    if watches:
                        parts.append(f"## {pf.stem} — Watch For:")
                        for w in watches:
                            parts.append(f"- {w}")
                except (OSError, UnicodeDecodeError):
                    continue
        return "\n".join(parts) if parts else ""

    @staticmethod
    def _extract_watch_fors(content):
        watches = []
        in_section = False
        for line in content.split("\n"):
            stripped = line.strip()
            if stripped.lower().startswith("## watch for") or stripped.lower().startswith("## watch-for"):
                in_section = True
                continue
            if in_section:
                if stripped.startswith("## "):
                    break
                if stripped.startswith("- "):
                    watches.append(stripped[2:])
                elif stripped.startswith("* "):
                    watches.append(stripped[2:])
        return watches

    def update_identity_facts(self, vault_name, facts):
        identity_path = self._vault.vault_path(vault_name) / "System" / "IDENTITY.md"
        if not identity_path.is_file():
            return

        text = identity_path.read_text(encoding="utf-8")

        lines = text.split("\n")
        in_section = False
        section_start = None
        section_end = None

        for i, line in enumerate(lines):
            if line.strip().startswith("## About the User"):
                in_section = True
                section_start = i
                continue
            if in_section and line.strip().startswith("## "):
                section_end = i
                break
        if section_end is None:
            section_end = len(lines)

        if section_start is None:
            return

        existing = {}
        for line in lines[section_start + 1:section_end]:
            line = line.strip()
            if ":" in line:
                k, v = line.split(":", 1)
                k = k.strip()
                v = v.strip()
                if v.startswith("[") and v.endswith("]"):
                    existing[k] = v
                else:
                    existing[k] = v

        traits = []
        preferences = []
        important = []
        name = None

        if "Traits" in existing:
            traits_str = existing["Traits"].strip("[]")
            if traits_str:
                traits = [t.strip() for t in traits_str.split(",")]
        if "Preferences" in existing:
            prefs_str = existing["Preferences"].strip("[]")
            if prefs_str:
                preferences = [p.strip() for p in prefs_str.split(",")]
        if "Important facts" in existing:
            facts_str = existing["Important facts"].strip("[]")
            if facts_str:
                important = [f.strip() for f in facts_str.split(",")]
        if "Name" in existing:
            name = existing["Name"]

        for key, value in facts.items():
            k = key.lower()
            if k == "name" or k == "full_name" or k == "username":
                name = value
            elif re.search(r"family|wife|husband|son|daughter|mother|father|brother|sister|parent|child|friend|partner|spouse|cousin|relative", k):
                entry = f"{key}: {value}"
                if entry not in important:
                    important.append(entry)
            elif re.search(r"like|prefer|favorite|enjoy|hobby|interest|music|food|book|movie|game|hate|dislike", k):
                entry = f"{key}: {value}"
                if entry not in preferences:
                    preferences.append(entry)
            elif re.search(r"work|job|career|project|task|deadline|meeting|office|code|programming", k):
                entry = f"{key}: {value}"
                if entry not in traits:
                    traits.append(entry)
            else:
                entry = f"{key}: {value}"
                if entry not in important:
                    important.append(entry)

        new_lines = lines[:section_start + 1]
        new_lines.append(f"Name: {name or 'unknown'}")
        new_lines.append(f"Traits: [{', '.join(traits)}]")
        new_lines.append(f"Preferences: [{', '.join(preferences)}]")
        new_lines.append(f"Important facts: [{', '.join(important)}]")
        new_lines.append("")
        new_lines.extend(lines[section_end:])

        identity_path.write_text("\n".join(new_lines), encoding="utf-8")


class Plugin:
    def on_load(self, ctx):
        dbm = ctx.db_module
        vault = ctx.vault_manager

        self._db = dbm
        self._vault = vault
        self._ctx = ctx

        ctx.register_migration(1, """
            CREATE TABLE IF NOT EXISTS memory_facts (
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
            );
            CREATE TABLE IF NOT EXISTS memory_people (
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
            );
        """)

        _ensure_columns(dbm.get_db())

        api = MemoryApi(dbm, vault, ctx.llm_client)
        self._api = api
        ctx.register_api("memory", api)

        router = APIRouter()

        @router.get("/facts", response_model=list[MemoryFact])
        async def list_facts():
            return await asyncio.to_thread(_get_facts, dbm.get_db(), ctx.vault_name)

        @router.put("/facts/{fact_id}")
        async def update_fact(fact_id: int, body: MemoryUpdate):
            ok = await asyncio.to_thread(_update_fact, dbm.get_db(), ctx.vault_name, fact_id, body.value, body.category)
            if not ok:
                raise HTTPException(404, "Fact not found")
            return {"detail": "Memory updated"}

        @router.delete("/facts/{fact_id}")
        async def delete_fact(fact_id: int):
            ok = await asyncio.to_thread(_delete_fact, dbm.get_db(), ctx.vault_name, fact_id)
            if not ok:
                raise HTTPException(404, "Fact not found")
            return {"detail": "Memory deleted"}

        @router.get("/contradictions", response_model=list[MemoryFact])
        async def list_contradictions():
            return await asyncio.to_thread(_get_contradictions, dbm.get_db(), ctx.vault_name)

        @router.post("/facts/{fact_id}/resolve")
        async def resolve_contradiction(fact_id: int, action: str = "confirm"):
            ok = await asyncio.to_thread(_resolve_contradiction, dbm.get_db(), ctx.vault_name, fact_id, action)
            if not ok:
                raise HTTPException(404, "Fact not found")
            return {"detail": f"Contradiction {action}ed"}

        @router.get("/people", response_model=list[Person])
        async def list_people():
            return await asyncio.to_thread(_get_people, dbm.get_db(), ctx.vault_name)

        @router.put("/people/{name}", response_model=PersonUpdateResponse)
        async def update_person(name: str, body: PersonUpdate):
            ok = await asyncio.to_thread(_update_person_hint, dbm.get_db(), ctx.vault_name, name, body.pronunciation_hint)
            if not ok:
                raise HTTPException(404, "Person not found")
            return PersonUpdateResponse(detail="Person updated")

        @router.get("/projects")
        async def list_projects():
            v = vault.vault_path(ctx.vault_name)
            projects_dir = v / "Projects"
            if not projects_dir.is_dir():
                return []
            results = []
            for f in sorted(projects_dir.glob("*.md")):
                entry = {"title": f.stem, "path": str(f.relative_to(v)), "filename": f.name, "status": "active"}
                try:
                    text = f.read_text(encoding="utf-8")
                    if text.startswith("---"):
                        fm_end = text.find("---", 4)
                        if fm_end > 0:
                            fm = yaml.safe_load(text[4:fm_end])
                            if isinstance(fm, dict):
                                entry["title"] = fm.get("title", f.stem)
                                entry["status"] = fm.get("status", "active")
                                entry["tags"] = fm.get("tags", [])
                except Exception:
                    pass
                results.append(entry)
            return results

        ctx.register_router(router)

        ctx.register_tool("save_fact", {
            "type": "function",
            "function": {
                "description": "Save a fact about the user to memory.",
                "parameters": {
                    "type": "object",
                    "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
                    "required": ["key", "value"],
                },
            },
        }, self.tool_save_fact)

        ctx.register_tool("get_facts", {
            "type": "function",
            "function": {
                "description": "Retrieve all saved facts about the user.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }, self.tool_get_facts)

        ctx.register_tool("save_person", {
            "type": "function",
            "function": {
                "description": "Save or update a person record in memory and the vault.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "relationship": {"type": "string"},
                        "notes": {"type": "string"},
                    },
                    "required": ["name"],
                },
            },
        }, self.tool_save_person)

        ctx.register_tool("save_project", {
            "type": "function",
            "function": {
                "description": "Create or update a project in the vault.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "status": {"type": "string"},
                    },
                    "required": ["title"],
                },
            },
        }, self.tool_save_project)

        ctx.add_prompt_fragment("chat",
            "You have memory tools (memory.save_fact, memory.get_facts, memory.save_person, memory.save_project). "
            "Use memory.save_fact to remember user facts. Use memory.get_facts to recall them. "
            "Use memory.save_person to save people you learn about. Use memory.save_project to track work."
        )

        # --- Context providers (Phase 6) ---
        ctx.register_context_provider("chat", self._facts_context_provider)
        ctx.register_context_provider("chat", self._watch_for_provider)

        def _on_diary_saved(payload):
            vn = ctx.vault_name
            content = payload.get("content", "")
            if content:
                async def _extract():
                    try:
                        await asyncio.to_thread(api.extract_facts, vn, [{"role": "user", "content": content}])
                        await ctx.event_bus.emit("memory_updated", {"source": "diary"})
                    except Exception:
                        logger.exception("Failed to extract facts from diary_saved event")
                asyncio.create_task(_extract())

        ctx.on_event("diary_saved", _on_diary_saved)

    # --- Context providers (Phase 6) ---

    def _facts_context_provider(self, user_text, thread_id, vault_name):
        """Return relevant facts section for chat context."""
        keywords = extract_keywords(user_text or "")
        facts = _get_facts(self._db.get_db(), vault_name)
        relevant = [item for score, item in score_relevance(facts, keywords)[:5] if score > 0]
        if relevant:
            return "## Relevant facts\n\n" + "\n".join(f"- {f['key']}: {f['value']}" for f in relevant)
        return None

    def _watch_for_provider(self, user_text, thread_id, vault_name):
        """Return watch-for items from People files."""
        watch_fors = self._api.get_watch_fors(vault_name)
        if watch_fors:
            return "## Watch For\n\n" + watch_fors
        return None

    def tool_save_fact(self, args: dict) -> str:
        vn = self._ctx.vault_name
        key = args.get("key", "").strip()
        value = args.get("value", "").strip()
        if not key or not value:
            return "Error: key and value are required."
        _save_fact(self._db.get_db(), vn, key, value)
        return f"Fact saved: {key} = {value}"

    def tool_get_facts(self, args: dict) -> str:
        facts = _get_facts(self._db.get_db(), self._ctx.vault_name)
        return json.dumps(facts) if facts else "(no facts saved)"

    def tool_save_person(self, args: dict) -> str:
        vn = self._ctx.vault_name
        name = args.get("name", "").strip()
        if not name:
            return "Error: name is required."
        rel = args.get("relationship")
        notes = args.get("notes", "")
        _save_person(self._db.get_db(), vn, name, rel, notes, [])
        self._api.create_person(vn, name, rel, notes, [])
        return f"Person saved: {name}"

    def tool_save_project(self, args: dict) -> str:
        vn = self._ctx.vault_name
        title = args.get("title", "").strip()
        if not title:
            return "Error: title is required."
        desc = args.get("description", "")
        status = args.get("status", "active")
        self._api.create_project(vn, title, desc, status, [])
        return f"Project saved: {title} (status: {status})"
