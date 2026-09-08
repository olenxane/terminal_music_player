"""听歌报告服务器。

读取 stats.json，聚合收听数据，通过本地 HTTP 服务提供报告页面与数据接口。

用法:
    python report/server.py              # 默认 http://127.0.0.1:8765
    python report/server.py 9000         # 指定端口
    python report/server.py --open       # 启动后自动打开浏览器
    python report/server.py --stats X:\\path\\stats.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import webbrowser
from collections import Counter
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_STATS_PATH = BASE_DIR.parent / "stats.json"
INDEX_PATH = BASE_DIR / "index.html"
DEFAULT_PORT = 8765

WEEKDAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
DATE_KEY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# 中间空缺日期最多补齐的天数，避免长期未使用后图表被大量零值拉长
MAX_FILL_SPAN = 62

# ---- 歌手/声源启发式提取 ----------------------------------------------------
# 歌名形如 “【warma翻唱】xxx - 洛天依（合唱）”，从中提取声源信息，仅供报告参考。
_TAG_NOISE_SUFFIXES = sorted(
    (
        "原创国风曲", "古风原创", "国风原创", "原创曲", "原创", "翻唱",
        "专辑", "单曲", "合唱团", "合唱", "拜年纪", "拜年祭", "贺岁纪",
        "贺岁", "单品", "Remix", "remix",
    ),
    key=len,
    reverse=True,
)
_BRACKET_RE = re.compile(r"【([^【】]{2,24})】")
_FEAT_PAREN_RE = re.compile(r"[（(]\s*feat\.?\s*([^（）()]{1,24})[）)]", re.IGNORECASE)
_FEAT_BARE_RE = re.compile(r"feat\.?\s+([^【】（）()]{1,24})$", re.IGNORECASE)
_TRAILING_PAREN_RE = re.compile(r"[（(][^（）()]*[）)]\s*$")
_SUFFIX_ARTIST_RE = re.compile(r"[-–—]\s*([^【】（）()\-–—/|]{2,15})\s*$")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 ]{1,14}$")
_ARTIST_STOPWORDS = {"翻唱", "原创", "合唱", "单曲", "专辑", "PV", "Live", "live"}

# 数据层中代表"歌手未知"的占位值，统计时跳过并退回歌名启发式
_UNKNOWN_ARTISTS = {"神秘艺术家", "未知艺人"}
_PLATFORM_LABELS = {
    "local": "本地文件",
    "qq": "QQ音乐",
    "wy": "网易云音乐",
    "bili": "B站",
}


def _clean_artist_tag(tag: str) -> str:
    tag = tag.strip().strip("·。 ")
    changed = True
    while changed:
        changed = False
        for suffix in _TAG_NOISE_SUFFIXES:
            if tag.endswith(suffix) and len(tag) > len(suffix):
                tag = tag[: -len(suffix)].rstrip(" ·")
                changed = True
    return tag.strip()


def _is_plausible_artist(name: str) -> bool:
    if not 2 <= len(name) <= 15 or name in _ARTIST_STOPWORDS:
        return False
    return bool(_CJK_RE.search(name) or _LATIN_NAME_RE.match(name))


def extract_artists(title: str) -> frozenset[str]:
    """从歌名中启发式提取歌手/声源名。"""
    names: set[str] = set()

    tag = _BRACKET_RE.search(title)
    if tag:
        cleaned = _clean_artist_tag(tag.group(1))
        if _is_plausible_artist(cleaned):
            names.add(cleaned)

    for pattern in (_FEAT_PAREN_RE, _FEAT_BARE_RE):
        found = pattern.search(title)
        if found:
            name = found.group(1).strip().rstrip("，,、 ")
            if _is_plausible_artist(name):
                names.add(name)

    stripped = _TRAILING_PAREN_RE.sub("", title).strip()
    suffix = _SUFFIX_ARTIST_RE.search(stripped)
    if suffix:
        name = suffix.group(1).strip()
        if _is_plausible_artist(name):
            names.add(name)

    return frozenset(names)


# ---- 数据聚合 ---------------------------------------------------------------

def normalize_entries(songs):
    """把播放明细规整为 (title, artist, hour, platform)。

    兼容 v1 纯歌名字符串（视为无歌手/无时间/无平台），v2 为 {t,a,ts,p} 对象。
    """
    for e in songs or []:
        if isinstance(e, str):
            title = e.strip()
            if title:
                yield title, None, None, None
        elif isinstance(e, dict):
            title = str(e.get("t") or "").strip()
            if not title:
                continue
            ts = e.get("ts")
            hour = None
            if isinstance(ts, str) and ts[:2].isdigit():
                h = int(ts[:2])
                if 0 <= h <= 23:
                    hour = h
            platform = e.get("p")
            if platform not in _PLATFORM_LABELS:
                platform = None
            yield title, (e.get("a") or None), hour, platform


def _longest_streak(dates: list[date]) -> dict:
    best_days, best_start, best_end = 0, None, None
    run_days, run_start, prev = 0, None, None
    for day in dates:
        if prev is not None and (day - prev).days == 1:
            run_days += 1
        else:
            run_days, run_start = 1, day
        if run_days > best_days:
            best_days, best_start, best_end = run_days, run_start, day
        prev = day
    return {
        "days": best_days,
        "start": best_start.isoformat() if best_start else None,
        "end": best_end.isoformat() if best_end else None,
    }


def build_report(stats_path: Path) -> dict:
    """读取 stats.json 并聚合成报告数据。"""
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        raw = json.loads(stats_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"ok": False, "error": f"未找到数据文件：{stats_path}", "generated_at": generated_at}
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return {"ok": False, "error": f"数据文件解析失败：{exc}", "generated_at": generated_at}

    days: dict = raw.get("days") or {}
    active_days = sum(
        1 for info in days.values()
        if (info.get("songs") or (info.get("seconds") or 0) > 0)
    )

    daily: list[dict] = []
    total_seconds = 0.0
    total_plays = 0
    unique_titles: set[str] = set()
    song_counts: Counter[str] = Counter()
    song_first: dict[str, str] = {}
    song_last: dict[str, str] = {}
    singer_counts: Counter[str] = Counter()  # 真实歌手字段统计
    title_artist_counts: Counter[str] = Counter()  # 歌名启发式声源统计
    title_heur_cache: dict[str, frozenset[str]] = {}
    platform_counts: Counter[str] = Counter()
    hourly_plays = [0] * 24
    hourly_known = 0
    weekday_seconds = [0.0] * 7
    weekday_plays = [0] * 7

    for date_key in sorted(k for k in days if DATE_KEY_RE.match(k)):
        info = days[date_key] or {}
        seconds = float(info.get("seconds") or 0)
        entries = list(normalize_entries(info.get("songs")))
        daily.append({"date": date_key, "seconds": round(seconds, 1), "plays": len(entries)})

        total_seconds += seconds
        total_plays += len(entries)
        try:
            weekday = date.fromisoformat(date_key).weekday()
        except ValueError:
            weekday = None
        if weekday is not None:
            weekday_seconds[weekday] += seconds
            weekday_plays[weekday] += len(entries)

        for title, artist, hour, platform in entries:
            unique_titles.add(title)
            song_counts[title] += 1
            song_first.setdefault(title, date_key)
            song_last[title] = date_key
            if artist and artist not in _UNKNOWN_ARTISTS:
                singer_counts[artist] += 1
            if title not in title_heur_cache:
                title_heur_cache[title] = extract_artists(title)
            for name in title_heur_cache[title]:
                title_artist_counts[name] += 1
            if hour is not None:
                hourly_plays[hour] += 1
                hourly_known += 1
            platform_counts[platform or "unknown"] += 1

    # 补齐中间未收听的日期，让趋势图更真实
    if daily:
        start_day = date.fromisoformat(daily[0]["date"])
        end_day = date.fromisoformat(daily[-1]["date"])
        if (end_day - start_day).days + 1 <= MAX_FILL_SPAN:
            known = {d["date"]: d for d in daily}
            filled, cursor = [], start_day
            while cursor <= end_day:
                key = cursor.isoformat()
                filled.append(known.get(key, {"date": key, "seconds": 0, "plays": 0}))
                cursor += timedelta(days=1)
            daily = filled

    top_songs = [
        {"title": title, "count": count, "first": song_first[title], "last": song_last[title]}
        for title, count in song_counts.most_common(10)
    ]

    def _top_names(counter: Counter) -> list[dict]:
        threshold = 2 if len(counter) > 10 else 1
        return [
            {"name": name, "count": count}
            for name, count in counter.most_common(15)
            if count >= threshold
        ]

    singers = _top_names(singer_counts)
    title_artists = _top_names(title_artist_counts)

    platforms = [
        {"key": key, "label": _PLATFORM_LABELS.get(key, "未知"), "plays": count}
        for key, count in platform_counts.most_common()
    ]
    peak_idx = max(range(24), key=lambda h: hourly_plays[h])
    peak_hour = (
        {"hour": peak_idx, "plays": hourly_plays[peak_idx]}
        if hourly_plays[peak_idx] > 0 else None
    )
    top_platform = next(
        (p for p in platforms if p["key"] != "unknown" and p["plays"] > 0), None
    )

    busiest = max(daily, key=lambda d: d["seconds"], default=None)
    highlights = {
        "busiest_day": dict(busiest) if busiest and busiest["seconds"] > 0 else None,
        "top_song": top_songs[0] if top_songs else None,
        "max_streak": _longest_streak(sorted(
            date.fromisoformat(key) for key in days if DATE_KEY_RE.match(key)
        )),
        "peak_hour": peak_hour,
        "top_platform": top_platform,
    }

    span_days = 0
    if daily:
        span_days = (date.fromisoformat(daily[-1]["date"]) - date.fromisoformat(daily[0]["date"])).days + 1

    return {
        "ok": True,
        "generated_at": generated_at,
        "source": stats_path.name,
        "range": {
            "start": daily[0]["date"] if daily else None,
            "end": daily[-1]["date"] if daily else None,
            "span_days": span_days,
            "active_days": active_days,
        },
        "totals": {
            "seconds": round(total_seconds, 1),
            "plays": total_plays,
            "unique_songs": len(unique_titles),
            "avg_per_active_day": round(total_seconds / active_days, 1) if active_days else 0,
        },
        "daily": daily,
        "top_songs": top_songs,
        "weekday": [
            {"label": WEEKDAY_LABELS[i], "seconds": round(weekday_seconds[i], 1), "plays": weekday_plays[i]}
            for i in range(7)
        ],
        "artists": title_artists,
        "singers": singers,
        "platforms": platforms,
        "hourly": {
            "plays": [{"hour": h, "plays": hourly_plays[h]} for h in range(24)],
            "known": hourly_known,
        },
        "highlights": highlights,
    }


# ---- HTTP 服务 ---------------------------------------------------------------

class ReportHandler(BaseHTTPRequestHandler):
    server_version = "MusicReport/1.0"
    protocol_version = "HTTP/1.1"

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self) -> None:  # noqa: N802  (BaseHTTPRequestHandler 命名约定)
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            try:
                body = INDEX_PATH.read_bytes()
            except FileNotFoundError:
                self._send(500, "缺少 index.html".encode("utf-8"), "text/plain; charset=utf-8")
                return
            self._send(200, body, "text/html; charset=utf-8")
        elif path == "/api/report":
            payload = build_report(STATS_PATH)
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
        else:
            self._send(404, '{"ok": false, "error": "not found"}'.encode("utf-8"),
                       "application/json; charset=utf-8")


STATS_PATH: Path = DEFAULT_STATS_PATH


def main(argv: list[str] | None = None) -> None:
    global STATS_PATH

    # Windows 控制台默认 GBK，输出中文时做保护
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass

    parser = argparse.ArgumentParser(description="听歌报告服务器")
    parser.add_argument("port", nargs="?", type=int, default=DEFAULT_PORT,
                        help=f"监听端口（默认 {DEFAULT_PORT}）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--stats", default=str(DEFAULT_STATS_PATH), help="stats.json 路径")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    args = parser.parse_args(argv)

    STATS_PATH = Path(args.stats)
    if not INDEX_PATH.exists():
        sys.exit(f"缺少页面文件：{INDEX_PATH}")

    server = ThreadingHTTPServer((args.host, args.port), ReportHandler)
    url = f"http://{args.host}:{args.port}"
    print(f"♪ 听歌报告已就绪: {url}")
    print(f"  数据文件: {STATS_PATH}")
    print("  按 Ctrl+C 退出")
    if args.open:
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
