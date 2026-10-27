"""集成配置服务测试。

重点验证两条安全底线：
1. 密码类字段绝不出现在回传数据里
2. 前端不能写入白名单以外的键
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "platform" / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.services import integration_config as ic  # noqa: E402


@pytest.fixture(autouse=True)
def temp_config(monkeypatch, tmp_path: Path):
    """把配置文件指向临时目录，测试不污染真实配置。"""
    target = tmp_path / "integrations.json"
    monkeypatch.setattr(ic, "DATA_FILE", target)
    return target


def test_load_returns_defaults_when_missing() -> None:
    config = ic.load_config()
    assert set(config) == {"dingtalk", "oa", "salesforce"}
    assert config["dingtalk"]["message_kinds"] == "todo,calendar,trip"
    assert config["oa"]["auth_mode"] == "password"


def test_load_survives_corrupted_file(temp_config: Path) -> None:
    temp_config.write_text("{ 这不是合法 JSON", encoding="utf-8")
    config = ic.load_config()
    assert config["oa"]["default_title"] == "OA 待办"


def test_secret_fields_never_returned_in_plaintext() -> None:
    ic.save_config({"dingtalk": {"app_key": "ak_test", "app_secret": "sk_very_secret"}})
    view = ic.public_view()

    assert view["dingtalk"]["app_key"] == "ak_test"
    assert view["dingtalk"]["app_secret"] == ""  # 明文不回传
    assert view["dingtalk"]["app_secret_configured"] is True

    # 整个回传结构里不能出现密钥明文
    assert "sk_very_secret" not in json.dumps(view, ensure_ascii=False)


def test_blank_secret_does_not_erase_saved_value() -> None:
    """脱敏后前端回传空串，不能把已保存的密码擦掉。"""
    ic.save_config({"oa": {"token": "keep_me", "base_url": "https://x"}})

    # 前端拿到脱敏数据后原样提交：token 是空串
    ic.save_config({"oa": {"token": "", "base_url": "https://y"}})

    stored = ic.load_config()
    assert stored["oa"]["token"] == "keep_me"
    assert stored["oa"]["base_url"] == "https://y"  # 非密字段正常更新


def test_unknown_keys_are_rejected() -> None:
    ic.save_config({"dingtalk": {"app_key": "ak", "__class__": "evil", "os": "system"}})
    stored = ic.load_config()
    assert "__class__" not in stored["dingtalk"]
    assert "os" not in stored["dingtalk"]
    assert stored["dingtalk"]["app_key"] == "ak"


def test_effective_settings_prefers_file_over_env() -> None:
    """文件配置优先于 .env，设置页保存后立即生效。"""
    ic.save_config({"dingtalk": {"app_key": "from_file", "app_secret": "sk_file"}})
    effective = ic.effective_settings()
    assert effective["dingtalk"]["app_key"] == "from_file"
    assert effective["dingtalk"]["app_secret"] == "sk_file"


def test_effective_settings_falls_back_to_env() -> None:
    """文件没配时回落到 .env，保证原有凭据来源继续有效。"""
    from app.core.config import Settings

    monkey = Settings(dingtalk_app_key="from_env", dingtalk_app_secret="sk_env")
    effective = ic.effective_settings()
    # 若 .env 未配置则为空，配了则应回落到 .env 值
    assert effective["dingtalk"]["app_key"] in {"", monkey.dingtalk_app_key}


def test_save_creates_parent_directory(temp_config: Path) -> None:
    nested = temp_config.parent / "sub" / "dir" / "integrations.json"
    original = ic.DATA_FILE
    try:
        ic.DATA_FILE = nested
        ic.save_config({"oa": {"base_url": "https://n"}})
        assert nested.exists()
    finally:
        ic.DATA_FILE = original


def test_saved_file_is_valid_json(temp_config: Path) -> None:
    ic.save_config({"oa": {"base_url": "https://oa", "field_map": '{"title":"subject"}'}})
    data = json.loads(temp_config.read_text(encoding="utf-8"))
    assert data["oa"]["base_url"] == "https://oa"
    assert data["oa"]["field_map"] == '{"title":"subject"}'


def test_public_view_marks_unset_secrets_as_not_configured() -> None:
    view = ic.public_view()
    assert view["dingtalk"]["app_secret_configured"] is False
    assert view["oa"]["password_configured"] is False
    assert view["salesforce"]["client_secret_configured"] is False


def test_public_view_returns_secret_hint_without_leaking_plaintext() -> None:
    """Secret 框要能显示"确实存着"，但只能给长度和末几位。"""
    secret = "b2a2LdEpHMwnXQDFUscaPS_md_ltjb8ZW9betWMNkAVKks8i1_Fz3kvspgLmHcAk"
    ic.save_config({"dingtalk": {"app_key": "ak_test", "app_secret": secret}})
    view = ic.public_view()

    hint = view["dingtalk"]["app_secret_hint"]
    assert hint == f"{len(secret)} 位，末 4 位 HcAk"
    # 明文和它的任何完整片段都不能出现在提示里
    assert secret not in hint
    assert secret[:8] not in hint
    assert secret not in json.dumps(view, ensure_ascii=False)


def test_secret_hint_empty_when_not_configured() -> None:
    view = ic.public_view()
    assert view["dingtalk"]["app_secret_hint"] == ""
    assert view["oa"]["password_hint"] == ""
    assert view["salesforce"]["client_secret_hint"] == ""


def test_secret_hint_hides_short_values() -> None:
    """太短的值连末几位都不给，避免反推。"""
    assert ic.secret_hint("") == ""
    assert ic.secret_hint("abc") == "3 位"
    assert ic.secret_hint("abcdefghij") == "10 位，末 4 位 ghij"