"""数据层：SQLite（WAL）+ 资料条目 .md 文件（真相源）+ FTS5 trigram 索引 + 研发平台用户（只读）。"""
import datetime
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

HOME = Path(os.environ.get("KB_HOME", Path(__file__).resolve().parent.parent))
DATA = Path(os.environ.get("KB_DATA", HOME / "data"))
DB = DATA / "kb.db"
ENTRIES = DATA / "entries"
UPLOADS = DATA / "uploads"
PLATFORM_DB = os.environ.get("KB_PLATFORM_DB", str(Path.home() / "cardorder-platform/data/platform.db"))

DAILY_LIMIT = int(os.environ.get("KB_DAILY_LIMIT", "20"))
ADMIN_ROLES = {"admin", "wei"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS questions(
  id TEXT PRIMARY KEY, asker TEXT NOT NULL, raw_text TEXT NOT NULL,
  card_json TEXT, clarify_json TEXT, images_json TEXT NOT NULL DEFAULT '[]',
  scope TEXT NOT NULL DEFAULT 'full', status TEXT NOT NULL, round INTEGER NOT NULL DEFAULT 0,
  hit_entry TEXT, entry_id TEXT, source TEXT NOT NULL DEFAULT 'web', created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS q_asker ON questions(asker, created_at);
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, question_id TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('clarify','answer')),
  priority INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('queued','leased','done','failed')),
  attempts INTEGER NOT NULL DEFAULT 0, lease_until REAL, worker TEXT, payload TEXT NOT NULL,
  result TEXT, error TEXT, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS jobs_pick ON jobs(state, priority, created_at);
CREATE TABLE IF NOT EXISTS answers(
  question_id TEXT, version INTEGER, plain_md TEXT, detail_md TEXT, evidence_json TEXT,
  dropped_json TEXT, mismatches_json TEXT, confidence TEXT, commit_sha TEXT, needs_review INTEGER,
  created_at REAL, PRIMARY KEY(question_id, version));
CREATE TABLE IF NOT EXISTS feedback(question_id TEXT, version INTEGER, user TEXT, verdict TEXT, note TEXT, at REAL);
CREATE TABLE IF NOT EXISTS entries(
  id TEXT PRIMARY KEY, title TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '[]', scope TEXT NOT NULL DEFAULT 'public',
  status TEXT NOT NULL DEFAULT 'published', source_question TEXT, updated TEXT, path TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(id UNINDEXED, title, body, tags, tokenize='trigram');
CREATE TABLE IF NOT EXISTS workers(name TEXT PRIMARY KEY, last_seen REAL);
CREATE TABLE IF NOT EXISTS audit_log(at REAL, actor TEXT, action TEXT, target TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS question_turns(
  question_id TEXT NOT NULL, round INTEGER NOT NULL, asker TEXT NOT NULL,
  text TEXT NOT NULL, created_at REAL NOT NULL, legacy INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(question_id, round));
"""


@contextmanager
def db():
    DATA.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=10, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    try:
        yield c
    finally:
        c.close()


def init():
    for d in (ENTRIES, UPLOADS):
        d.mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.executescript(SCHEMA)
        if 'turn_round' not in {r['name'] for r in c.execute('PRAGMA table_info(answers)')}:
            c.execute('ALTER TABLE answers ADD COLUMN turn_round INTEGER')
    reindex()
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        for q in c.execute('SELECT * FROM questions').fetchall():
            c.execute('INSERT OR IGNORE INTO question_turns VALUES(?,?,?,?,?,0)',
                      (q['id'], 0, q['asker'], q['raw_text'], q['created_at']))
            # Old follow-ups only survive as truncated audit entries. Preserve them as such.
            old = c.execute("SELECT * FROM audit_log WHERE target=? AND action='followup' ORDER BY at,rowid",
                            (q['id'],)).fetchall()
            for index, event in enumerate(old, 1):
                if c.execute('SELECT 1 FROM question_turns WHERE question_id=? AND created_at=?',
                             (q['id'], event['at'])).fetchone():
                    continue
                c.execute('INSERT OR IGNORE INTO question_turns VALUES(?,?,?,?,?,1)',
                          (q['id'], index, event['actor'] or '未知历史作者', event['detail'], event['at']))
            sync_question_entry(c, q['id'])
        c.commit()


def new_id():
    return uuid.uuid4().hex[:10]


def audit(c, actor, action, target, detail=""):
    c.execute("INSERT INTO audit_log VALUES(?,?,?,?,?)", (time.time(), actor or "-", action, target or "", str(detail)[:2000]))


# ── 用户：直接只读研发平台的 users 表，不另建账号 ──
def get_user(uid):
    if not uid or len(uid) > 64:
        return None
    try:
        c = sqlite3.connect(f"file:{PLATFORM_DB}?mode=ro", uri=True, timeout=5)
        try:
            r = c.execute("SELECT userid, name, role FROM users WHERE userid=?", (uid,)).fetchone()
        finally:
            c.close()
    except sqlite3.Error:
        return None
    if not r:
        return None
    return {"userid": r[0], "name": r[1], "role": r[2], "is_admin": r[2] in ADMIN_ROLES,
            "scope": "full"}          # P1：所有人 full；P2 在这里按角色映射 ops / dev / full


def visible_scopes(user):
    """L6 查看过滤：唯一出口，网页 / .md / MCP / llms.txt 共用。P1 全员可看 public + internal。"""
    if not user:
        return ("public",)
    if user.get("is_admin"):
        return ("public", "internal", "restricted")
    return ("public", "internal")


def search_platform_bugs(keyword, limit=30):
    """给 worker 的 bug_search 用：只读研发平台库，按标题关键词。"""
    try:
        c = sqlite3.connect(f"file:{PLATFORM_DB}?mode=ro", uri=True, timeout=5)
        try:
            rows = c.execute("SELECT id, status, title FROM bugs WHERE title LIKE ? ORDER BY id DESC LIMIT ?",
                             (f"%{keyword}%", limit)).fetchall()
        finally:
            c.close()
    except sqlite3.Error as e:
        return f"研发平台暂不可读：{e}"
    return "\n".join(f"#{i} [{s}] {t}" for i, s, t in rows) or "(无匹配)"


def asked_today(c, uid):
    start = datetime.datetime.combine(datetime.date.today(), datetime.time()).timestamp()
    return c.execute("SELECT COUNT(*) FROM questions WHERE asker=? AND created_at>=?", (uid, start)).fetchone()[0]


# ── 资料条目：.md + frontmatter ──
_FM = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.S)


def parse_md(text):
    m = _FM.match(text.replace("\r\n", "\n"))
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        v = v.strip()
        if v.startswith("[") or v.startswith("{"):
            try:
                v = json.loads(v)
            except ValueError:
                pass
        meta[k.strip()] = v
    return meta, m.group(2)


def dump_md(meta, body):
    lines = ["---"]
    for k, v in meta.items():
        lines.append(f"{k}: {json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v}")
    return "\n".join(lines) + "\n---\n" + body.strip() + "\n"


def next_entry_id(c):
    n = c.execute("SELECT COALESCE(MAX(CAST(SUBSTR(id,4) AS INTEGER)),0)+1 FROM entries WHERE id LIKE 'KB-%'").fetchone()[0]
    return f"KB-{n:04d}"


def _index_one(c, path):
    meta, body = parse_md(path.read_text(encoding="utf-8"))
    eid = meta.get("id") or path.stem
    tags = meta.get("tags") if isinstance(meta.get("tags"), list) else []
    c.execute("INSERT OR REPLACE INTO entries VALUES(?,?,?,?,?,?,?,?)",
              (eid, meta.get("title") or eid, json.dumps(tags, ensure_ascii=False), meta.get("scope", "public"),
               meta.get("status", "published"), meta.get("source_question"), str(meta.get("updated", "")), path.name))
    c.execute("DELETE FROM entries_fts WHERE id=?", (eid,))
    c.execute("INSERT INTO entries_fts VALUES(?,?,?,?)", (eid, meta.get("title") or "", body, " ".join(tags)))
    return eid


def reindex():
    with db() as c:
        c.execute("DELETE FROM entries")
        c.execute("DELETE FROM entries_fts")
        n = 0
        for p in sorted(ENTRIES.glob("*.md")):
            try:
                _index_one(c, p)
                n += 1
            except (OSError, UnicodeDecodeError):
                continue
    return n


def save_entry(meta, body):
    ENTRIES.mkdir(parents=True, exist_ok=True)
    path = ENTRIES / f"{meta['id']}.md"
    path.write_text(dump_md(meta, body), encoding="utf-8")
    with db() as c:
        _index_one(c, path)
    return path


def thread_messages(c, qid):
    messages = [dict(role='user', author=r['asker'], text=r['text'], at=r['created_at'],
                     round=r['round'], legacy=bool(r['legacy'])) for r in c.execute(
                         'SELECT * FROM question_turns WHERE question_id=? ORDER BY round', (qid,))]
    for row in c.execute('SELECT * FROM answers WHERE question_id=? ORDER BY version', (qid,)):
        messages.append(dict(role='assistant', author='卡序问答助手', text=row['plain_md'],
                             at=row['created_at'], version=row['version'], round=row['turn_round'],
                             needs_review=bool(row['needs_review']), detail=row['detail_md'],
                             evidence=json.loads(row['evidence_json'] or '[]'),
                             mismatches=json.loads(row['mismatches_json'] or '[]'), commit=row['commit_sha']))
    return sorted(messages, key=lambda item: (item['at'], item['role'] == 'assistant'))


def sync_question_entry(c, qid):
    """Archive every thread immediately, including pending/failed/unreviewed answers.

    Internal scope keeps shared team questions out of anonymous search and exports.
    The caller owns the DB transaction; Markdown remains the entry truth source.
    """
    q = c.execute('SELECT * FROM questions WHERE id=?', (qid,)).fetchone()
    if not q:
        return
    # Keep previously curated KB-* Markdown untouched; the thread gets its own entry.
    eid = 'Q-' + qid
    title = q['raw_text'].strip().replace('\n', ' ')[:100]
    body = ['## 问题记录', f"状态：{q['status']}", f"原始提问人：{q['asker'] or '未知历史作者'}",
            '本页自动收录问答记录；收录不代表答案已审核或证据充分。']
    for message in thread_messages(c, qid):
        stamp = datetime.datetime.fromtimestamp(message['at'], datetime.timezone.utc).astimezone(
            datetime.timezone(datetime.timedelta(hours=8))).isoformat(timespec='seconds')
        body.extend([f"## {'提问' if message['role']=='user' else '回答'} · {message['author']} · {stamp}", message['text']])
        if message.get('legacy'):
            body.append('历史追问由旧审计记录恢复，可能截断；原完整内容及轮次关联无法确认。')
        if message.get('needs_review'):
            body.append('此回答需审核：证据不足或命中过滤，请勿视作已确认结论。')
        if message.get('detail'):
            body.extend(['### 依据（专业版）', message['detail']])
        for ev in message.get('evidence', []):
            body.append(f"- `{ev.get('file')}:{ev.get('line')}` {ev.get('quote', '')}")
        for mismatch in message.get('mismatches', []):
            body.append('文案与代码不一致：' + mismatch)
    meta = dict(id=eid, title=title, tags=['问答记录'], scope='internal', status='published',
                source_question=qid, updated=datetime.date.today().isoformat())
    path = ENTRIES / f'{eid}.md'
    tmp = path.with_suffix('.tmp')
    tmp.write_text(dump_md(meta, '\n\n'.join(body)), encoding='utf-8')
    tmp.replace(path)
    _index_one(c, path)
    c.execute('UPDATE questions SET entry_id=? WHERE id=?', (eid, qid))


def _trigrams(q):
    segs = [s for s in re.split(r"[\s,，。？?！!、；;：:“”\"'（）()]+", q) if s]
    grams = []
    for s in segs:
        if len(s) <= 3:
            grams.append(s)
        else:
            grams += [s[i:i + 3] for i in range(len(s) - 2)]
    seen, out = set(), []
    for g in grams:
        if g not in seen:
            seen.add(g)
            out.append(g)
    return out[:40]


def search_entries(q, scopes, limit=10):
    q = (q or "").strip()[:200]
    if not q:
        return []
    marks = ",".join("?" * len(scopes))
    with db() as c:
        grams = [g for g in _trigrams(q) if len(g) >= 3]
        rows = []
        if grams:
            match = " OR ".join('"' + g.replace('"', '""') + '"' for g in grams)
            rows = c.execute(
                f"SELECT e.id, e.title, e.tags, snippet(entries_fts, 2, '', '', '…', 24) AS snip, bm25(entries_fts) AS r "
                f"FROM entries_fts JOIN entries e ON e.id=entries_fts.id "
                f"WHERE entries_fts MATCH ? AND e.status='published' AND e.scope IN ({marks}) ORDER BY r LIMIT ?",
                (match, *scopes, limit)).fetchall()
        if not rows:   # 短词（<3 字）退回 LIKE
            like = f"%{q[:20]}%"
            rows = c.execute(
                f"SELECT e.id, e.title, e.tags, substr(f.body,1,80) AS snip, 0 AS r FROM entries e "
                f"JOIN entries_fts f ON f.id=e.id WHERE (e.title LIKE ? OR f.body LIKE ?) "
                f"AND e.status='published' AND e.scope IN ({marks}) LIMIT ?",
                (like, like, *scopes, limit)).fetchall()
    return [{"id": r["id"], "title": r["title"], "tags": json.loads(r["tags"]), "snippet": r["snip"]} for r in rows]


def get_entry(eid, scopes):
    with db() as c:
        r = c.execute(f"SELECT * FROM entries WHERE id=? AND scope IN ({','.join('?' * len(scopes))})",
                      (eid, *scopes)).fetchone()
    if not r:
        return None
    meta, body = parse_md((ENTRIES / r["path"]).read_text(encoding="utf-8"))
    return {"meta": meta, "body": body, "raw": (ENTRIES / r["path"]).read_text(encoding="utf-8"), **dict(r)}


def list_entries(scopes, tag=None, limit=200):
    with db() as c:
        rows = c.execute(f"SELECT * FROM entries WHERE status='published' AND scope IN ({','.join('?' * len(scopes))}) "
                         f"ORDER BY updated DESC, id DESC LIMIT ?", (*scopes, limit)).fetchall()
    out = [dict(r, tags=json.loads(r["tags"])) for r in rows]
    return [e for e in out if not tag or tag in e["tags"]]
