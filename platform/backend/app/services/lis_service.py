from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import csv
import hashlib
import io
import json
import os
import re
import unicodedata
import uuid

import openpyxl
import pandas as pd

from modules.data_analysis import table_format


PROJECT_ROOT = Path(__file__).resolve().parents[4]
TEMPLATE_SOURCE = Path(__file__).resolve().parents[1] / "resources" / "LIS数据标准模板_中文列名.xlsx"
TAXONOMY_SOURCE = Path(__file__).resolve().parents[1] / "resources" / "LIS项目分类表.xlsx"
# ★ LIS 数据归属数据分析模块：modules/data_analysis/data/lis
#   SLH_LIS_DIR 仅供 pytest 隔离使用（见 tests/conftest.py），
#   正常运行时该变量不存在，LIS_DIR 仍指向模块自己的 data/lis。
_LIS_HOME = os.environ.get("SLH_LIS_DIR") or ""
if _LIS_HOME:
    LIS_DIR = Path(_LIS_HOME) / "lis"
else:
    LIS_DIR = PROJECT_ROOT / "modules" / "data_analysis" / "data" / "lis"
STAGING_DIR = LIS_DIR / "staging"
OUTPUT_DIR = LIS_DIR / "standardized"
CONFIG_DIR = LIS_DIR / "mappings"
# ★ 标准字段的自定义配置（出厂定义在 LIS数据标准模板_中文列名.xlsx 的 field_dict 表）。
#   有这个文件就用它覆盖出厂定义 —— 改的是**本机**的标准字段，不动模板原件。
FIELDS_CONFIG = LIS_DIR / "config" / "lis_fields.json"
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
PREVIEW_ROWS = 30
# ★ 标准化结果的内存缓存：session_id → {fingerprint, payload}
#   全量标准化动辄几万行，翻页预览时重复构建太慢；
#   而且保存必须和预览看到的一模一样，所以预览/保存/导出共用 _standard_payload()。
_PAYLOAD_CACHE: dict[str, dict] = {}
_PAYLOAD_CACHE_MAX = 3
# ★ v1.2 起取消必填：源表有的就填，没有的留空，不再拒绝整行。
#   字段级「是否必填」以 field_dict 的必填列为准（现已全为「否」），
#   这里保留空集合只为兼容旧调用签名。
REQUIRED_KEYS: set[str] = set()

# 项目分类：检测项目名称 / 项目英文名称 → 专业 + 组套
SPECIALTY_KEY = "test_specialty"
SUITE_KEY = "test_suite"


FIELD_TYPES = ("文本", "日期时间", "数值", "枚举")


def _template_fields() -> list[dict[str, object]]:
    """出厂的标准字段定义（模板里 field_dict 工作表）。"""
    if not TEMPLATE_SOURCE.is_file():
        raise FileNotFoundError(f"找不到内嵌 LIS 标准模板：{TEMPLATE_SOURCE}")
    wb = openpyxl.load_workbook(TEMPLATE_SOURCE, read_only=True, data_only=True)
    try:
        ws = wb["field_dict"]
        rows = list(ws.iter_rows(min_row=2, values_only=True))
    finally:
        wb.close()
    out: list[dict[str, object]] = []
    for row in rows:
        if not row or not row[0] or not row[1]:
            continue
        out.append({
            "label": str(row[0]), "key": str(row[1]), "group": str(row[2] or ""),
            "required": str(row[3]) == "是", "type": str(row[4] or "文本"),
            "desc": str(row[5] or ""), "source": str(row[6] or ""),
        })
    return out


def _load_fields_config() -> list[dict[str, object]] | None:
    """读本机自定义的标准字段；没有或坏了返回 None（用出厂定义）。"""
    try:
        if not FIELDS_CONFIG.is_file():
            return None
        data = json.loads(FIELDS_CONFIG.read_text(encoding="utf-8"))
        fields = data.get("fields") if isinstance(data, dict) else None
        if not isinstance(fields, list) or not fields:
            return None
        return [dict(item) for item in fields if isinstance(item, dict) and item.get("key")]
    except Exception:
        return None


def _field_definitions() -> list[dict[str, str]]:
    """**生效**的标准字段：优先用本机自定义配置，否则用模板里的出厂定义。

    ★ 字段字典改成可配置后，这里是唯一读取入口 ——
      字段对照审核的下拉、自动映射建议、标准化输出的列，全部跟着它走。
    """
    custom = _load_fields_config()
    if custom is not None:
        return [{
            "label": str(item.get("label") or item.get("key")),
            "key": str(item.get("key")),
            "group": str(item.get("group") or ""),
            "required": bool(item.get("required")),
            "type": str(item.get("type") or "文本"),
            "desc": str(item.get("desc") or ""),
            "source": str(item.get("source") or ""),
        } for item in custom]
    return _template_fields()


def field_definitions_full() -> list[dict[str, object]]:
    """给设置界面用：带说明与来源的完整字段定义。"""
    return [dict(item) for item in _field_definitions()]


def save_field_definitions(payload: dict) -> dict:
    """保存本机的标准字段定义（可改名/改分组/改必填/改类型/新增/删除）。

    ★ key（英文字段名）是数据契约，**不允许改** ——
      已保存的映射记录、标准表列、历史导出都按它对应。
      要换显示名请改「列名(中文)」。
    """
    incoming = payload.get("fields") if isinstance(payload, dict) else None
    if not isinstance(incoming, list) or not incoming:
        raise ValueError("没有要保存的字段")
    # ★ 基线要用「当前生效定义」，不能用出厂模板 ——
    #   否则以前删过的字段每次保存都会被算成「本次删除」，提示数字越滚越大，
    #   还会重复去改历史映射。
    current = {str(item["key"]): item for item in _field_definitions()}
    cleaned: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in incoming:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        label = str(item.get("label") or "").strip()
        if not key or not label:
            continue
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", key):
            raise ValueError(f"英文字段名不合法：{key}（只能用字母开头、字母数字下划线）")
        if key in seen:
            raise ValueError(f"英文字段名重复：{key}")
        seen.add(key)
        field_type = str(item.get("type") or "文本")
        if field_type not in FIELD_TYPES:
            field_type = "文本"
        prev = current.get(key) or {}

        def _text(name: str, fallback: str = "") -> str:
            """传了键就以传入为准（允许清空）；没传才回填旧值。"""
            if name in item and item.get(name) is not None:
                return str(item.get(name))
            return str(prev.get(name) or fallback)

        cleaned.append({
            "label": label, "key": key,
            "group": _text("group") or str(prev.get("group") or "") or "自定义",
            "required": bool(item.get("required")),
            "type": field_type,
            "desc": _text("desc"),
            "source": _text("source"),
        })
    if not cleaned:
        raise ValueError("至少要保留一个字段")
    # 专业 / 组套是系统自动填的，删了整条链路会断
    for protected in (SPECIALTY_KEY, SUITE_KEY):
        if protected not in seen:
            raise ValueError(f"「专业」「组套」是系统自动填的，不能删除（{protected}）")

    removed = [key for key in current if key not in seen]
    FIELDS_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "version": "1.0",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "fields": cleaned,
    }
    tmp = FIELDS_CONFIG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(FIELDS_CONFIG)
    # 删掉的字段：已保存的映射记录里还指着它 → 改成"不导入"，否则记录打不开
    if removed:
        _detach_removed_mappings(removed)
    return {"fields": cleaned, "removed": removed, "updated_at": data["updated_at"]}


def _detach_removed_mappings(removed: list[str]) -> int:
    """字段被删后，把已保存映射里指向它的项改成"不导入"。"""
    if not CONFIG_DIR.is_dir():
        return 0
    changed = 0
    for path in CONFIG_DIR.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        touched = False
        for item in record.get("mapping") or []:
            if isinstance(item, dict) and item.get("target_key") in removed:
                item["target_key"] = ""
                item["target_label"] = ""
                item["rule"] = "不导入"
                touched = True
        if touched:
            try:
                path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
                changed += 1
            except Exception:
                pass
    return changed


def reset_field_definitions() -> dict:
    """恢复出厂标准字段（删掉本机自定义配置）。"""
    if FIELDS_CONFIG.is_file():
        FIELDS_CONFIG.unlink()
    return {"fields": _template_fields(), "updated_at": ""}


def field_definitions_status() -> dict:
    """设置界面用：生效字段 + 是否自定义过 + 出厂数量。"""
    fields = field_definitions_full()
    return {
        "fields": fields,
        "customized": _load_fields_config() is not None,
        "default_count": len(_template_fields()),
        "types": list(FIELD_TYPES),
        "groups": sorted({str(item.get("group") or "") for item in fields if item.get("group")}),
    }


def template() -> dict:
    fields = _field_definitions()
    return {"version": "1.2", "fields": fields, "required_keys": sorted(REQUIRED_KEYS)}


# ---------------------------------------------------------------- 项目分类
_taxonomy_cache: dict[str, dict] | None = None


def _norm_key(value: object) -> str:
    """分类匹配用的归一化键：全角转半角、去空格标点、转小写。

    试剂分类表和 LIS 导出表的写法常有全角/半角括号、撇号、空格差异，
    不归一化会把"肉眼一样"的同一个项目判成两个。
    """
    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    text = re.sub(r"\s+", "", text)
    return re.sub(r"[()（）\[\]【】{}<>《》:：,，.。;；、/\\\-—_~!！?？\"'’”‘*#·+=|]", "", text)


def _split_bracket(value: object) -> list[str]:
    """按中英文括号把一个名称拆成若干段。

    「谷丙转氨酶(ALT)」→ ['谷丙转氨酶', 'ALT']
    「白蛋白（溴甲酚绿法）」→ ['白蛋白', '溴甲酚绿法']
    """
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not text:
        return []
    parts = re.split(r"[\(\[\u3010\u3011]", text)
    return [part.strip(")】\u3011 　\t") for part in parts if part.strip(")】\u3011 　\t")]


def _name_variants(name: object) -> list[str]:
    """一个试剂名派生多个匹配键：全名、去括号头部、去包装后缀。

    分类表里写的是「白蛋白（溴甲酚绿法）」「D-二聚体（直接化学发光法）」，
    LIS 检测项目里写的是「白蛋白」「D-二聚体」——不剥后缀就永远对不上。

    注意：必须**先按括号切分**再做归一化。归一化会把括号删掉，
    之后再切分就永远切不出「白蛋白」这个头部键。
    """
    key = _norm_key(name)
    if not key:
        return []
    out = {key}
    for segment in _split_bracket(name):
        out.add(_norm_key(segment))
    # 去掉末尾的「测定试剂盒」「试剂盒」「试剂」等包装词
    for suffix in ("测定试剂盒", "检测试剂盒", "试剂盒", "试剂", "测定", "检测"):
        if key.endswith(suffix) and len(key) > len(suffix) + 1:
            out.add(key[: -len(suffix)])
    return [item for item in out if item]


def _inner_segments(name: object) -> list[str]:
    """括号内的补充说明，常是英文简称或方法学名。"""
    segments = _split_bracket(name)
    return [_norm_key(item) for item in segments[1:] if _norm_key(item)]


def _split_slash(value: str) -> list[str]:
    """按斜杠拆多试剂名，但只在每段都够长时才拆。

    分类表里「HIV Ag/Ab」是一个整体项目，拆开会留下「Ab」这种噪声键；
    而「谷丙转氨酶/丙氨酸氨基转移酶」这种才需要拆。
    """
    parts = [part.strip() for part in value.split("/") if part.strip()]
    if len(parts) > 1 and all(len(part) >= 4 for part in parts):
        return parts
    return [value.strip()]


def _split_composite(value: object) -> list[str]:
    """拆复合项目名：ASO、RF、CCP / 血常规+CRP / 肺支、肺衣。

    LIS 一个送检项目格里可能塞了好几个检测项目，分类时逐段尝试，
    命中任意一段即用该段的分类。
    """
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not text:
        return []
    parts = re.split(r"[、,，+＋/&和及]", text)
    parts = [part.strip() for part in parts if part.strip()]
    return parts if len(parts) > 1 else [text]


def _load_taxonomy() -> dict[str, dict[str, dict]]:
    """读试剂分类表，建立两套独立索引。

    - ``name``：品名 → {专业, 组套}，用于匹配「检测项目名称」
    - ``en``  ：产品简称 → {专业, 组套}，用于匹配「项目英文名称」

    两套索引必须分开：分类表里「谷丙转氨酶」这类中文名可能压根没有，
    但它的英文简称 ALT 在「产品简称」列里有；混在一个字典里会让
    品名变体抢占英文简称的键位，导致英文名对上了却判不出来。
    """
    global _taxonomy_cache
    if _taxonomy_cache is not None:
        return _taxonomy_cache
    name_index: dict[str, dict] = {}
    en_index: dict[str, dict] = {}
    suite_index: dict[str, dict] = {}
    if TAXONOMY_SOURCE.is_file():
        wb = openpyxl.load_workbook(TAXONOMY_SOURCE, read_only=True, data_only=True)
        try:
            ws = wb["组套核对"]
            for row in ws.iter_rows(min_row=2, values_only=True):
                if not row or not row[0]:
                    continue
                specialty = str(row[0]).strip()
                suite = str(row[1]).strip() if row[1] else ""
                if not suite:
                    continue
                entry = {"specialty": specialty, "suite": suite}
                # 组套名 → 专业/组套（用于「肝功能」「肾功能」这类组套级送检项目）
                suite_key = _norm_key(suite)
                if suite_key and suite_key not in suite_index:
                    suite_index[suite_key] = entry
                # 品名 → 检测项目名称
                if row[2]:
                    for name in _split_slash(str(row[2])):
                        for variant in _name_variants(name):
                            if variant and variant not in name_index:
                                name_index[variant] = entry
                # 产品简称 → 项目英文名称
                if row[3]:
                    for name in _split_slash(str(row[3])):
                        for variant in _name_variants(name):
                            if variant and variant not in en_index:
                                en_index[variant] = entry
        finally:
            wb.close()
    _taxonomy_cache = {"name": name_index, "en": en_index, "suite": suite_index}
    return _taxonomy_cache


def taxonomy_items() -> list[dict[str, str]]:
    """返回给前端做预览时分类用的精简清单（品名 / 产品简称 / 专业 / 组套）。"""
    out = []
    if not TAXONOMY_SOURCE.is_file():
        return out
    wb = openpyxl.load_workbook(TAXONOMY_SOURCE, read_only=True, data_only=True)
    try:
        ws = wb["组套核对"]
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or not row[0] or not row[1]:
                continue
            out.append({
                "name": str(row[2]).strip() if row[2] else "",
                "en": str(row[3]).strip() if row[3] else "",
                "specialty": str(row[0]).strip(),
                "suite": str(row[1]).strip(),
            })
    finally:
        wb.close()
    return out


def _blank_classification() -> dict[str, str]:
    return {SPECIALTY_KEY: "", SUITE_KEY: ""}


# ★ 检测项目中文惯用名 → 试剂分类表里的产品简称（英文缩写）别名桥。
#   分类表是「试剂」维度，品名用规范名（丙氨酸氨基转移酶（丙氨酸底物法）），
#   LIS 检测项目用科室惯用名（谷丙转氨酶）。两者中文对不上，但缩写 ALT 是通用的。
#   这里**只做中文名 → 英文缩写的翻译**，专业/组套一律回分类表查，绝不硬编码分类。
_COMMON_ALIASES: dict[str, str] = {
    "谷丙转氨酶": "ALT",
    "丙氨酸氨基转移酶": "ALT",
    "天门冬氨酸氨基转移酶": "AST",
    "天冬氨酸氨基转移酶": "AST",
    "谷草转氨酶": "AST",
    "总胆红素": "TBIL",
    "总胆汁酸": "TBA",
    "碱性磷酸酶": "ALP",
    "乳酸脱氢酶": "LD",
    "肌酸激酶": "CK",
    "肌酸激酶同工酶": "CK-MB",
    "超敏c反应蛋白": "hsCRP",
    "c反应蛋白": "CRP",
    "癌胚抗原": "CEA",
    "甲胎蛋白": "AFP",
    "总前列腺特异性抗原": "tPSA",
    "游离前列腺特异性抗原": "fPSA",
    "前列腺特异性抗原": "tPSA",
    "游离甲状腺素": "FT4",
    "游离三碘甲状腺原氨酸": "FT3",
    "促甲状腺激素": "TSH",
    "乙肝表面抗原": "HBsAg",
    "乙肝表面抗体": "HBsAb",
    "乙肝e抗原": "HBeAg",
    "乙肝e抗体": "HBeAb",
    "乙肝核心抗体": "HBcAb",
    "丙肝抗体": "Anti-HCV",
    "丙型肝炎病毒抗体": "Anti-HCV",
    "梅毒螺旋体抗体": "TP",
    "d二聚体": "D-Dimer",
    # 电解质与常见单测定（分类表里品名是「钾（离子选择电极法）」这类全称）
    "钾": "K",
    "钠": "Na",
    "氯": "Cl",
    "钙": "Ca",
    "镁": "Mg",
    "铁": "IRON",
    "铁蛋白": "Ferritin",
    "葡萄糖": "Glu",
    "血糖": "Glu",
    "尿淀粉酶": "AMY",
    "血淀粉酶": "AMY",
    "淀粉酶": "AMY",
    "血氨": "AMM",
    "氨": "AMM",
    "尿素": "Urea",
    "胆碱酯酶": "ChE",
    "胱抑素": "CysC",
    "同型半胱氨酸": "Hcy",
    "降钙素原": "PCT",
    "降钙素": "CT",
    "肌钙蛋白i": "cTnI",
    "高敏肌钙蛋白i": "hs-TnI",
    "高敏肌钙蛋白t": "hs-TnT",
    "高敏肌钙蛋白": "hs-TnI",
    "肌钙蛋白": "cTnI",
    "b型钠尿肽": "BNP",
    "脑钠肽": "NT-proBNP",
    "n末端脑利钠肽前体": "NT-proBNP",
    "抗环瓜氨酸肽抗体": "Anti-CCP",
    "类风湿因子": "RF",
    "抗链球菌溶血素o": "ASO",
    "抗缪勒氏激素": "AMH",
    "抗缪勒管激素": "AMH",
    "促卵泡刺激素": "FSH",
    "黄体生成素": "LH",
    "雌二醇": "E2",
    "睾酮": "T",
    "孕酮": "P",
    "泌乳素": "PRL",
    "糖化血红蛋白": "HbA1c",
    "c肽": "C-P",
    "胰岛素": "INS",
    "瘦素": "Leptin",
    "脂联素": "APN",
    "视黄醇结合蛋白": "RBP",
    "前白蛋白": "PA",
    "视黄醇": "VA",
    # 传染病与呼吸道（分类表里品名带方法学后缀）
    "抗-hav": "HAV IgM",
    "甲型肝炎病毒igm抗体": "HAV IgM",
    "抗-hiv": "HIV Ag/Ab",
    "人类免疫缺陷病毒抗原抗体": "HIV Ag/Ab",
    "肺炎支原体": "MP-IgM",
    "肺炎衣原体": "CP-IgM",
    "支原体": "MP-IgM",
    "衣原体": "CP-IgM",
    "新型冠状病毒": "2019-nCoV",
    "甲型流感病毒": "IAV",
    "乙型流感病毒": "IBV",
    "结核分枝杆菌": "MTB",
    "腺病毒": "AdV",
    "鼻病毒": "RhV",
    # 凝血
    "凝血酶原时间": "PT",
    "凝血酶时间": "TT",
    "活化部分凝血活酶时间": "APTT",
    "纤维蛋白原": "Fib",
    "血浆凝血酶": "PT",
    # 分类表里简称带后缀的（归一化后为 asogen2 / rfgen2 等）
    "抗链球菌溶血素o": "ASO Gen.2",
    "aso": "ASO Gen.2",
    "类风湿因子": "RF Gen.2",
    "rf": "RF Gen.2",
    "抗环瓜氨酸肽抗体": "Anti-CCP Gen.2",
    "抗ccp": "Anti-CCP Gen.2",
    "ccp": "Anti-CCP Gen.2",
    # 甲状旁腺
    "甲状旁腺素": "iPTH",
    "全段甲状旁腺激素": "iPTH",
    "甲状旁腺激素": "iPTH",
    # 超敏 / 普通 CRP
    "h-crp": "hsCRP",
    "超敏c反应蛋白": "hsCRP",
    # 免疫缺陷
    "抗-hiv": "HIV Ag/Ab",
    "hiv": "HIV Ag/Ab",
    "乙肝两对半": "HBsAg",
    "血清α羟基丁酸脱氢酶": "α-HBDH",
    "α羟基丁酸脱氢酶": "α-HBDH",
    "抗链球菌溶血素o试验": "ASO Gen.2",
}

# ★ 组套级别别名：LIS 送检项目写的是「肝功能」「肾功能」这种组套名，
#   分类表里对应的是组套字段（肝功类 / 肾功类…）。这里只做**组套名映射**，
#   专业仍然从分类表的组套记录里取，不硬编码。
_SUITE_ALIASES: dict[str, str] = {
    "肝功能": "肝功类",
    "肝功": "肝功类",
    "肝功能全套": "肝功类",
    "肾功能": "肾功类",
    "肾功": "肾功类",
    "肾功能全套": "肾功类",
    "电解质": "电解质类",
    "血脂": "血脂及脂蛋白类",
    "血脂四项": "血脂及脂蛋白类",
    "血脂四项": "血脂及脂蛋白类",
    "血脂六项": "血脂及脂蛋白类",
    "血糖": "糖代谢类",
    "糖代谢": "糖代谢类",
    "糖化": "糖化类",
    "心肌酶": "心肌标志物类",
    "心肌酶谱": "心肌标志物类",
    "心肌标志物": "心肌标志物类",
    "心肌损伤标志物": "心肌标志物类",
    "肌钙蛋白": "心肌标志物类",
    "甲功": "甲状腺功能类",
    "甲状腺功能": "甲状腺功能类",
    "肿瘤标志物": "肿瘤类",
    "肿标": "肿瘤类",
    "肿标标志物": "肿瘤类",
    "炎症标志物": "炎症类",
    "自身免疫": "自身免疫类",
    "自免肝": "自身免疫类",
    "特殊蛋白": "特殊蛋白类",
    "凝血": "常规凝血类",
    "凝血全套": "常规凝血类",
    "血常规": "血球类",
    "血常规五分类": "血球类",
    "血常规五分类": "血球类",
    "尿常规": "尿液类",
    "尿液分析": "尿液类",
    "血型": "输血类",
    "血型鉴定": "输血类",
    "输血": "输血类",
    "性激素": "性激素类",
    "优生优育": "优生优育类",
    "唐筛": "唐筛类",
    "神经相关": "神经相关标志物类",
    "骨代谢": "骨代谢标志物类",
    "高血压": "高血压类",
    "过敏原": "过敏原类",
    "贫血": "贫血类",
    "胰腺": "胰腺类",
    "纤溶": "纤溶类",
    "易栓症": "易栓症类",
    "推片": "推片类",
}


# ★ 中文送检名 → 分类表品名 的翻译桥（不经过英文缩写）。
#   有些分类表品名本身就带中文（白介素-6），LIS 送检名却是另一种中文写法，
#   两者对不上又找不到通用英文缩写时走这里。
#   同样**只做名称翻译**，专业/组套一律回分类表查，绝不硬编码。
_NAME_ALIASES: dict[str, str] = {
    "白细胞介素6": "白介素6",
    "白细胞介素-6": "白介素6",
    "粪便隐血": "便隐血",
    "大便隐血": "便隐血",
    "白带": "白带",
    "血气": "血气",
    "钾": "钾",
    "钠": "钠",
    "氯": "氯",
}


def _alias_candidates(test_name: object, test_en_name: object) -> list[str]:
    """把中文惯用名翻译成候选英文缩写 / 中文品名。"""
    out: list[str] = []
    for raw in (test_name, test_en_name):
        for segment in _split_bracket(raw) or [raw]:
            key = _norm_key(segment)
            if not key:
                continue
            for alias, abbr in _COMMON_ALIASES.items():
                if _norm_key(alias) == key:
                    out.append(_norm_key(abbr))
            for alias, target in _NAME_ALIASES.items():
                if _norm_key(alias) == key:
                    out.append(_norm_key(target))
    return out


def _suite_alias_candidates(test_name: object, test_en_name: object) -> list[str]:
    """把组套级送检项目名翻译成候选组套名。"""
    out: list[str] = []
    for raw in (test_name, test_en_name):
        for segment in _split_bracket(raw) or [raw]:
            key = _norm_key(segment)
            if not key:
                continue
            for alias, suite in _SUITE_ALIASES.items():
                if _norm_key(alias) == key:
                    out.append(_norm_key(suite))
    return out


# 送检项目名里的常见包装/修饰词，剥掉后往往就是试剂本名
# 血清钾测定 → 钾；血清淀粉酶 → 淀粉酶；24小时尿蛋白定量测定 → 尿蛋白
_NAME_NOISE_PREFIX = ("血清", "血浆", "全血", "尿", "粪便", "大便", "脑脊液", "胸腹水", "心包液", "关节液")
_NAME_NOISE_SUFFIX = (
    "测定", "检测", "定量测定", "定量检测", "定性", "定量", "分析", "试验", "检查", "筛查",
    "测定法", "定量试验", "全套", "组合", "五项", "四项", "三项", "二项", "六项", "七项", "八项",
    "十五项", "15项", "5项", "4项", "3项", "2项", "6项", "7项", "8项",
)


def _strip_name_noise(value: object) -> str:
    """剥掉送检项目名里的包装词与项数修饰，保留试剂本名。"""
    key = _norm_key(value)
    if not key:
        return ""
    changed = True
    while changed:
        changed = False
        for prefix in _NAME_NOISE_PREFIX:
            if key.startswith(prefix) and len(key) > len(prefix):
                key = key[len(prefix):]
                changed = True
        for suffix in _NAME_NOISE_SUFFIX:
            if key.endswith(suffix) and len(key) >= len(suffix) + 1:
                key = key[: -len(suffix)]
                changed = True
    return key


def classify_project(test_name: object, test_en_name: object = "") -> dict[str, str]:
    """按检测项目名称 + 项目英文名称判定专业与组套，命中不了就留空。

    ★ 判定口径（与用户约定一致）
    1. **英文名对上就算命中**。分类表是试剂维度，中文品名和 LIS 检测项目名
       经常不是同一个写法（「丙氨酸氨基转移酶（丙氨酸底物法）」vs「谷丙转氨酶」），
       但产品简称 ALT 是通用的 —— 英文名命中英文索引即判定成功。
    2. **检测项目名称 / 项目英文名称一律保留原始数据的名称**，本函数只产出
       「专业」「组套」两列，绝不回写或替换项目名。

    匹配优先级：复合拆分 → 英文精确 → 中文精确 → 英文变体 → 中文变体 →
    别名桥 → 剥修饰词 → 组套级别 → 英文包含 → 中文包含 → 前缀。
    """
    cn_raw = str(test_name or "")
    en_raw = str(test_en_name or "")

    # 0) 复合项目名：ASO、RF、CCP / 血常规+CRP —— 逐段尝试，命中任一段即用
    cn_parts = _split_composite(cn_raw)
    if len(cn_parts) > 1:
        for part in cn_parts:
            result = _classify_single(part, en_raw if part == cn_raw else "")
            if result[SPECIALTY_KEY]:
                return result
    en_parts = _split_composite(en_raw)
    if len(en_parts) > 1:
        for part in en_parts:
            result = _classify_single(cn_raw, part)
            if result[SPECIALTY_KEY]:
                return result
    return _classify_single(cn_raw, en_raw)


def _classify_single(test_name: object, test_en_name: object = "") -> dict[str, str]:
    """单段项目名的分类判定。"""
    taxonomy = _load_taxonomy()
    name_index = taxonomy.get("name") or {}
    en_index = taxonomy.get("en") or {}
    suite_index = taxonomy.get("suite") or {}
    if not name_index and not en_index:
        return _blank_classification()

    def hit(entry: dict) -> dict[str, str]:
        return {SPECIALTY_KEY: entry["specialty"], SUITE_KEY: entry["suite"]}

    cn_raw = str(test_name or "")
    en_raw = str(test_en_name or "")
    cn_key = _norm_key(cn_raw)
    en_key = _norm_key(en_raw)

    # 1) 英文名精确命中（用户口径：英文名对得上就是同一个项目）
    if en_key and en_key in en_index:
        return hit(en_index[en_key])
    # 2) 中文名精确命中
    if cn_key and cn_key in name_index:
        return hit(name_index[cn_key])

    # 3) 英文名变体：括号内英文 / 去包装词
    for variant in _name_variants(en_raw) + _inner_segments(en_raw):
        if variant in en_index:
            return hit(en_index[variant])
    # 4) 中文名变体：括号头部 / 去包装词
    for variant in _name_variants(cn_raw) + _inner_segments(cn_raw):
        if variant in name_index:
            return hit(name_index[variant])
        if variant in en_index:
            return hit(en_index[variant])

    # 5) 别名桥：中文惯用名 → 英文缩写 → 分类表（分类仍由分类表决定）
    for alias_key in _alias_candidates(cn_raw, en_raw):
        if alias_key in en_index:
            return hit(en_index[alias_key])
        if alias_key in name_index:
            return hit(name_index[alias_key])

    # 5b) 剥修饰词后精确匹配：血清钾测定 → 钾
    for raw in (cn_raw, en_raw):
        stripped = _strip_name_noise(raw)
        if not stripped:
            continue
        if stripped in name_index:
            return hit(name_index[stripped])
        if stripped in en_index:
            return hit(en_index[stripped])

    # 5b2) 剥修饰词后前缀匹配：钙测定 → 钙（偶氮砷Ⅲ法）
    for raw in (cn_raw, en_raw):
        stripped = _strip_name_noise(raw)
        if len(stripped) < 2:
            continue
        for index in (name_index, en_index):
            matched = [item for item in index if len(item) >= len(stripped) and item.startswith(stripped)]
            if matched:
                return hit(index[min(matched, key=len)])

    # 5c) 组套级别匹配：肝功能 → 生化/肝功类
    for suite_key in _suite_alias_candidates(cn_raw, en_raw):
        if suite_key in suite_index:
            return hit(suite_index[suite_key])
    # 5d) 剥修饰词后的组套名：肝功能2 → 肝功类
    for raw in (cn_raw, en_raw):
        stripped = _strip_name_noise(raw)
        if stripped and stripped in suite_index:
            return hit(suite_index[stripped])

    # 6) 英文名包含分类表键（如英文全称里带缩写）
    if len(en_key) >= 4:
        matched = [key for key in en_index if len(key) >= 3 and key in en_key]
        if matched:
            return hit(en_index[max(matched, key=len)])
    # 7) 中文名包含分类表键
    if len(cn_key) >= 3:
        matched = [key for key in name_index if len(key) >= 3 and key in cn_key]
        if matched:
            return hit(name_index[max(matched, key=len)])

    # 8) 反向：源名是分类表品名的前缀（分类表写全称，LIS 写简称）
    for index in (name_index, en_index):
        for key in (cn_key, en_key):
            if len(key) < 2:
                continue
            matched = [item for item in index if len(item) >= len(key) and item.startswith(key)]
            if matched:
                return hit(index[min(matched, key=len)])
    return _blank_classification()


def _decode_csv(content: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("CSV 编码无法识别，请另存为 UTF-8 或 GB18030")


def _decode_html(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _html_table_rows(content: bytes) -> list[list[object]]:
    """解析 Excel「导出为网页」产生的 HTML 表格（LIS 导出常见形式）。

    先用标准库解析（零额外依赖）；标准库处理不了的复杂表格再退回 pandas+lxml。
    """
    text = _decode_html(content)
    try:
        rows = _html_rows_with_stdlib(text)
    except ValueError:
        rows = []
    if rows:
        return rows

    from io import StringIO

    try:
        tables = pd.read_html(StringIO(text))
    except ImportError as error:  # pragma: no cover - 依赖缺失时的兜底说明
        raise ValueError("解析 HTML 表格需要 lxml，请安装 lxml==5.3.0") from error
    if not tables:
        raise ValueError("HTML 文件里没有找到表格")
    raw = max(tables, key=lambda frame: frame.shape[0]).where(lambda f: f.notna(), None)
    return [[None if pd.isna(value) else value for value in row] for row in raw.values.tolist()]


def _html_rows_with_stdlib(text: str) -> list[list[object]]:
    """用 html.parser 抓第一个 <table> 的行，不引入额外依赖。"""
    import re
    from html.parser import HTMLParser

    class _TableParser(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.rows: list[list[object]] = []
            self._depth = 0
            self._row: list[object] | None = None
            self._cell: list[str] | None = None

        def handle_starttag(self, tag, attrs):
            if tag == "table":
                self._depth += 1
            elif self._depth == 1 and tag == "tr":
                self._row = []
            elif self._depth == 1 and tag in ("td", "th"):
                self._cell = []

        def handle_endtag(self, tag):
            if tag == "table" and self._depth:
                self._depth -= 1
            elif self._depth == 1 and tag == "tr" and self._row is not None:
                self.rows.append(self._row)
                self._row = None
            elif self._depth == 1 and tag in ("td", "th") and self._cell is not None:
                if self._row is None:
                    self._row = []
                self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
                self._cell = None

        def handle_data(self, data):
            if self._cell is not None:
                self._cell.append(data)

    parser = _TableParser()
    parser.feed(text)
    return [row for row in parser.rows if any(str(value).strip() for value in row)]


def _read_workbook(content: bytes, filename: str, selected_sheet: str = "") -> tuple[list[str], list[list[object]], str, int]:
    suffix = Path(filename).suffix.lower()
    if suffix not in {".xls", ".xlsx", ".xlsm", ".csv", ".txt"}:
        raise ValueError("当前支持 .xls、.xlsx、.xlsm、.csv")

    # ★ 一律按内容分派：LIS 系统导出时经常把 CSV 或 HTML 存成 .xls（实测有 3 份），
    #   只看扩展名会把这些文件全判成"不是有效的 .xls"。
    fmt = table_format.sniff_bytes(content[:8192]) if content else table_format.sniff_extension(suffix)
    if fmt == table_format.FMT_UNKNOWN:
        fmt = table_format.sniff_extension(suffix)

    if fmt in (table_format.FMT_CSV, table_format.FMT_TEXT):
        rows = list(csv.reader(io.StringIO(_decode_csv(content))))
        if not rows:
            raise ValueError("上传文件没有内容")
        header, body, sheet, header_row = _choose_header(rows)
        if selected_sheet and selected_sheet != sheet:
            raise ValueError(f"工作表不存在：{selected_sheet}")
        return header, body, sheet, header_row
    if fmt == table_format.FMT_HTML:
        rows = _html_table_rows(content)
        if not rows:
            raise ValueError("HTML 文件里没有可识别的数据表")
        header, body, _, header_row = _choose_header(rows)
        sheet = "HTML表格"
        if selected_sheet and selected_sheet != sheet:
            raise ValueError(f"工作表不存在：{selected_sheet}")
        return header, body, sheet, header_row
    if fmt == table_format.FMT_OLE_XLS:
        try:
            workbook = pd.ExcelFile(io.BytesIO(content), engine="xlrd")
            sheet_names = list(workbook.sheet_names)
            if selected_sheet and selected_sheet not in sheet_names:
                raise ValueError(f"工作表不存在：{selected_sheet}")
            candidates = []
            target_sheets = [selected_sheet] if selected_sheet else sheet_names
            for sheet_name in target_sheets:
                raw = pd.read_excel(workbook, sheet_name=sheet_name, header=None, dtype=object)
                rows = raw.where(raw.notna(), None).values.tolist()
                if not rows:
                    if selected_sheet:
                        raise ValueError("工作表「%s」是空表，没有可整理的数据" % sheet_name)
                    continue
                header, body, _, header_row = _choose_header(rows)
                candidates.append((len([x for x in header if x]), len(body), header, body, sheet_name, header_row))
            if not candidates:
                raise ValueError("工作簿中没有可识别的数据表")
            _, _, header, body, sheet, header_row = max(candidates, key=lambda item: (item[0], item[1]))
            return header, body, sheet, header_row
        except ImportError as error:
            raise ValueError("读取 .xls 需要安装 xlrd==2.0.1") from error
        except ValueError:
            raise
        except Exception as error:
            raise ValueError("旧版 Excel .xls 文件已损坏，无法读取：%s" % error) from error
    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as error:
        raise ValueError("文件不是有效的 Excel Open XML 工作簿，或扩展名与文件内容不匹配") from error
    try:
        sheet_names = list(wb.sheetnames)
        if selected_sheet and selected_sheet not in sheet_names:
            raise ValueError(f"工作表不存在：{selected_sheet}")
        candidates = []
        target_sheets = [selected_sheet] if selected_sheet else sheet_names
        for sheet_name in target_sheets:
            ws = wb[sheet_name]
            raw = list(ws.iter_rows(values_only=True))
            if not raw:
                if selected_sheet:
                    # 明确说清是"这张表空的"，而不是含糊的"工作簿里没有表"
                    raise ValueError("工作表「%s」是空表，没有可整理的数据" % sheet_name)
                continue
            header, body, _, header_row = _choose_header([list(row) for row in raw])
            candidates.append((len([x for x in header if x]), len(body), header, body, ws.title, header_row))
        if not candidates:
            raise ValueError("工作簿中没有可识别的数据表")
        _, _, header, body, sheet, header_row = max(candidates, key=lambda item: (item[0], item[1]))
        return header, body, sheet, header_row
    finally:
        wb.close()


def _read_sheet_rows(content: bytes, filename: str, sheet: str) -> list[list[object]]:
    """按"重新指定表头行"重读整表；同样按内容分派，与 _read_workbook 保持一致。"""
    suffix = Path(filename).suffix.lower()
    fmt = table_format.sniff_bytes(content[:8192]) if content else table_format.sniff_extension(suffix)
    if fmt == table_format.FMT_UNKNOWN:
        fmt = table_format.sniff_extension(suffix)
    if fmt in (table_format.FMT_CSV, table_format.FMT_TEXT):
        return list(csv.reader(io.StringIO(_decode_csv(content))))
    if fmt == table_format.FMT_HTML:
        return _html_table_rows(content)
    if fmt == table_format.FMT_OLE_XLS:
        workbook = pd.ExcelFile(io.BytesIO(content), engine="xlrd")
        raw = pd.read_excel(workbook, sheet_name=sheet, header=None, dtype=object)
        return raw.where(raw.notna(), None).values.tolist()
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    try:
        return [list(row) for row in wb[sheet].iter_rows(values_only=True)]
    finally:
        wb.close()


def _choose_header(rows: list[list[object]]) -> tuple[list[str], list[list[object]], str, int]:
    limit = min(len(rows), 30)
    ranked = []
    for idx in range(limit):
        values = [str(v).strip() if v is not None else "" for v in rows[idx]]
        nonempty = [v for v in values if v]
        if not nonempty:
            continue
        unique = len(set(nonempty))
        score = len(nonempty) + unique * 0.3 + (3 if any(k in " ".join(nonempty).lower() for k in ("条码", "姓名", "项目", "结果", "时间")) else 0)
        ranked.append((score, idx, values))
    if not ranked:
        raise ValueError("前 30 行内未找到有效表头")
    _, idx, header = max(ranked)
    header = [h or f"未命名列{n + 1}" for n, h in enumerate(header)]
    body = [list(row) for row in rows[idx + 1:] if any(value not in (None, "") for value in row)]
    return header, body, "CSV", idx + 1


def _suggest_mappings(headers: list[str], fields: list[dict[str, str]]) -> list[dict]:
    synonyms = {
        "record_id": ["记录唯一号", "记录ID"], "barcode": ["条码号", "条码", "标本条码", "标本条码号"],
        "patient_name": ["姓名", "病人姓名", "患者姓名"], "patient_sex": ["性别", "患者性别"],
        "patient_age": ["年龄"], "patient_id": ["病人号", "患者ID", "病历号", "His病历号"],
        "dept_name": ["科室", "申请科室"], "specimen_type": ["标本类型", "样本类型", "标本种类"],
        "test_name": ["检验项目", "项目名称", "送检项目", "申请项目", "检验目的", "检测项目名称"],
        "test_code": ["检验项目编号", "项目编码"], "result_text": ["检验结果", "结果", "结果文本"],
        "result_value": ["定量结果", "结果数值"], "unit": ["单位", "结果单位"],
        "ref_range": ["参考范围", "参考区间"], "abnormal_flag": ["结果状态Code", "异常标记"],
        "ordered_at": ["申请时间", "开立时间"], "collected_at": ["采样时间", "采集时间"],
        "received_at": ["接收时间", "签收时间", "核收时间", "检验科接收时间"],
        "verified_at": ["审核时间"], "reported_at": ["报告时间", "发布时间"], "tat_minutes": ["TAT", "接收-报告(分钟)", "检验中TAT(分钟)"],
        "instrument": ["检验仪器", "报告单元", "仪器"], "test_group": ["检验分组", "检验小组"],
    }
    field_by_key = {f["key"]: f for f in fields}
    result = []
    used: set[str] = set()
    for source in headers:
        normalized = re.sub(r"[\s_（）()【】\[\]：:·-]", "", source).casefold()
        candidates = []
        for field in fields:
            names = [field["label"], field["key"], *synonyms.get(field["key"], [])]
            score = max((1.0 if normalized == re.sub(r"[\s_（）()【】\[\]：:·-]", "", name).casefold() else 0.0 for name in names), default=0)
            if score:
                candidates.append(field)
        chosen = next((f for f in candidates if f["key"] not in used), None)
        if chosen:
            used.add(chosen["key"])
        result.append({"source": source, "target_key": chosen["key"] if chosen else "", "target_label": chosen["label"] if chosen else "", "rule": "自动匹配" if chosen else "不导入"})
    return result


def preview_upload(filename: str, content: bytes, header_row: int | None = None,
                   selected_sheet: str = "", source_ref: str = "") -> dict:
    if not content:
        raise ValueError("上传文件为空")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("文件超过 100MB 限制")
    headers, body, sheet, detected_header = _read_workbook(content, filename, selected_sheet)
    if header_row and header_row != detected_header:
        rows = _read_sheet_rows(content, filename, sheet)
        if not 1 <= header_row <= min(len(rows), 100):
            raise ValueError("表头行号超出范围")
        headers = [str(v).strip() if v is not None else "" for v in rows[header_row - 1]]
        headers = [h or f"未命名列{n + 1}" for n, h in enumerate(headers)]
        body = [list(row) for row in rows[header_row:] if any(value not in (None, "") for value in row)]
        detected_header = header_row
    fields = _field_definitions()
    mapping = _suggest_mappings(headers, fields)
    session_id = uuid.uuid4().hex
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    metadata = {"filename": Path(filename).name, "sheet": sheet, "header_row": detected_header}
    if source_ref:
        # ★ 已导入数据集：源文件本来就在 library/sources 里，**不再复制一份到 staging**。
        #   以前每预览一次就白拷一份完整文件（66MB 的《标本明细》一次 66MB），
        #   staging 目录几天就堆到 344MB，而且读的时候还要再读一遍拷贝。
        #   现在只在 metadata 里记一个指针，确认阶段直接读库里的原件。
        metadata["dataset_id"] = source_ref
        staged_name = ""
    else:
        staged_name = f"{session_id}{Path(filename).suffix.lower()}"
        (STAGING_DIR / staged_name).write_bytes(content)
    metadata_path = STAGING_DIR / f"{session_id}.json"
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    rows = [[_plain(value) for value in row] for row in body]
    return {
        "session_id": session_id, "filename": Path(filename).name, "sheet": sheet,
        "header_row": detected_header, "headers": headers, "row_count": len(rows),
        "mapping": mapping, "fields": fields,
        "preview": rows[:PREVIEW_ROWS], "staged_name": staged_name,
        "format_note": _format_note(content, filename),
        "required_keys": sorted(REQUIRED_KEYS),
        "taxonomy_size": len(_load_taxonomy()),
    }


def imported_sources() -> list[dict]:
    """返回可供 LIS 复用的数据源摘要，不暴露本机路径。"""
    from modules.data_analysis import smartlab_engine as engine

    engine.load_state()
    source_root = Path(engine.SRC_DIR).resolve()
    result = []
    for dataset in engine.STATE.get("datasets", []):
        path = Path(engine.ds_abs_path(dataset)).resolve()
        if path.parent != source_root or not path.is_file():
            continue
        sheets = [
            {"name": item.get("name", ""), "rows": int(item.get("rows") or 0), "columns": item.get("columns", [])}
            for item in dataset.get("sheets", []) if item.get("name")
        ]
        result.append({
            "id": dataset.get("id", ""),
            "name": dataset.get("name") or path.name,
            "size": int(dataset.get("size") or path.stat().st_size),
            "sheet_count": len(sheets),
            "total_rows": sum(item["rows"] for item in sheets),
            "sheets": sheets,
        })
    return result


def preview_imported_dataset(dataset_id: str, sheet: str = "", header_row: int | None = None) -> dict:
    """读取数据分析库中的已导入源文件，交给 LIS 预览流程，不再要求用户重新选择本地文件。"""
    from modules.data_analysis import smartlab_engine as engine

    engine.load_state()
    dataset = next((item for item in engine.STATE.get("datasets", []) if item.get("id") == dataset_id), None)
    if dataset is None:
        raise ValueError("数据源不存在")
    path = Path(engine.ds_abs_path(dataset)).resolve()
    source_root = Path(engine.SRC_DIR).resolve()
    if path.parent != source_root or not path.is_file():
        raise ValueError("已导入数据的源文件不存在，请先在数据源管理中检查")
    if path.stat().st_size > MAX_UPLOAD_BYTES:
        raise ValueError("已导入数据源超过 100MB 限制，请先拆分文件后再整理")
    filename = str(dataset.get("name") or path.name)
    # source_ref：源文件本来就在 library/sources 里，让 LIS 会话直接引用它，
    # 不再往 staging 复制一份完整文件（66MB 一次，攒几次就是几百 MB）。
    return preview_upload(filename, path.read_bytes(), header_row, sheet, source_ref=dataset_id)


def _format_note(content: bytes, filename: str) -> str:
    """把"实际识别到的格式"讲给用户听，避免扩展名与内容不符时用户以为读错了。"""
    suffix = Path(filename).suffix.lower()
    fmt = table_format.sniff_bytes(content[:8192]) if content else table_format.sniff_extension(suffix)
    if fmt == table_format.FMT_UNKNOWN:
        fmt = table_format.sniff_extension(suffix)
    if fmt in {table_format.FMT_OLE_XLS, table_format.FMT_OPENXML} and suffix in {
            ".xls", ".xlsx", ".xlsm"}:
        return ""
    return table_format.describe_mismatch(suffix, fmt)


def _plain(value: object) -> object:
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    return value


def _normalize(value: object, field: dict[str, str]) -> str | float | int | None:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    kind = field["type"]
    if kind == "日期时间":
        if isinstance(value, (datetime, date)):
            dt = datetime.combine(value, datetime.min.time()) if isinstance(value, date) and not isinstance(value, datetime) else value
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y%m%d%H%M%S", "%Y%m%d", "%y.%m.%d"):
            try:
                return datetime.strptime(text, fmt).strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass
        return text
    if kind == "数值":
        match = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
        if not match:
            return None
        number = float(match.group())
        if number < 0:
            return None
        return int(number) if number.is_integer() else number
    if field["key"] == "patient_sex":
        return {"男": "男", "男性": "男", "M": "男", "m": "男", "女": "女", "女性": "女", "F": "女", "f": "女"}.get(text, text)
    if field["key"] == "patient_age_years":
        match = re.search(r"\d+(?:\.\d+)?", text)
        return float(match.group()) if match else None
    return text


def _build_standard(rows: list[list[object]], headers: list[str], mapping: list[dict], fields: list[dict], filename: str, sheet: str, header_row: int, batch_id: str) -> tuple[list[list], list[dict]]:
    indexes = {name: idx for idx, name in enumerate(headers)}
    field_by_key = {field["key"]: field for field in fields}
    keys = [field["key"] for field in fields]
    has_specialty = SPECIALTY_KEY in field_by_key
    has_suite = SUITE_KEY in field_by_key
    out_rows = []
    issues = []
    for row_number, source_row in enumerate(rows, start=header_row + 1):
        record = {field["key"]: None for field in fields}
        for item in mapping:
            target = item.get("target_key", "")
            source = item.get("source", "")
            if not target:
                continue
            if target not in field_by_key or source not in indexes:
                raise ValueError("字段映射包含无效的来源列或标准字段")
            record[target] = _normalize(source_row[indexes[source]] if indexes[source] < len(source_row) else None, field_by_key[target])
        record["record_id"] = record.get("record_id") or f"{batch_id}-{row_number}"
        record["source_file"] = filename
        record["source_sheet"] = sheet
        record["source_header_row"] = header_row
        record["source_row_number"] = row_number
        record["import_batch_id"] = batch_id
        # ★ v1.2：按检测项目名称 + 项目英文名称自动填专业/组套，命中不了就留空
        if has_specialty or has_suite:
            hits = classify_project(record.get("test_name"), record.get("test_en_name"))
            if has_specialty:
                record[SPECIALTY_KEY] = hits[SPECIALTY_KEY]
            if has_suite:
                record[SUITE_KEY] = hits[SUITE_KEY]
        # 不设必填：空值只做提示，不拒绝该行
        missing = [field_by_key[key]["label"] for key in REQUIRED_KEYS if not record.get(key)]
        if missing:
            issues.append({"row": row_number, "missing": missing})
        out_rows.append([record.get(key) for key in keys])
    return out_rows, issues


def _visible_fields(fields: list[dict], rows: list[list]) -> list[dict]:
    """预览/导出用：整列全空的标准字段不体现（溯源列与分类列始终保留）。"""
    always = {"record_id", "source_file", "source_sheet", "source_header_row",
              "source_row_number", "import_batch_id", "source_lab",
              SPECIALTY_KEY, SUITE_KEY}
    if not rows:
        return list(fields)
    used = {index for index in range(len(fields))
            if fields[index]["key"] in always
            or any(row[index] not in (None, "") for row in rows)}
    return [field for index, field in enumerate(fields) if index in used]


def confirm_save(session_id: str, mapping: list[dict], header_row: int, lab: str = "") -> dict:
    payload = _standard_payload(session_id, mapping, header_row, lab)
    batch_id = payload["batch_id"]
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    output_name = f"LIS标准化_{batch_id}.xlsx"
    output_path = OUTPUT_DIR / output_name
    wb = openpyxl.Workbook(write_only=True)
    ws = wb.create_sheet("lis_std")
    ws.append([field["label"] for field in payload["visible"]])
    for row in payload["trimmed"]:
        ws.append(row)
    temp_path = output_path.with_suffix(".xlsx.tmp")
    wb.save(temp_path)
    temp_path.replace(output_path)
    mapping_record = {
        "batch_id": batch_id, "source_filename": payload["source_filename"], "source_sha256": payload["source_sha256"],
        "sheet": payload["sheet"], "header_row": header_row, "lab": lab, "mapping": mapping,
        "row_count": len(payload["trimmed"]), "quality_issue_count": len(payload["issues"]), "created_at": payload["created_at"],
        "output_file": output_name, "columns": [field["key"] for field in payload["visible"]],
        "column_labels": [field["label"] for field in payload["visible"]],
        "missing_required": payload["missing_required"],
    }
    (CONFIG_DIR / f"{batch_id}.json").write_text(json.dumps(mapping_record, ensure_ascii=False, indent=2), encoding="utf-8")
    # ★ 只有"自己上传的暂存副本"才能删 —— 已导入数据集模式下 staged_path 指向
    #   库里的源文件原件，删了就是把用户的数据删了！
    if payload.get("staged_owned"):
        try:
            payload["staged_path"].unlink(missing_ok=True)
        except OSError:
            pass
    return {
        "batch_id": batch_id, "file": output_name, "rows": len(payload["trimmed"]),
        "column_count": len(payload["visible"]), "hidden_empty_columns": len(payload["fields"]) - len(payload["visible"]),
        "quality_issue_count": len(payload["issues"]), "quality_issues": payload["issues"][:100],
        "missing_required": payload["missing_required"],
    }


def _standard_payload(session_id: str, mapping: list[dict], header_row: int, lab: str) -> dict:
    """构建标准化结果（预览 / 保存 / 导出共用同一份逻辑，保证三处完全一致）。

    结果按 (会话 + 映射 + 表头行) 缓存在内存里：全量标准化动辄几万行，
    翻页时重复构建太慢，而保存时必须和预览看到的一模一样。
    """
    fingerprint = _mapping_fingerprint(mapping, header_row)
    cached = _PAYLOAD_CACHE.get(session_id)
    if cached and cached["fingerprint"] == fingerprint:
        return cached["payload"]

    if not re.fullmatch(r"[0-9a-f]{32}", session_id or ""):
        raise ValueError("导入会话无效")
    metadata_path = STAGING_DIR / f"{session_id}.json"
    if not metadata_path.is_file():
        raise ValueError("暂存文件不存在或已过期，请重新上传")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    dataset_id = str(metadata.get("dataset_id") or "")
    staged_owned = False
    if dataset_id:
        # ★ 已导入数据集：直接读库里的原件，staging 不留副本
        from modules.data_analysis import smartlab_engine as engine
        dataset = next((item for item in engine.STATE.get("datasets", [])
                        if item.get("id") == dataset_id), None)
        if dataset is None:
            raise ValueError("源数据已不存在，请重新选择已导入的数据表")
        staged_path = Path(engine.ds_abs_path(dataset))
        if not staged_path.is_file():
            raise ValueError("源文件已不存在，请重新选择已导入的数据表")
        content = staged_path.read_bytes()
    else:
        candidates = list(STAGING_DIR.glob(f"{session_id}.*"))
        staged_path = next((path for path in candidates
                            if path.suffix.lower() in {".xls", ".xlsx", ".xlsm", ".csv"}), None)
        if staged_path is None or not staged_path.is_file():
            raise ValueError("暂存文件不存在或已过期，请重新上传")
        staged_owned = True
        content = staged_path.read_bytes()
    filename = metadata["filename"]
    sheet = metadata["sheet"]
    stored_header_row = int(metadata["header_row"])
    # ★ 必须按「预览时选中的那个工作表」来读，不能不传 sheet 让它自己挑。
    #   不传的话 _read_workbook 会把工作簿里所有表都读一遍，再按
    #   "列数多、行数多"挑一个 —— 挑到的很可能不是用户选的那张，
    #   于是字段映射里的来源列在表头里找不到，直接报
    #   「来源字段不存在：xxx」。
    #   实测《标本明细》17 个表：用户选「生化」，自动挑中的是「2026.07数据」，
    #   前者有「采样-报告」、后者是「采集-报告」，就差这一个字就报错。
    headers, rows, sheet, detected = _read_workbook(content, staged_path.name, sheet or "")
    if header_row != stored_header_row:
        raw_rows = _read_sheet_rows(content, staged_path.name, sheet)
        if not 1 <= header_row <= min(len(raw_rows), 100):
            raise ValueError("表头行号超出范围")
        headers = [str(value).strip() if value is not None else "" for value in raw_rows[header_row - 1]]
        headers = [value or f"未命名列{index + 1}" for index, value in enumerate(headers)]
        rows = [list(row) for row in raw_rows[header_row:] if any(value not in (None, "") for value in row)]
        detected = header_row
    for item in mapping:
        if item.get("target_key") and item["target_key"] not in {f["key"] for f in _field_definitions()}:
            raise ValueError("映射含未知标准字段")
    fields = _field_definitions()
    keys_seen: set[str] = set()
    sources_seen: set[str] = set()
    for item in mapping:
        target = item.get("target_key", "")
        source = item.get("source", "")
        if source not in headers:
            raise ValueError(f"来源字段不存在：{source}")
        if target and target not in {f["key"] for f in fields}:
            raise ValueError(f"标准字段不存在：{target}")
        if target and target in keys_seen:
            raise ValueError(f"标准字段重复映射：{target}")
        if source in sources_seen:
            raise ValueError(f"来源字段重复映射：{source}")
        if target:
            keys_seen.add(target)
        sources_seen.add(source)
    source_filename = filename
    batch_id = uuid.uuid4().hex[:12]
    normalized, issues = _build_standard(rows, headers, mapping, fields, source_filename, sheet, header_row, batch_id)
    # 全空列不体现：只导出真正有数据的字段
    visible = _visible_fields(fields, normalized)
    kept_indexes = sorted({index for index, field in enumerate(fields)
                           if any(f["key"] == field["key"] for f in visible)})
    trimmed = [[row[index] for index in kept_indexes] for row in normalized]
    # ★ v1.2：不拦截必填映射；只提示还缺哪些
    missing_required_mapping = [f["label"] for f in fields
                                if f["key"] in REQUIRED_KEYS - {"record_id"} and f["key"] not in keys_seen]
    payload = {
        "batch_id": batch_id, "source_filename": source_filename,
        "source_sha256": hashlib.sha256(content).hexdigest(),
        "sheet": sheet, "header_row": header_row, "fields": fields, "visible": visible,
        "trimmed": trimmed, "issues": issues, "missing_required": missing_required_mapping,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "staged_path": staged_path,
        "staged_owned": staged_owned,
    }
    _PAYLOAD_CACHE[session_id] = {"fingerprint": fingerprint, "payload": payload}
    while len(_PAYLOAD_CACHE) > _PAYLOAD_CACHE_MAX:
        _PAYLOAD_CACHE.pop(next(iter(_PAYLOAD_CACHE)))
    return payload


def _mapping_fingerprint(mapping: list[dict], header_row: int) -> str:
    pairs = sorted((str(item.get("source", "")), str(item.get("target_key", ""))) for item in mapping)
    raw = json.dumps({"pairs": pairs, "header_row": header_row}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def preview_standard(session_id: str, mapping: list[dict], header_row: int, lab: str = "",
                      page: int = 1, page_size: int = 50) -> dict:
    """按当前字段映射生成标准化预览 —— 可翻页浏览全部数据行。"""
    payload = _standard_payload(session_id, mapping, header_row, lab)
    rows = payload["trimmed"]
    total = len(rows)
    size = max(1, min(int(page_size or 50), 500))
    pages = max(1, (total + size - 1) // size)
    index = max(1, min(int(page or 1), pages))
    start = (index - 1) * size
    window = rows[start:start + size]
    return {
        "batch_id": payload["batch_id"],
        "columns": [field["label"] for field in payload["visible"]],
        "column_keys": [field["key"] for field in payload["visible"]],
        "rows": window,
        "total_rows": total,
        "page": index,
        "page_size": size,
        "total_pages": pages,
        "hidden_empty_columns": len(payload["fields"]) - len(payload["visible"]),
        "quality_issue_count": len(payload["issues"]),
        "quality_issues": payload["issues"][:100],
        "missing_required": payload["missing_required"],
    }


def export_standard(session_id: str, mapping: list[dict], header_row: int, lab: str = "") -> tuple[Path, str]:
    """按当前映射导出标准化 Excel（不落"已整理"记录，供直接下载）。"""
    payload = _standard_payload(session_id, mapping, header_row, lab)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    name = f"LIS标准化导出_{payload['batch_id']}.xlsx"
    path = OUTPUT_DIR / name
    wb = openpyxl.Workbook(write_only=True)
    ws = wb.create_sheet("lis_std")
    ws.append([field["label"] for field in payload["visible"]])
    for row in payload["trimmed"]:
        ws.append(row)
    temp_path = path.with_suffix(".xlsx.tmp")
    wb.save(temp_path)
    temp_path.replace(path)
    return path, name


def saved_batches() -> list[dict]:
    """已整理并保存的数据表格列表（按保存时间倒序）。"""
    if not CONFIG_DIR.is_dir():
        return []
    items = []
    for path in CONFIG_DIR.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        batch_id = str(record.get("batch_id") or path.stem)
        output_name = Path(str(record.get("output_file") or "")).name
        exists = bool(output_name) and (OUTPUT_DIR / output_name).is_file()
        items.append({
            "batch_id": batch_id,
            "source_filename": record.get("source_filename", ""),
            "sheet": record.get("sheet", ""),
            "lab": record.get("lab", ""),
            "row_count": int(record.get("row_count") or 0),
            "column_count": len(record.get("column_labels") or record.get("columns") or []),
            "column_labels": record.get("column_labels") or [],
            "created_at": record.get("created_at", ""),
            "output_file": output_name,
            "available": exists,
        })
    items.sort(key=lambda item: item["created_at"], reverse=True)
    return items


def delete_saved_batch(batch_id: str) -> dict:
    """删除一条「已整理并保存」的记录：连带删掉映射 json 和标准化 Excel。

    只允许操作 CONFIG_DIR 下的 json 与 OUTPUT_DIR 下的 xlsx，路径越界直接拒绝。
    """
    if not re.fullmatch(r"[0-9a-f]{12}", batch_id or ""):
        raise ValueError("批次号无效")
    metadata_path = CONFIG_DIR / f"{batch_id}.json"
    if not metadata_path.is_file():
        raise FileNotFoundError("找不到该 LIS 标准化批次")
    try:
        record = json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception:
        record = {}
    output_name = Path(str(record.get("output_file") or "")).name
    deleted_file = False
    if output_name:
        output_path = OUTPUT_DIR / output_name
        # 二次校验：文件必须真的在 OUTPUT_DIR 里才允许删
        if output_path.is_file() and output_path.parent.resolve() == OUTPUT_DIR.resolve():
            output_path.unlink()
            deleted_file = True
    metadata_path.unlink()
    # 缓存里若还留着这条批次，一并清掉，避免删完还能从缓存里导出
    for key, cached in list(_PAYLOAD_CACHE.items()):
        if cached.get("payload", {}).get("batch_id") == batch_id:
            _PAYLOAD_CACHE.pop(key, None)
    return {
        "batch_id": batch_id,
        "deleted_file": deleted_file,
        "output_file": output_name,
    }


def output_file(batch_id: str) -> tuple[Path, str]:
    if not re.fullmatch(r"[0-9a-f]{12}", batch_id or ""):
        raise ValueError("批次号无效")
    metadata_path = CONFIG_DIR / f"{batch_id}.json"
    if not metadata_path.is_file():
        raise FileNotFoundError("找不到该 LIS 标准化批次")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    name = Path(metadata.get("output_file", "")).name
    path = OUTPUT_DIR / name
    if not path.is_file() or path.parent.resolve() != OUTPUT_DIR.resolve():
        raise FileNotFoundError("标准化文件不存在")
    return path, name
