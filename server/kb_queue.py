"""问题流程 + 任务队列 + worker 接口（长轮询 / 租约 / 心跳 / 回传 / L5 出口过滤）。"""
import asyncio
import datetime
import json
import os
import time

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


def _payload(q, card):
    return {"card": card, "scope": q["scope"],
            "images": [f"/api/worker/upload/{q['id']}/{n}" for n in json.loads(q["images_json"])]}


def create_question(user, text, images=(), source="web", card=None, qid=None):
    """新问题。card 给了（MCP 直接提问）就跳过追问直接作答。"""
    qid = qid or new_id()
    with db() as c:
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
    return qid


def submit_clarify(user, qid, picks):
    """picks: [{q, answer}]。合成问题卡并入队作答。"""
    with db() as c:
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


def follow_up(user, qid, text):
    """「没懂，继续问」：带上一版答案重新作答。"""
    with db() as c:
        q = c.execute("SELECT * FROM questions WHERE id=? AND asker=?", (qid, user["userid"])).fetchone()
        if not q or q["status"] not in ("已答", "证据不足", "已发布", "失败"):
            raise HTTPException(409, "当前状态不能追问")
        prev = c.execute("SELECT plain_md FROM answers WHERE question_id=? ORDER BY version DESC LIMIT 1", (qid,)).fetchone()
        card = json.loads(q["card_json"] or "null") or {"raw_text": q["raw_text"]}
        card["previous_answer"] = prev[0] if prev else ""
        card["followup"] = text[:1000]
        c.execute("UPDATE questions SET card_json=?, status='排队', round=round+1 WHERE id=?",
                  (json.dumps(card, ensure_ascii=False), qid))
        enqueue(c, qid, "answer", _payload(q, card))
        audit(c, user["userid"], "followup", qid, text[:200])


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
    for a in ans:
        for k in ("evidence_json", "dropped_json", "mismatches_json"):
            a[k[:-5]] = json.loads(a.pop(k) or "[]")
    q = dict(q)
    q["card"] = json.loads(q.pop("card_json") or "null")
    q["clarify"] = json.loads(q.pop("clarify_json") or "null")
    q["images"] = json.loads(q.pop("images_json") or "[]")
    return {"q": q, "answers": ans, "queue_ahead": ahead, "worker_online": online}


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
            c.execute("INSERT INTO answers VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (qid, ver, plain, detail, json.dumps(kept, ensure_ascii=False),
                       json.dumps(body.get("dropped", []), ensure_ascii=False), json.dumps(mism, ensure_ascii=False),
                       res.get("confidence"), body.get("commit"), needs_review, time.time()))
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
    return store.search_entries(q, ("public", "internal"), limit=8)


@router.get("/api/worker/bugs/search", response_class=PlainTextResponse)
def worker_bugs_search(q: str, x_kb_worker: str = Header("")):
    _auth(x_kb_worker)
    return store.search_platform_bugs(q[:50])


@router.get("/api/worker/status")
def worker_status():
    with db() as c:
        return {"online": worker_online(c),
                "queued": c.execute("SELECT COUNT(*) FROM jobs WHERE state='queued'").fetchone()[0]}


# ── 发布为资料条目 ──
def publish(user, qid, force=False):
    view = question_view(qid)
    if not view or not view["answers"]:
        raise HTTPException(404, "没有可发布的答案")
    q, a = view["q"], view["answers"][-1]
    if q["entry_id"]:
        return q["entry_id"]
    if q["asker"] != user["userid"] and not user.get("is_admin"):
        raise HTTPException(403, "只有提问人或管理员能发布")
    if (a["needs_review"] or not a["evidence"]) and not (force and user.get("is_admin")):
        raise HTTPException(409, "这条答案需要管理员审核后才能发布")
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
        v = c.execute("SELECT MAX(version) FROM answers WHERE question_id=?", (qid,)).fetchone()[0]
        c.execute("INSERT INTO feedback VALUES(?,?,?,?,?,?)", (qid, v, user["userid"], verdict, note[:1000], time.time()))
        audit(c, user["userid"], "feedback:" + verdict, qid, note[:200])
