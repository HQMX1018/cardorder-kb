"""本地冒烟：假研发平台库 → 起服务 → 网页流程 + MCP + 越权检查。worker 另行启动。

  python tests/local_smoke.py prepare <data_dir>        # 建假 platform.db、灌种子条目
  python tests/local_smoke.py web <base>                 # 网页流程（不等 worker）
  python tests/local_smoke.py mcp <base>                 # MCP 工具
"""
import asyncio
import http.cookiejar
import json
import shutil
import sqlite3
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ok, bad = [], []


def check(name, cond, extra=""):
    (ok if cond else bad).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))


def prepare(data):
    data = Path(data)
    shutil.rmtree(data, ignore_errors=True)
    (data / "entries").mkdir(parents=True)
    shutil.copy(ROOT / "seed/KB-0001.md", data / "entries/KB-0001.md")
    c = sqlite3.connect(data / "platform.db")
    c.executescript("""CREATE TABLE users(id INTEGER PRIMARY KEY, userid TEXT UNIQUE, name TEXT, role TEXT, phone TEXT, created_at TEXT);
      INSERT INTO users(userid,name,role,created_at) VALUES('h','lh','admin','x'),('test','test','tester','x');
      CREATE TABLE bugs(id INTEGER PRIMARY KEY, title TEXT, status TEXT);
      INSERT INTO bugs VALUES(409,'订单列表发货时限与详情不一致','测试通过');""")
    c.commit()
    print("prepared", data)


class Client:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def req(self, method, path, data=None, headers=None, multipart=None):
        body, hdr = None, dict(headers or {})
        if multipart is not None:
            b = "----kbtest"
            parts = []
            for k, v in multipart.items():
                if isinstance(v, tuple):
                    parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"; filename="{v[0]}"\r\n'
                                 f'Content-Type: {v[2]}\r\n\r\n'.encode() + v[1] + b"\r\n")
                else:
                    parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
            body = b"".join(parts) + f"--{b}--\r\n".encode()
            hdr["Content-Type"] = f"multipart/form-data; boundary={b}"
        elif data is not None:
            body = urllib.parse.urlencode(data).encode()
            hdr["Content-Type"] = "application/x-www-form-urlencoded"
        r = urllib.request.Request(self.base + urllib.parse.quote(path, safe="/?=&%:"), data=body, method=method, headers=hdr)
        try:
            with self.op.open(r, timeout=40) as resp:
                return resp.status, resp.geturl(), resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, path, e.read().decode("utf-8", "replace")


def web(base):
    anon = Client(base)
    s, url, _ = anon.req("GET", "/")
    check("未登录跳登录页", "/login" in url)
    s, _, body = anon.req("GET", "/api/entries/search?q=市场参考价")
    check("匿名只能搜到 public", s == 200 and "KB-0001" in body)
    s, _, body = anon.req("GET", "/llms.txt")
    check("llms.txt 公开", s == 200 and "KB-0001" in body)
    s, _, _ = anon.req("GET", "/api/worker/next?wait=0", headers={"X-KB-Worker": "wrong"})
    check("worker 错误令牌 401", s == 401)

    u = Client(base)
    s, url, body = u.req("POST", "/login", {"userid": "nobody", "next": "/"})
    check("不存在的用户拒绝登录", "没有这个用户名" in body)
    s, url, body = u.req("POST", "/login", {"userid": "test", "next": "/"})
    check("登录成功", s == 200 and "有什么想问的" in body)
    s, _, body = u.req("GET", "/kb?q=参考价")
    check("资料库检索", "KB-0001" in body)
    s, _, body = u.req("GET", "/kb/KB-0001")
    check("条目页渲染", "结论：不会变" in body and "<script" not in body.split("<main>")[1])
    s, _, body = u.req("GET", "/e/KB-0001.md")
    check(".md 下载", body.startswith("---\nid: KB-0001"))
    s, _, body = u.req("GET", "/export.zip")
    check("导出 zip", s == 200)
    s, _, body = u.req("GET", "/admin")
    check("非管理员进不了管理页", s == 403)

    xss = "<script>alert(1)</script> 市场参考价切 90 天会变吗？"
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 100
    s, url, body = u.req("POST", "/ask", multipart={"text": xss, "images": ("a.png", png, "image/png")})
    qid = url.rstrip("/").split("/")[-1]
    check("提问成功并跳到问题页", "/q/" in url, url)
    check("留言里的 HTML 被转义", "<script>alert(1)</script>" not in body and "&lt;script&gt;" in body)
    s, _, body = u.req("POST", "/ask", multipart={"text": "x", "images": ("a.png", b"GIF89a....", "image/png")})
    check("伪装成 png 的非图片被拒", s == 415)
    s, _, body = u.req("GET", f"/upload/{qid}/1.png")
    check("提问人能看自己的截图", s == 200)
    other = Client(base)
    other.req("POST", "/login", {"userid": "h", "next": "/"})
    s, _, _ = Client(base).req("GET", f"/upload/{qid}/1.png")
    check("匿名看不到截图", s in (401, 303) or "/login" in _)
    print(json.dumps({"qid": qid}))
    return qid


def clarify_and_wait(base, qid):
    import time
    u = Client(base)
    u.req("POST", "/login", {"userid": "test", "next": "/"})
    deadline = time.time() + 120
    while time.time() < deadline:
        st = json.loads(u.req("GET", f"/api/q/{qid}/state")[2])["status"]
        if st == "追问中":
            break
        time.sleep(2)
    s, _, body = u.req("GET", f"/q/{qid}")
    check("出现追问选择题", "帮我们把问题问清楚" in body, st)
    s, url, body = u.req("POST", f"/q/{qid}/clarify", {"pick_0": "商品详情·市场数据", "pick_1": "和别的数有什么区别", "other_2": "我自己看懂"})
    check("提交追问后排队", "排队" in body or "正在查项目" in body)
    deadline = time.time() + 900
    while time.time() < deadline:
        st = json.loads(u.req("GET", f"/api/q/{qid}/state")[2])["status"]
        if st not in ("排队", "作答中"):
            break
        time.sleep(5)
    s, _, body = u.req("GET", f"/q/{qid}")
    check("已答", st == "已答", st)
    check("答案页有证据", "条证据" in body)
    s, url, body = u.req("POST", f"/q/{qid}/feedback", {"verdict": "useful"})
    check("点有用后收入资料库", "已收入资料库" in body, url)


async def _mcp(base):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    async with streamablehttp_client(base.rstrip("/") + "/mcp/", headers={"X-KB-Uid": "test"}) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = sorted(t.name for t in (await s.list_tools()).tools)
            check("MCP 8 个工具", tools == ["kb_ask", "kb_feedback", "kb_followup", "kb_get", "kb_onboarding", "kb_publish", "kb_search", "kb_status"], tools)
            res = await s.call_tool("kb_search", {"query": "市场参考价"})
            check("MCP kb_search", any("KB-0001" in getattr(item, 'text', '') for item in res.content))
            res = await s.call_tool("kb_get", {"entry_id": "KB-0001"})
            check("MCP kb_get", "id: KB-0001" in res.content[0].text)
    async with streamablehttp_client(base.rstrip("/") + "/mcp/") as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("kb_ask", {"question": "x"})
            check("MCP 无身份不能提问", res.isError)


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "prepare":
        prepare(sys.argv[2])
    elif cmd == "web":
        web(sys.argv[2])
    elif cmd == "flow":
        clarify_and_wait(sys.argv[2], sys.argv[3])
    elif cmd == "mcp":
        asyncio.run(_mcp(sys.argv[2]))
    print(f"\n通过 {len(ok)}  失败 {len(bad)} {bad}")
