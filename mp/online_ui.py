"""在线音乐界面渲染：沿用现有主题色彩"""
from __future__ import annotations
from rich.console import Group
from rich.text import Text
from rich.cells import cell_len

from .ui import _fit_text, _gradient_color


# ---- 视图名常量 ----
VIEW_PLATFORM = "platform"
VIEW_QQ_HOME = "qq_home"
VIEW_QQ_SEARCH = "qq_search"
VIEW_QQ_FAV_SONGS = "qq_fav"
VIEW_QQ_PLAYLISTS = "qq_playlists"
VIEW_QQ_RANKINGS = "qq_rankings"
VIEW_QQ_DAILY = "qq_daily"
VIEW_WY_HOME = "wy_home"
VIEW_WY_SEARCH = "wy_search"
VIEW_WY_RANKINGS = "wy_rankings"
VIEW_WY_DAILY = "wy_daily"
VIEW_BILI_HOME = "bili_home"
VIEW_BILI_SEARCH = "bili_search"
VIEW_BILI_USER_SEARCH = "bili_user_search"
VIEW_BILI_RECOMMEND = "bili_recommend"
VIEW_BILI_FAVORITES = "bili_favorites"
VIEW_SONG_LIST = "song_list"
VIEW_LOADING = "loading"
VIEW_LOGIN = "login"

# ---- 下载视图名常量 ----
VIEW_DOWNLOAD_SEARCH = "download_search"
VIEW_DOWNLOAD_PROGRESS = "download_progress"
VIEW_DOWNLOAD_QUEUE = "download_queue"

QQ_MENU = ["搜索歌曲", "每日推荐", "收藏歌曲", "我的歌单", "排行榜", "登录/退出"]
WY_MENU = ["搜索歌曲", "排行榜", "每日推荐", "登录/退出"]
BILI_MENU = ["搜索视频", "UP主搜索", "热门", "首页推荐", "我的收藏", "导入Cookie", "登录/退出"]

# 列表可视行数（粘性视口的窗口大小，_render_list 默认值保持一致）
LIST_MAX_VISIBLE = 15

# 平台标识 → 显示名（替代原先 "qq" 二元写死的写法）
PLATFORM_NAMES = {"qq": "QQ音乐", "wy": "网易云音乐", "bili": "哔哩哔哩"}


def platform_display_name(platform: str) -> str:
    return PLATFORM_NAMES.get(platform, platform)


def _header(title: str, theme, subtitle: str = "") -> Text:
    line = Text(no_wrap=True)
    line.append(f"🎵 {title}", style=f"bold {theme.accent}")
    if subtitle:
        line.append(f"  {subtitle}", style=theme.secondary)
    return line


def _footer(text: str, theme) -> Text:
    return Text(text, style=theme.dim)


def _render_list(items: list, selector_index: int, theme, width: int,
                  max_visible: int = 15, get_name=None,
                  in_queue_fn=None, gradient_items: bool = False,
                  view_start: int | None = None) -> list:
    """通用列表渲染，返回 Text 行列表

    gradient_items=True 时，非选中行名称用左右渐变色（歌曲列表场景）。
    view_start 不为 None 时使用"粘性视口"：窗口首行由调用方维护，
    仅在选择器越出视口边缘时滚动（大列表尾部追加内容不会引起窗口跳动）；
    为 None 时保持旧的"以选中项为中心"逻辑。
    """
    lines = []
    total = len(items)
    if total == 0:
        lines.append(Text("  无结果", style=theme.dim))
        return lines

    if view_start is not None:
        start = max(0, min(view_start, max(0, total - max_visible)))
    else:
        start = max(0, selector_index - max_visible // 2)
        end = min(total, start + max_visible)
        if end - start < max_visible and start > 0:
            start = max(0, end - max_visible)
    end = min(total, start + max_visible)

    for i in range(start, end):
        name = get_name(items[i]) if get_name else str(items[i])
        name = _fit_text(name, width - 12)
        line = Text(no_wrap=True)
        if gradient_items:
            # 上下渐变：每行一个颜色，按可见窗口内的行位置取色
            # （单行仍是一个样式段，不会增加每帧 SGR 段数）
            visible = max(end - start - 1, 1)
            row_color = _gradient_color((i - start) / visible,
                                        theme.spectrum_gradient)
        if i == selector_index:
            line.append(f"  ▶ ", style=f"bold {theme.accent}")
            if gradient_items:
                line.append(name, style=f"bold {row_color}")
            else:
                line.append(name, style=f"bold {theme.accent}")
        else:
            if gradient_items:
                line.append(f"    {name}", style=row_color)
            else:
                line.append(f"    {name}", style=theme.primary)
        if in_queue_fn and in_queue_fn(items[i]):
            line.append(" [队列]", style=theme.secondary)
        lines.append(line)

    if total > max_visible:
        lines.append(Text(f"  ({selector_index + 1}/{total})", style=theme.dim))
    return lines


def build_online_platform_ui(cfg, selector_index: int, width: int = 50) -> Group:
    theme = cfg.theme
    lines = [_header("在线音乐", theme)]
    lines.append(Text("", style=""))
    platforms = ["QQ音乐", "网易云音乐", "哔哩哔哩"]
    lines.extend(_render_list(platforms, selector_index, theme, width,
                              get_name=lambda x: x))
    lines.append(_footer("Enter: 选择  Esc: 返回", theme))
    return Group(*lines)


def build_online_menu_ui(cfg, platform: str, menu_items: list,
                          selector_index: int, logged_in: bool = False,
                          user_name: str = "", width: int = 50) -> Group:
    theme = cfg.theme
    name = platform_display_name(platform)
    sub = f"[{'已登录: ' + user_name if logged_in else '未登录'}]"
    lines = [_header(name, theme, sub)]
    lines.append(Text("", style=""))
    lines.extend(_render_list(menu_items, selector_index, theme, width,
                              get_name=lambda x: x))
    lines.append(_footer("Enter: 选择  Esc: 返回", theme))
    return Group(*lines)


def _song_display_name(track) -> str:
    """歌曲显示名：歌名-歌手 + [VIP] + ♥(已收藏)"""
    liked = " ♥" if getattr(track, "liked", False) else ""
    vip = " [VIP]" if getattr(track, "is_vip", False) else ""
    return f"{track.title} - {track.artist}{liked}{vip}"


def build_online_search_ui(cfg, platform: str, search_str: str,
                             tracks: list, selector_index: int,
                             width: int = 50, playlist=None,
                             view_start: int | None = None) -> Group:
    theme = cfg.theme
    name = platform_display_name(platform)
    lines = [_header(f"{name}搜索", theme)]
    search_line = Text(no_wrap=True)
    search_line.append("搜索: ", style=theme.secondary)
    search_line.append(search_str, style=theme.primary)
    search_line.append("█", style=theme.accent)
    lines.append(search_line)
    lines.append(Text("", style=""))
    in_q = playlist.is_online_in_queue if playlist else None
    lines.extend(_render_list(tracks, selector_index, theme, width,
                              get_name=_song_display_name, in_queue_fn=in_q,
                              gradient_items=True, view_start=view_start))
    lines.append(_footer("Enter: 立即播放  →: 加入队列  ←: 收藏  Esc: 返回", theme))
    return Group(*lines)


def build_online_user_search_ui(cfg, search_str: str, users: list,
                                selector_index: int, width: int = 50) -> Group:
    """B站UP主搜索界面：搜索栏 + 用户列表（Enter 查看该UP主视频）"""
    # 防御：仅保留 dict 条目（UP主对象），防止共享列表残留的 OnlineTrack 等被当用户渲染
    users = [u for u in users if isinstance(u, dict)]
    theme = cfg.theme
    lines = [_header("哔哩哔哩UP主搜索", theme)]
    search_line = Text(no_wrap=True)
    search_line.append("搜索: ", style=theme.secondary)
    search_line.append(search_str, style=theme.primary)
    search_line.append("█", style=theme.accent)
    lines.append(search_line)
    lines.append(Text("", style=""))

    def _user_name(u):
        return (f"{u.get('uname', '')}  "
                f"(粉丝 {u.get('fans', 0)} · 视频 {u.get('videos', 0)})")

    lines.extend(_render_list(users, selector_index, theme, width,
                              get_name=_user_name))
    lines.append(_footer("Enter: 查看该UP主视频  Esc: 返回", theme))
    return Group(*lines)


def build_online_song_list_ui(cfg, title: str, tracks: list,
                               selector_index: int, width: int = 50,
                               playlist=None, view_start: int | None = None) -> Group:
    theme = cfg.theme
    lines = [_header(title, theme)]
    lines.append(Text("", style=""))
    in_q = playlist.is_online_in_queue if playlist else None
    lines.extend(_render_list(tracks, selector_index, theme, width,
                              get_name=_song_display_name, in_queue_fn=in_q,
                              gradient_items=True, view_start=view_start))
    lines.append(_footer("Enter: 立即播放  →: 加入队列  ←: 收藏  Esc: 返回", theme))
    return Group(*lines)


def build_online_playlist_list_ui(cfg, title: str, items: list,
                                   selector_index: int, width: int = 50) -> Group:
    theme = cfg.theme
    lines = [_header(title, theme)]
    lines.append(Text("", style=""))

    def _item_name(item):
        if isinstance(item, dict):
            name = item.get("name", item.get("title", "未知"))
            count = item.get("song_count", item.get("listen_num", ""))
            if count:
                return f"{name} ({count}首)"
            return name
        return str(item)
    lines.extend(_render_list(items, selector_index, theme, width,
                              get_name=_item_name))
    lines.append(_footer("Enter: 查看歌曲  Esc: 返回", theme))
    return Group(*lines)


def build_online_loading_ui(cfg, message: str = "加载中...") -> Group:
    theme = cfg.theme
    lines = [
        _header("在线音乐", theme),
        Text("", style=""),
        Text(f"  ⏳ {message}", style=theme.secondary),
    ]
    return Group(*lines)


def build_online_login_ui(cfg, platform: str, qr_text: str = "",
                          status: str = "", width: int = 50) -> Group:
    theme = cfg.theme
    name = platform_display_name(platform)
    lines = [_header(f"{name}登录", theme)]
    lines.append(Text("", style=""))
    if qr_text:
        for line in qr_text.splitlines():
            lines.append(Text(line, style=theme.text))
    else:
        lines.append(Text("  正在获取二维码...", style=theme.secondary))
    if status:
        lines.append(Text(f"  {status}", style=theme.secondary))
    lines.append(_footer("Esc: 取消", theme))
    return Group(*lines)


# ═══════════════════════════════════════════════════════════
# 下载工具 UI 函数（供 music_downloader.py 使用）
# ═══════════════════════════════════════════════════════════

def build_download_search_ui(cfg, platform: str, search_str: str,
                             tracks: list, selector_index: int,
                             width: int = 50, queue_size: int = 0) -> Group:
    """下载搜索界面：搜索栏 + 歌曲列表"""
    theme = cfg.theme
    name = platform_display_name(platform)
    lines = [_header(f"{name}下载", theme)]
    search_line = Text(no_wrap=True)
    search_line.append("搜索: ", style=theme.secondary)
    search_line.append(search_str, style=theme.primary)
    search_line.append("█", style=theme.accent)
    lines.append(search_line)
    lines.append(Text("", style=""))

    def _song_name(track):
        vip = " [VIP]" if getattr(track, "is_vip", False) else ""
        return f"{track.title} - {track.artist}{vip}"

    lines.extend(_render_list(tracks, selector_index, theme, width,
                              get_name=_song_name, gradient_items=True))
    footer_parts = [Text("Enter: 下载", style=theme.dim)]
    footer_parts.append(Text("  →: 加入队列", style=theme.dim))
    if queue_size > 0:
        footer_parts.append(Text(f"  队列: {queue_size}首", style=theme.secondary))
    footer_parts.append(Text("  Esc: 返回", style=theme.dim))
    lines.append(Group(*footer_parts))
    return Group(*lines)


def _render_bar(pct: float, length: int = 30, filled_char: str = "█",
                 empty_char: str = "░") -> str:
    """渲染纯文本进度条"""
    filled = int(round(pct * length))
    return filled_char * filled + empty_char * (length - filled)


def build_download_progress_ui(cfg, title: str, artist: str,
                                pct: float, speed: str,
                                total_mb: float, cur_mb: float,
                                width: int = 50) -> Group:
    """下载进度界面"""
    theme = cfg.theme
    lines = [_header("正在下载", theme)]
    lines.append(Text("", style=""))

    # 歌曲名（截断适配宽度）
    name = f"{title} - {artist}"
    lines.append(Text(f"  {_fit_text(name, width - 4)}", style=theme.primary))
    lines.append(Text("", style=""))

    # 进度条
    bar_line = Text(no_wrap=True)
    bar = _render_bar(pct / 100.0, 30)
    bar_line.append(f"  [{bar}] {pct:5.1f}%", style=theme.accent)
    lines.append(bar_line)

    # 进度信息
    info = Text(no_wrap=True)
    info.append(f"  {cur_mb:.1f} MB / ", style=theme.text)
    info.append(f"{total_mb:.1f} MB", style=theme.dim)
    if speed:
        info.append(f"  {speed}/s", style=theme.secondary)
    lines.append(info)

    if pct >= 100:
        lines.append(Text("", style=""))
        lines.append(Text("  ✓ 下载完成", style=f"bold {theme.accent}"))

    return Group(*lines)


def build_download_queue_ui(cfg, queue_items: list, selector_index: int,
                             width: int = 50) -> Group:
    """下载队列界面"""
    theme = cfg.theme
    lines = [_header("下载队列", theme)]
    lines.append(Text("", style=""))

    if not queue_items:
        lines.append(Text("  队列为空", style=theme.dim))
    else:
        def _item_name(item):
            status = item.get("status", "pending")
            prefix = {"done": "✓", "downloading": "⬇", "failed": "✗",
                      "pending": "○"}.get(status, "○")
            return f"{prefix} {item.get('title', '')} - {item.get('artist', '')}"

        lines.extend(_render_list(queue_items, selector_index, theme, width,
                                  get_name=_item_name))
    lines.append(_footer("Enter: 下载选中  →: 全部下载  Esc: 返回", theme))
    return Group(*lines)
