"""调试：用已存密码走到 Salesforce TOTP 验证页，抓取页面 HTML 与表单结构。

用途：搞清楚动态口令提交表单的 action / 字段名 / 按钮值，避免瞎猜。
不需要 6 位动态码——只做「密码」这一步，拿到验证码页即可。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

HERE = Path(__file__).resolve()
BACKEND_DIR = HERE.parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import httpx  # noqa: E402
from app.services import integration_config  # noqa: E402

TOTP_PATH = "/_ui/identity/verification/method/TotpVerificationUi/e"


def main() -> int:
    cfg = integration_config.load_config()["salesforce"]
    base = str(cfg.get("instance_url") or "").rstrip("/")
    username = str(cfg.get("username") or "")
    password = str(cfg.get("password") or "")
    print(f"base={base}  username={username}  password_len={len(password)}")

    client = httpx.Client(
        follow_redirects=True,
        timeout=30,
        trust_env=False,
        headers={"User-Agent": "SmartLabHub/1.0"},
    )
    resp = client.post(
        f"{base}/",
        data={
            "un": username,
            "pw": password,
            "username": username,
            "loginType": "",
            "useSecure": "true",
            "login": "",
            "display": "page",
            "width": "1200",
            "height": "800",
            "lt": "standard",
        },
        headers={"Referer": f"{base}/"},
    )
    html = resp.text
    print(f"status={resp.status_code}  final_url={resp.url}")
    print(f"has_tc_input={'name=\"tc\"' in html}  has_verify_text={'验证您的身份' in html}")

    out = HERE.parent.parent / "logs" / "totp_page.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"saved html -> {out}  (len={len(html)})")

    print("\n===== <form ...> 标签 =====")
    for m in re.finditer(r"<form[^>]*>", html, re.I | re.S):
        print(m.group(0))

    print("\n===== 所有 <input>（name / type / value）=====")
    for m in re.finditer(r"<input[^>]*>", html, re.I):
        tag = m.group(0)
        name = re.search(r'name="([^"]*)"', tag, re.I)
        typ = re.search(r'type="([^"]*)"', tag, re.I)
        val = re.search(r'value="([^"]*)"', tag, re.I)
        print(
            f"  name={name.group(1) if name else None!r:30} "
            f"type={(typ.group(1) if typ else 'text'):10} "
            f"value={val.group(1) if val else ''!r}"
        )

    print("\n===== <button> / submit =====")
    for m in re.finditer(r"<button[^>]*>.*?</button>", html, re.I | re.S):
        snippet = re.sub(r"\s+", " ", m.group(0))[:200]
        print("  ", snippet)
    for m in re.finditer(r"<input[^>]*type=\"submit\"[^>]*>", html, re.I):
        print("  ", re.sub(r"\s+", " ", m.group(0))[:200])

    # 页面上可能的错误提示
    print("\n===== 可见文本片段（前 500 字）=====")
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    print(text[:500])
    return 0


if __name__ == "__main__":
    sys.exit(main())
