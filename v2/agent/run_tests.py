"""PyCharm 入口：跑 v2/agent 的测试，不需要 pytest。

为什么不直接运行 test_agent.py：PyCharm 看到 test_ 开头的文件名会自动切到 pytest
runner，而 pytest 插件依赖 pkg_resources（setuptools），项目的 v2/conftest.py 又
依赖 python-dotenv。一个只装了 LLM 客户端的干净环境两样都没有，于是还没跑到测试
就 collection error。这个文件不叫 test_*，PyCharm 会当普通脚本执行，走
test_agent.py 里那个纯 stdlib 的 runner，零依赖。

装了完整依赖的话，`pytest v2/agent/` 一样可以跑，两条路径跑的是同一批测试函数。
"""

from __future__ import annotations

import pathlib
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import importlib  # noqa: E402
import traceback  # noqa: E402


def _test_modules() -> list:
    """Discover every test_*.py in this package and its subpackages."""
    package = pathlib.Path(__file__).resolve().parent
    paths = sorted(package.glob("test_*.py")) + sorted(package.glob("*/test_*.py"))
    modules = []
    for path in paths:
        dotted = ".".join(path.relative_to(package).with_suffix("").parts)
        modules.append(importlib.import_module(f"v2.agent.{dotted}"))
    return modules


def main() -> int:
    modules = _test_modules()
    total = 0
    failures: list[str] = []

    for module in modules:
        tests = [(name, obj) for name, obj in sorted(vars(module).items())
                 if name.startswith("test_") and callable(obj)
                 and getattr(obj, "__module__", "") == module.__name__]
        print(f"\n{module.__name__}  ({len(tests)} 个)")
        total += len(tests)
        for name, fn in tests:
            try:
                fn()
                print(f"  ✓ {name}")
            except Exception:
                failures.append(f"{module.__name__}.{name}")
                print(f"  ✗ {name}")
                traceback.print_exc()

    print(f"\n{total - len(failures)}/{total} passed")
    if failures:
        print("失败：" + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
