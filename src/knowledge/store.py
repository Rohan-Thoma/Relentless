import os
import yaml
import json
from dataclasses import dataclass, field
from datetime import datetime, UTC
from src.db import get_connection

KNOWLEDGE_DIR = "knowledge"
RESERVED = {"index.md", "log.md"}


@dataclass
class Concept:
    path: str
    concept_type: str
    name: str = ""
    description: str = ""
    resource: str = ""
    tags: list = field(default_factory=list)
    frontmatter: dict = field(default_factory=dict)
    verified: list = field(default_factory=list)
    status: str = "stable"
    stale_after: str | None = None
    body: str = ""
    updated_at: str | None = None


def agent_actor(model="relentless/unknown"):
    return f"relentless/{model}"


def human_actor():
    return "human:owner"


def process_actor(pid):
    return f"process:{pid}"


def trust_tier(concept: Concept) -> str:
    for v in concept.verified or []:
        by = v.get("by", "") if isinstance(v, dict) else ""
        if by.startswith("human:"):
            return "human-reviewed"
    if concept.verified:
        return "machine-confirmed"
    return "unverified"


def is_stale(concept: Concept) -> bool:
    if not concept.stale_after:
        return False
    try:
        sa = datetime.fromisoformat(concept.stale_after.replace("Z", "+00:00"))
        return datetime.now(UTC) >= sa
    except Exception:
        return False


class Store:
    def __init__(self):
        self.concepts: dict[str, Concept] = {}
        self.dirty: set[str] = set()
        self.deleted: set[str] = set()
        self.revisions: dict[str, list] = {}
        self._hydrated = False
        self._last_flushed = None

    def hydrate(self):
        os.makedirs(KNOWLEDGE_DIR, exist_ok=True)
        self.concepts = {}
        self.dirty = set()
        self.deleted = set()
        self.revisions = {}
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT * FROM okf_concepts;")
                    rows = cur.fetchall()
        except Exception:
            rows = []
        for row in rows:
            c = Concept(
                path=row["path"],
                concept_type=row["concept_type"],
                name=row.get("name") or "",
                description=row.get("description") or "",
                resource=row.get("resource") or "",
                tags=list(row.get("tags") or []),
                frontmatter=dict(row.get("frontmatter") or {}),
                verified=list(row.get("verified") or []),
                status=row.get("status") or "stable",
                stale_after=str(row["stale_after"]) if row.get("stale_after") else None,
                body=row.get("body") or "",
                updated_at=str(row.get("updated_at")) if row.get("updated_at") else None,
            )
            self.concepts[c.path] = c
            self._write_file(c)
        self._hydrated = True
        self.render_indexes()

    def _file_path(self, concept_path):
        return os.path.join(KNOWLEDGE_DIR, concept_path + ".md")

    def _write_file(self, c: Concept):
        os.makedirs(os.path.dirname(self._file_path(c.path)), exist_ok=True) if os.path.dirname(c.path) else None
        fm = dict(c.frontmatter or {})
        fm.setdefault("type", c.concept_type)
        if c.name:
            fm.setdefault("title", c.name)
        if c.description:
            fm.setdefault("description", c.description)
        if c.resource:
            fm.setdefault("resource", c.resource)
        if c.tags:
            fm.setdefault("tags", c.tags)
        if c.verified:
            fm.setdefault("verified", c.verified)
        if c.status:
            fm.setdefault("status", c.status)
        if c.stale_after:
            fm.setdefault("stale_after", c.stale_after)
        with open(self._file_path(c.path), "w", encoding="utf-8") as f:
            f.write("---\n")
            f.write(yaml.safe_dump(fm, sort_keys=False, allow_unicode=True))
            f.write("---\n\n")
            f.write(c.body or "")

    def _read_file(self, path):
        fp = self._file_path(path)
        if not os.path.exists(fp):
            return None
        with open(fp, encoding="utf-8") as f:
            text = f.read()
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                fm = yaml.safe_load(parts[1]) or {}
                body = parts[2].strip("\n")
                return fm, body
        return {}, text

    def import_markdown(self, rel_path: str):
        full = os.path.join(KNOWLEDGE_DIR, rel_path)
        if not os.path.exists(full):
            return None
        # rel_path without .md -> concept path
        concept_path = rel_path.removesuffix(".md")
        if concept_path in ("index", "log"):
            return None
        res = self._read_file(concept_path)
        if not res:
            return None
        fm, body = res
        ctype = fm.get("type", "concept")
        c = Concept(
            path=concept_path,
            concept_type=ctype,
            name=fm.get("title", fm.get("name", "")),
            description=fm.get("description", ""),
            resource=fm.get("resource", ""),
            tags=list(fm.get("tags") or []),
            frontmatter=fm,
            verified=list(fm.get("verified") or []),
            status=fm.get("status", "stable"),
            stale_after=fm.get("stale_after"),
            body=body,
        )
        self.upsert(c, actor="human:import")
        return c

    def sync_from_files(self):
        if not os.path.exists(KNOWLEDGE_DIR):
            return
        for root, _, files in os.walk(KNOWLEDGE_DIR):
            for fn in files:
                if not fn.endswith(".md"):
                    continue
                rel = os.path.relpath(os.path.join(root, fn), KNOWLEDGE_DIR).replace("\\", "/")
                if rel in ("index.md", "log.md"):
                    continue
                self.import_markdown(rel)

    def upsert(self, concept: Concept, actor="relentless/unknown"):
        if not concept.concept_type:
            raise ValueError("type required")
        if concept.path in ("index.md", "log.md") or concept.path.endswith("index.md"):
            raise ValueError("reserved path")
        # normalize verified bare mapping
        if isinstance(concept.verified, dict):
            concept.verified = [concept.verified]
        concept.updated_at = datetime.now(UTC).isoformat()
        # frontmatter merge
        concept.frontmatter = concept.frontmatter or {}
        concept.frontmatter.setdefault("type", concept.concept_type)
        self.concepts[concept.path] = concept
        self._write_file(concept)
        self.dirty.add(concept.path)
        self.revisions.setdefault(concept.path, []).append({
            "actor": actor,
            "frontmatter": concept.frontmatter,
            "body": concept.body,
        })
        # hash snapshot not needed

    def delete(self, path: str):
        if path in self.concepts:
            del self.concepts[path]
        self.deleted.add(path)
        fp = self._file_path(path)
        if os.path.exists(fp):
            os.remove(fp)
        # scrub inbound links: remove references in other bodies
        for c in self.concepts.values():
            if path in (c.body or ""):
                c.body = c.body.replace(path, "")
                self.dirty.add(c.path)
                self._write_file(c)

    def search(self, query="", type=None, tag=None, min_trust=None, include_stale=True):
        q = (query or "").lower()
        results = []
        for c in self.concepts.values():
            if type and c.concept_type != type:
                continue
            if tag and tag not in (c.tags or []):
                continue
            if not include_stale and is_stale(c):
                continue
            if min_trust:
                tier = trust_tier(c)
                order = {"unverified": 0, "machine-confirmed": 1, "human-reviewed": 2}
                if order.get(tier, 0) < order.get(min_trust, 0):
                    continue
            score = 0
            hay = (c.name + " " + c.description + " " + c.body + " " + " ".join(c.tags or [])).lower()
            if q:
                for token in q.split():
                    if token in hay:
                        score += 1
            else:
                score = 1
            if score > 0:
                results.append((score, c))
        results.sort(key=lambda x: x[0], reverse=True)
        return [c for _, c in results]

    def children(self, path):
        prefix = path.rstrip("/") + "/"
        return [c for p, c in self.concepts.items() if p.startswith(prefix) and p.count("/") == path.count("/") + 1]

    def subtree(self, path):
        prefix = path.rstrip("/") + "/"
        return [c for p, c in self.concepts.items() if p == path or p.startswith(prefix)]

    def neighbors(self, path):
        c = self.concepts.get(path)
        if not c:
            return []
        out = []
        for other in self.concepts.values():
            if other.path != path and path in (other.body or ""):
                out.append(other)
        return out

    def render_indexes(self):
        os.makedirs(KNOWLEDGE_DIR, exist_ok=True)
        lines = ["---\n", "okf_version: \"0.2\"\n", "---\n\n", "# Knowledge Index\n\n"]
        for path in sorted(self.concepts):
            c = self.concepts[path]
            lines.append(f"- [{path}](/{path}.md) — {c.concept_type}: {c.name}\n")
        with open(os.path.join(KNOWLEDGE_DIR, "index.md"), "w", encoding="utf-8") as f:
            f.write("".join(lines))

    def append_log(self, entry):
        os.makedirs(KNOWLEDGE_DIR, exist_ok=True)
        with open(os.path.join(KNOWLEDGE_DIR, "log.md"), "a", encoding="utf-8") as f:
            f.write(entry + "\n")

    def flush(self):
        with get_connection() as conn:
            with conn.cursor() as cur:
                for path in list(self.deleted):
                    cur.execute("DELETE FROM okf_concepts WHERE path = %s;", (path,))
                for path in list(self.dirty):
                    c = self.concepts.get(path)
                    if c is None:
                        continue
                    cur.execute(
                        """
                        INSERT INTO okf_concepts (path, concept_type, name, description, resource, tags, frontmatter, verified, status, stale_after, body, updated_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                        ON CONFLICT (path) DO UPDATE SET
                            concept_type=EXCLUDED.concept_type,
                            name=EXCLUDED.name,
                            description=EXCLUDED.description,
                            resource=EXCLUDED.resource,
                            tags=EXCLUDED.tags,
                            frontmatter=EXCLUDED.frontmatter,
                            verified=EXCLUDED.verified,
                            status=EXCLUDED.status,
                            stale_after=EXCLUDED.stale_after,
                            body=EXCLUDED.body,
                            updated_at=NOW();
                        """,
                        (c.path, c.concept_type, c.name, c.description, c.resource, c.tags,
                         json.dumps(c.frontmatter), json.dumps(c.verified), c.status, c.stale_after, c.body),
                    )
                    for rev in self.revisions.get(path, []):
                        cur.execute("SELECT COALESCE(MAX(revision_no),0) AS m FROM knowledge_revisions WHERE path=%s;", (path,))
                        rn = (cur.fetchone().get("m") or 0) + 1
                        cur.execute(
                            "INSERT INTO knowledge_revisions (path, revision_no, frontmatter, body, actor, at) VALUES (%s,%s,%s,%s,%s,NOW());",
                            (path, rn, json.dumps(rev["frontmatter"]), rev["body"], rev["actor"]),
                        )
                conn.commit()
            self.deleted.clear()
            self.dirty.clear()
            self.revisions.clear()
        self._last_flushed = datetime.now(UTC).isoformat()


_store = Store()


def get_store():
    return _store


def hydrate():
    _store.hydrate()


def flush():
    _store.flush()