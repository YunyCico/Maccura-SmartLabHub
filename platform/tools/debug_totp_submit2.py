"""诊断2：对比『POST 到裸 action 路径』vs『POST 到带查询串的页面 URL』，
以及 `&amp;` 是否反转义，判断哪种提交能被 Salesforce 正确识别。

每个变体都重新走一次密码步骤，拿全新的 _CONFIRMATIONTOKEN / vcsrf。
为降低账号锁风险，只跑 2 个变体。
"""
from __future__ import annotations

import os
import re
import sys
import html as htmllib
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


def form_fields(html: str, unescape: bool) -> dict[str, str]:
    fields: dict[str, str] = {}
    for m in re.finditer(r"<input[^>]*>", html, re.I):
        tag = m.group(0)
        name = re.search(r'name="([^"]*)"', tag, re.I)
        if not name:
            continue
        ftype = re.search(r'type="([^"]*)"', tag, re.I)
        if ftype and ftype.group(1).lower() not in ("hidden", "text"):
            continue
        val = re.search(r'value="([^"]*)"', tag, re.I)
        v = val.group(1) if val else ""
        fields[name.group(1)] = htmllib.unescape(v) if unescape else v
    return fields


def strip_text(html: str) -> str:
    t = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    t = re.sub(r"<style.*?</style>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def run_variant(cfg, label: str, post_to_page_url: bool, unescape: bool) -> None:
    base = str(cfg.get("instance_url") or "").rstrip("/")
    username = str(cfg.get("username") or "")
    password = str(cfg.get("password") or "")
    client = httpx.Client(follow_redirects=True, timeout=30, trust_env=False,
                          headers={"User-Agent": "SmartLabHub/1.0"})
    resp = client.post(f"{base}/", data={
        "un": username, "pw": password, "username": username,
        "loginType": "", "useSecure": "true", "login": "",
        "display": "page", "width": "1200", "height": "800", "lt": "standard",
    }, headers={"Referer": f"{base}/"})
    if 'name="tc"' not in resp.text:
        print(f"[{label}] 未进入验证码页 final={resp.url}")
        return
    form = form_fields(resp.text, unescape)
    form["tc"] = "000000"
    form["save"] = " 验证 "
    target = str(resp.url) if post_to_page_url else f"{base}{TOTP_PATH}"
    v = client.post(target, data=form, headers={"Referer": str(resp.url)})
    txt = strip_text(v.text)
    print(f"\n[{label}]")
    print(f"  POST -> {target[:110]}{'...' if len(target)>110 else ''}")
    print(f"  status={v.status_code}  final={str(v.url)[:100]}")
    print(f"  仍含『验证您的身份』: {'验证您的身份' in v.text}")
    print(f"  可见文本: {txt[:220]}")


def main() -> int:
    cfg = integration_config.load_config()["salesforce"]
    run_variant(cfg, "A 裸action路径+不反转义(现状)", post_to_page_url=False, unescape=False)
    run_variant(cfg, "B 带查询串URL+反转义", post_to_page_url=True, unescape=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
