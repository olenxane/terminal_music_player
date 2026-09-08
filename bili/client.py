"""Bilibili API 客户端（裁剪自 bilibili-cli 的 client.py）。

异步封装层：所有公共函数均为 async，基于 bilibili-api-python + aiohttp。
调用方（mp/online_music.py 的 AsyncRunner 或任意事件循环）负责桥接为同步调用。

错误模型：第三方 SDK 异常统一映射为 .exceptions 中的本地异常类型
（AuthenticationError / NotFoundError / RateLimitError / NetworkError / BiliError）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any

import aiohttp
import requests
from bilibili_api import favorite_list, hot, search, user, video
from bilibili_api.exceptions import (
    ApiException,
    CredentialNoBiliJctException,
    CredentialNoSessdataException,
    NetworkException,
    ResponseCodeException,
    ResponseException,
)
from bilibili_api.utils.network import Credential

from .exceptions import AuthenticationError, BiliError, InvalidBvidError, NetworkError, NotFoundError, RateLimitError

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/133.0.0.0 Safari/537.36"
)

# 访问 B站音频/视频直链必须携带的请求头（CDN 防盗链校验 Referer）。
# 播放器侧 ffmpeg（-headers）与下载回退（requests）都必须使用。
STREAM_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Referer": "https://www.bilibili.com",
}


# ---------------------------------------------------------------------------
# BV 号工具
# ---------------------------------------------------------------------------

_BVID_RE = re.compile(r"\bBV[0-9A-Za-z]{10}\b")


def extract_bvid(url_or_bvid: str) -> str:
    """从 URL 或裸 BV 号中提取 BV 号。"""
    match = _BVID_RE.search(url_or_bvid)
    if match:
        return match.group(0)
    raise InvalidBvidError(f"无法提取 BV 号: {url_or_bvid}")


def _map_api_error(action: str, exc: Exception) -> BiliError:
    """第三方 API 异常 → 本地稳定异常类型。"""
    if isinstance(exc, BiliError):
        return exc

    if isinstance(exc, (CredentialNoSessdataException, CredentialNoBiliJctException)):
        return AuthenticationError(f"{action}: {exc}")

    if isinstance(exc, ResponseCodeException):
        code = getattr(exc, "code", None)
        if code in {-101, -111}:
            return AuthenticationError(f"{action}: {exc}")
        if code in {-404, 62002, 62004}:
            return NotFoundError(f"{action}: {exc}")
        if code in {-412, 412}:
            return RateLimitError(f"{action}: {exc}")
        return BiliError(f"{action}: [{code}] {exc}")

    if isinstance(exc, (NetworkException, ResponseException, aiohttp.ClientError, asyncio.TimeoutError)):
        return NetworkError(f"{action}: {exc}")

    if isinstance(exc, ApiException):
        return BiliError(f"{action}: {exc}")

    return BiliError(f"{action}: {exc}")


async def _call_api(action: str, awaitable):
    """执行协程并归一化异常。"""
    try:
        return await awaitable
    except Exception as exc:
        raise _map_api_error(action, exc) from exc


# ---------------------------------------------------------------------------
# 视频信息（元数据：标题 / UP主 / 时长）
# ---------------------------------------------------------------------------

async def get_video_info(bvid: str, credential: Credential | None = None) -> dict[str, Any]:
    """获取视频元数据（title / owner.name / duration / stat 等）。"""
    v = video.Video(bvid=bvid, credential=credential)
    return await _call_api("获取视频信息", v.get_info())


# ---------------------------------------------------------------------------
# 字幕（歌词来源）
# ---------------------------------------------------------------------------

async def get_video_subtitle(
    bvid: str, credential: Credential | None = None
) -> tuple[str, list]:
    """获取视频字幕。

    返回 (纯文本, 原始字幕条目列表)。条目结构 {"from": 秒, "to": 秒, "content": 文本}。
    注意：B站字幕接口要求登录。未传入凭证时自动加载已保存凭证；
    无凭证或凭证失效时返回 ("", [])（调用方作"无歌词"处理），不抛异常。
    """
    if credential is None or not getattr(credential, "sessdata", ""):
        from . import auth
        # mode="optional"：纯文件/缓存读取，不联网校验（本函数可能运行在
        # 调用方事件循环内，禁止嵌套 asyncio.run）；过期凭证由下方
        # AuthenticationError 兜底。
        credential = auth.get_credential(mode="optional")
        if credential is None:
            logger.info("字幕接口需要登录，当前无凭证: %s", bvid)
            return "", []

    v = video.Video(bvid=bvid, credential=credential)

    try:
        pages = await _call_api("获取视频分P信息", v.get_pages())
    except AuthenticationError as e:
        logger.warning("获取字幕失败（凭证无效）: %s", e)
        return "", []
    if not pages:
        logger.warning("视频 %s 无分P信息", bvid)
        return "", []

    cid = pages[0].get("cid")
    if not cid:
        return "", []

    try:
        player_info = await _call_api("获取播放器信息", v.get_player_info(cid=cid))
    except AuthenticationError as e:
        logger.warning("获取字幕失败（凭证无效）: %s", e)
        return "", []
    subtitle_info = player_info.get("subtitle", {})
    if not subtitle_info or not subtitle_info.get("subtitles"):
        return "", []

    subtitle_list = subtitle_info["subtitles"]

    subtitle_url = None
    for sub in subtitle_list:
        if "zh" in sub.get("lan", "").lower():
            subtitle_url = sub.get("subtitle_url", "")
            break
    if not subtitle_url and subtitle_list:
        subtitle_url = subtitle_list[0].get("subtitle_url", "")
    if not subtitle_url:
        return "", []

    if subtitle_url.startswith("//"):
        subtitle_url = "https:" + subtitle_url

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(subtitle_url) as resp:
                resp.raise_for_status()
                subtitle_data = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
        raise NetworkError(f"下载字幕失败: {e}") from e

    if "body" in subtitle_data:
        raw = subtitle_data["body"]
        texts = [item.get("content", "") for item in raw]
        return "\n".join(texts), raw

    return "", []


# ---------------------------------------------------------------------------
# 搜索
# ---------------------------------------------------------------------------

async def search_video(keyword: str, page: int = 1) -> list[dict[str, Any]]:
    """按关键词搜索视频。返回结果条目列表（bvid/title/author/duration/play 等）。"""
    res = await _call_api("搜索视频", search.search_by_type(
        keyword=keyword,
        search_type=search.SearchObjectType.VIDEO,
        page=page,
    ))
    return res.get("result", [])


async def search_user(keyword: str, page: int = 1) -> list[dict[str, Any]]:
    """按关键词搜索 UP 主。返回结果条目列表（mid/uname/fans/videos/usign 等）。"""
    res = await _call_api("搜索用户", search.search_by_type(
        keyword=keyword,
        search_type=search.SearchObjectType.USER,
        page=page,
    ))
    return res.get("result", [])


# ---------------------------------------------------------------------------
# UP 主视频列表
# ---------------------------------------------------------------------------

async def get_user_videos(
    uid: int, count: int = 10, credential: Credential | None = None
) -> list[dict[str, Any]]:
    """获取 UP 主最新视频列表（自动翻页凑满 count 条，最多 20 页）。"""
    u = user.User(uid=uid, credential=credential)

    results: list[dict[str, Any]] = []
    page = 1
    per_page = min(count, 50)

    while len(results) < count:
        try:
            data = await _call_api("获取用户视频列表", u.get_videos(ps=per_page, pn=page))
        except BiliError as e:
            if page == 1:
                raise
            logger.warning("获取用户视频第 %d 页失败: %s", page, e)
            break

        vlist = data.get("list", {}).get("vlist", [])
        if not vlist:
            break

        for v in vlist:
            results.append(v)
            if len(results) >= count:
                break

        page += 1
        if page > 20:
            break

    return results


# ---------------------------------------------------------------------------
# 收藏夹（需登录）
# ---------------------------------------------------------------------------

async def get_self_info(credential: Credential) -> dict[str, Any]:
    """获取当前登录用户信息。"""
    return await _call_api("获取当前登录用户信息", user.get_self_info(credential))


async def get_favorite_list(credential: Credential) -> list[dict[str, Any]]:
    """获取登录用户的所有收藏夹（id / title / media_count）。"""
    me = await get_self_info(credential)
    uid = me.get("mid")
    if uid is None:
        raise BiliError("获取收藏夹列表: 当前用户信息缺少 mid")

    fav_data = await _call_api(
        "获取收藏夹列表",
        favorite_list.get_video_favorite_list(uid=uid, credential=credential),
    )
    return fav_data.get("list", [])


async def get_favorite_videos(
    fav_id: int, credential: Credential, page: int = 1
) -> dict[str, Any]:
    """获取收藏夹内容。返回原始响应（medias / has_more 等），每页 20 条。"""
    return await _call_api(
        "获取收藏夹内容",
        favorite_list.get_video_favorite_list_content(
            media_id=fav_id, page=page, credential=credential
        ),
    )


# ---------------------------------------------------------------------------
# 首页推荐（热门视频，pn/ps 分页可叠加）
# ---------------------------------------------------------------------------

async def get_hot_videos(pn: int = 1, ps: int = 20) -> dict[str, Any]:
    """获取热门视频列表（可翻页，调用方多页叠加增长列表）。"""
    return await _call_api("获取热门视频", hot.get_hot_videos(pn=pn, ps=ps))


_RCMD_URL = "https://api.bilibili.com/x/web-interface/index/top/rcmd"
_FINGER_SPI = "https://api.bilibili.com/x/frontend/finger/spi"

# 匿名推荐流需要的设备指纹（实测无 buvid Cookie 会被 412 风控拦截）
_anon_buvid: tuple[str, str] | None = None


def _buvid_cookie_header(credential: Credential | None) -> str:
    """构造推荐流请求的 Cookie 头。

    有凭证 → 账号 Cookie（个性化推荐）；无凭证 → finger/spi 匿名设备指纹。
    """
    global _anon_buvid
    if credential is not None and getattr(credential, "sessdata", ""):
        parts = [f"SESSDATA={credential.sessdata}",
                 f"bili_jct={credential.bili_jct}",
                 f"DedeUserID={credential.dedeuserid}"]
        if getattr(credential, "buvid3", ""):
            parts.append(f"buvid3={credential.buvid3}")
        if getattr(credential, "buvid4", ""):
            parts.append(f"buvid4={credential.buvid4}")
        return "; ".join(parts)
    if _anon_buvid is None:
        try:
            d = requests.get(_FINGER_SPI, headers=STREAM_HEADERS, timeout=10).json().get("data") or {}
            _anon_buvid = (d.get("b_3", ""), d.get("b_4", ""))
        except Exception as e:
            logger.warning("获取匿名设备指纹失败: %s", e)
            _anon_buvid = ("", "")
    b3, b4 = _anon_buvid
    return "; ".join(p for p in (f"buvid3={b3}" if b3 else "",
                                 f"buvid4={b4}" if b4 else "") if p)


async def get_recommend_videos(page: int = 1, per_page: int = 12,
                               credential: Credential | None = None) -> list[dict[str, Any]]:
    """B站首页**个性化推荐流**（每次请求随 fresh_idx 返回不同内容）。

    匿名可访问（通用推荐）；传入 credential 时按账号个性化推荐。
    注意热门接口（popular）是编辑榜单、更新频率为天级，与本接口用途不同。
    """

    def _fetch() -> list[dict[str, Any]]:
        headers = dict(STREAM_HEADERS)
        cookie = _buvid_cookie_header(credential)
        if cookie:
            headers["Cookie"] = cookie
        r = requests.get(_RCMD_URL, headers=headers, timeout=10, params={
            "fresh_idx": max(1, page),
            "fresh_idx_1h": max(1, page),
            "ps": max(1, min(per_page, 30)),
        })
        return r.json()

    j = await asyncio.to_thread(_fetch)
    if j.get("code") != 0:
        raise BiliError(f"获取推荐失败: code={j.get('code')} {j.get('message', '')}")
    items = (j.get("data") or {}).get("item") or []
    # goto=av 为普通视频；番剧/课程等其它类型没有 bvid，直接过滤
    return [x for x in items if x.get("goto") == "av" and x.get("bvid")]


# ---------------------------------------------------------------------------
# 音频直链（播放用）
# ---------------------------------------------------------------------------

# 音质阶梯：请求更高音质取不到时逐级回退
# 注意：B站标准音频最高 192K（SDK 17.x 枚举无 320K），"320" 表示尽力而为最高音质
_AUDIO_QUALITY_LADDER = (320, 192, 132, 64)

# 音质代码 → 质量等级（数值大小不代表质量顺序：30251 Hi-Res < 30280 192K）
# Dolby(30250)/Hi-Res(30251) 需大会员，播放器默认排除
_AUDIO_RANK = {30216: 1, 30232: 2, 30280: 3}
_AUDIO_RANK_CAP = {"_192K": 3, "_132K": 2, "_64K": 1}


def _audio_quality_choices(max_quality: int) -> tuple:
    """按请求上限生成去重后的音质枚举优先级列表。"""
    from bilibili_api.video import AudioQuality
    preferred = {
        320: AudioQuality._192K,
        192: AudioQuality._192K,
        132: AudioQuality._132K,
        64: AudioQuality._64K,
    }
    first = preferred.get(int(max_quality), AudioQuality._192K)
    ladder = (AudioQuality._192K, AudioQuality._132K, AudioQuality._64K)
    out, seen = [], set()
    for q in (first, *ladder):
        if q not in seen:
            seen.add(q)
            out.append(q)
    return tuple(out)


async def get_audio_urls(
    bvid: str, credential: Credential | None = None, max_quality: int = 320
) -> list[str]:
    """获取音频直链候选列表（按音质从高到低，含备用线路）。

    max_quality: 音质上限 kbps（320=尽力最高/192/132/64），取不到时自动回退。
    DASH 音频走原生解析：规避 SDK VideoCodecs 枚举对 hvc1 编码的覆盖缺陷
    （detect_best_streams 排序时崩溃），并收集 backupUrl 备用线路——
    主线路常被分配到不可达的 mcdn P2P 节点，备用线路实测可达。
    全部候选不可用时抛 BiliError。
    """
    from bilibili_api.video import VideoDownloadURLDataDetecter

    v = video.Video(bvid=bvid, credential=credential)
    last_error: Exception | None = None

    for quality in _audio_quality_choices(max_quality):
        download_data = await _call_api("获取下载地址", v.get_download_url(page_index=0))

        # 原生 DASH 音频解析：按音质等级取最高档，收集 baseUrl + backupUrl
        rank_cap = _AUDIO_RANK_CAP.get(quality.name, 3)
        best_rank, best_urls = -1, []
        for a in (download_data.get("dash") or {}).get("audio") or []:
            rank = _AUDIO_RANK.get(a.get("id", 0), -1)
            if rank < 0 or rank > rank_cap:
                continue
            urls = [a.get("baseUrl") or a.get("base_url") or ""] + \
                   list(a.get("backupUrl") or a.get("backup_url") or [])
            urls = [u for u in urls if u]
            if urls and rank > best_rank:
                best_rank, best_urls = rank, urls
            elif urls and rank == best_rank:
                best_urls += [u for u in urls if u not in best_urls]
        if best_urls:
            return best_urls

        # 无 DASH（老式 FLV/MP4 容器）：SDK 兜底检测
        try:
            detector = VideoDownloadURLDataDetecter(download_data)
            if detector.check_flv_mp4_stream():
                streams = detector.detect_best_streams(
                    audio_max_quality=quality,
                    no_dolby_audio=True,
                    no_hires=True,
                )
                if streams and streams[0] and hasattr(streams[0], "url"):
                    return [streams[0].url]
        except Exception as e:
            logger.warning("SDK 流检测失败（%s）", e)

        last_error = BiliError(f"无可用音频流（音质上限 {quality.name}）")

    raise last_error or BiliError("无法获取音频流（可能是会员专属视频）")


async def get_audio_url(
    bvid: str, credential: Credential | None = None, max_quality: int = 320
) -> str:
    """获取首选音频直链（兼容包装，完整候选线路见 get_audio_urls）。"""
    return (await get_audio_urls(bvid, credential=credential, max_quality=max_quality))[0]


# ---------------------------------------------------------------------------
# 音频下载（播放回退 / 离线）
# ---------------------------------------------------------------------------

async def download_audio(audio_url: str, output_path: str) -> int:
    """下载音频流到文件，返回写入字节数。带 Referer/UA 头、3 次重试、256KB 分块。"""
    timeout = aiohttp.ClientTimeout(total=300)
    max_retries = 3

    for attempt in range(max_retries):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(audio_url, headers=STREAM_HEADERS) as resp:
                    if resp.status == 200:
                        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
                        total_bytes = 0
                        with open(output_path, "wb") as f:
                            async for chunk in resp.content.iter_chunked(256 * 1024):
                                if not chunk:
                                    continue
                                f.write(chunk)
                                total_bytes += len(chunk)
                        return total_bytes
                    if attempt < max_retries - 1:
                        logger.warning("下载 HTTP %d，重试...", resp.status)
                        await asyncio.sleep(2)
                    else:
                        raise NetworkError(f"音频下载失败: HTTP {resp.status}")
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if attempt < max_retries - 1:
                logger.warning("下载异常: %s，重试...", e)
                await asyncio.sleep(2)
            else:
                raise NetworkError(f"音频下载失败: {e}") from e

    raise NetworkError("音频下载失败: 重试次数用尽")
