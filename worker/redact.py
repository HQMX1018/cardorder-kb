"""L3/L5 内容过滤：把敏感片段替换成 ***。codebox 返回前、worker 回传前、145 入库前各跑一次。

规则是程序强制的，和提示词无关。误伤（如 18 位订单号被当成身份证）宁可多遮，
需要放行的写进 ALLOW_LITERALS。
"""
import re

ALLOW_LITERALS = {
    # 测试账号写在 CLAUDE.md / test-accounts.csv，属公开测试数据，可放行
    *[f"131000001{i:02d}" for i in range(1, 11)],
    *[f"131000002{i:02d}" for i in range(1, 11)],
}

RULES = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), "***PRIVATE_KEY***"),
    ("aliyun_ak", re.compile(r"\bLTAI[0-9A-Za-z]{12,30}\b"), "***AK***"),
    ("jwt", re.compile(r"\beyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}"), "***JWT***"),
    ("url_password", re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^:/\s@]+:)[^@\s]+@"), r"\1***@"),
    ("kv_secret", re.compile(
        r"(?i)\b(password|passwd|pwd|secret|secret_key|token|api_?key|access_?key_?secret|private_?key)\b"
        r"(\s*[:=]\s*)(['\"]?)(?!\*\*\*)[^\s'\",;]{4,}"), r"\1\2\3***"),
    ("id_card", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "***ID***"),
    ("bank_card", re.compile(r"(?<!\d)\d{16,19}(?!\d)"), "***CARD***"),
    ("phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "***PHONE***"),
]


def redact(text: str):
    """返回 (脱敏后文本, 命中规则名列表)。"""
    hits = []

    def _sub(name, pat, rep, s):
        def f(m):
            if m.group(0) in ALLOW_LITERALS:
                return m.group(0)
            hits.append(name)
            return m.expand(rep)
        return pat.sub(f, s)

    for name, pat, rep in RULES:
        text = _sub(name, pat, rep, text)
    return text, hits
