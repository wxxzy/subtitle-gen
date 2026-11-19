"""让 `python -m subgen` 直接可用，也提供 `python -m subgen.cli` 的等价入口。"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
