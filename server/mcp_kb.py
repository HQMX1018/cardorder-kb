"""MCP（streamable-http，挂在 /mcp/）。身份：请求头 X-KB-Uid = 研发平台 userid。"""
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

import kb_queue
import store

mcp = FastMCP("cardorder-kb", stateless_http=True,
              transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
mcp.settings.streamable_http_path = "/"


def _user(ctx: Context):
    try:
        uid = ctx.request_context.request.headers.get("x-kb-uid")
    except Exception:
        uid = None
    return store.get_user(uid)


def _need(ctx):
    u = _user(ctx)
    if not u:
        raise ValueError("需要身份：MCP 连接请带请求头 X-KB-Uid: <研发平台 userid>")
    return u


@mcp.tool()
def kb_search(query: str, ctx: Context) -> list:
    """检索卡序问答库已发布资料，返回 id / 标题 / 标签 / 摘要。"""
    return store.search_entries(query, store.visible_scopes(_user(ctx)), limit=10)


@mcp.tool()
def kb_get(entry_id: str, ctx: Context) -> str:
    """取一篇资料的 Markdown 全文（含小白版与代码证据）。"""
    e = store.get_entry(entry_id, store.visible_scopes(_user(ctx)))
    return e["raw"] if e else "没有这篇资料"


@mcp.tool()
def kb_ask(question: str, ctx: Context, page: str = "", intent: str = "", audience: str = "") -> dict:
    """提交一个问题（跳过网页追问，直接排队作答）。page=涉及的页面/功能，intent=想知道什么，audience=给谁看。
    返回 question_id，用 kb_status 取答案（一般 1–3 分钟）。"""
    u = _need(ctx)
    with store.db() as c:
        if store.asked_today(c, u["userid"]) >= store.DAILY_LIMIT:
            raise ValueError("今天的提问次数已用完")
    q = question.strip()[:2000]
    if not q:
        raise ValueError("问题不能为空")
    qid = kb_queue.create_question(u, q, source="mcp",
                                   card={"page": page[:100], "intent": intent[:100], "audience": audience[:100], "summary": q[:200]})
    return {"question_id": qid, "url": f"/q/{qid}"}


@mcp.tool()
def kb_status(question_id: str, ctx: Context) -> dict:
    """查问题进度；已答时返回小白版、专业版、证据与不一致项。"""
    u = _need(ctx)
    v = kb_queue.question_view(question_id)
    if not v or (v["q"]["asker"] != u["userid"] and not u["is_admin"]):
        raise ValueError("没有这个问题")
    out = {"status": v["q"]["status"], "queue_ahead": v["queue_ahead"], "worker_online": v["worker_online"],
           "entry_id": v["q"]["entry_id"]}
    if v["answers"]:
        a = v["answers"][-1]
        out.update(plain=a["plain_md"], detail=a["detail_md"], evidence=a["evidence"], mismatches=a["mismatches"],
                   confidence=a["confidence"], needs_review=bool(a["needs_review"]), commit=a["commit_sha"])
    return out


@mcp.tool()
def kb_feedback(question_id: str, verdict: str, ctx: Context, note: str = "") -> str:
    """反馈：verdict = useful | unclear | wrong。unclear 带 note 时会按 note 继续追问。"""
    u = _need(ctx)
    if verdict not in ("useful", "unclear", "wrong"):
        raise ValueError("verdict 只能是 useful / unclear / wrong")
    kb_queue.record_feedback(u, question_id, verdict, note)
    if verdict == "unclear" and note.strip():
        kb_queue.follow_up(u, question_id, note.strip())
        return "已按你的补充重新排队作答"
    return "已记录"


@mcp.tool()
def kb_publish(question_id: str, ctx: Context) -> str:
    """把已答问题发布为资料条目（提问人或管理员；需审核的答案只有管理员能发）。"""
    u = _need(ctx)
    return kb_queue.publish(u, question_id, force=u["is_admin"])
