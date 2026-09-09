"""旧文件名兼容入口。

请运行同目录下的 ``95598_interactive_console.py``。保留这个入口是为了让
之前创建的快捷方式或命令仍然有效；实际控制台由新脚本自带的本地服务提供。
"""

from __future__ import annotations

import runpy
from pathlib import Path


if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).with_name("95598_interactive_console.py")), run_name="__main__")
