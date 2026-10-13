"""pytest 全局配置。

★ 最重要的一条：**测试绝不能碰用户真实的数据目录**。
引擎在导入时就会按 LIB_DIR 建目录，跑测试时的 import_file() 会调 save_state()
写 index.json。如果不隔离，测试会把真实索引覆盖成空、把源文件副本写进真实
sources —— 曾因此把 7 个数据集、70 万行的索引清空过一次。

做法：在 import 引擎**之前**设好 SLH_DATA_DIR，引擎导入时会把所有数据路径
重定向到临时目录（见 smartlab_engine.py 顶部的 _DATA_HOME 分支）。
"""
from pathlib import Path
import os
import sys
import tempfile

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
