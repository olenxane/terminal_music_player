"""
QQ音乐API - 歌单与排行榜功能模块

提供歌单歌曲、热门歌曲、推荐歌单、排行榜等功能
"""

import logging
from typing import List, Optional, Dict

from .utils import handle_api_errors

__all__ = ["PlaylistMixin"]

logger = logging.getLogger(__name__)


class PlaylistMixin:
    """歌单与排行榜功能混入类
    
    提供歌单歌曲、热门推荐、排行榜等功能
    需与QQMusicClient组合使用
    """
    
    @handle_api_errors("获取歌单歌曲", default=[])
    async def get_playlist_songs(self, dissid: str, num: int = 300) -> List['Song']:
        """获取歌单歌曲（支持获取更多）"""
        from .config import API_MAX_PAGE_SIZE

        # 尝试将dissid转换为int
        try:
            dissid_int = int(dissid)
        except (ValueError, TypeError):
            logger.warning("无效的 dissid: %s", dissid)
            return []

        await self._rate_limit("playlist")

        songs = []
        page = 1
        page_size = API_MAX_PAGE_SIZE

        max_pages = 10
        while len(songs) < num and page <= max_pages:
            result = await self._client.songlist.get_detail(
                dissid_int,
                num=page_size,
                page=page
            )
            
            # 使用songs属性
            items = getattr(result, 'songs', [])
            if not items:
                break
            
            for item in items:
                song = self._parse_song(item)
                songs.append(song)
            
            # 如果没有更多数据，退出循环
            if not getattr(result, 'hasmore', False):
                break
            page += 1
        
        return songs[:num]
    
    @handle_api_errors("获取热门歌曲", default=[])
    async def get_hot_songs(self, num: Optional[int] = None) -> List['Song']:
        """获取热门歌曲（从热歌榜）"""
        from .config import HOT_SONGS_CHART_ID

        await self._rate_limit("playlist")
        result = await self._client.top.get_detail(HOT_SONGS_CHART_ID)
        songs = []
        
        items = getattr(result, 'songs', [])[:num]
        
        for item in items:
            song = self._parse_song(item)
            songs.append(song)
        
        return songs
    
    @handle_api_errors("获取推荐歌单", default=[])
    async def get_recommend_playlists(self, num: Optional[int] = None) -> List['Playlist']:
        """获取推荐歌单"""
        from .api import Playlist

        await self._rate_limit("playlist")
        result = await self._client.recommend.get_recommend_songlist()
        playlists = []
        
        items = getattr(result, 'songlists', [])[:num]
        
        for item in items:
            playlist_obj = Playlist(
                dissid=str(item.id),
                name=getattr(item, 'title', ''),
                image_url=getattr(item, 'picurl', ''),
            )
            playlists.append(playlist_obj)
        
        return playlists
    
    @handle_api_errors("获取排行榜列表", default=[])
    async def get_top_list(self) -> List[Dict]:
        """获取排行榜列表"""
        await self._rate_limit("playlist")
        result = await self._client.top.get_category()
        tops = []
        
        # 遍历分组
        groups = getattr(result, 'group', [])
        for group in groups:
            items = getattr(group, 'toplist', [])
            for item in items:
                tops.append({
                    "id": item.id,
                    "title": item.name,  # 使用name而不是title
                    "listen_num": getattr(item, 'listen_num', 0),
                })
        
        return tops
    
    @handle_api_errors("获取排行榜歌曲", default=[])
    async def get_top_songs(self, top_id: int = 0, num: Optional[int] = None) -> List['Song']:
        """获取排行榜歌曲"""
        await self._rate_limit("playlist")
        result = await self._client.top.get_detail(top_id)
        songs = []
        
        # 使用songs属性
        items = getattr(result, 'songs', [])[:num]
        
        for item in items:
            song = self._parse_song(item)
            songs.append(song)
        
        return songs
    
    @handle_api_errors("获取歌手歌曲", default=[])
    async def get_artist_songs(self, singer_mid: str, num: Optional[int] = None) -> List['Song']:
        """获取歌手热门歌曲"""
        await self._rate_limit("playlist")
        result = await self._client.singer.get_songs_list(singer_mid, num=num)
        songs = []
        
        items = getattr(result, 'song_list', [])[:num]
        
        for item in items:
            song = self._parse_song(item)
            songs.append(song)
        
        return songs
    
    @handle_api_errors("获取歌手信息", default=None)
    async def get_artist_info(self, singer_mid: str) -> Optional[Dict]:
        """获取歌手信息"""
        await self._rate_limit("playlist")
        result = await self._client.singer.get_info(singer_mid)
        singer = getattr(result, 'singer', None)
        info = {
            "mid": singer_mid,
            "name": getattr(singer, 'name', '') if singer else '',
        }
        return info
