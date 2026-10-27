"""验证 frontdoor 免登在各种 retURL 下的落地效果。"""
import json
import pathlib
import re
import sys

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[1]
CFG = ROOT / "data" / "integrations.json"


def main() -> int:
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    sf = cfg.get("salesforce", {})
    base = str(sf.get("instance_url") or "").rstrip("/")
    sid = str(sf.get("access_token") or "").strip()

    with httpx.Client(timeout=30, follow_redirects=True) as client:
        headers = {"Accept": "application/json", "Authorization": f"Bearer {sid}"}
        # 拿一条真实 Task 记录，测详情页路径
        q = "SELECT Id FROM Task ORDER BY LastModifiedDate DESC LIMIT 1"
        rec = client.get(f"{base}/services/data/v62.0/query", params={"q": q}, headers=headers).json()
        task_id = (rec.get("records") or [{}])[0].get("Id", "")
        print("真实 Task Id =", task_id)

        targets = ["/007/o", "/00T/e", f"/00T{task_id}" if task_id else "/00T/e", "/00U/e", "/a0y/o"]
        for ret in targets:
            resp = client.get(f"{base}/secur/frontdoor.jsp", params={"sid": sid, "retURL": ret})
            m = re.search(r'window\.location\.replace\("([^"]+)"', resp.text)
            jump = m.group(1) if m else "(无 JS 跳转)"
            # startURL 参数里是 base64/编码后的真实目标
            sm = re.search(r"startURL=([^&\"']+)", jump)
            print(f"\nretURL={ret}")
            print("  HTTP:", resp.status_code, " 页面类型:", "登录页" if any(
                k in resp.text for k in ("Log In to Salesforce", "Salesforce login", "验证您的身份")) else "已登录")
            print("  JS 跳转:", jump[:180])
            if sm:
                import urllib.parse
                print("  startURL 解码:", urllib.parse.unquote(sm.group(1))[:200])
            print("  cookie:", [c.name for c in resp.cookies.jar if c.name in ("sid", "sid_Client", "oid", "clientSrc")])
    return 0


if __name__ == "__main__":
    sys.exit(main())
