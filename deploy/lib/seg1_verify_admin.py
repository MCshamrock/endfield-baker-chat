#!/usr/bin/env python3
"""第 1 段 ⑤⑥ 验收：admin 登录 → /v1/admin/characters 行数/唯一 slug，群会话 members/member_weights。

凭据从 stdin 读两行（user / pass），不落盘、不进 argv。
"""
import json
import sys
import urllib.error
import urllib.request

B = "http://127.0.0.1:5660"
user = sys.stdin.readline().rstrip("\r\n")
pwd = sys.stdin.readline().rstrip("\r\n")
if not user or not pwd:
    # 没给凭据就跳过（退出 0）：full_sync.sh 把它当"可选步骤"，不该因为没喂密码就判失败
    print("  - SKIP admin 两项（未提供凭据：stdin 前两行应为 user / pass）")
    raise SystemExit(0)


def call(method, path, payload=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(B + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


s, dev = call("POST", "/v1/auth/device", {"platform": "seg1-verify", "app_version": "3.2.2"})
tok = json.loads(dev)["token"]
s_login, login = call("POST", "/v1/auth/login", {"username": user, "password": pwd}, tok)
print(f"  login = {s_login} {'' if s_login == 200 else login[:120]}")
if s_login != 200:
    raise SystemExit(1)

s_adm, body = call("GET", "/v1/admin/characters", token=tok)
d = json.loads(body) if body.strip().startswith(("{", "[")) else {}
rows = (d.get("items") or d.get("characters") or []) if isinstance(d, dict) else d
slugs = {r.get("slug") or r.get("character_id") or r.get("id") for r in rows}
print(f"  ⑤ /v1/admin/characters = {s_adm}  rows={len(rows)}  unique_slugs={len(slugs)}")
if rows:
    print(f"     样例：{json.dumps({k: rows[0].get(k) for k in ('slug', 'name', 'active', 'weight')}, ensure_ascii=False)}")

s1, conv = call("POST", "/v1/conversations",
                {"character_id": "perlica", "mode": "group", "members": ["perlica", "chenqianyu"]}, tok)
cj = json.loads(conv) if s1 in (200, 201) else {}
cid = cj.get("conversation_id") or cj.get("id") or cj.get("public_id")
s2, one = call("GET", f"/v1/conversations/{cid}", token=tok)
o = json.loads(one)
members = o.get("members") or []
mw = o.get("member_weights") or []
ok = all(isinstance(x, str) for x in members) and len(members) == len(mw) and len(members) > 0
print(f"  ⑥ conv={s1} get={s2} members={members} member_weights_len={len(mw)} 同长同序={'✓' if ok else '✗'}")
if mw:
    print(f"     权重样例：{json.dumps(mw[0], ensure_ascii=False)}")
raise SystemExit(0 if (s_adm == 200 and len(rows) == 36 and len(slugs) == 36 and ok) else 1)
