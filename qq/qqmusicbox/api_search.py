"""
QQ音乐API - 搜索功能模块

提供歌曲、专辑、歌手、歌单的搜索功能
"""

import logging
from typing import List, Optional

from .utils import handle_api_errors

__all__ = ["SearchMixin"]

logger = logging.getLogger(__name__)


class SearchMixin:
    """搜索功能混入类
    
    提供各类搜索功能,需与QQMusicClient组合使用
    """

    @handle_api_errors("搜索歌曲", default=[])
    async def search_songs(
        self,
        keyword: str,
        num: Optional[int] = None
    ) -> List['Song']:
        """搜索歌曲

        Args:
            keyword: 搜索关键词
            num: 返回数量,默认30

        Returns:
            歌曲列表,失败返回空列表
        """
        from .api import SearchType

        await self._rate_limit("search")
        result = await self._client.search.search_by_type(
            keyword=keyword,
            search_type=SearchType.SONG.value,
            num=num
        )

        # result.song 是歌曲列表
        items = getattr(result, 'song', [])
        return [self._parse_song(item) for item in items]

    @handle_api_errors("搜索专辑", default=[])
    async def search_albums(
        self,
        keyword: str,
        num: Optional[int] = None
    ) -> List['Album']:
        """搜索专辑

        Args:
            keyword: 搜索关键词
            num: 返回数量,默认30

        Returns:
            专辑列表,失败返回空列表
        """
        from .api import SearchType, Album

        await self._rate_limit("search")
        result = await self._client.search.search_by_type(
            keyword=keyword,
            search_type=SearchType.ALBUM.value,
            num=num
        )

        albums = []
        items = getattr(result, 'album', [])

        for item in items:
            singer = self._parse_singers(item)

            album_obj = Album(
                mid=item.mid,
                name=item.name,
                singer=singer,
            )
            albums.append(album_obj)

        return albums

    @handle_api_errors("搜索歌手", default=[])
    async def search_artists(
        self,
        keyword: str,
        num: Optional[int] = None
    ) -> List['Artist']:
        """搜索歌手

        使用 quick_search 因为 search_by_type 返回空歌手列表

        Args:
            keyword: 搜索关键词
            num: 返回数量,默认30

        Returns:
            歌手信息列表,失败返回空列表
        """
        from .api import Artist

        await self._rate_limit("search")
        result = await self._client.search.quick_search(keyword)

        artists = []
        singer_data = result.get('singer', {})
        items = singer_data.get('itemlist', [])

        for item in items[:num]:
            mid = item.get('mid', '')
            artist_info = Artist(
                mid=mid,
                name=item.get('name', ''),
                image_url=(
                    f"https://y.qq.com/music/photo_new/"
                    f"T001R300x300M000{mid}.jpg"
                ),
            )
            artists.append(artist_info)

        return artists

    @handle_api_errors("搜索歌单", default=[])
    async def search_playlists(
        self,
        keyword: str,
        num: Optional[int] = None
    ) -> List['Playlist']:
        """搜索歌单

        Args:
            keyword: 搜索关键词
            num: 返回数量,默认30

        Returns:
            歌单列表,失败返回空列表
        """
        from .api import SearchType, Playlist

        await self._rate_limit("search")
        result = await self._client.search.search_by_type(
            keyword=keyword,
            search_type=SearchType.PLAYLIST.value,
            num=num
        )

        items = getattr(result, 'songlist', [])
        return [
            Playlist(
                dissid=str(item.id),
                name=item.title,
            )
            for item in items
        ]
