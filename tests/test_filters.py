"""过滤器验收：路径越界 / 禁读 / 脱敏 / 配额 / profile 收窄。"""
import sys as _s; from pathlib import Path as _P; _s.path.insert(0, str(_P(__file__).resolve().parent.parent / "worker"))
import sys, json
from pathlib import Path
from codebox import Box, LIMITS
from redact import redact
snap = Path(sys.argv[1]); audit = Path(sys.argv[2]); audit.unlink(missing_ok=True)
prof = str(Path(__file__).resolve().parent.parent / "worker" / "scope_profiles.json")
b = Box(snap, "full", prof, "T1", audit)
ok = 0; fail = []
def expect(name, cond):
    global ok
    if cond: ok += 1
    else: fail.append(name)
for bad in ["../../x", r"..\..\Windows\win.ini", r"\Windows\win.ini", "C:/Windows/win.ini", "/etc/passwd", "backend/.env", "backend/.env.example",
            "flutter/android/key.jks", ".claude/settings.json"]:
    try: b.resolve(bad); expect("resolve " + bad, False)
    except ValueError: expect(bad, True)
expect("index has dart", any(f.endswith(".dart") for f in b.index))
expect("index no env", not any(f.rsplit("/",1)[-1].startswith(".env") for f in b.index))
t, h = redact("password=abc12345 LTAIabcdefghijklmnop 13912345678 13100000201 110101199003071234 postgres://u:pw@h/db")
print(t, h)
expect("redact", "abc12345" not in t and "LTAI" not in t and "13912345678" not in t and "13100000201" in t and "pw@" not in t)
r = b.guard("read_file", {}, lambda: ("x" * 10, {}))
expect("guard ok", r == "x" * 10)
b.calls = LIMITS["max_calls"]
expect("quota", b.guard("read_file", {}, lambda: ("y", {})).startswith("[拒绝]"))
ops = Box(snap, "ops", prof, "T2", audit)
expect("ops narrow", all(f.startswith(("docs/", "flutter/lib/l10n/")) or f == "CLAUDE.md" for f in ops.index))
try: ops.resolve("backend/app/services/market.py"); expect("ops deny backend", False)
except ValueError: expect("ops deny backend", True)
print(f"PASS {ok}  FAIL {fail}  index_full={len(b.index)} index_ops={len(ops.index)}")
print("audit lines:", len(audit.read_text(encoding='utf-8').splitlines()))
