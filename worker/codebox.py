"""codebox：研究员（Codex）读代码的唯一通道。只读 + 过滤器（L2 路径 / L3 内容 / 配额 / 审计）。

权限全部来自启动参数，模型只能传 path / pattern，改不了范围：
  python codebox.py --root <快照目录> --profile full --profiles scope_profiles.json
                    --job <job_id> --audit <审计 jsonl> [--tools all|clarify]
"""
import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from redact import redact

LIMITS = dict(max_calls=120, max_bytes=2_000_000, read_lines=400, file_bytes=300_000,
              list_max=500, find_max=200, grep_max=50, regex_len=200, grep_timeout=12)  # 全仓一次约 3.3s
# 只连问答库一个地址（由 worker 经 MCP env 注入，模型改不了）；bug 单也由问答库代查研发平台只读库
KB_BASE = os.environ.get("KB_BASE", "http://192.168.31.145:8090").rstrip("/")
KB_TOKEN = os.environ.get("KB_WORKER_TOKEN", "")


def _kb_get(path, params):
    req = urllib.request.Request(f"{KB_BASE}{path}?" + urllib.parse.urlencode(params), headers={"X-KB-Worker": KB_TOKEN})
    with urllib.request.urlopen(req, timeout=8) as r:
        return r.read().decode("utf-8")
DENIED = "不存在或无权查看"                  # 不存在与无权限同一句话，防止探测


def _glob_hit(rel: str, patterns) -> bool:
    name = rel.rsplit("/", 1)[-1]
    for p in patterns:
        if fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(name, p):
            return True
        if p.endswith("/*") and (rel + "/").startswith(p[:-1]):
            return True
    return False


class Box:
    def __init__(self, root, profile, profiles_file, job, audit):
        cfg = json.loads(Path(profiles_file).read_text(encoding="utf-8"))
        prof = cfg["profiles"][profile]
        self.root = Path(root).resolve()
        self.allow = prof["allow"]
        self.deny = prof.get("deny", []) + cfg["global_deny"]
        self.text_ext = tuple(cfg["text_ext"])
        self.job, self.audit = job, Path(audit)
        self.calls = 0
        self.bytes = 0
        self._index = None

    @property
    def index(self):
        """首次用到才建（扫 8000+ 文件要数秒）。启动时建会拖慢 MCP 握手：
        后台任务计划里实测 6.7s，Codex 等不及就丢掉 codebox，模型零调用答「未查到」。"""
        if self._index is None:
            self._index = self._build_index()
        return self._index

    # ── L2 路径过滤 ──
    def visible(self, rel: str) -> bool:
        parts = rel.split("/")
        for i in range(1, len(parts) + 1):  # 任何一级目录命中禁止规则，整棵子树不可见
            if _glob_hit("/".join(parts[:i]), self.deny):
                return False
        return _glob_hit(rel, self.allow)

    def resolve(self, rel):
        if not isinstance(rel, str) or len(rel) > 300:
            raise ValueError(DENIED)
        if rel.strip() not in ("", ".", "/") and rel.strip()[:1] in ("/", "\\"):  # 绝对路径
            raise ValueError(DENIED)
        rel = rel.replace("\\", "/").strip().strip("/") if rel.strip() not in ("", ".", "/") else ""
        if re.match(r"^[A-Za-z]:", rel) or any(p == ".." for p in rel.split("/")):
            raise ValueError(DENIED)
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:  # 符号链接逃逸
            raise ValueError(DENIED)
        r = p.relative_to(self.root).as_posix() if p != self.root else ""
        if r and not self.visible(r):
            raise ValueError(DENIED)
        return p, r

    def _build_index(self):
        out = []
        for p in self.root.rglob("*"):
            if p.is_file():
                r = p.relative_to(self.root).as_posix()
                if self.visible(r) and r.endswith(self.text_ext) and p.stat().st_size <= LIMITS["file_bytes"]:
                    out.append(r)
        return sorted(out)

    # ── 配额 + 审计 + L3 内容过滤 ──
    def guard(self, tool, args, fn):
        self.calls += 1
        rec = {"t": time.time(), "job": self.job, "tool": tool, "args": args}
        try:
            if self.calls > LIMITS["max_calls"] or self.bytes > LIMITS["max_bytes"]:
                raise ValueError("本题读取配额已用完，请基于已读内容作答")
            out, meta = fn()
            out, hits = redact(out)
            self.bytes += len(out.encode("utf-8"))
            rec.update(ok=True, bytes=len(out), redact=hits, **meta)
            return out
        except ValueError as e:
            rec.update(ok=False, err=str(e))
            return f"[拒绝] {e}"
        except Exception as e:  # 任何意外都记审计、给模型一句话，不把堆栈（可能含路径）漏出去
            rec.update(ok=False, err=f"{type(e).__name__}: {e}"[:300])
            return "[拒绝] 工具内部错误，请换个方式查"
        finally:
            with self.audit.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _grep_child():
    """子进程里跑正则，父进程按超时 kill，防止灾难性回溯卡死。"""
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows 管道默认 GBK，中文 / emoji 会炸
    req = json.loads(sys.stdin.read())
    pat = re.compile(req["pattern"], 0 if req["case"] else re.I)
    hits = []
    for rel in req["files"]:
        try:
            lines = Path(req["root"], rel).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            if pat.search(line):
                hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                if len(hits) >= req["max"]:
                    print("\n".join(hits))
                    return
    print("\n".join(hits))


def main():
    if "--grep-child" in sys.argv:
        return _grep_child()
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--profiles", required=True)
    ap.add_argument("--job", required=True)
    ap.add_argument("--audit", required=True)
    ap.add_argument("--tools", default="all", choices=["all", "clarify"])
    ap.add_argument("--kbconf", default="")
    a = ap.parse_args()
    global KB_BASE, KB_TOKEN
    if a.kbconf and Path(a.kbconf).exists():
        conf = json.loads(Path(a.kbconf).read_text(encoding="utf-8"))
        KB_BASE, KB_TOKEN = (conf.get("base") or KB_BASE).rstrip("/"), conf.get("token") or KB_TOKEN
    box = Box(a.root, a.profile, a.profiles, a.job, a.audit)

    from mcp.server.fastmcp import FastMCP
    m = FastMCP("codebox")

    def kb_search(query: str) -> str:
        """检索问答库已发布条目，返回 id / 标题 / 摘要。"""
        def run():
            try:
                return _kb_get("/api/worker/entries/search", {"q": query[:100]})[:20000], {}
            except Exception as e:  # 145 不可达时不影响作答
                return f"资料库暂不可用：{type(e).__name__}", {}
        return box.guard("kb_search", {"query": query}, run)

    m.tool()(kb_search)
    if a.tools == "clarify":  # 追问官只有这一个工具
        return m.run()

    @m.tool()
    def list_dir(path: str = "") -> str:
        """列出目录下可见的子目录和文件（相对项目根的路径）。"""
        def run():
            p, r = box.resolve(path)
            if not p.is_dir():
                raise ValueError(DENIED)
            prefix = (r + "/") if r else ""
            seen = set()
            for f in box.index:
                if f.startswith(prefix):
                    head = f[len(prefix):].split("/", 1)
                    seen.add(head[0] + ("/" if len(head) > 1 else ""))
            items = sorted(seen)[: LIMITS["list_max"]]
            return "\n".join(items) or "(空)", {"path": r}
        return box.guard("list_dir", {"path": path}, run)

    @m.tool()
    def find_files(glob: str) -> str:
        """按文件名通配符找文件，例如 '*market*.dart' 或 'backend/app/services/*.py'。"""
        def run():
            hits = [f for f in box.index if _glob_hit(f, [glob])][: LIMITS["find_max"]]
            return "\n".join(hits) or "(无匹配)", {}
        return box.guard("find_files", {"glob": glob}, run)

    @m.tool()
    def grep(pattern: str, path_glob: str = "*", case_sensitive: bool = False) -> str:
        """全文搜索（正则），返回 文件:行号: 片段，最多 50 条。path_glob 限定范围。"""
        def run():
            if len(pattern) > LIMITS["regex_len"]:
                raise ValueError("正则过长")
            re.compile(pattern)
            files = [f for f in box.index if _glob_hit(f, [path_glob])]
            req = json.dumps({"root": str(box.root), "files": files, "pattern": pattern,
                              "case": case_sensitive, "max": LIMITS["grep_max"]})
            try:
                cp = subprocess.run([sys.executable, __file__, "--grep-child"], input=req,
                                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    timeout=LIMITS["grep_timeout"])
            except subprocess.TimeoutExpired:
                raise ValueError("搜索超时，请换更具体的关键词")
            out = cp.stdout.strip()
            hits = [h.split(":", 2)[:2] for h in out.splitlines()]
            return out or "(无匹配)", {"hits": [[f, int(n)] for f, n in hits if n.isdigit()]}
        return box.guard("grep", {"pattern": pattern, "path_glob": path_glob}, run)

    @m.tool()
    def read_file(path: str, start_line: int = 1, end_line: int = 200) -> str:
        """按行读文件（含行号），单次最多 400 行。"""
        def run():
            p, r = box.resolve(path)
            if r not in box.index:
                raise ValueError(DENIED)
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            s = max(1, int(start_line))
            e = min(len(lines), int(end_line), s + LIMITS["read_lines"] - 1)
            body = "\n".join(f"{i}\t{lines[i - 1]}" for i in range(s, e + 1))
            return f"{r} 第 {s}-{e} 行 / 共 {len(lines)} 行\n{body}", {"file": r, "range": [s, e]}
        return box.guard("read_file", {"path": path, "start": start_line, "end": end_line}, run)

    @m.tool()
    def bug_search(keyword: str) -> str:
        """按标题关键词查研发平台 bug 单（只读），返回编号 / 状态 / 标题。"""
        def run():
            try:
                return _kb_get("/api/worker/bugs/search", {"q": keyword[:50]})[:20000], {}
            except Exception as e:
                return f"研发平台暂不可用：{type(e).__name__}", {}
        return box.guard("bug_search", {"keyword": keyword}, run)

    m.run()


if __name__ == "__main__":
    main()
