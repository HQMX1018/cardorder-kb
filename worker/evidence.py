"""证据核对（取代校对员 agent，纯脚本、无模型）。

每条证据必须同时满足：
  1. 文件在本题 scope 内可见（复用 codebox 的 L2 判断）；
  2. 行号存在，quote 去空白后能在该行 ±3 行内找到；
  3. 本题 codebox 审计日志里真读过：read_file 覆盖该行，或 grep 命中过该行。
"""
import json
import re
from pathlib import Path

_WS = re.compile(r"\s+")


def _norm(s):
    return _WS.sub("", s)


def _seen_lines(audit_path, job):
    reads, greps = {}, set()
    p = Path(audit_path)
    if not p.exists():
        return reads, greps
    for line in p.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        if r.get("job") != job or not r.get("ok"):
            continue
        if r["tool"] == "read_file" and "file" in r:
            reads.setdefault(r["file"], []).append(tuple(r["range"]))
        if r["tool"] == "grep":
            for f, n in r.get("hits", []):
                greps.add((f, n))
    return reads, greps


def check(answer, box, audit_path, job):
    """返回 (保留的证据, 剔除的证据及原因)。box = codebox.Box（同一 snapshot / profile）。"""
    reads, greps = _seen_lines(audit_path, job)
    kept, dropped = [], []
    for ev in answer.get("evidence", []):
        f, n, q = ev.get("file", "").replace("\\", "/").strip("/"), ev.get("line", 0), ev.get("quote", "")
        why = None
        if f not in box.index:
            why = "文件不在可见范围"
        else:
            lines = (box.root / f).read_text(encoding="utf-8", errors="replace").splitlines()
            if not 1 <= n <= len(lines):
                why = "行号不存在"
            elif _norm(q) and _norm(q)[:60] not in _norm("".join(lines[max(0, n - 4): n + 3])):
                why = "引用原文对不上"
            elif not (any(s <= n <= e for s, e in reads.get(f, [])) or (f, n) in greps):
                why = "本题未实际读过该行"
        (dropped if why else kept).append({**ev, **({"reason": why} if why else {})})
    return kept, dropped
