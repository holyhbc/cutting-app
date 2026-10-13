# -*- coding: utf-8 -*-
"""一键跑全部测试。

    python tests/run_all.py            # 跑全部
    python tests/run_all.py -v         # 详细
    python tests/run_all.py -k 权限     # 只跑名字里含「权限」的

不需要装任何第三方包，也不连数据库、不联网。
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

os.environ["HBC_QUIET_MIGRATE"] = "1"


def main():
    argv = [a for a in sys.argv[1:] if a not in ("-v", "--verbose")]
    verbosity = 2 if ("-v" in sys.argv or "--verbose" in sys.argv) else 1

    loader = unittest.TestLoader()
    if argv:
        # -k 过滤
        pats = [a for a in argv if a.startswith("-k")]
        if pats:
            kw = pats[0][2:] or pats[0][3:]
            loader.testNamePatterns = [f"*{kw}*"]
        else:
            loader.testNamePatterns = argv
    suite = loader.discover(HERE, pattern="test_*.py")

    runner = unittest.TextTestRunner(verbosity=verbosity, buffer=False)
    result = runner.run(suite)

    n_ok = result.testsRun - len(result.failures) - len(result.errors)
    print()
    print("=" * 56)
    print(f"  通过 {n_ok} / 共 {result.testsRun}")
    if result.failures:
        print(f"  失败 {len(result.failures)}")
    if result.errors:
        print(f"  错误 {len(result.errors)}")
    print("=" * 56)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())