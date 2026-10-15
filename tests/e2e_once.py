"""端到端：一道题 → Codex(仅 codebox) → 证据核对 → 脱敏。用法: e2e_once.py <job> <问题卡json文件>"""
import json, sys, time
from pathlib import Path
import run_codex, evidence
from codebox import Box
from redact import redact
job, card = sys.argv[1], json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
rt = Path(__file__).resolve().parent.parent / "runtime"
snap = Path((rt / "snapshots/current_full.txt").read_text(encoding="utf-8").splitlines()[0])
audit = rt / f"audit_{job}.jsonl"; audit.unlink(missing_ok=True)
t0 = time.time()
ans, events = run_codex.run("answer", job, card, snap, "full", audit)
dt = time.time() - t0
box = Box(snap, "full", Path(run_codex.__file__).with_name("scope_profiles.json"), job, rt / "audit_check.jsonl")
kept, dropped = evidence.check(ans, box, audit, job)
plain, h1 = redact(ans["plain"]); detail, h2 = redact(ans["detail"])
calls = [json.loads(l) for l in audit.read_text(encoding="utf-8").splitlines()] if audit.exists() else []
non_codebox = [json.loads(l)["item"] for l in events.splitlines() if l.startswith("{") and '"item"' in l
               and json.loads(l)["item"].get("type") in ("command_execution", "file_change", "web_search")]
(rt / f"events_{job}.jsonl").write_text(events, encoding="utf-8"); (rt / f"result_{job}.json").write_text(json.dumps({"answer": ans, "kept": kept, "dropped": dropped}, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"耗时 {dt:.0f}s  codebox调用 {len(calls)} 次  工具分布 {sorted({c['tool'] for c in calls})}  拒绝 {sum(not c.get('ok') for c in calls)}")
print(f"非codebox的执行/写/联网事件: {len(non_codebox)}  脱敏命中: {h1 + h2}")
print(f"证据 保留 {len(kept)} / 剔除 {len(dropped)}  confidence={ans['confidence']}")
for d in dropped: print("  剔除:", d["file"], d["line"], d["reason"])
print("---plain---\n" + plain[:900]); print("---mismatches---"); [print(" -", m[:200]) for m in ans["mismatches"]]
