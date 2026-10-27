"""探测 Salesforce 免登跳转链路是否可行（只读，不改任何配置）。

验证三件事：
  1. 配置里的 access_token（sid）现在还有效吗
  2. 这个 sid 属于哪个账号
  3. 干净客户端访问 /secur/frontdoor.jsp?sid=...&retURL=... 能不能直接进已登录页面
"""
import json
import pathlib
import re
import sys

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[1]
CFG = ROOT / "data" / "integrations.json"


def strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html or "")


def main() -> int:
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    sf = cfg.get("salesforce", {})
    base = str(sf.get("instance_url") or "").rstrip("/")
    sid = str(sf.get("access_token") or "").strip()
    print(f"instance_url = {base}")
    print(f"sid 长度 = {len(sid)}")
    if not base or not sid:
        print("配置不完整")
        return 1

    api = f"{base}/services/data/v62.0"

    # 1) sid 还有效吗 + 属于哪个账号
    print("\n--- 1) 用 sid 调 REST ---")
    with httpx.Client(timeout=25, follow_redirects=True) as client:
        headers = {"Accept": "application/json", "Authorization": f"Bearer {sid}"}
        try:
            resp = client.get(f"{api}/query", params={"q": "SELECT Id,Name FROM Organization LIMIT 1"}, headers=headers)
            print("Organization 状态:", resp.status_code, resp.text[:200])
        except Exception as exc:
            print("Organization 请求失败:", exc)

        for path in ("/services/oauth2/userinfo", "/services/data/v62.0/chatter/users/me"):
            try:
                resp = client.get(base + path, headers=headers)
                body = resp.text[:400]
                print(f"{path} -> {resp.status_code} {body}")
            except Exception as exc:
                print(f"{path} 请求失败: {exc}")

    # 2) 干净客户端走 frontdoor（这才是浏览器侧要走的路）
    print("\n--- 2) 干净客户端访问 frontdoor.jsp?sid=... ---")
    ret = "/007/o"
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        url = f"{base}/secur/frontdoor.jsp"
        resp = client.get(url, params={"sid": sid, "retURL": ret})
        print("状态:", resp.status_code)
        print("Location:", str(resp.headers.get("location"))[:220])
        print("Set-Cookie 域名:", [c.split(";")[0][:40] for c in resp.headers.get_list("set-cookie")][:5])
        print("响应体片段:", strip_tags(resp.text)[:200].replace("\n", " "))

    # 3) 跟着重定向走完，看最终是不是已登录页面
    print("\n--- 3) 跟随重定向走完 ---")
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        resp = client.get(f"{base}/secur/frontdoor.jsp", params={"sid": sid, "retURL": ret})
        text = strip_tags(resp.text)
        print("最终状态:", resp.status_code, "最终 URL:", str(resp.url)[:200])
        print("页面标题片段:", re.findall(r"<title>(.*?)</title>", resp.text, re.S)[:1])
        for kw in ("Log In", "登录", "Salesforce login", "验证您的身份", "欢迎", "Welcome", "Salesforce 首页"):
            if kw in resp.text:
                print(f"  命中关键词: {kw}")
        print("是否落到登录页:", any(k in resp.text for k in ("Log In to Salesforce", "Salesforce login", "验证您的身份")))
        print("cookie:", [c.name for c in resp.cookies.jar][:8])
    return 0


if __name__ == "__main__":
    sys.exit(main())
