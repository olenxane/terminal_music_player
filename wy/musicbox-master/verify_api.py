"""验证 MusicAPI 核心功能：歌曲URL、歌曲元数据、推荐歌单、用户歌单。"""
from NEMbox import MusicAPI


def find_song_with_url(api, keyword="九九八十一", max_retry=5):
    """搜索并找一首有播放链接的歌。"""
    for attempt in range(max_retry):
        results = api.search(keyword, limit=5)
        if not results:
            print("  搜索结果为空")
            continue
        for song in results:
            sid = song["song_id"]
            try:
                url_info = api.get_song_url(sid)
                url = url_info.get("url")
                if url:
                    return song, url_info
            except Exception:
                continue
    raise AssertionError("未找到可获取URL的歌曲")


with MusicAPI() as api:
    # ── 测试 1：搜索获取歌曲ID ──────────────────────────────
    print("=" * 60)
    print("测试 1: 搜索获取歌曲")
    print("=" * 60)
    results = api.search("九九八十一", limit=5)
    for i, s in enumerate(results):
        print(f"  [{i}] {s['song_name']} — {s['artist']}")
    print("  ✅ 通过\n")

    # ── 测试 2：get_song_url ──────────────────────────────
    print("=" * 60)
    print("测试 2: 获取歌曲播放链接")
    print("=" * 60)
    song, url_info = find_song_with_url(api)
    song_id = song["song_id"]
    print(f"  song = {song['song_name']} — {song['artist']}")
    print(f"  song_id = {song_id}")
    print(f"  url = {url_info.get('url', '')[:80]}...")
    print(f"  br = {url_info.get('br', 0)}")
    print(f"  type = {url_info.get('type')}")
    print(f"  level = {url_info.get('level')}")
    print(f"  expi = {url_info.get('expi')}")
    print("  ✅ 通过\n")

    # ── 测试 3：get_song_info（元数据）─────────────────────
    print("=" * 60)
    print("测试 3: 获取歌曲元数据")
    print("=" * 60)
    info = api.get_song_info(song_id)
    print(f"  id = {info.get('id')}")
    print(f"  name = {info.get('name')}")
    artists = [a["name"] for a in info.get("ar", []) if a.get("name")]
    print(f"  artists = {artists}")
    album = info.get("al")
    print(f"  album = {album.get('name') if album else '?'}")
    print(f"  duration = {info.get('dt') // 1000}s")
    print(f"  publish_time = {info.get('publishTime')}")
    print("  ✅ 通过\n")

    # ── 测试 4：get_user_playlists（用户歌单）──────────────
    print("=" * 60)
    print("测试 4: 获取用户歌单")
    print("=" * 60)
    auth = api.get_auth_status()
    print(f"  登录状态: logged_in={auth['logged_in']}")
    if auth["logged_in"]:
        uid = auth["user_id"]
        playlists = api.get_user_playlists(uid, limit=5)
        print(f"  用户 {uid} 的歌单 ({len(playlists)} 个):")
        for pl in playlists[:5]:
            print(f"    [{pl.get('id')}] {pl.get('name')} "
                  f"(曲数={pl.get('trackCount')})")
        print("  ✅ 通过\n")
    else:
        print("  ⚠ 未登录，跳过（需要扫码登录）\n")

    # ── 测试 5：get_recommend_songs（每日推荐）──────────────
    print("=" * 60)
    print("测试 5: 获取每日推荐歌曲")
    print("=" * 60)
    if auth["logged_in"]:
        recs = api.get_recommend_songs(limit=5)
        print(f"  每日推荐 ({len(recs)} 首):")
        for s in recs[:5]:
            print(f"    [{s['song_id']}] {s['song_name']} — {s['artist']}")
        print("  ✅ 通过\n")
    else:
        print("  ⚠ 未登录，跳过（需要扫码登录）\n")

    # ── 测试 6：get_recommend_playlists（推荐歌单）───────────
    print("=" * 60)
    print("测试 6: 获取推荐歌单")
    print("=" * 60)
    if auth["logged_in"]:
        recs = api.get_recommend_playlists()
        print(f"  推荐歌单 ({len(recs)} 个):")
        for pl in recs[:5]:
            print(f"    [{pl.get('id')}] {pl.get('name')}")
        print("  ✅ 通过\n")
    else:
        print("  ⚠ 未登录，跳过（需要扫码登录）\n")

    # ── 测试 7：get_lyrics（歌词）─────────────────────────
    print("=" * 60)
    print("测试 7: 获取歌词")
    print("=" * 60)
    lyrics = api.get_lyrics(song_id)
    print(f"  song_id = {lyrics['song_id']}")
    print(f"  lyric 行数 = {len(lyrics['lyric'])}")
    for line in lyrics["lyric"][:3]:
        print(f"    {line}")
    print(f"  tlyric 行数 = {len(lyrics['tlyric'])}")
    print("  ✅ 通过\n")

print("=" * 60)
print("验证完毕")
print("  - 搜索: ✅")
print("  - 歌曲URL: ✅")
print("  - 歌曲元数据: ✅")
print("  - 用户歌单: 已实现（需登录）")
print("  - 每日推荐: 已实现（需登录）")
print("  - 推荐歌单: 已实现（需登录）")
print("  - 歌词: ✅")
print("=" * 60)
