"""bili 包接口冒烟测试。

验证项：
1. search_video / search_user（搜索）
2. get_video_info（元数据：标题/作者/时长）
3. get_audio_url + ffmpeg 带 STREAM_HEADERS 实流解码 5 秒（播放核心链路）
4. get_hot_videos 两页叠加（首页推荐）
5. get_video_subtitle + lyrics.subtitle_to_lrc（歌词）
6. auth.has_credential（凭证状态，不联网；收藏夹联网测试仅在已登录时执行）

用法: python -m bili.smoke_test
"""
from __future__ import annotations

import asyncio
import subprocess
import sys

from bili import auth, client, lyrics
from bili.exceptions import BiliError

_SECTIONS: list[tuple[str, bool, str]] = []


def _report(name: str, ok: bool, detail: str):
    _SECTIONS.append((name, ok, detail))
    flag = "✅" if ok else "❌"
    print(f"\n{flag} {name}\n   {detail}")


async def main() -> int:
    print("=" * 60)
    print("bili 包接口冒烟测试")
    print("=" * 60)

    # ---- 1. 搜索视频 ----
    bvid = ""
    try:
        results = await client.search_video("晴天 周杰伦", page=1)
        videos = [r for r in results if r.get("bvid")]
        if videos:
            top = videos[0]
            bvid = top["bvid"]
            _report("search_video", True,
                    f"{len(videos)} 条结果，第一条: {top.get('title', '')[:40]} "
                    f"(BV={bvid}, UP={top.get('author', '')}, 时长={top.get('duration', '')})")
        else:
            _report("search_video", False, "搜索成功但无视频结果")
    except BiliError as e:
        _report("search_video", False, f"失败: {e}")

    # ---- 2. 搜索用户 ----
    try:
        users = await client.search_user("周杰伦", page=1)
        if users:
            u = users[0]
            _report("search_user", True,
                    f"{len(users)} 条结果，第一条: {u.get('uname', '')} "
                    f"(mid={u.get('mid')}, 粉丝={u.get('fans')}, 视频数={u.get('videos')})")
        else:
            _report("search_user", False, "搜索成功但无用户结果")
    except BiliError as e:
        _report("search_user", False, f"失败: {e}")

    # ---- 3. 视频元数据 ----
    if bvid:
        try:
            info = await client.get_video_info(bvid)
            title = info.get("title", "")
            artist = info.get("owner", {}).get("name", "")
            duration = info.get("duration", 0)
            _report("get_video_info", bool(title),
                    f"标题={title[:40]} UP主={artist} 时长={duration}s")
        except BiliError as e:
            _report("get_video_info", False, f"失败: {e}")

    # ---- 4. 音频直链 + ffmpeg 带请求头实流解码 ----
    if bvid:
        try:
            url = await client.get_audio_url(bvid, max_quality=320)
            shown = url[:80] + "..." if len(url) > 80 else url
            hdr = "".join(f"{k}: {v}\r\n" for k, v in client.STREAM_HEADERS.items())
            cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "warning",
                "-headers", hdr, "-i", url,
                "-t", "5", "-f", "null", "-",
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                                  creationflags=subprocess.CREATE_NO_WINDOW
                                  if sys.platform == "win32" else 0)
            ok = proc.returncode == 0
            err_tail = (proc.stderr or "").strip().splitlines()[-3:]
            detail = f"直链: {shown}\n   ffmpeg 退出码={proc.returncode}"
            if err_tail:
                detail += "\n   stderr 末尾: " + " | ".join(err_tail)
            _report("get_audio_url + ffmpeg 流式解码(带Referer)", ok, detail)
        except BiliError as e:
            _report("get_audio_url + ffmpeg 流式解码(带Referer)", False, f"失败: {e}")
        except subprocess.TimeoutExpired:
            _report("get_audio_url + ffmpeg 流式解码(带Referer)", False, "ffmpeg 60s 超时")

    # ---- 5. 首页推荐：两页叠加 ----
    try:
        page1 = (await client.get_hot_videos(pn=1, ps=20)).get("list") or []
        page2 = (await client.get_hot_videos(pn=2, ps=20)).get("list") or []
        stacked = page1 + page2
        unique = len({v.get("bvid") for v in stacked})
        sample = f"{stacked[0].get('title', '')[:30]} / {stacked[0].get('owner', {}).get('name', '')}" if stacked else "-"
        _report("get_hot_videos 翻页叠加", unique >= 20,
                f"第1页 {len(page1)} 条 + 第2页 {len(page2)} 条 = {len(stacked)} 条"
                f"（去重 {unique}），示例: {sample}")
    except BiliError as e:
        _report("get_hot_videos 翻页叠加", False, f"失败: {e}")

    # ---- 6. 字幕 → LRC（歌词） ----
    if bvid:
        try:
            text, raw = await client.get_video_subtitle(bvid)
            lrc = lyrics.subtitle_to_lrc(raw)
            if lrc:
                first_lines = "\n   ".join(lrc.splitlines()[:3])
                _report("get_video_subtitle + subtitle_to_lrc", True,
                        f"{len(raw)} 条字幕，LRC 前 3 行:\n   {first_lines}")
            else:
                _report("get_video_subtitle + subtitle_to_lrc", True,
                        "该视频无字幕（接口正常返回空，播放器走无歌词兜底）")
        except BiliError as e:
            _report("get_video_subtitle + subtitle_to_lrc", False, f"失败: {e}")

    # ---- 7. 凭证状态 + 收藏夹（已登录才测） ----
    has_cred = auth.has_credential()
    if has_cred:
        try:
            folders = await client.get_favorite_list(auth.get_credential(mode="read"))
            names = [f.get("title", "") for f in folders[:3]]
            _report("get_favorite_list", True,
                    f"已有凭证，{len(folders)} 个收藏夹: {names}")
            if folders:
                fid = folders[0].get("id")
                data = await client.get_favorite_videos(fid, auth.get_credential(mode="read"), page=1)
                medias = data.get("medias") or []
                _report("get_favorite_videos", True,
                        f"收藏夹 {fid} 第1页 {len(medias)} 条, has_more={data.get('has_more')}")
        except BiliError as e:
            _report("收藏夹接口", False, f"失败: {e}")
    else:
        _report("收藏夹接口（需登录）", True,
                f"无已保存凭证（{auth.CREDENTIAL_FILE} 不存在），跳过联网测试——登录后可用")

    # ---- 汇总 ----
    passed = sum(1 for _, ok, _ in _SECTIONS if ok)
    print("\n" + "=" * 60)
    print(f"结果: {passed}/{len(_SECTIONS)} 通过")
    for name, ok, _ in _SECTIONS:
        if not ok:
            print(f"  ❌ {name}")
    print("=" * 60)
    return 0 if passed == len(_SECTIONS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
