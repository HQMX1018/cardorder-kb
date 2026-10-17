"""问题流程 + 任务队列 + worker 接口（推送 / 长轮询 / 租约 / 心跳 / 回传 / L5 出口过滤）。"""
import asyncio
import datetime
import json
import os
import threading
import time
import urllib.error
import urllib.request

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse

import store
from redact import redact
from store import audit, db, new_id

WORKER_TOKEN = os.environ.get("KB_WORKER_TOKEN", "")
LEASE = {"clarify": 120, "answer": 900}
MAX_ATTEMPTS = 3
OFFLINE_AFTER = 90
PENDING = ("追问生成中", "排队", "作答中")
router = APIRouter()

# 本机离线时的模板追问（不需要模型）
TEMPLATE_CLARIFY = {
    "hit_entry_id": "",
    "template": True,
    "questions": [
        {"q": "你在哪个页面或功能看到的？", "options": ["商品详情·市场数据", "首页 / 行情", "订单 / 发货", "发布挂单 / 卖家中心"], "allow_other": True},
        {"q": "你想知道什么？", "options": ["它是怎么算的", "和别的数有什么区别", "是不是 bug", "该怎么操作"], "allow_other": True},
        {"q": "解释给谁看？", "options": ["我自己看懂就行", "要转给客户或运营", "要给开发定口径"], "allow_other": True},
    ],
    "draft_card": {"page": "", "intent": "", "audience": "", "summary": ""},
}


# ── 流程 ──
def worker_online(c):
    last = c.execute("SELECT MAX(last_seen) FROM workers").fetchone()[0] or 0
    return time.time() - last < OFFLINE_AFTER


def enqueue(c, qid, kind, payload):
    c.execute("INSERT INTO jobs(id,question_id,kind,priority,state,payload,created_at) VALUES(?,?,?,?,?,?,?)",
              (new_id(), qid, kind, 0 if kind == "clarify" else 1, "queued",
               json.dumps(payload, ensure_ascii=False), time.time()))
    WAKE.set()                        # 连接是自动提交，此刻推送线程已能看到这条任务


def _payload(q, card):
    return {"card": card, "scope": q["scope"],
            "images": [f"/api/worker/upload/{q['id']}/{n}" for n in json.loads(q["images_json"])]}


def create_question(user, text, images=(), source="web", card=None, qid=None):
    """新问题。card 给了（MCP 直接提问）就跳过追问直接作答。"""
    qid = qid or new_id()
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("INSERT INTO questions(id,asker,raw_text,images_json,scope,status,created_at,source) VALUES(?,?,?,?,?,?,?,?)",
                  (qid, user["userid"], text, json.dumps(list(images)), user["scope"], "排队", time.time(), source))
        q = c.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
        if card is not None:
            card = {"raw_text": text, **card}
            c.execute("UPDATE questions SET card_json=? WHERE id=?", (json.dumps(card, ensure_ascii=False), qid))
            enqueue(c, qid, "answer", _payload(q, card))
        elif worker_online(c):
            c.execute("UPDATE questions SET status='追问生成中' WHERE id=?", (qid,))
            enqueue(c, qid, "clarify", _payload(q, {"raw_text": text}))
        else:
            c.execute("UPDATE questions SET status='追问中', clarify_json=? WHERE id=?",
                      (json.dumps(TEMPLATE_CLARIFY, ensure_ascii=False), qid))
        audit(c, user["userid"], "ask", qid, text[:200])
        c.execute('INSERT INTO question_turns VALUES(?,?,?,?,?,0)',
                  (qid, 0, user['userid'], text, q['created_at']))
        store.sync_question_entry(c, qid)
        c.commit()
    WAKE.set()
    return qid


def submit_clarify(user, qid, picks):
    """picks: [{q, answer}]。合成问题卡并入队作答。"""
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        q = c.execute("SELECT * FROM questions WHERE id=? AND asker=?", (qid, user["userid"])).fetchone()
        if not q or q["status"] not in ("追问中", "命中资料", "追问生成中"):
            raise HTTPException(409, "当前状态不能提交")
        cl = json.loads(q["clarify_json"] or "{}")
        card = {"raw_text": q["raw_text"], **(cl.get("draft_card") or {}),
                "clarify": [p for p in picks if p.get("answer")]}
        c.execute("UPDATE questions SET card_json=?, status='排队', round=round+1 WHERE id=?",
                  (json.dumps(card, ensure_ascii=False), qid))
        c.execute("UPDATE jobs SET state='failed', error='skipped' WHERE question_id=? AND kind='clarify' AND state='queued'", (qid,))
        enqueue(c, qid, "answer", _payload(q, card))
        audit(c, user["userid"], "clarify", qid, json.dumps(picks, ensure_ascii=False)[:500])
        store.sync_question_entry(c, qid)
        c.commit()
    WAKE.set()


def follow_up(user, qid, text):
    """Any logged-in member can continue the same thread, preserving all authors."""
    text = text.strip()
    if not text or len(text) > 1000:
        raise HTTPException(400, "追问须为1至1000字")
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        q = c.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
        if not q or q["status"] not in ("已答", "证据不足", "已发布", "失败", "追问中", "命中资料"):
            raise HTTPException(409, "当前状态不能追问")
        prev = c.execute("SELECT plain_md FROM answers WHERE question_id=? ORDER BY version DESC LIMIT 1", (qid,)).fetchone()
        card = json.loads(q["card_json"] or "null") or {"raw_text": q["raw_text"]}
        card["previous_answer"] = prev[0] if prev else ""
        card["followup"] = text
        now = time.time()
        turn_round = max(q['round'], c.execute(
            'SELECT COALESCE(MAX(round),0) FROM question_turns WHERE question_id=?', (qid,)).fetchone()[0]) + 1
        c.execute('INSERT INTO question_turns VALUES(?,?,?,?,?,0)',
                  (qid, turn_round, user['userid'], text, now))
        card['thread'] = store.thread_messages(c, qid)
        card['followup_author'] = user['userid']
        c.execute("UPDATE questions SET card_json=?, status='排队', round=? WHERE id=?",
                  (json.dumps(card, ensure_ascii=False), turn_round, qid))
        c.execute("UPDATE jobs SET state='failed', error='shared follow-up' WHERE question_id=? AND kind='clarify' AND state='queued'", (qid,))
        enqueue(c, qid, "answer", {**_payload(q, card), 'turn_round': turn_round})
        audit(c, user["userid"], "followup_recorded", qid, text)
        store.sync_question_entry(c, qid)
        c.commit()
    WAKE.set()


CLARIFY_WAIT = 30   # 追问任务排队超过这么久还没被领走（或作答电脑离线）→ 换模板题，不让用户干等


def _clarify_fallback(c, q):
    if q["status"] != "追问生成中":
        return q
    job = c.execute("SELECT id, created_at FROM jobs WHERE question_id=? AND kind='clarify' AND state='queued'",
                    (q["id"],)).fetchone()
    if job and (not worker_online(c) or time.time() - job["created_at"] > CLARIFY_WAIT):
        c.execute("UPDATE jobs SET state='failed', error='clarify fallback: not picked up' WHERE id=?", (job["id"],))
        c.execute("UPDATE questions SET status='追问中', clarify_json=? WHERE id=?",
                  (json.dumps(TEMPLATE_CLARIFY, ensure_ascii=False), q["id"]))
        q = c.execute("SELECT * FROM questions WHERE id=?", (q["id"],)).fetchone()
    return q


def question_view(qid):
    with db() as c:
        q = c.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
        if not q:
            return None
        q = _clarify_fallback(c, q)
        ans = [dict(r) for r in c.execute("SELECT * FROM answers WHERE question_id=? ORDER BY version", (qid,))]
        mine = c.execute("SELECT MAX(created_at) FROM jobs WHERE question_id=? AND state='queued'", (qid,)).fetchone()[0]
        ahead = c.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','leased') AND created_at<?",
                          (mine,)).fetchone()[0] if mine else 0
        online = worker_online(c)
        messages = store.thread_messages(c, qid)
    for a in ans:
        for k in ("evidence_json", "dropped_json", "mismatches_json"):
            a[k[:-5]] = json.loads(a.pop(k) or "[]")
    q = dict(q)
    q["card"] = json.loads(q.pop("card_json") or "null")
    q["clarify"] = json.loads(q.pop("clarify_json") or "null")
    q["images"] = json.loads(q.pop("images_json") or "[]")
    return {"q": q, "answers": ans, "messages": messages, "queue_ahead": ahead, "worker_online": online}


# ── worker 接口 ──
def _auth(token):
    if not WORKER_TOKEN or token != WORKER_TOKEN:
        raise HTTPException(401, "bad worker token")


def _on_fail(c, qid, kind):
    if kind == "clarify":   # 追问失败 → 退回模板题，用户不受影响
        c.execute("UPDATE questions SET status='追问中', clarify_json=? WHERE id=? AND status='追问生成中'",
                  (json.dumps(TEMPLATE_CLARIFY, ensure_ascii=False), qid))
    else:
        c.execute("UPDATE questions SET status='失败' WHERE id=?", (qid,))
    store.sync_question_entry(c, qid)


def _reap(c):
    now = time.time()
    for r in c.execute("SELECT id, question_id, kind FROM jobs WHERE state='leased' AND lease_until<? AND attempts>=?",
                       (now, MAX_ATTEMPTS)).fetchall():
        c.execute("UPDATE jobs SET state='failed', error='lease expired' WHERE id=?", (r["id"],))
        _on_fail(c, r["question_id"], r["kind"])
    c.execute("UPDATE jobs SET state='queued', worker=NULL WHERE state='leased' AND lease_until<?", (now,))


def _claim(c, worker):
    now = time.time()
    row = c.execute(
        "UPDATE jobs SET state='leased', worker=?, attempts=attempts+1, "
        "lease_until=? + CASE kind WHEN 'clarify' THEN ? ELSE ? END "
        "WHERE id=(SELECT id FROM jobs WHERE state='queued' ORDER BY priority, created_at LIMIT 1) "
        "RETURNING id, question_id, kind, payload, attempts",
        (worker, now, LEASE["clarify"], LEASE["answer"])).fetchone()
    if row and row["kind"] == "answer":
        c.execute("UPDATE questions SET status='作答中' WHERE id=?", (row["question_id"],))
        store.sync_question_entry(c, row['question_id'])
    return dict(row) if row else None


@router.get("/api/worker/next")
async def worker_next(wait: int = 25, worker: str = "w1", x_kb_worker: str = Header("")):
    """长轮询：最多挂 25 秒，期间有任务立即返回；无任务 204。"""
    _auth(x_kb_worker)
    deadline = time.time() + min(max(wait, 0), 25)
    while True:
        with db() as c:
            c.execute("INSERT INTO workers VALUES(?,?) ON CONFLICT(name) DO UPDATE SET last_seen=excluded.last_seen",
                      (worker[:40], time.time()))
            _reap(c)
            job = _claim(c, worker[:40])
        if job:
            job["payload"] = json.loads(job["payload"])
            return job
        if time.time() >= deadline:
            return Response(status_code=204)
        await asyncio.sleep(1)


@router.post("/api/worker/heartbeat/{job_id}")
def worker_heartbeat(job_id: str, worker: str = "w1", x_kb_worker: str = Header("")):
    _auth(x_kb_worker)
    with db() as c:
        r = c.execute("SELECT kind FROM jobs WHERE id=? AND state='leased' AND worker=?", (job_id, worker)).fetchone()
        if not r:
            raise HTTPException(409, "lease lost")
        c.execute("UPDATE jobs SET lease_until=? WHERE id=?", (time.time() + LEASE[r[0]], job_id))
        c.execute("UPDATE workers SET last_seen=? WHERE name=?", (time.time(), worker))
    return {"ok": True}


@router.post("/api/worker/result/{job_id}")
async def worker_result(job_id: str, request: Request, worker: str = "w1", x_kb_worker: str = Header("")):
    _auth(x_kb_worker)
    body = await request.json()
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        job = c.execute("SELECT * FROM jobs WHERE id=? AND state='leased' AND worker=?", (job_id, worker)).fetchone()
        if not job:
            raise HTTPException(409, "lease lost")
        qid = job["question_id"]
        if body.get("error"):
            failed = job["attempts"] >= MAX_ATTEMPTS
            c.execute("UPDATE jobs SET state=?, error=?, worker=NULL WHERE id=?",
                      ("failed" if failed else "queued", str(body["error"])[:2000], job_id))
            if failed:
                _on_fail(c, qid, job["kind"])
            audit(c, worker, "error:" + job["kind"], qid, body["error"])
            c.commit()
            WAKE.set()
            return {"ok": True}
        res = body["result"]
        if job["kind"] == "answer":
            plain, h1 = redact(res.get("plain", ""))                       # L5（145 侧再过一遍）
            detail, h2 = redact(res.get("detail", ""))
            kept = body.get("kept", [])
            for ev in kept:
                ev["quote"], _ = redact(ev.get("quote", ""))
            mism = [redact(m)[0] for m in res.get("mismatches", [])]
            needs_review = int(bool(h1 or h2) or not kept)
            ver = c.execute("SELECT COALESCE(MAX(version),0)+1 FROM answers WHERE question_id=?", (qid,)).fetchone()[0]
            c.execute("INSERT INTO answers(question_id,version,plain_md,detail_md,evidence_json,dropped_json,"
                      "mismatches_json,confidence,commit_sha,needs_review,created_at,turn_round) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      (qid, ver, plain, detail, json.dumps(kept, ensure_ascii=False),
                       json.dumps(body.get("dropped", []), ensure_ascii=False), json.dumps(mism, ensure_ascii=False),
                       res.get("confidence"), body.get("commit"), needs_review, time.time(),
                       json.loads(job['payload']).get('turn_round', 0)))
            c.execute("UPDATE questions SET status=? WHERE id=?", ("已答" if kept else "证据不足", qid))
        else:
            q = c.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
            hit = (res.get("hit_entry_id") or "").strip()
            if q["status"] != "追问生成中":
                pass                                  # 用户已跳过追问，结果作废
            elif hit and c.execute("SELECT 1 FROM entries WHERE id=? AND status='published'", (hit,)).fetchone():
                c.execute("UPDATE questions SET status='命中资料', hit_entry=?, clarify_json=? WHERE id=?",
                          (hit, json.dumps(res, ensure_ascii=False), qid))
            elif not res.get("questions"):            # 留言已经够清楚 → 直接作答
                card = {"raw_text": q["raw_text"], **(res.get("draft_card") or {})}
                c.execute("UPDATE questions SET status='排队', card_json=?, clarify_json=? WHERE id=?",
                          (json.dumps(card, ensure_ascii=False), json.dumps(res, ensure_ascii=False), qid))
                enqueue(c, qid, "answer", _payload(q, card))
            else:
                c.execute("UPDATE questions SET status='追问中', clarify_json=? WHERE id=?",
                          (json.dumps(res, ensure_ascii=False), qid))
        c.execute("UPDATE jobs SET state='done', result=? WHERE id=?", (json.dumps(res, ensure_ascii=False), job_id))
        audit(c, worker, "result:" + job["kind"], qid, json.dumps(body.get("dropped", []), ensure_ascii=False)[:500])
        store.sync_question_entry(c, qid)
        c.commit()
    WAKE.set()                        # 本机空出一个作答位，排着的题可以推了
    return {"ok": True}


@router.get("/api/worker/upload/{qid}/{name}")
def worker_upload(qid: str, name: str, x_kb_worker: str = Header("")):
    _auth(x_kb_worker)
    p = (store.UPLOADS / qid / name).resolve()
    if store.UPLOADS.resolve() not in p.parents or not p.is_file():
        raise HTTPException(404)
    return FileResponse(p)


@router.get("/api/worker/entries/search")
def worker_entries_search(q: str, x_kb_worker: str = Header("")):
    _auth(x_kb_worker)
    # Pending/unreviewed question logs are discoverable to people, not evidence for the AI.
    return [item for item in store.search_entries(q, ("public", "internal"), limit=50)
            if not item['id'].startswith('Q-')][:8]


@router.get("/api/worker/bugs/search", response_class=PlainTextResponse)
def worker_bugs_search(q: str, x_kb_worker: str = Header("")):
    _auth(x_kb_worker)
    return store.search_platform_bugs(q[:50])


@router.get("/api/worker/status")
def worker_status():
    with db() as c:
        return {"online": worker_online(c), "push": bool(PUSH.get("port")) and time.time() - PUSH_OK[0] < OFFLINE_AFTER,
                "queued": c.execute("SELECT COUNT(*) FROM jobs WHERE state='queued'").fetchone()[0]}


# ── 推送：留言入队即经 SSH 反向隧道推给本机 worker ──
# 本机 worker 常驻 `ssh -R 127.0.0.1:0:127.0.0.1:8091 cardxu@145`，sshd 在 145 回环口分配端口，
# worker 拿到端口后 POST /api/worker/register 登记；之后 145 只往自己的 127.0.0.1:<port> 推，
# 任务经隧道直达本机。145 不需要能访问本机、本机也不开入站端口。
PUSH_FILE = store.DATA / "push_target.json"     # 登记持久化：145 服务重启后不用等本机重登记
PUSH_SWEEP = 15                                 # 无事件时的巡检间隔：探活 + 补推（租约过期回队的题）
PUSH_BUSY_RETRY = 3                             # 本机作答位满时的重试间隔
WAKE = threading.Event()
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 推的是本机回环口，绝不走代理
PUSH_OK = [0.0]                                 # 最近一次推送 / 探活成功的时间
try:
    PUSH = json.loads(PUSH_FILE.read_text(encoding="utf-8"))
except (OSError, ValueError):
    PUSH = {}


def _push_call(method, path, body=None, port=None, timeout=5):
    port = port or PUSH.get("port")
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                 headers={"X-KB-Worker": WORKER_TOKEN, "Content-Type": "application/json"},
                                 data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, None


def _alive(worker):
    PUSH_OK[0] = time.time()
    with db() as c:                   # 沿用 workers.last_seen：worker_online / 模板追问兜底的判断不变
        c.execute("INSERT INTO workers VALUES(?,?) ON CONFLICT(name) DO UPDATE SET last_seen=excluded.last_seen",
                  (worker, time.time()))


def _unclaim(job_id):
    """推送没送达：任务原样退回队列，不消耗重试次数。"""
    with db() as c:
        r = c.execute("UPDATE jobs SET state='queued', worker=NULL, attempts=attempts-1 WHERE id=? AND state='leased' "
                      "RETURNING question_id, kind", (job_id,)).fetchone()
        if r and r["kind"] == "answer":
            c.execute("UPDATE questions SET status='排队' WHERE id=? AND status='作答中'", (r["question_id"],))


def _dispatch_once():
    """推空队列或推到本机满为止。返回下次巡检前要等的秒数。"""
    worker = PUSH.get("worker")
    if not PUSH.get("port"):
        return PUSH_SWEEP
    try:
        status, _ = _push_call("GET", "/ping")
    except (urllib.error.URLError, OSError):
        return PUSH_SWEEP             # 隧道不通：本机关机/断网/重连中，题留在队列等它重新登记
    if status != 200:
        return PUSH_SWEEP
    _alive(worker)
    while True:
        with db() as c:
            _reap(c)
            job = _claim(c, worker)
        if not job:
            return PUSH_SWEEP
        job["payload"] = json.loads(job["payload"])
        try:
            status, _ = _push_call("POST", "/job", job)
        except (urllib.error.URLError, OSError):
            _unclaim(job["id"])
            return PUSH_SWEEP
        if status == 503:             # 本机两个作答位都在忙
            _unclaim(job["id"])
            return PUSH_BUSY_RETRY
        if status != 202:
            _unclaim(job["id"])
            return PUSH_SWEEP
        _alive(worker)


def _dispatcher():
    wait = 0
    while True:
        WAKE.wait(wait)
        WAKE.clear()
        try:
            wait = _dispatch_once()
        except Exception as e:        # 推送线程不能死，死了就退化成没人派题
            print("push dispatcher error:", type(e).__name__, e, flush=True)
            wait = PUSH_SWEEP


def start_dispatcher():
    threading.Thread(target=_dispatcher, name="kb-push", daemon=True).start()


@router.post("/api/worker/register")
def worker_register(port: int, worker: str = "w1", x_kb_worker: str = Header("")):
    """本机隧道建好后登记端口；先经隧道回探一次，确认真能推到才生效。"""
    _auth(x_kb_worker)
    if not 1024 <= port <= 65535:
        raise HTTPException(400, "bad port")
    try:
        status, info = _push_call("GET", "/ping", port=port)
    except (urllib.error.URLError, OSError) as e:
        raise HTTPException(502, f"隧道回探失败：{e}")
    if status != 200:
        raise HTTPException(502, f"隧道回探失败：HTTP {status}")
    PUSH.clear()
    PUSH.update(port=port, worker=worker[:40], at=time.time())
    tmp = PUSH_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(PUSH), encoding="utf-8")
    tmp.replace(PUSH_FILE)
    _alive(PUSH["worker"])
    WAKE.set()                        # 本机离线期间排着的题立刻补推
    return {"ok": True, "port": port}


# ── 发布为资料条目 ──
def publish(user, qid, force=False):
    view = question_view(qid)
    if not view or not view["answers"]:
        raise HTTPException(404, "没有可发布的答案")
    q, a = view["q"], view["answers"][-1]
    if q["asker"] != user["userid"] and not user.get("is_admin"):
        raise HTTPException(403, "只有提问人或管理员能发布")
    if (a["needs_review"] or not a["evidence"]) and not (force and user.get("is_admin")):
        raise HTTPException(409, "这条答案需要管理员审核后才能发布")
    if q['entry_id']:
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            if force and user.get('is_admin'):
                c.execute('UPDATE answers SET needs_review=0 WHERE question_id=? AND version=?', (qid, a['version']))
            store.sync_question_entry(c, qid)
            audit(c, user['userid'], 'review_archive', qid)
            c.commit()
        return q['entry_id']
    card = q["card"] or {}
    title = (card.get("summary") or q["raw_text"]).strip().replace("\n", " ")[:60]
    tags = [t for t in [card.get("page", "")] if t]
    ev_lines = "\n".join(f"- `{e['file']}:{e['line']}` {e['quote']}" for e in a["evidence"])
    mism = "\n".join(f"- {m}" for m in a["mismatches"])
    body = f"{a['plain_md']}\n\n## 依据（专业版）\n\n{a['detail_md']}\n\n### 证据\n\n{ev_lines}\n"
    if mism:
        body += f"\n### 文案与代码不一致\n\n{mism}\n"
    with db() as c:
        eid = store.next_entry_id(c)
    meta = {"id": eid, "title": title, "tags": tags, "scope": "public", "audience": "小白",
            "source_question": qid, "source_commit": a["commit_sha"] or "",
            "evidence": [f"{e['file']}:{e['line']}" for e in a["evidence"]],
            "status": "published", "updated": datetime.date.today().isoformat()}
    store.save_entry(meta, body)
    with db() as c:
        c.execute("UPDATE questions SET entry_id=?, status='已发布' WHERE id=?", (eid, qid))
        audit(c, user["userid"], "publish", eid, qid)
    return eid


def record_feedback(user, qid, verdict, note=""):
    with db() as c:
        if not c.execute('SELECT 1 FROM questions WHERE id=?', (qid,)).fetchone():
            raise HTTPException(404, '没有这个问题')
        v = c.execute("SELECT MAX(version) FROM answers WHERE question_id=?", (qid,)).fetchone()[0]
        c.execute("INSERT INTO feedback VALUES(?,?,?,?,?,?)", (qid, v, user["userid"], verdict, note[:1000], time.time()))
        audit(c, user["userid"], "feedback:" + verdict, qid, note[:200])
