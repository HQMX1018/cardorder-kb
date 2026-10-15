"""网页：登录 / 提问 / 追问 / 答案 / 资料库 / 我的 / 帮助 / 管理。L0 入口过滤在这里。"""
import html
import io
import json
import re
import zipfile
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

import kb_queue
import store
from store import db

router = APIRouter()
T = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))
MAX_TEXT = 2000
MAX_IMAGES = 3
MAX_IMAGE_BYTES = 5 * 1024 * 1024
PUBLIC = ("/login", "/static", "/mcp", "/llms.txt", "/api/worker", "/favicon.ico", "/help")


def md_lite(text):
    """极简 Markdown → HTML：先全量转义，再认 ##/###、- 列表、`code`、**粗体**。"""
    out, in_list = [], False
    for raw in (text or "").splitlines():
        line = html.escape(raw.rstrip())
        line = re.sub(r"`([^`]+)`", r"<code>\1</code>", line)
        line = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", line)
        if re.match(r"^\s*[-*] ", line):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append("<li>" + re.sub(r"^\s*[-*] ", "", line) + "</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if line.startswith("### "):
            out.append(f"<h4>{line[4:]}</h4>")
        elif line.startswith("## "):
            out.append(f"<h3>{line[3:]}</h3>")
        elif line.strip():
            out.append(f"<p>{line}</p>")
    if in_list:
        out.append("</ul>")
    return Markup("\n".join(out))      # 每行已 html.escape，可安全标记


T.env.filters["md"] = md_lite


def current_user(request: Request):
    return store.get_user(request.cookies.get("uid"))


def need_user(request: Request):
    u = current_user(request)
    if not u:
        raise HTTPException(401, "未登录")
    return u


def page(request, name, **ctx):
    with db() as c:
        online = kb_queue.worker_online(c)
    return T.TemplateResponse(request, name, {"user": current_user(request), "worker_online": online, **ctx})


# ── 登录（与研发平台共用 cookie uid） ──
@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    return page(request, "login.html", next=next if next.startswith("/") else "/", error="")


@router.post("/login")
def login(request: Request, userid: str = Form(...), next: str = Form("/")):
    u = store.get_user(userid.strip())
    if not u:
        return page(request, "login.html", next=next, error="没有这个用户名（和研发平台的用户名一致）")
    r = RedirectResponse(next if next.startswith("/") else "/", status_code=303)
    r.set_cookie("uid", u["userid"], max_age=180 * 86400, httponly=True, samesite="lax")
    return r


@router.get("/logout")
def logout():
    r = RedirectResponse("/login", status_code=303)
    r.delete_cookie("uid")
    return r


# ── 提问 ──
@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    u = current_user(request)
    return page(request, "home.html", recent=store.list_entries(store.visible_scopes(u), limit=8))


def _check_image(data: bytes):
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(413, "单张图片不能超过 5MB")
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    raise HTTPException(415, "只支持 png / jpg 截图")


@router.post("/ask")
async def ask(request: Request, text: str = Form(...), images: List[UploadFile] = File(default=[])):
    u = need_user(request)
    text = text.strip()
    if not text:
        raise HTTPException(400, "问题不能为空")
    if len(text) > MAX_TEXT:
        raise HTTPException(413, f"问题不能超过 {MAX_TEXT} 字")
    files = [f for f in images if f and f.filename]
    if len(files) > MAX_IMAGES:
        raise HTTPException(413, f"最多 {MAX_IMAGES} 张截图")
    with db() as c:
        if store.asked_today(c, u["userid"]) >= store.DAILY_LIMIT:
            raise HTTPException(429, f"今天已经问了 {store.DAILY_LIMIT} 个问题，明天再来")
    qid = store.new_id()
    names = []
    for i, f in enumerate(files, 1):
        data = await f.read()
        ext = _check_image(data)
        d = store.UPLOADS / qid
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{i}.{ext}").write_bytes(data)
        names.append(f"{i}.{ext}")
    kb_queue.create_question(u, text, names, qid=qid)
    return RedirectResponse(f"/q/{qid}", status_code=303)


@router.get("/q/{qid}", response_class=HTMLResponse)
def question(request: Request, qid: str):
    u = need_user(request)
    v = kb_queue.question_view(qid)
    if not v or (v["q"]["asker"] != u["userid"] and not u["is_admin"]):
        raise HTTPException(404, "没有这个问题")
    hit = store.get_entry(v["q"]["hit_entry"], store.visible_scopes(u)) if v["q"]["hit_entry"] else None
    return page(request, "question.html", v=v, hit=hit, pending=kb_queue.PENDING)


@router.get("/upload/{qid}/{name}")
def upload(request: Request, qid: str, name: str):
    u = need_user(request)
    v = kb_queue.question_view(qid)
    if not v or (v["q"]["asker"] != u["userid"] and not u["is_admin"]) or name not in v["q"]["images"]:
        raise HTTPException(404)
    return FileResponse(store.UPLOADS / qid / name)


@router.get("/api/q/{qid}/state")
def question_state(request: Request, qid: str):
    need_user(request)
    v = kb_queue.question_view(qid)
    if not v:
        raise HTTPException(404)
    return {"status": v["q"]["status"], "answers": len(v["answers"]), "ahead": v["queue_ahead"],
            "online": v["worker_online"]}


@router.post("/q/{qid}/clarify")
async def clarify(request: Request, qid: str):
    u = need_user(request)
    form = await request.form()
    v = kb_queue.question_view(qid)
    qs = ((v or {}).get("q", {}).get("clarify") or {}).get("questions", [])
    picks = []
    for i, item in enumerate(qs):
        ans = (form.get(f"other_{i}") or "").strip()[:300] or (form.get(f"pick_{i}") or "").strip()[:300]
        picks.append({"q": item["q"], "answer": ans})
    kb_queue.submit_clarify(u, qid, picks)
    return RedirectResponse(f"/q/{qid}", status_code=303)


@router.post("/q/{qid}/skip")
def skip(request: Request, qid: str):
    kb_queue.submit_clarify(need_user(request), qid, [])
    return RedirectResponse(f"/q/{qid}", status_code=303)


@router.post("/q/{qid}/feedback")
def feedback(request: Request, qid: str, verdict: str = Form(...), note: str = Form("")):
    u = need_user(request)
    if verdict not in ("useful", "unclear", "wrong"):
        raise HTTPException(400)
    kb_queue.record_feedback(u, qid, verdict, note)
    if verdict == "unclear" and note.strip():
        kb_queue.follow_up(u, qid, note.strip())
    elif verdict == "useful":
        try:
            kb_queue.publish(u, qid)
        except HTTPException:
            pass                              # 需审核的不自动发布，页面会提示
    return RedirectResponse(f"/q/{qid}", status_code=303)


@router.post("/q/{qid}/publish")
def publish(request: Request, qid: str):
    eid = kb_queue.publish(need_user(request), qid, force=True)
    return RedirectResponse(f"/kb/{eid}", status_code=303)


@router.get("/mine", response_class=HTMLResponse)
def mine(request: Request):
    u = need_user(request)
    with db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id, raw_text, status, created_at, entry_id FROM questions WHERE asker=? ORDER BY created_at DESC LIMIT 100",
            (u["userid"],))]
    return page(request, "mine.html", rows=rows)


# ── 资料库 ──
@router.get("/api/entries/search")
def entries_search(request: Request, q: str = ""):
    return store.search_entries(q, store.visible_scopes(current_user(request)), limit=6)


@router.get("/kb", response_class=HTMLResponse)
def kb_list(request: Request, q: str = "", tag: str = ""):
    scopes = store.visible_scopes(current_user(request))
    items = store.search_entries(q, scopes, limit=50) if q else store.list_entries(scopes, tag or None)
    tags = sorted({t for e in store.list_entries(scopes) for t in e["tags"]})
    return page(request, "kb_list.html", items=items, q=q, tag=tag, tags=tags)


@router.get("/kb/{eid}", response_class=HTMLResponse)
def kb_entry(request: Request, eid: str):
    e = store.get_entry(eid, store.visible_scopes(current_user(request)))
    if not e:
        raise HTTPException(404, "没有这篇资料")
    return page(request, "entry.html", e=e)


@router.get("/e/{eid}.md")
def entry_md(request: Request, eid: str):
    e = store.get_entry(eid, store.visible_scopes(current_user(request)))
    if not e:
        raise HTTPException(404)
    return Response(e["raw"], media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{eid}.md"'})


@router.get("/export.zip")
def export_zip(request: Request):
    scopes = store.visible_scopes(current_user(request))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for e in store.list_entries(scopes, limit=100000):
            z.write(store.ENTRIES / e["path"], e["path"])
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="cardorder-kb.zip"'})


@router.get("/llms.txt", response_class=PlainTextResponse)
def llms(request: Request):
    base = str(request.base_url).rstrip("/")
    lines = ["# 卡序问答库", "", "> 卡序（CardOrder）项目资料库。每篇为 Markdown，含小白版解释与代码证据。",
             f"> MCP：{base}/mcp/ （请求头 X-KB-Uid: <研发平台 userid>）", "", "## 资料"]
    for e in store.list_entries(("public",), limit=100000):
        lines.append(f"- [{e['title']}]({base}/e/{e['id']}.md)")
    return "\n".join(lines) + "\n"


@router.get("/help", response_class=HTMLResponse)
def help_page(request: Request):
    return page(request, "help.html")


# ── 管理 ──
@router.get("/admin", response_class=HTMLResponse)
def admin(request: Request, msg: str = ""):
    u = need_user(request)
    if not u["is_admin"]:
        raise HTTPException(403)
    with db() as c:
        review = [dict(r) for r in c.execute(
            "SELECT q.id, q.raw_text, q.asker, a.version, a.confidence FROM questions q JOIN answers a ON a.question_id=q.id "
            "WHERE a.needs_review=1 AND q.entry_id IS NULL AND a.version=(SELECT MAX(version) FROM answers WHERE question_id=q.id) "
            "ORDER BY a.created_at DESC LIMIT 50")]
        logs = [dict(r) for r in c.execute("SELECT * FROM audit_log ORDER BY at DESC LIMIT 100")]
        jobs = [dict(r) for r in c.execute("SELECT id, question_id, kind, state, attempts, worker, error, created_at "
                                           "FROM jobs ORDER BY created_at DESC LIMIT 30")]
    return page(request, "admin.html", review=review, logs=logs, jobs=jobs, msg=msg)


@router.post("/admin/import")
async def admin_import(request: Request, files: List[UploadFile] = File(...)):
    u = need_user(request)
    if not u["is_admin"]:
        raise HTTPException(403)
    n = 0
    for f in files:
        name = Path(f.filename or "").name
        if not re.fullmatch(r"[A-Za-z0-9_\-]+\.md", name):
            continue
        data = (await f.read()).decode("utf-8", errors="replace")
        meta, body = store.parse_md(data)
        with db() as c:
            meta.setdefault("id", store.next_entry_id(c))
        meta.setdefault("title", name[:-3])
        meta.setdefault("status", "published")
        meta.setdefault("scope", "public")
        store.save_entry(meta, body)
        n += 1
    with db() as c:
        store.audit(c, u["userid"], "import", "", f"{n} files")
    return RedirectResponse(f"/admin?msg=导入 {n} 篇", status_code=303)


@router.post("/admin/reindex")
def admin_reindex(request: Request):
    u = need_user(request)
    if not u["is_admin"]:
        raise HTTPException(403)
    n = store.reindex()
    return RedirectResponse(f"/admin?msg=已重建索引 {n} 篇", status_code=303)
