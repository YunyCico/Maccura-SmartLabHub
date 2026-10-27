"""LIS 标准字段设置：可配置的字段字典。

★ 2026-10-26 梦云要的：能设置 LIS 的标准字段（出厂定义在模板的 field_dict 表，
  以前只能看不能改，界面标题还写死「46 个中文字段」）。

规则：
  · 改的是**本机**配置（library/lis/config/lis_fields.json），出厂模板不动
  · 「英文字段名(key)」是数据契约 → 已有的不能改；新字段可以起名
  · 「专业」「组套」由试剂分类表自动回填 → 不能删
  · 删字段后，已保存的映射记录里指向它的项自动改成「不导入」（否则记录打不开）
"""
import json

import pytest

from app.services import lis_service


@pytest.fixture
def fields_env(tmp_path, monkeypatch):
    """把标准字段配置指到临时目录，配置目录也隔离。"""
    cfg = tmp_path / "lis" / "config"
    mappings = tmp_path / "lis" / "mappings"
    cfg.mkdir(parents=True)
    mappings.mkdir(parents=True)
    monkeypatch.setattr(lis_service, "FIELDS_CONFIG", cfg / "lis_fields.json")
    monkeypatch.setattr(lis_service, "CONFIG_DIR", mappings)
    return cfg


def _default_fields():
    return [dict(item) for item in lis_service._template_fields()]


def test_default_reads_template_fields(fields_env):
    """没有自定义配置时，用模板里的出厂定义。"""
    status = lis_service.field_definitions_status()
    assert status["customized"] is False
    assert status["default_count"] == 46
    assert len(status["fields"]) == 46
    labels = {f["key"]: f["label"] for f in status["fields"]}
    assert labels["barcode"] == "标本条码号"
    assert labels["collected_at"] == "采集时间"
    # 类型与分组也带出来（设置界面要显示）
    assert status["types"] == ["文本", "日期时间", "数值", "枚举"]
    assert "F时间戳" in status["groups"]


def test_save_overrides_and_reset_restores(fields_env):
    fields = _default_fields()
    fields[1]["label"] = "标本条码"
    fields[1]["required"] = True
    fields.append({"label": "急诊标志", "key": "emergency_flag", "group": "B患者信息",
                   "required": False, "type": "枚举", "desc": "是否急诊", "source": "急诊标志"})

    result = lis_service.save_field_definitions({"fields": fields})
    assert result["removed"] == []
    status = lis_service.field_definitions_status()
    assert status["customized"] is True
    assert len(status["fields"]) == 47
    by_key = {f["key"]: f for f in status["fields"]}
    assert by_key["barcode"]["label"] == "标本条码"
    assert by_key["barcode"]["required"] is True
    assert by_key["emergency_flag"]["type"] == "枚举"

    # 配置落盘了（不是只在内存）
    assert fields_env.joinpath("lis_fields.json").is_file()

    lis_service.reset_field_definitions()
    after = lis_service.field_definitions_status()
    assert after["customized"] is False
    assert len(after["fields"]) == 46
    assert {f["key"]: f["label"] for f in after["fields"]}["barcode"] == "标本条码号"


def test_cannot_delete_specialty_and_suite(fields_env):
    """专业 / 组套是系统自动回填的，删了整条链路会断。"""
    fields = [f for f in _default_fields() if f["key"] != "test_specialty"]
    with pytest.raises(ValueError, match="不能删除"):
        lis_service.save_field_definitions({"fields": fields})
    fields = [f for f in _default_fields() if f["key"] != "test_suite"]
    with pytest.raises(ValueError, match="不能删除"):
        lis_service.save_field_definitions({"fields": fields})


def test_rejects_bad_key_and_duplicate(fields_env):
    fields = _default_fields()
    bad = [dict(f) for f in fields]
    bad[0]["key"] = "1bad-key"
    with pytest.raises(ValueError, match="不合法"):
        lis_service.save_field_definitions({"fields": bad})
    dup = [dict(f) for f in fields]
    dup[1]["key"] = dup[0]["key"]
    with pytest.raises(ValueError, match="重复"):
        lis_service.save_field_definitions({"fields": dup})


def test_removed_field_detaches_saved_mappings(fields_env):
    """删掉一个字段后，已保存的映射记录里指向它的项要改成「不导入」。"""
    fields = _default_fields()
    record = {
        "batch_id": "abc123",
        "mapping": [
            {"source": "条码号", "target_key": "barcode", "target_label": "标本条码号", "rule": "原值保留"},
            {"source": "姓名", "target_key": "patient_name", "target_label": "患者姓名", "rule": "原值保留"},
        ],
    }
    path = lis_service.CONFIG_DIR / "abc123.json"
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    kept = [f for f in fields if f["key"] != "barcode"]
    result = lis_service.save_field_definitions({"fields": kept})
    assert "barcode" in result["removed"]

    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["mapping"][0]["target_key"] == ""      # 指向被删字段的改成不导入
    assert after["mapping"][0]["rule"] == "不导入"
    assert after["mapping"][1]["target_key"] == "patient_name"   # 其它不动


def test_field_definitions_used_by_mapping_suggestions(fields_env):
    """生效字段要真的被用上：改名后，字段对照的下拉里就是新名字。"""
    fields = _default_fields()
    fields[1]["label"] = "标本条码(新)"
    lis_service.save_field_definitions({"fields": fields})
    labels = [f["label"] for f in lis_service._field_definitions()]
    assert "标本条码(新)" in labels
    assert "标本条码号" not in labels


def test_custom_field_appears_in_standard_output(fields_env):
    """新增字段后，标准化结果的列里要有它。"""
    fields = _default_fields()
    fields.append({"label": "急诊标志", "key": "emergency_flag", "group": "B患者信息",
                   "required": False, "type": "枚举", "desc": "", "source": "急诊标志"})
    lis_service.save_field_definitions({"fields": fields})
    keys = [f["key"] for f in lis_service._field_definitions()]
    assert "emergency_flag" in keys
    assert len(keys) == 47


def test_removed_only_counts_this_save(fields_env):
    """★ 2026-10-27：以前删过的字段，不能每次保存又被算进「本次删除」。

    否则界面提示会一直是「删除 17 个字段」，历史映射也会被反复重写。
    """
    fields = _default_fields()
    kept = fields[:29]                      # 一次性删掉 17 个
    first = lis_service.save_field_definitions({"fields": kept})
    assert len(first["removed"]) == 17

    # 内容没变再保存一次 → 本次没删任何字段
    again = lis_service.save_field_definitions({"fields": kept})
    assert again["removed"] == []

    # 再删一个，本次只算这一个
    third = lis_service.save_field_definitions({"fields": kept[:-1]})
    assert third["removed"] == [kept[-1]["key"]]


def test_can_clear_desc_and_source(fields_env):
    """说明 / 原始数据来源可以清空（不能因为回填旧值而清不掉）。"""
    fields = _default_fields()
    target = next(f for f in fields if f["key"] == "barcode")
    target["desc"] = ""
    target["source"] = ""
    lis_service.save_field_definitions({"fields": fields})
    after = {f["key"]: f for f in lis_service.field_definitions_status()["fields"]}
    assert after["barcode"]["desc"] == ""
    assert after["barcode"]["source"] == ""
