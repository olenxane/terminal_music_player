"""
QQ音乐API - 歌曲功能模块

提供歌曲URL、歌曲信息、专辑歌曲获取功能
"""
from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

from qqmusic_api.modules.song import SongFileType, SongFileInfo

from .exceptions import NETWORK_ERRORS, RateLimitError, CredentialError
from .utils import handle_api_errors, log_api_error
from .config import API_MAX_RETRIES, API_RETRY_DELAY, API_RETRY_BACKOFF

__all__ = ["SongMixin"]

logger = logging.getLogger(__name__)


class SongMixin:
    """歌曲功能混入类

    提供歌曲URL获取、歌曲详情、专辑歌曲等功能
    需与QQMusicClient组合使用
    """

    async def get_song_url(self, song_mid: str, quality: int = 320) -> Optional[str]:
        """获取歌曲播放URL(带重试机制)

        Args:
            song_mid: 歌曲mid
            quality: 音质 (128, 320)

        Returns:
            播放URL,如果需要VIP/版权限制返回None
        """
        from .api import QQMusicErrorCode
        
        credential_refreshed = False

        for attempt in range(API_MAX_RETRIES):
            try:
                await self._rate_limit("song")
                cred = self.get_credential()
                logger.debug("获取URL: mid=%s, credential存在=%s", song_mid, cred is not None)

                # 根据quality选择音质类型
                quality_int = int(quality) if isinstance(quality, str) else quality
                if quality_int >= 320:
                    file_type = SongFileType.MP3_320
                else:
                    file_type = SongFileType.MP3_128

                logger.debug("获取URL: mid=%s, quality=%d, file_type=%s", song_mid, quality_int, file_type)

                # 创建 SongFileInfo 对象 (qqmusic-api-python 0.5.1+)
                song_info = SongFileInfo(mid=song_mid)

                # 使用song模块获取播放URL,传递credential和音质
                result = await self._client.song.get_song_urls(
                    [song_info],
                    file_type=file_type,
                    credential=cred
                )

                # 检查返回数据
                data = getattr(result, 'data', [])
                if not data:
                    logger.warning("获取URL失败: API返回数据为空, mid=%s", song_mid)
                    return None

                url_info = data[0]

                # 检查结果码
                # 0: 成功, 其他: 需要VIP/版权限制等
                result_code = getattr(url_info, 'result', -1)
                if result_code != 0:
                    # 使用错误码枚举获取消息
                    msg = QQMusicErrorCode.get_error_message(result_code)
                    logger.warning("获取URL失败: %s, mid=%s, result_code=%d", msg, song_mid, result_code)

                    # 永久性错误码：重试无意义，直接返回 None
                    _PERMANENT_ERROR_CODES = {
                        QQMusicErrorCode.VIP_REQUIRED.value,
                        QQMusicErrorCode.COPYRIGHT_RESTRICTED.value,
                        QQMusicErrorCode.LOGIN_REQUIRED.value,
                    }
                    if result_code in _PERMANENT_ERROR_CODES:
                        logger.info("永久性错误 (%s)，跳过重试", msg)
                        return None

                    # 如果是URL过期错误(101404)，尝试刷新凭证并重试
                    # 注意：只重试一次（第一次失败后），避免无限重试
                    # 本次内层重试消耗了 credential_refreshed 标志，后续外层循环的
                    # 剩余尝试不会再次触发凭证刷新（只会在没有新凭证的情况下重试网络请求）。
                    if result_code == QQMusicErrorCode.URL_EXPIRED.value and not credential_refreshed:
                        credential_refreshed = True
                        logger.info("尝试刷新凭证后重试获取URL")
                        try:
                            if hasattr(self, 'refresh_credential'):
                                await self.refresh_credential()
                                # 重新获取credential
                                cred = self.get_credential()
                                await self._rate_limit("song")
                                result = await self._client.song.get_song_urls(
                                    [song_info],
                                    file_type=file_type,
                                    credential=cred
                                )
                                data = getattr(result, 'data', [])
                                if data:
                                    url_info = data[0]
                                    result_code = getattr(url_info, 'result', -1)
                                    if result_code == 0:
                                        purl = getattr(url_info, 'purl', '')
                                        if purl:
                                            if '://' in purl or purl.startswith('@') or '..' in purl:
                                                logger.warning("刷新重试: purl 格式异常, purl=%r", purl)
                                                return None
                                            else:
                                                return f"https://ws.stream.qqmusic.qq.com/{purl}"
                        except Exception as e:
                            logger.warning("刷新凭证并重试失败: %s", e)

                    # A5 修复：内层刷新重试失败后，不直接 return None，
                    # 让外层 retry 循环继续尝试（可能有瞬时网络问题或 API 抖动）。
                    # 仅在最后一次尝试时返回 None。
                    if attempt >= API_MAX_RETRIES - 1:
                        return None
                    logger.info("URL 获取失败 (%s)，将继续重试 (%d/%d)",
                                QQMusicErrorCode.get_error_message(result_code),
                                attempt + 1, API_MAX_RETRIES)
                    await asyncio.sleep(API_RETRY_DELAY * (API_RETRY_BACKOFF ** attempt))
                    continue

                # 获取purl并构建完整URL
                purl = getattr(url_info, 'purl', '')
                if purl:
                    # A10 修复：验证 purl 格式，防止 URL 注入（中间人/DNS 污染场景）
                    # 合法 purl 应为相对路径，不包含 scheme/authority/路径遍历。
                    if '://' in purl or purl.startswith('@') or '..' in purl:
                        logger.warning("获取URL失败: purl 格式异常, mid=%s, purl=%r", song_mid, purl)
                        return None
                    # QQ音乐CDN地址
                    return f"https://ws.stream.qqmusic.qq.com/{purl}"

                logger.warning("获取URL失败: purl为空, mid=%s", song_mid)
                return None
            except RateLimitError as e:
                log_api_error("获取歌曲URL（限流）", e)
                return None
            except CredentialError as e:
                # 登录凭证过期 / 未登录等凭证类错误：尝试刷新凭证后重试
                if not credential_refreshed and hasattr(self, 'refresh_credential'):
                    credential_refreshed = True
                    logger.warning("获取歌曲URL时凭证失效 (%s)，尝试刷新后重试", type(e).__name__)
                    try:
                        refresh_ok = await self.refresh_credential()
                    except Exception as refresh_e:
                        logger.warning("刷新凭证失败: %s", refresh_e)
                        refresh_ok = False
                    if refresh_ok:
                        # 刷新成功，继续本轮重试（不消耗 attempt 的延迟等待）
                        continue
                # 刷新失败或已刷新过：记日志并退出（让上层处理）
                log_api_error("获取歌曲URL（凭证失效）", e)
                return None
            except NETWORK_ERRORS as e:
                if attempt < API_MAX_RETRIES - 1:
                    delay = API_RETRY_DELAY * (API_RETRY_BACKOFF ** attempt)
                    logger.warning("获取歌曲URL失败 (尝试 %d/%d): %s,%.1f秒后重试",
                                   attempt + 1, API_MAX_RETRIES, e, delay)
                    await asyncio.sleep(delay)
                else:
                    logger.error("获取歌曲URL失败,已重试 %d 次: %s", API_MAX_RETRIES, e)
            except (AttributeError, KeyError, TypeError, ValueError) as e:
                log_api_error("获取歌曲URL（数据解析错误）", e)
                return None

        return None

    @handle_api_errors("获取歌曲信息", default=None)
    async def get_song_info(self, song_mid: str) -> Optional['Song']:
        """获取歌曲详情

        Args:
            song_mid: 歌曲mid

        Returns:
            歌曲信息对象,失败返回None
        """
        from .api import Song

        await self._rate_limit("song")
        info = await self._client.song.get_detail(song_mid)
        if info:
            singers = getattr(info, 'singer', [])
            if singers:
                singer = "/".join(s.name for s in singers if s.name)
            else:
                singer = ""

            album_obj = getattr(info, 'album', None)
            album_name = album_obj.name if album_obj else ""

            return Song(
                mid=song_mid,
                name=info.name,
                singer=singer,
                album=album_name,
                duration=getattr(info, 'interval', 0),
            )
        return None

    @handle_api_errors("获取专辑歌曲", default=[])
    async def get_album_songs(self, album_mid: str) -> List['Song']:
        """获取专辑歌曲

        Args:
            album_mid: 专辑mid

        Returns:
            歌曲列表,失败返回空列表
        """
        from .config import API_MAX_PAGE_SIZE

        await self._rate_limit("song")
        songs = []
        page = 1
        page_size = API_MAX_PAGE_SIZE
        max_pages = 10  # 最大页数限制，防止无限循环

        while page <= max_pages:
            result = await self._client.album.get_song(album_mid, num=page_size, page=page)

            items = getattr(result, 'song_list', [])
            if not items:
                break

            for item in items:
                song = self._parse_song(item)
                songs.append(song)

            # 已获取全部歌曲则退出
            total_num = getattr(result, 'total_num', 0)
            if total_num and len(songs) >= total_num:
                break
            if len(items) < page_size:
                break
            page += 1

        return songs
