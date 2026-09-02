"""示例：搜索并下载《九九八十一》"""
import requests
from NEMbox import MusicAPI

with MusicAPI() as api:
    # 搜索
    results = api.search("九九八十一", limit=5)
    print(f"搜索到 {len(results)} 首结果:")
    for i, song in enumerate(results):
        print(f"  [{i}] {song['song_name']} — {song['artist']}")

    # 选择第 2 首（洛天依Official / 乐正绫 原版）
    song = results[1]
    print(f"\n选择: {song['song_name']} — {song['artist']}")

    # 获取播放/下载链接
    url_info = api.get_song_url(song["song_id"])
    url = url_info["url"]
    ext = ".flac" if url_info.get("type") == "flac" else ".mp3"
    print(f"音质: {url_info.get('br', 0) // 1000}kbps, 格式: {url_info.get('type')}")

    # 下载保存
    filename = f"九九八十一 - {song['artist']}{ext}"
    print(f"正在下载: {url[:80]}...")
    resp = requests.get(url, stream=True, timeout=30)
    resp.raise_for_status()

    total = 0
    with open(filename, "wb") as f:
        for chunk in resp.iter_content(8192):
            f.write(chunk)
            total += len(chunk)

    print(f"已保存: {filename} ({total // 1024} KB)")
