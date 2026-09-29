"""``python -m objstore_tool`` 的入口。

Python 不能直接执行一个包，必须由这个模块把控制权转给 main()。
缺了它 ``python -m objstore_tool`` 会直接报 "No module named objstore_tool.__main__"。
"""

from .main import main

if __name__ == "__main__":
    raise SystemExit(main())
