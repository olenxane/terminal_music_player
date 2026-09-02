"""QQMusicBox API 集成示例

使用 qq/ 目录下的 qqmusicbox 包。
将本文件所在目录加入 sys.path 后即可导入。
"""
import asyncio
import sys
from pathlib import Path

# 将 qq/ 目录加入搜索路径（本示例假设 example.py 位于 qq/ 目录内）
sys.path.insert(0, str(Path(__file__).parent))

from qqmusicbox import QQMusicClient, configure_paths
from qqmusicbox import Song, Album, Artist, Playlist, Lyric, LyricLine


async def demo():
    # 可选：自定义凭证/缓存路径（在创建客户端前调用）
    # configure_paths(config_dir="./data/config", cache_dir="./data/cache")

    client = QQMusicClient()

    # 1. 恢复登录态（如之前登录过，凭证自动从加密文件加载）
    if client.load_credential():
        print(f"已恢复登录，账号ID: {client.get_credential().musicid}")

    # 2. 搜索歌曲
    songs = await client.search_songs("晴天", num=5)
    print(f"搜索到 {len(songs)} 首歌曲")
    for s in songs[:3]:
        print(f"  {s.name} - {s.singer} (mid={s.mid}, {s.duration}s, VIP={s.is_vip})")

    if not songs:
        return
    song = songs[0]

    # 3. 获取歌词（原文 + 翻译 + 罗马音）
    lyric = await client.get_lyric(song.mid)
    if lyric:
        print(f"歌词 {len(lyric.lines)} 行:")
        for line in lyric.lines[:3]:
            print(f"  [{line.time_ms}ms] {line.text}")
            if line.translation:
                print(f"     翻译: {line.translation}")

    # 4. 获取播放/下载 URL（VIP 歌曲需登录）
    url = await client.get_song_url(song.mid, quality=320)
    print(f"播放链接: {url if url else '（VIP歌曲需登录）'}")

    # 5. 获取歌曲信息 / 专辑歌曲
    info = await client.get_song_info(song.mid)
    if info:
        print(f"歌曲详情: {info.name} - {info.singer}")

    # 6. 热门歌曲 / 推荐歌单 / 排行榜
    hot = await client.get_hot_songs(num=3)
    print(f"热歌榜: {[f'{h.name}-{h.singer}' for h in hot]}")

    recs = await client.get_recommend_playlists(num=3)
    print(f"推荐歌单: {[r.name for r in recs]}")

    tops = await client.get_top_list()
    if tops:
        print(f"排行榜[{tops[0]['id']}]: {tops[0]['title']}")

    # 7. 收藏 / 检查收藏（需登录）
    if client.has_credential():
        liked = await client.is_song_liked(song.mid)
        print(f"收藏状态: {liked}")
        if not liked:
            await client.like_song(song.mid)
            print(f"已收藏: {song.name}")

    # 8. 上下文管理器（自动释放资源）
    async with QQMusicClient() as ctx:
        s = await ctx.search_songs("周杰伦", num=3)
        print(f"上下文管理器搜索: {len(s)} 首")

    await client.close()  # 或 close_client()


if __name__ == "__main__":
    asyncio.run(demo())
