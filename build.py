#!/usr/bin/env python3
"""打包脚本：零依赖生成可分发的单文件应用。

优先方案（stdlib zipapp）：把 bili_downloader 包打成 BiliDownload.pyz，
用系统 python3 即可运行，无需安装任何第三方库。

可选方案（PyInstaller）：若已安装 PyInstaller，可生成真正独立的可执行文件
（自带 Python 解释器），适合目标机器无 Python 的场景。

用法：
    python3 build.py                    # 生成 zipapp + 各平台启动器到 dist/
    python3 build.py --pyinstaller      # 额外生成独立二进制（需先安装 PyInstaller）
    python3 build.py --clean            # 清理 dist/
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PKG = ROOT / "bili_downloader"
DIST = ROOT / "dist"
APP_NAME = "BiliDownload"
VERSION = "0.1.0"


def read_version() -> str:
    init = PKG / "__init__.py"
    if init.is_file():
        for line in init.read_text(encoding="utf-8").splitlines():
            if line.startswith("__version__"):
                return line.split("=", 1)[1].strip().strip('"\'')
    return VERSION


def build_zipapp(target: Path) -> Path:
    """用标准库 zipapp 生成单文件 .pyz。"""
    staging = DIST / "_staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    # 复制包
    shutil.copytree(PKG, staging / "bili_downloader",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    # __main__.py：zipapp 入口，默认启动浏览器界面，带 cli 参数切换
    main_py = staging / "__main__.py"
    main_py.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if '--cli' in sys.argv:\n"
        "    sys.argv.remove('--cli')\n"
        "    from bili_downloader.cli import main\n"
        "    sys.exit(main())\n"
        "else:\n"
        "    from bili_downloader.webserver import main\n"
        "    sys.exit(main())\n",
        encoding="utf-8",
    )

    out = target / f"{APP_NAME}-{VERSION}.pyz"
    if out.exists():
        out.unlink()
    zipapp.create_archive(staging, out, interpreter="/usr/bin/env python3", compressed=True)
    shutil.rmtree(staging, ignore_errors=True)
    print(f"[OK] zipapp: {out}")
    return out


def write_launchers(target: Path, pyz_name: str) -> None:
    """生成各平台启动脚本。"""
    # macOS / Linux shell 启动器（保留错误输出，失败时不闪退）
    sh = target / f"启动{APP_NAME}.command"
    sh.write_text(
        "#!/bin/sh\n"
        'cd "$(dirname "$0")"\n'
        'echo "正在启动 BiliDownload，稍后会自动打开浏览器界面…"\n'
        'echo "（若浏览器没有自动打开，请复制下面打印的地址手动访问）"\n'
        'echo ""\n'
        f'python3 "{pyz_name}" "$@"\n'
        'status=$?\n'
        'if [ "$status" -ne 0 ]; then\n'
        '  echo ""\n'
        '  echo "程序异常退出（代码 $status）。按回车键关闭窗口…"\n'
        '  read dummy\n'
        'fi\n',
        encoding="utf-8",
    )
    os.chmod(sh, os.stat(sh).st_mode | stat.S_IEXEC)

    # Windows 批处理启动器
    bat = target / f"启动{APP_NAME}.bat"
    bat.write_text(
        "@echo off\n"
        'cd /d "%~dp0"\n'
        f'python "{pyz_name}" %*\n'
        "pause\n",
        encoding="utf-8-sig",
    )
    print(f"[OK] 启动器: {sh.name}, {bat.name}")


def build_pyinstaller(target: Path) -> None:
    """可选：用 PyInstaller 生成独立二进制。"""
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("[跳过] 未安装 PyInstaller（pip install pyinstaller 后可生成独立二进制）")
        return
    import subprocess

    spec = DIST / f"{APP_NAME}.spec"
    cmd = [
        sys.executable, "-m", "PyInstaller", "--onefile", "--windowed",
        "--name", APP_NAME,
        "--paths", str(ROOT),
        str(PKG / "gui.py") if (PKG / "gui.py").exists() else str(ROOT / "gui.py"),
        "--distpath", str(target), "--workpath", str(DIST / "build"),
        "--specpath", str(DIST),
    ]
    print("[信息] 运行 PyInstaller…")
    try:
        subprocess.run(cmd, check=True, cwd=str(ROOT))
        print(f"[OK] 独立二进制: {target / APP_NAME}")
    except subprocess.CalledProcessError as exc:
        print(f"[警告] PyInstaller 打包失败: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="打包 BiliDownload")
    parser.add_argument("--pyinstaller", action="store_true", help="额外生成独立二进制（需 PyInstaller）")
    parser.add_argument("--clean", action="store_true", help="清理 dist/ 目录")
    args = parser.parse_args()

    global VERSION
    VERSION = read_version()

    if args.clean:
        if DIST.exists():
            shutil.rmtree(DIST)
            print(f"[OK] 已清理 {DIST}")
        return 0

    DIST.mkdir(parents=True, exist_ok=True)
    pyz = build_zipapp(DIST)
    write_launchers(DIST, pyz.name)
    if args.pyinstaller:
        build_pyinstaller(DIST)

    # 使用说明
    (DIST / "README.txt").write_text(
        f"{APP_NAME} {VERSION}\n"
        "========================\n\n"
        "方式一（推荐，零依赖）：确保系统已安装 Python 3，双击「启动" + APP_NAME + ".command」\n"
        "  （Windows 双击「启动" + APP_NAME + ".bat」），或命令行运行：\n"
        f"    python3 {pyz.name}\n"
        f"    python3 {pyz.name} --cli download BV1xx411c7mD\n\n"
        "方式二（独立二进制，若已生成）：直接运行 " + APP_NAME + " 可执行文件，无需 Python。\n\n"
        "首次使用需在 GUI 中扫码登录，或在命令行运行 login。\n",
        encoding="utf-8",
    )
    print(f"[完成] 产物目录：{DIST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
