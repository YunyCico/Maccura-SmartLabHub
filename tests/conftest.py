"""pytest 全局配置。

★ 最重要的一条：**测试绝不能碰用户真实的数据目录**。
引擎在导入时就会按 LIB_DIR 建目录，跑测试时的 import_file() 会调 save_state()
写 index.json。如果不隔离，测试会把真实索引覆盖成空、把源文件副本写进真实
sources —— 曾因此把 7 个数据集、70 万行的索引清空过一次。

做法：在 import 引擎**之前**设好 SLH_DATA_DIR，引擎导入时会把所有数据路径
重定向到临时目录（见 smartlab_engine.py 顶部的 _DATA_HOME 分支）。

**消息集成配置也必须隔离**（同样的道理）：
`platform/data/integrations.json` 里存着钉钉/ OA / Salesforce 的真实凭据。
测试里任何一个会调 `integration_config.save_config()` 的用例
（比如「登录并获取 Token」端点）都会真的改写这个文件，
实测把 Salesforce 的账号密码写成了测试值，真实配置直接被污染。
所以这里把 `integration_config.DATA_FILE` 指向临时文件，
并让每个用例结束后自动还原。
"""
from pathlib import Path
import os
import sys
import tempfile

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "platform" / "backend"

for path in (PROJECT_ROOT, PROJECT_ROOT / "modules", BACKEND_DIR):
    path_string = str(path)
    if path_string not in sys.path:
        sys.path.insert(0, path_string)

# ★ 必须在 import 引擎之前设置
_TEST_DATA_DIR = Path(tempfile.gettempdir()) / "smartlabhub_pytest_data"
os.environ.setdefault("SLH_DATA_DIR", str(_TEST_DATA_DIR))
os.environ.setdefault("SLH_LIS_DIR", str(_TEST_DATA_DIR))


@pytest.fixture(autouse=True)
def _protect_integration_config(tmp_path, monkeypatch):
    """防止测试写真实凭据文件，但**不改变读取行为**。

    实测教训：早先版本把 `DATA_FILE` 整个重定向到临时文件，结果
    `test_integration_config.py` 之类依赖"配置缺失"的用例全挂，
    而依赖"真实钉钉凭据"的用例也一起挂了——读取语义被改掉了。

    现在只拦写入：真正要落盘时先复制到临时副本再写，
    真实文件（platform/data/integrations.json）永远不受影响。
    读取路径原样保留，各测试模块自己的 fixture 想指哪指哪。
    """
    from app.services import integration_config

    real_file = integration_config.DATA_FILE
    real_save = integration_config.save_config

    def guarded_save(updates, **kwargs):
        target = integration_config.DATA_FILE
        if target == real_file:
            # 即将写真实凭据文件 —— 换成临时副本
            safe = tmp_path / "integrations.json"
            if real_file.is_file():
                safe.write_bytes(real_file.read_bytes())
            else:
                safe.write_text("{}", encoding="utf-8")
            monkeypatch.setattr(integration_config, "DATA_FILE", safe)
            try:
                return real_save(updates, **kwargs)
            finally:
                monkeypatch.setattr(integration_config, "DATA_FILE", real_file)
        # 已经是临时文件，模块自己管就好
        return real_save(updates, **kwargs)

    monkeypatch.setattr(integration_config, "save_config", guarded_save)
    yield


@pytest.fixture(autouse=True)
def _clear_message_snapshot():
    """每个用例前清掉消息聚合的快照缓存。

    `collect_messages()` 带 TTL 快照（首页轮询用），不清的话
    后一个用例会直接拿到前一个的缓存结果，mock 全部失效——
    表现为"改了下断言也不对"。
    """
    from app.services import message_service

    message_service.invalidate_snapshot()
    yield
    message_service.invalidate_snapshot()


_ADMIN_FOR_TESTS = {
    "id": 1,
    "username": "test-admin",
    "display_name": "测试管理员",
    "role": "admin",
    "active": True,
    "created_at": "2026-01-01T00:00:00+00:00",
    "last_login_at": "",
}


@pytest.fixture(autouse=True)
def _bypass_login_for_business_tests(request, monkeypatch):
    """让**业务**测试不用真的走一遍登录。

    加了平台账号体系后，所有 /api/ 接口默认要求登录。业务测试关心的是
    导入/LIS/TAT/消息这些逻辑，不该为了访问接口先造账号再登录。
    所以这里直接把「解析会话」换成一个固定的管理员。

    ⚠️ `test_auth.py` 是唯一要测真实认证流程的模块，必须排除在外，
       否则它会读到假的管理员、测不到任何东西。
    """
    if "test_auth" in str(getattr(request.node, "fspath", "")):
        # autouse fixture 必须 yield，不能光 return
        yield
        return

    from app.services import auth_service

    monkeypatch.setattr(
        auth_service, "resolve_session", lambda token: dict(_ADMIN_FOR_TESTS)
    )
    yield
