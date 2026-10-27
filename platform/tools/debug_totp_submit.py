"""诊断：验证 TOTP 提交流程是否正常。

做法：用已存密码走到验证码页，再用「故意错误的码 000000」提交，
观察 Salesforce 返回：
  - 若返回页出现「无效/不正确/incorrect」→ 提交流程正确，问题在码本身（过期/时钟/密钥）。
  - 若原样返回验证码页且无错误提示 → 我们的提交方式有问题（字段/action/按钮值）。
同时解出 _CONFIRMATIONTOKEN 里内嵌的时间戳，排查服务器时钟偏差。
"""
from __future__ import annotations

import base64
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


def form_fields(html: str) -> dict[str, str]:
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
        fields[name.group(1)] = val.group(1) if val else ""
    return fields


def decode_confirmation_token(tok: str) -> str:
    try:
        once = base64.b64decode(tok).decode("utf-8", "replace")
        # 形如 VpE=,<b64>,<b64>
        parts = once.split(",")
        out = [once]
        for p in parts[1:]:
            try:
                out.append(base64.b64decode(p + "===").decode("utf-8", "replace"))
            except Exception:
                pass
        return " | ".join(out)
    except Exception as exc:
        return f"(decode fail: {exc})"


def strip_text(html: str) -> str:
    t = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    t = re.sub(r"<style.*?</style>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def main() -> int:
    cfg = integration_config.load_config()["salesforce"]
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
    html = resp.text
    if 'name="tc"' not in html:
        print("未进入验证码页！final_url=", resp.url)
        print(strip_text(html)[:300])
        return 1

    form = form_fields(html)
    tok = form.get("_CONFIRMATIONTOKEN", "")
    print("=== _CONFIRMATIONTOKEN 解码 ===")
    print(decode_confirmation_token(tok))
    print("\n=== 表单字段名 ===")
    print(sorted(form.keys()))
    print("tc in form:", "tc" in form, " save in form:", "save" in form)

    # 用错误码 000000 提交，按钮值用真实值 " 验证 "
    form["tc"] = "000000"
    form["save"] = " 验证 "
    v = client.post(f"{base}{TOTP_PATH}", data=form, headers={"Referer": str(resp.url)})
    print("\n=== 用 000000 提交后 ===")
    print("status=", v.status_code, " final_url=", v.url)
    print("响应是否仍含『验证您的身份』:", "验证您的身份" in v.text)
    txt = strip_text(v.text)
    print("可见文本(前 600):", txt[:600])
    # 关键词检测
    for kw in ("无效", "不正确", "错误", "过期", "incorrect", "invalid", "expired", "retry", "重试"):
        if kw in v.text or kw in txt:
            print("  >> 命中错误关键词:", kw)
    out = HERE.parent.parent / "logs" / "totp_submit_resp.html"
    out.write_text(v.text, encoding="utf-8")
    print("saved:", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
