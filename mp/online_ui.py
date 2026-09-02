"""在线音乐界面渲染：沿用现有主题色彩"""
from __future__ import annotations
from rich.console import Group
from rich.text import Text
from rich.cells import cell_len

from .ui import _fit_text


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
VIEW_SONG_LIST = "song_list"
VIEW_LOADING = "loading"
VIEW_LOGIN = "login"

QQ_MENU = ["搜索歌曲", "每日推荐", "收藏歌曲", "我的歌单", "排行榜", "登录/退出"]
WY_MENU = ["搜索歌曲", "排行榜", "每日推荐", "登录/退出"]


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
                  in_queue_fn=None) -> list:
    """通用列表渲染，返回 Text 行列表"""
    lines = []
    total = len(items)
    if total == 0:
        lines.append(Text("  无结果", style=theme.dim))
        return lines

    start = max(0, selector_index - max_visible // 2)
    end = min(total, start + max_visible)
    if end - start < max_visible and start > 0:
        start = max(0, end - max_visible)

    for i in range(start, end):
        name = get_name(items[i]) if get_name else str(items[i])
        name = _fit_text(name, width - 12)
        line = Text(no_wrap=True)
        if i == selector_index:
            line.append(f"  ▶ {name}", style=f"bold {theme.accent}")
        else:
            line.append(f"    {name}", style=theme.text)
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
    platforms = ["QQ音乐", "网易云音乐"]
    lines.extend(_render_list(platforms, selector_index, theme, width,
                              get_name=lambda x: x))
    lines.append(_footer("Enter: 选择  Esc: 返回", theme))
    return Group(*lines)


def build_online_menu_ui(cfg, platform: str, menu_items: list,
                          selector_index: int, logged_in: bool = False,
                          user_name: str = "", width: int = 50) -> Group:
    theme = cfg.theme
    name = "QQ音乐" if platform == "qq" else "网易云音乐"
    sub = f"[{'已登录: ' + user_name if logged_in else '未登录'}]"
    lines = [_header(name, theme, sub)]
    lines.append(Text("", style=""))
    lines.extend(_render_list(menu_items, selector_index, theme, width,
                              get_name=lambda x: x))
    lines.append(_footer("Enter: 选择  Esc: 返回", theme))
    return Group(*lines)


def build_online_search_ui(cfg, platform: str, search_str: str,
                             tracks: list, selector_index: int,
                             width: int = 50, playlist=None) -> Group:
    theme = cfg.theme
    name = "QQ音乐" if platform == "qq" else "网易云音乐"
    lines = [_header(f"{name}搜索", theme)]
    search_line = Text(no_wrap=True)
    search_line.append("搜索: ", style=theme.secondary)
    search_line.append(search_str, style=theme.primary)
    search_line.append("█", style=theme.accent)
    lines.append(search_line)
    lines.append(Text("", style=""))
    def _song_name(track):
        vip = " [VIP]" if getattr(track, "is_vip", False) else ""
        return f"{track.title} - {track.artist}{vip}"
    in_q = playlist.is_online_in_queue if playlist else None
    lines.extend(_render_list(tracks, selector_index, theme, width,
                              get_name=_song_name, in_queue_fn=in_q))
    lines.append(_footer("Enter: 立即播放  →: 加入队列  Esc: 返回", theme))
    return Group(*lines)


def build_online_song_list_ui(cfg, title: str, tracks: list,
                               selector_index: int, width: int = 50,
                               playlist=None) -> Group:
    theme = cfg.theme
    lines = [_header(title, theme)]
    lines.append(Text("", style=""))
    def _song_name(track):
        vip = " [VIP]" if getattr(track, "is_vip", False) else ""
        return f"{track.title} - {track.artist}{vip}"
    in_q = playlist.is_online_in_queue if playlist else None
    lines.extend(_render_list(tracks, selector_index, theme, width,
                              get_name=_song_name, in_queue_fn=in_q))
    lines.append(_footer("Enter: 立即播放  →: 加入队列  Esc: 返回", theme))
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
    name = "QQ音乐" if platform == "qq" else "网易云音乐"
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
