"""命令行入口：login / parse / download / resume / tasks / config / history。"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import List, Optional

from .__init__ import __version__
from .api import BiliApi, BiliApiError
from .config import Config, DEFAULTS
from .constants import QUALITY_MAP
from .cookie_store import CookieStore
from .manager import DownloadManager
from .parser import BiliParser, ParseError
from .storage import Storage
from .utils import (format_duration, format_size, parse_range_selection,
                    shorten)

# 数据目录：优先环境变量 BILI_HOME，默认 ~/.bilidownload
APP_DIR = os.environ.get("BILI_HOME") or os.path.join(os.path.expanduser("~"), ".bilidownload")


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


class App:
    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path or os.path.join(APP_DIR, "config.json")
        self.config = Config(self.config_path)
        self.cookie_store = CookieStore(os.path.join(APP_DIR, "cookies.enc"))
        self.storage = Storage(os.path.join(APP_DIR, "tasks.db"))
        self.api = BiliApi(self.config, self.cookie_store)
        self.parser = BiliParser(self.api)
        self.manager = DownloadManager(self.config, self.api, self.storage)

    def require_login(self) -> bool:
        if self.api.is_logged_in:
            return True
        print("提示：当前未登录。部分高清画质与会员内容需要登录，可先运行 `bilidownload login`。",
              file=sys.stderr)
        return False

    # ---------- 命令 ----------
    def cmd_login(self, args: argparse.Namespace) -> int:
        if args.cookie:
            self.api.set_cookie_string(args.cookie)
            if not self.api.is_logged_in:
                print("Cookie 中缺少 SESSDATA，请检查输入。", file=sys.stderr)
                return 1
            self.api.save_cookies()
            print("Cookie 已保存（本地加密）。")
        elif args.status:
            info = self.api.account_info()
            if info.get("isLogin"):
                print(f"已登录：{info.get('uname')}"
                      f"{'（大会员）' if info.get('vip') else ''}")
            else:
                print("未登录或 Cookie 已失效")
            return 0
        elif args.logout:
            self.api.clear_cookies()
            print("已清除本地 Cookie。")
            return 0
        else:
            def show_qr(url: str, key: str) -> None:
                # 终端里直接画出二维码，手机可以扫屏幕
                try:
                    from .qrcode_min import to_ascii
                    print()
                    print(to_ascii(url, border=2))
                except Exception:
                    pass

            try:
                self.api.qr_login(timeout=args.timeout, on_qr_url=show_qr)
            except BiliApiError as exc:
                print(f"登录失败: {exc}", file=sys.stderr)
                return 1
        return 0

    def cmd_parse(self, args: argparse.Namespace) -> int:
        try:
            result = self.parser.parse(args.url, require_login=args.login)
        except (ParseError, BiliApiError) as exc:
            print(f"解析失败: {exc}", file=sys.stderr)
            return 1
        print(f"\n标题     : {result.series_title}")
        print(f"类型     : {result.type}  标识: {result.media_id}")
        print(f"分集数   : {len(result.episodes)}")
        if result.quality_options:
            print(f"可用画质 : {result.quality_names()}")
        if result.description:
            print(f"简介     : {shorten(result.description.replace(chr(10), ' '), 80)}")
        print("\n分集列表：")
        width = min(max(len(ep.display_title) for ep in result.episodes) + 2, 60)
        for ep in result.episodes:
            print(f"  {ep.index:>3}. {ep.display_title:<{width}}  "
                  f"{format_duration(ep.duration):>8}  cid={ep.cid}")
        print("\n下载示例：")
        print(f"  bilidownload download \"{args.url}\" --range 1-{len(result.episodes)}")
        return 0

    def cmd_download(self, args: argparse.Namespace) -> int:
        # 应用命令行覆盖配置
        overrides = {}
        if args.dir:
            overrides["download_dir"] = args.dir
        if args.threads:
            overrides["threads"] = args.threads
        if args.concurrency:
            overrides["concurrency"] = args.concurrency
        if args.quality is not None:
            overrides["quality"] = args.quality
        if args.codec:
            overrides["codec_pref"] = args.codec
        if args.speed_limit:
            overrides["speed_limit_kbps"] = args.speed_limit
        if args.proxy:
            overrides["proxy"] = args.proxy
        if args.audio_only:
            overrides["download_audio_only"] = True
        if args.subtitle:
            overrides["save_subtitle"] = True
        if args.danmaku:
            overrides["save_danmaku"] = True
        if args.cover:
            overrides["save_cover"] = True
        if args.nfo:
            overrides["save_nfo"] = True
        if args.danmaku_ass:
            overrides["danmaku_ass"] = True
        if args.template:
            overrides["name_template"] = args.template
        if args.season:
            overrides["season"] = args.season
        if args.no_dedup:
            overrides["dedup"] = False
        if args.ffmpeg_path:
            overrides["ffmpeg_path"] = args.ffmpeg_path
        # 命令行参数只对本次运行生效，不写回配置文件。
        # 否则 `download X --quality 16` 会把这个选择悄悄变成永久默认，
        # 下次不带参数运行时用户会拿到意料之外的低画质/旧目录。
        self.config.update(**overrides)

        # 恢复模式
        if args.resume:
            self.manager.resume_all()
            return 0

        try:
            result = self.parser.parse(args.url, require_login=args.login)
            if args.list_only:
                print(f"{result.series_title}: {len(result.episodes)} 集")
                for ep in result.episodes:
                    print(f"  {ep.index:>3}. {ep.display_title}")
                return 0
            indices = parse_range_selection(args.range or "all", len(result.episodes))
            if not indices:
                print("选集为空，请检查 --range 参数（如 1-12）。", file=sys.stderr)
                return 1
            print(f"已解析：{result.series_title}，共 {len(result.episodes)} 集，选中 {len(indices)} 集")
            eff_quality = args.quality if args.quality is not None else int(self.config.get("quality") or 0)
            if eff_quality:
                print(f"指定画质：{eff_quality} ({QUALITY_MAP.get(eff_quality, '未知')})")
            else:
                print("画质：自动选择最高可用")
            tasks = self.manager.build_tasks(
                result, indices,
                quality=eff_quality,
                codec_pref=args.codec or str(self.config.get("codec_pref") or ""),
                audio_only=args.audio_only or None,
            )
            if not tasks:
                print("没有需要下载的任务（可能全部已下载，使用 --no-dedup 强制下载）。")
                return 0
            self.manager.run(tasks, concurrency=args.concurrency or self.config.concurrency)
            return 0
        except (ParseError, BiliApiError) as exc:
            print(f"下载失败: {exc}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("\n已中断，未完成任务可在下次运行时用 --resume 继续。", file=sys.stderr)
            return 130

    def cmd_resume(self, args: argparse.Namespace) -> int:
        self.manager.resume_all()
        return 0

    def cmd_tasks(self, args: argparse.Namespace) -> int:
        if args.clear:
            self.storage.clear_tasks(only_terminal=not args.all)
            print("任务记录已清除。")
            return 0
        if args.retry_failed:
            self.manager.retry_failed()
            return 0
        tasks = self.storage.list_tasks()
        if not tasks:
            print("暂无任务记录。")
            return 0
        print(f"{'ID':<22} {'状态':<12} {'进度':>7} {'标题'}")
        print("-" * 88)
        for t in tasks:
            print(f"{t.task_id:<22} {t.status:<12} {t.progress:>6.1f}% "
                  f"{shorten(t.episode.display_title, 36)}")
        return 0

    def cmd_template(self, args: argparse.Namespace) -> int:
        from .naming import AVAILABLE_PLACEHOLDERS, preview, validate

        tpl = args.template or str(self.config.get("name_template"))
        if args.list_placeholders:
            print("可用占位符（支持中文别名，见 README）：")
            print("  " + ", ".join(AVAILABLE_PLACEHOLDERS))
            return 0
        unknown = validate(tpl)
        if unknown:
            print(f"警告：未知占位符 {unknown}（将被忽略）", file=sys.stderr)
        print(f"模板：{tpl}")
        print(f"预览：{preview(tpl)}")
        return 0

    def cmd_config(self, args: argparse.Namespace) -> int:
        if args.key:
            if args.value is None:
                print(f"{args.key} = {self.config.get(args.key)}")
                return 0
            try:
                # 尝试解析数字/布尔
                value: object = args.value
                if value.lower() in ("true", "false"):
                    value = value.lower() == "true"
                else:
                    try:
                        value = int(value)
                    except ValueError:
                        try:
                            value = float(value)
                        except ValueError:
                            pass
                self.config.set(args.key, value)
                self.config.save()
                print(f"已设置 {args.key} = {value}")
            except KeyError as exc:
                print(f"错误: {exc}", file=sys.stderr)
                return 1
            return 0
        print(f"配置文件: {self.config.path}\n")
        for key, value in self.config.data.items():
            print(f"  {key:<22} = {value}")
        print("\n可用配置项：")
        print("  " + ", ".join(DEFAULTS.keys()))
        return 0

    def cmd_history(self, args: argparse.Namespace) -> int:
        rows = self.storage.list_history(args.limit)
        if not rows:
            print("暂无下载历史。")
            return 0
        for row in rows:
            print(f"{row['downloaded_at']}  {shorten(row['title'], 40):<42} "
                  f"{shorten(row['output_path'], 50)}")
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bilidownload",
        description="B 站批量下载工具 (M1 CLI MVP) - 开箱即用、多线程分片、无损混流",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--gui", action="store_true", help="启动图形界面（浏览器界面）")
    parser.add_argument("--web", action="store_true", help="启动浏览器界面（同 --gui）")
    parser.add_argument("--port", type=int, default=0, help="浏览器界面端口（0=自动）")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--config", help="指定配置文件路径")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    sub = parser.add_subparsers(dest="command")

    p_login = sub.add_parser("login", help="扫码登录或管理 Cookie")
    p_login.add_argument("--cookie", help="直接传入 Cookie 字符串（含 SESSDATA）")
    p_login.add_argument("--status", action="store_true", help="查看登录状态")
    p_login.add_argument("--logout", action="store_true", help="清除本地 Cookie")
    p_login.add_argument("--timeout", type=int, default=180, help="扫码等待秒数")

    p_parse = sub.add_parser("parse", help="解析链接，列出分集")
    p_parse.add_argument("url", help="B 站链接或 BV/av 号")
    p_parse.add_argument("--login", action="store_true", help="要求登录态解析")

    p_dl = sub.add_parser("download", help="下载链接（可接 --resume 恢复）")
    p_dl.add_argument("url", nargs="?", help="B 站链接或 BV/av 号")
    p_dl.add_argument("--range", "-r", help="选集范围，如 1-12,15,18-20；默认全部")
    p_dl.add_argument("--dir", help="下载目录")
    p_dl.add_argument("--threads", "-t", type=int, help="单文件分片线程数 (4-32)")
    p_dl.add_argument("--concurrency", "-c", type=int, help="同时下载任务数 (2-3)")
    p_dl.add_argument("--quality", "-q", type=int, help="画质 qn（0=自动最高）")
    p_dl.add_argument("--codec", help="编码偏好，如 AVC,HEVC,AV1")
    p_dl.add_argument("--speed-limit", type=int, help="限速 KB/s (0=不限)")
    p_dl.add_argument("--proxy", help="代理 http:// 或 socks5://")
    p_dl.add_argument("--audio-only", action="store_true", help="仅下载音频")
    p_dl.add_argument("--subtitle", action="store_true", help="同时下载字幕")
    p_dl.add_argument("--danmaku", action="store_true", help="同时下载弹幕")
    p_dl.add_argument("--cover", action="store_true", help="同时下载封面")
    p_dl.add_argument("--nfo", action="store_true", help="生成 NFO 元数据")
    p_dl.add_argument("--danmaku-ass", action="store_true", help="弹幕同时转 ASS 字幕")
    p_dl.add_argument("--template", help="命名模板，如 {系列名}/S{season:02d}E{index:02d} - {标题}.{ext}")
    p_dl.add_argument("--season", type=int, help="番剧赛季号（默认 1）")
    p_dl.add_argument("--no-dedup", action="store_true", help="不跳过已下载项")
    p_dl.add_argument("--ffmpeg-path", help="ffmpeg 可执行文件路径")
    p_dl.add_argument("--login", action="store_true", help="要求登录态")
    p_dl.add_argument("--list-only", action="store_true", help="仅列出选集不下载")
    p_dl.add_argument("--resume", action="store_true", help="恢复所有未完成任务")

    p_resume = sub.add_parser("resume", help="恢复未完成任务（断点续传）")

    p_tasks = sub.add_parser("tasks", help="查看/管理任务队列")
    p_tasks.add_argument("--retry-failed", action="store_true", help="重置失败任务以便重试")
    p_tasks.add_argument("--clear", action="store_true", help="清除任务记录")
    p_tasks.add_argument("--all", action="store_true", help="配合 --clear 清除所有（含未完成）")

    p_config = sub.add_parser("config", help="查看/修改配置")
    p_config.add_argument("key", nargs="?", help="配置项名称")
    p_config.add_argument("value", nargs="?", help="新值（留空则读取）")

    p_hist = sub.add_parser("history", help="查看下载历史（去重记录）")
    p_hist.add_argument("--limit", type=int, default=50)

    p_tpl = sub.add_parser("template", help="命名模板：查看占位符与预览")
    p_tpl.add_argument("template", nargs="?", help="待预览的模板（留空用当前配置）")
    p_tpl.add_argument("--list", dest="list_placeholders", action="store_true",
                       help="列出所有可用占位符")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.gui or args.web:
        # 浏览器界面：绕开 macOS 26 自带 Tk 8.5 的渲染缺陷
        from .webserver import serve
        serve(port=args.port, open_browser=not args.no_browser)
        return 0
    if not args.command:
        parser.print_help()
        return 0
    setup_logging(args.log_level)
    app = App(args.config)
    handlers = {
        "login": app.cmd_login,
        "parse": app.cmd_parse,
        "download": app.cmd_download,
        "resume": app.cmd_resume,
        "tasks": app.cmd_tasks,
        "config": app.cmd_config,
        "history": app.cmd_history,
        "template": app.cmd_template,
    }
    try:
        return handlers[args.command](args)
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
