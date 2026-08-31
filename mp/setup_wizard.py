"""首次启动向导：交互式询问用户输入一个或多个音频目录，
校验路径并写回配置文件，后续启动直接复用，不再询问
（除非用户主动通过命令行参数 --setup 重新配置，或 music_dirs 为空）。
"""
from __future__ import annotations
import os
from rich.console import Console
from rich.prompt import Prompt, Confirm

from .config import save_music_dirs
from .playlist import scan_music_dir


def _validate_dir(raw_path: str) -> tuple:
    """返回 (是否有效, 规范化路径, 提示信息)"""
    path = os.path.abspath(os.path.expanduser(raw_path.strip().strip('"').strip("'")))
    if not os.path.exists(path):
        return False, path, f"路径不存在: {path}"
    if not (os.path.isdir(path) or os.path.isfile(path)):
        return False, path, f"既不是目录也不是文件: {path}"
    count = len(scan_music_dir(path))
    if count == 0:
        return True, path, f"[黄色警告] 该路径下暂未扫描到支持的音频文件（mp3/wav/flac/ogg/m4a/aiff），仍会保存"
    return True, path, f"找到 {count} 个音频文件"


def run_setup_wizard(console: Console, existing_dirs: list = None) -> list:
    """交互式收集音频目录，返回最终确认的目录列表（不做磁盘写入）"""
    console.print()
    console.print("[bold cyan]♫ 欢迎使用终端音乐播放器！[/bold cyan]")
    console.print("首次启动需要设置音乐库目录，之后每次启动会自动扫描这些目录并播放。")
    console.print("[dim]可以输入文件夹路径，也可以输入单个音频文件路径；每次输入一条，回车确认，直接回车结束。[/dim]")
    console.print()

    dirs = list(existing_dirs) if existing_dirs else []

    if dirs:
        console.print(f"[dim]当前已保存的目录（共 {len(dirs)} 条）:[/dim]")
        for i, d in enumerate(dirs, 1):
            console.print(f"  {i}. {d}")
        if not Confirm.ask("是否要重新设置音频目录？", default=False):
            return dirs
        dirs = []

    while True:
        prompt_label = f"请输入第 {len(dirs) + 1} 条音频目录路径（直接回车结束输入）"
        raw = Prompt.ask(prompt_label, default="", show_default=False)
        if raw.strip() == "":
            if not dirs:
                if Confirm.ask("尚未输入任何目录，是否确认不设置任何音乐目录？", default=False):
                    break
                continue
            break
        ok, norm_path, msg = _validate_dir(raw)
        if not ok:
            console.print(f"[bold red]✗ {msg}[/bold red] 请重新输入。")
            continue
        if norm_path in dirs:
            console.print("[yellow]该路径已添加过，跳过。[/yellow]")
            continue
        console.print(f"[green]✓ {msg}[/green]  -> {norm_path}")
        dirs.append(norm_path)

    if dirs:
        console.print()
        console.print(f"[bold green]已设置 {len(dirs)} 个音频目录：[/bold green]")
        for i, d in enumerate(dirs, 1):
            console.print(f"  {i}. {d}")
    else:
        console.print("[yellow]未设置任何音频目录，稍后可通过命令行传入路径，或重新运行配置向导。[/yellow]")

    console.print()
    return dirs


def ensure_music_dirs(console: Console, cfg, force_setup: bool = False) -> list:
    """确保有可用的音频目录配置：
    - 若 force_setup 为 True，或配置中 music_dirs 为空，则运行向导并保存
    - 否则直接返回已有配置
    """
    if not force_setup and cfg.music_dirs:
        return cfg.music_dirs

    dirs = run_setup_wizard(console, existing_dirs=cfg.music_dirs)
    try:
        save_music_dirs(cfg.path, dirs)
    except Exception as e:
        console.print(f"[bold red]警告: 保存配置文件失败 ({e})，本次仍会使用刚才输入的目录运行。[/bold red]")
    cfg.music_dirs = dirs
    return dirs
