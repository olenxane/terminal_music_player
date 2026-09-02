"""
QQ音乐API - 用户认证与收藏功能模块

提供登录、凭证管理、用户收藏等功能
"""

import asyncio
import logging
import os
import pickle
import time
from typing import List, Dict, Optional

from qqmusic_api.models.login import QRLoginType

from .config import API_MAX_PAGE_SIZE, DEFAULT_SEARCH_RESULTS as _DEFAULT_NUM
from .config import PROACTIVE_REFRESH_INTERVAL, PROACTIVE_REFRESH_ADVANCE
from .exceptions import NETWORK_ERRORS, RateLimitError, CredentialError, EncryptionError
from . import paths
from .utils import handle_api_errors, log_api_error, safe_get

logger = logging.getLogger(__name__)

__all__ = ["AuthMixin"]


class AuthMixin:
    """用户认证与收藏功能混入类

    提供登录、凭证管理、收藏等功能
    需与QQMusicClient组合使用
    """

    async def get_qrcode(self, login_type: Optional[QRLoginType] = None) -> Dict:
        """获取登录二维码

        Args:
            login_type: 登录类型。
                - None（默认）: QQ 授权登录（向后兼容，TUI 使用）
                - QRLoginType.MOBILE: QQ音乐APP 扫码登录（推荐作为库使用）
                - QRLoginType.WX: 微信扫码登录
        """
        try:
            if self.get_credential() is None:
                from qqmusic_api import Credential
                self.set_credential(Credential())

            login_type = login_type or QRLoginType.QQ
            self._login_qr = await self._client.login.get_qrcode(login_type)
            return {
                "identifier": self._login_qr.identifier,
                "data": self._login_qr.data,
                "mimetype": self._login_qr.mimetype,
                "qr_type": str(self._login_qr.qr_type),
            }
        except NETWORK_ERRORS as e:
            log_api_error("获取二维码（网络错误）", e)
            return {}
        except (AttributeError, TypeError) as e:
            log_api_error("获取二维码（数据解析错误）", e)
            return {}

    async def check_qrcode(self) -> Dict:
        """检查二维码登录状态

        支持两种轮询方式：
        - MOBILE 类型（QQ音乐APP扫码）：MQTT 订阅推送，单次调用会阻塞
          直到收到登录事件（成功/拒绝/超时）
        - QQ/WX 类型：HTTP 轮询，每次调用返回当前状态
        """
        try:
            if self._login_qr is None:
                return {"status": -1, "error": "请先获取二维码"}

            # MOBILE 类型：通过 MQTT 订阅等待登录事件
            if getattr(self._login_qr, 'qr_type', None) == QRLoginType.MOBILE:
                return await self._check_mobile_qrcode()

            result = await self._client.login.check_qrcode(self._login_qr)
            event = str(getattr(result, 'event', ''))
            done = getattr(result, 'done', False)

            if done:
                status = 2
                credential = getattr(result, 'credential', None)
                if credential:
                    # 同步更新客户端内存中的凭证，使后续 is_logged_in() / API 调用
                    # 能立即使用新凭证，避免"登录成功→立刻被判过期"的竞态。
                    try:
                        self.set_credential(credential)
                    except Exception as e:
                        logger.warning("更新客户端凭证内存状态失败: %s", e)
                    await self._save_credential(credential)
            elif 'CONFIRM' in event:
                status = 1
            elif 'SCAN' in event:
                status = 0
            else:
                status = -1

            return {
                "status": status,
                "event": event,
                "done": done,
            }
        except NETWORK_ERRORS as e:
            log_api_error("检查二维码（网络错误）", e)
            return {"status": -1, "error": str(e)}
        except (AttributeError, KeyError, TypeError) as e:
            log_api_error("检查二维码（数据解析错误）", e)
            return {"status": -1, "error": str(e)}

    async def _check_mobile_qrcode(self, timeout: float = 120.0) -> Dict:
        """检查手机端二维码登录状态（MQTT 订阅推送）

        QQ音乐APP 扫码登录通过 MQTT 实时推送状态，本方法建立订阅并阻塞
        等待，直到登录成功/拒绝/超时。

        Args:
            timeout: 最长等待时间（秒）

        Returns:
            与 check_qrcode() 相同的状态字典：
            - status=2 登录成功（凭证已保存）
            - status=-1 失败/拒绝/超时
        """
        try:
            import anyio
            deadline = anyio.current_time() + timeout
            last_event = ""
            async for result in self._client.login.checking_mobile_qrcode(
                self._login_qr, deadline=deadline
            ):
                event = str(getattr(result, 'event', ''))
                done = getattr(result, 'done', False)
                last_event = event

                if done:
                    credential = getattr(result, 'credential', None)
                    if credential:
                        try:
                            self.set_credential(credential)
                        except Exception as e:
                            logger.warning("更新客户端凭证内存状态失败: %s", e)
                        await self._save_credential(credential)
                    return {"status": 2, "event": event, "done": True}

                # 非终态事件（如 SCAN/CONFIRM）：继续等待推送
            return {"status": -1, "event": last_event or "TIMEOUT", "done": False}
        except NETWORK_ERRORS as e:
            log_api_error("检查手机二维码（网络错误）", e)
            return {"status": -1, "error": str(e)}
        except (AttributeError, KeyError, TypeError) as e:
            log_api_error("检查手机二维码（数据解析错误）", e)
            return {"status": -1, "error": str(e)}

    async def _save_credential(self, credential) -> None:
        """保存凭证到文件（异步，避免阻塞事件循环）"""
        from .config import save_credential

        try:
            cred_dict = {
                "musicid": getattr(credential, 'musicid', 0),
                "musickey": getattr(credential, 'musickey', ''),
                "refresh_key": getattr(credential, 'refresh_key', ''),
                "expired_at": getattr(credential, 'expired_at', 0),
                "musickey_create_time": getattr(credential, 'musickey_create_time', 0),
                "key_expires_in": getattr(credential, 'key_expires_in', 0),
                "encrypt_uin": getattr(credential, 'encrypt_uin', ''),
                "openid": getattr(credential, 'openid', ''),
                "unionid": getattr(credential, 'unionid', ''),
                "refresh_token": getattr(credential, 'refresh_token', ''),
                "access_token": getattr(credential, 'access_token', ''),
                "str_musicid": getattr(credential, 'str_musicid', ''),
                "login_type": getattr(credential, 'login_type', 0),
            }
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, save_credential, cred_dict)
        except OSError as e:
            log_api_error("保存凭证（文件错误）", e)
        except (AttributeError, TypeError) as e:
            log_api_error("保存凭证（数据错误）", e)
        except (EncryptionError, ValueError) as e:
            log_api_error("保存凭证（加密错误）", e)

    def load_credential(self) -> bool:
        """加载凭证"""
        from .config import load_credential

        try:
            cred_dict = load_credential()
            if not cred_dict:
                return False

            from qqmusic_api import Credential
            credential = Credential(
                musicid=cred_dict.get("musicid", 0),
                musickey=cred_dict.get("musickey", ""),
                refresh_key=cred_dict.get("refresh_key", ""),
                expired_at=cred_dict.get("expired_at", 0),
                musickey_create_time=cred_dict.get("musickey_create_time", 0),
                key_expires_in=cred_dict.get("key_expires_in", 0),
                encrypt_uin=cred_dict.get("encrypt_uin", ""),
                openid=cred_dict.get("openid", ""),
                unionid=cred_dict.get("unionid", ""),
                refresh_token=cred_dict.get("refresh_token", ""),
                access_token=cred_dict.get("access_token", ""),
                str_musicid=cred_dict.get("str_musicid", ""),
                login_type=cred_dict.get("login_type", 0),
            )
            self.set_credential(credential)
            return True
        except (OSError, IOError) as e:
            log_api_error("加载凭证（文件错误）", e)
            return False
        except (AttributeError, KeyError, TypeError, ValueError) as e:
            log_api_error("加载凭证（数据错误）", e)
            return False

    async def ensure_credential_loaded(self) -> bool:
        """异步加载凭证（将 Fernet/HKDF 解密卸载到线程池）

        应在 async 入口点调用一次，避免同步的 load_credential 阻塞事件循环。
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.load_credential)

    async def refresh_credential(self) -> bool:
        """刷新凭证

        通过 _refresh_lock 串行化刷新操作。多个并发调用者中只有一个会真正
        发起 API 请求；其他调用者在获得锁后会通过双重检查判断凭证是否已被
        刷新（未过期），若是则直接返回成功，避免服务端作废 refresh_key。

        Raises:
            CredentialError: refresh_key 已被服务端作废，需要重新登录
        """
        async with self._refresh_lock:
            # 双重检查：其他协程可能已在本协程等待锁期间完成刷新
            try:
                credential = self.get_credential()
                if credential is not None:
                    now = int(time.time())
                    # C14 修复：同时检查 musickey 实际有效期（musickey_create_time + key_expires_in）
                    # qqmusic_api 的 Credential.is_expired() 使用此公式判断凭证是否过期，
                    # 而 expired_at 是业务层过期时间戳（~60天），musickey 有效期通常仅 ~3天。
                    # 如果只检查 expired_at，musickey 已过期但 expired_at 仍有效时会误判为无需刷新。
                    mck = getattr(credential, 'musickey_create_time', 0)
                    kei = getattr(credential, 'key_expires_in', 0)
                    expired_at = getattr(credential, 'expired_at', 0)
                    musickey_expired = mck > 0 and kei > 0 and now >= mck + kei
                    expired_at_expired = expired_at > 0 and now >= expired_at
                    if not musickey_expired and not expired_at_expired:
                        logger.debug("refresh_credential: 凭证已被其他协程刷新，复用结果")
                        return True
            except Exception as e:
                logger.debug("refresh_credential 预检查失败: %s", e, exc_info=True)

            try:
                # qqmusic_api 的 refresh_credential() 返回新凭证但不修改 client.credential，
                # 需要显式赋值才能让后续 API 调用使用刷新后的凭证。
                new_credential = await self._client.login.refresh_credential()
                if new_credential is not None:
                    # C14 修复：如果刷新响应未返回 musickey_create_time 或 key_expires_in
                    # （刷新 API 可能不包含这些字段），保留旧值以避免 is_expired() 误判过期。
                    old_cred = self.get_credential()
                    if old_cred is not None:
                        old_mck = getattr(old_cred, 'musickey_create_time', 0)
                        old_kei = getattr(old_cred, 'key_expires_in', 0)
                        new_mck = getattr(new_credential, 'musickey_create_time', 0)
                        new_kei = getattr(new_credential, 'key_expires_in', 0)
                        merged_data = dict(new_credential)
                        if new_mck <= 0 and old_mck > 0:
                            # 如果新 musickey 与旧不同，用当前时间作为新创建时间
                            old_musickey = getattr(old_cred, 'musickey', '')
                            new_musickey = getattr(new_credential, 'musickey', '')
                            if new_musickey != old_musickey:
                                merged_data['musickey_create_time'] = int(time.time())
                            else:
                                merged_data['musickey_create_time'] = old_mck
                        if new_kei <= 0 and old_kei > 0:
                            merged_data['key_expires_in'] = old_kei
                        from qqmusic_api import Credential
                        new_credential = Credential.model_validate(merged_data)
                    try:
                        self.set_credential(new_credential)
                    except Exception as e:
                        logger.warning("赋值刷新后的凭证到客户端失败: %s", e)
                    await self._save_credential(new_credential)
                else:
                    # API 返回 None 表示刷新失败，不应将旧凭证重新保存并返回成功
                    logger.warning("刷新凭证返回空结果，刷新可能失败")
                    return False
                return True
            except CredentialError as e:
                # refresh_key 本身已失效（如被服务端作废），无法刷新，需重新登录
                # 向上抛出，让调用方区分"凭证作废"和"网络错误"
                logger.warning("刷新凭证失败（凭证失效）: %s: %s", type(e).__name__, e)
                raise
            except NETWORK_ERRORS as e:
                log_api_error("刷新凭证（网络错误）", e)
                return False
            except (AttributeError, TypeError) as e:
                log_api_error("刷新凭证（数据错误）", e)
                return False

    def logout(self) -> None:
        """退出登录"""
        from .config import clear_credential

        self.set_credential(None)
        self._fav_dirid = None
        self._fav_cache = None
        self._song_id_cache.clear()
        clear_credential()

    async def proactive_refresh(self) -> bool:
        """主动提前刷新凭证（在被判过期前续期）

        C10 + C13 修复：每 PROACTIVE_REFRESH_INTERVAL（30分钟）检查一次，
        如果凭证将在 PROACTIVE_REFRESH_ADVANCE（1小时）内过期，则提前刷新。

        检查两个过期时间并取最近的一个：
        - expired_at：QQ 音乐扫码登录返回的过期时间戳（~60 天）
        - musickey_create_time + key_expires_in：musickey 实际有效期（~24h）
        任一个即将到期都会触发刷新。

        这样可以避免：
        1. 长时间运行中凭证静默过期导致播放中断
        2. 下次启动时因 refresh_key 也失效而必须重新扫码
        3. 多首歌曲自动播放时因凭证过期链式失败
        4. musickey 已过期但 expired_at 仍有效时（~24h~60天窗口），
           每次启动首次播放会触发反应式刷新 → 现改为主动刷新

        Returns:
            bool: True=已刷新或不需要刷新, False=刷新失败
        """
        # 频率控制（monotonic：不受系统时钟调整影响）
        now_mono = time.monotonic()
        if now_mono - self._last_proactive_refresh < PROACTIVE_REFRESH_INTERVAL:
            return True

        try:
            credential = self.get_credential()
            if credential is None:
                self._last_proactive_refresh = now_mono
                return True  # 未登录，不需要刷新

            now = int(time.time())

            # 1. 检查 expired_at（业务层的过期时间戳）
            expired_at = getattr(credential, 'expired_at', 0)
            remaining_expired = expired_at - now if expired_at and expired_at > 0 else None

            # 2. 检查 musickey 实际有效期
            mck = getattr(credential, 'musickey_create_time', 0)
            kei = getattr(credential, 'key_expires_in', 0)
            remaining_musickey = None
            if mck > 0 and kei > 0:
                musickey_expires = mck + kei
                remaining_musickey = musickey_expires - now

            # 取两个过期时间中离现在最近的
            if remaining_expired is None and remaining_musickey is None:
                logger.debug("proactive_refresh: 无过期时间信息，跳过")
                self._last_proactive_refresh = now_mono
                return True

            remaining = min(
                [r for r in [remaining_expired, remaining_musickey] if r is not None]
            )

            # 仅在即将过期时才刷新
            if remaining > PROACTIVE_REFRESH_ADVANCE:
                logger.debug(
                    "proactive_refresh: 凭证仍有效(expired_at剩余%ss, musickey剩余%ss)，跳过",
                    remaining_expired if remaining_expired else 'N/A',
                    remaining_musickey if remaining_musickey else 'N/A',
                )
                self._last_proactive_refresh = now_mono
                return True

            reason = []
            if remaining_expired is not None and remaining_expired <= PROACTIVE_REFRESH_ADVANCE:
                reason.append(f"expired_at 剩{remaining_expired}s")
            if remaining_musickey is not None and remaining_musickey <= PROACTIVE_REFRESH_ADVANCE:
                reason.append(f"musickey 剩{remaining_musickey}s")
            logger.info("proactive_refresh: 凭证即将过期(%s)，主动刷新", ', '.join(reason))

            success = await self.refresh_credential()
            if success:
                self._last_proactive_refresh = now_mono
                logger.info("proactive_refresh: 刷新成功，凭证有效期已延长")
            else:
                # 失败时使用 5 分钟退避，避免 30 分钟内无法重试
                self._last_proactive_refresh = now_mono - PROACTIVE_REFRESH_INTERVAL + 300
                logger.warning("proactive_refresh: 刷新失败，将在 5 分钟后重试")
            return success

        except CredentialError:
            self._last_proactive_refresh = now_mono - PROACTIVE_REFRESH_INTERVAL + 300
            logger.warning("proactive_refresh: refresh_key 已失效，将在 5 分钟后重试")
            return False
        except Exception as e:
            self._last_proactive_refresh = now_mono - PROACTIVE_REFRESH_INTERVAL + 300
            logger.warning("proactive_refresh: 异常: %s: %s，将在 5 分钟后重试", type(e).__name__, e)
            return False

    async def is_logged_in(self) -> bool:
        """检查是否已登录（包括凭证过期自动刷新）

        返回值语义：
        - True: 已登录（凭证有效，或凭证过期但刷新成功，或刷新因网络错误失败但凭证字段完整）
        - False: 未登录（无凭证、凭证字段缺失、或 refresh_key 已被服务端作废）

        关键设计：网络错误导致刷新失败时，如果凭证字段完整，仍返回 True。
        原因：refresh_key 可能仍然有效，只是网络暂时不通，不应因此删除凭证文件。
        """
        try:
            credential = self.get_credential()
            if credential is None:
                return False

            musicid = getattr(credential, 'musicid', 0)
            if musicid == 0:
                return False

            musickey = getattr(credential, 'musickey', '')
            if not musickey:
                logger.warning("登录凭证缺少 musickey")
                return False

            refresh_key = getattr(credential, 'refresh_key', '')
            if not refresh_key:
                logger.warning("登录凭证缺少 refresh_key")
                return False

            expired_at = getattr(credential, 'expired_at', 0)
            current_time = int(time.time())

            # C14 修复：检查 musickey 是否过期（musickey_create_time + key_expires_in）
            # qqmusic_api 的 Credential.is_expired() 使用此公式判断凭证是否过期，
            # 而 expired_at 是业务层过期时间戳（~60天），musickey 有效期通常仅 ~3天。
            mck = getattr(credential, 'musickey_create_time', 0)
            kei = getattr(credential, 'key_expires_in', 0)
            musickey_expired = mck > 0 and kei > 0 and current_time >= mck + kei
            expired_at_expired = expired_at > 0 and current_time >= expired_at

            if musickey_expired or expired_at_expired:
                logger.info("登录凭证已过期（musickey=%s, expired_at=%s），尝试刷新凭证",
                           "过期" if musickey_expired else "有效",
                           "过期" if expired_at_expired else "有效")
                try:
                    refresh_success = await self.refresh_credential()
                    if refresh_success:
                        logger.info("凭证刷新成功")
                        return True
                    else:
                        # 刷新失败（网络错误等），但凭证字段完整，保留凭证以便重试
                        logger.info("凭证刷新失败，保留凭证以便下次重试")
                        return True
                except CredentialError:
                    # refresh_key 已被服务端作废，凭证确实无效，需要重新登录
                    logger.warning("凭证已失效（refresh_key 被服务端作废），需要重新登录")
                    return False

            return True
        except (AttributeError, TypeError) as e:
            logger.debug("检查登录状态异常（数据错误）: %s", e)
            return False

    async def get_fav_songs(self, num: int = 300, start_page: int = 1, use_cache: bool = True) -> List:
        """获取我喜欢的歌曲（支持获取更多）

        Args:
            num: 最多获取的歌曲数
            start_page: 起始页码（用于增量加载，跳过已获取的页）
            use_cache: 是否使用缓存（默认 True，首次请求或强制刷新时设为 False）
        """
        from .api import Song, _FavCache

        try:
            cred = self.get_credential()
            if not cred:
                return []

            if start_page == 1 and use_cache:
                # 先在锁外从磁盘加载缓存（避免 pickle 反序列化阻塞事件循环持锁）
                file_cache = await self._load_fav_cache_from_file_async()
                async with self._fav_cache_lock:
                    cache = getattr(self, '_fav_cache', None)
                    if cache is None or not cache.songs:
                        if file_cache:
                            self._fav_cache = file_cache
                            cache = file_cache
                    if cache is not None and cache.songs:
                        now = time.monotonic()
                        if now - cache.timestamp < 300:
                            return cache.songs[:num]

            await self._rate_limit("fav")

            euin = getattr(cred, 'encrypt_uin', '')
            songs = []
            page = start_page
            # 始终使用 API_MAX_PAGE_SIZE 拉取每页，最大化吞吐量；num 仅用于最终切片。
            # 否则 num=30 会导致 page_size=30，使第 1 页只拉 30 首而错过后台加载机会。
            page_size = API_MAX_PAGE_SIZE
            max_pages = 10

            while len(songs) < num and page <= max_pages:
                result = await self._client.user.get_fav_song(
                    euin,
                    page=page,
                    num=page_size,
                    credential=cred,
                )

                items = getattr(result, 'songs', [])
                if not items:
                    break

                for item in items:
                    song = self._parse_song(item)
                    songs.append(song)

                if not getattr(result, 'hasmore', False):
                    break
                
                if start_page == 1 and page == 1 and songs:
                    async with self._fav_cache_lock:
                        now = time.monotonic()
                        mids = {s.mid for s in songs}
                        self._fav_cache = _FavCache(timestamp=now, mids=mids, songs=songs)
                        await self._save_fav_cache_to_file_async(self._fav_cache)
                    # 不再在此处启动后台加载；由 UI 层 (_load_fav_songs_remaining) 统一控制，
                    # 避免 API 层和 UI 层同时拉取第 2 页及以后的重复请求。
                    # 返回全部已加载歌曲（不按 num 截断）—— page_size 已固定为 API_MAX_PAGE_SIZE，
                    # 首屏多返回一些数据有助于 UI 层判断是否需要触发后台加载剩余。
                    return songs
                
                page += 1

            if start_page == 1 and songs:
                async with self._fav_cache_lock:
                    now = time.monotonic()
                    mids = {s.mid for s in songs}
                    self._fav_cache = _FavCache(timestamp=now, mids=mids, songs=songs)
                    await self._save_fav_cache_to_file_async(self._fav_cache)

            # page_size 已固定为 API_MAX_PAGE_SIZE，全部已加载歌曲均已缓存，
            # 不再按 num 截断——让调用方自行决定显示数量。
            return songs
        except CredentialError as e:
            logger.error("获取喜欢的歌曲失败（凭证失效）: %s: %s", type(e).__name__, e)
            return []
        except NETWORK_ERRORS as e:
            logger.error("获取喜欢的歌曲失败（网络错误）: %s", e, exc_info=True)
            return []
        except RateLimitError:
            logger.warning("获取喜欢的歌曲失败（限流超时）")
            return []
        except (AttributeError, KeyError, TypeError) as e:
            logger.error("获取喜欢的歌曲失败（数据解析错误）: %s", e, exc_info=True)
            return []
    
    def _load_fav_cache_from_file(self) -> Optional["_FavCache"]:
        """从文件加载收藏歌曲缓存（同步，供 run_in_executor 卸载）"""
        from .api import _FavCache

        try:
            fav_file = paths.FAV_CACHE_FILE
            if not fav_file.exists():
                return None

            with open(fav_file, 'rb') as f:
                data = f.read()
            
            cache = pickle.loads(data)
            
            if isinstance(cache, _FavCache) and cache.songs:
                logger.debug("成功从文件加载收藏歌曲缓存（%d首）", len(cache.songs))
                return cache
        except Exception as e:
            logger.debug("从文件加载收藏歌曲缓存失败: %s", e)
        
        return None

    async def _load_fav_cache_from_file_async(self) -> Optional["_FavCache"]:
        """异步加载缓存（pickle 反序列化卸载到线程池，避免阻塞事件循环）"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._load_fav_cache_from_file)
    
    def _save_fav_cache_to_file(self, cache: "_FavCache") -> None:
        """将收藏歌曲缓存保存到文件（同步，供 run_in_executor 卸载）"""
        try:
            data = pickle.dumps(cache)

            fav_file = paths.FAV_CACHE_FILE
            temp_file = fav_file.with_suffix('.tmp')
            with open(temp_file, 'wb') as f:
                f.write(data)

            os.replace(str(temp_file), str(fav_file))
            
            logger.debug("收藏歌曲缓存已保存到文件")
        except Exception as e:
            logger.debug("保存收藏歌曲缓存到文件失败: %s", e)

    async def _save_fav_cache_to_file_async(self, cache: "_FavCache") -> None:
        """异步保存缓存（pickle 序列化卸载到线程池，避免阻塞事件循环）"""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._save_fav_cache_to_file, cache)

    async def get_created_songlist(self, num: int = None) -> List:
        """获取我创建的歌单"""
        from .api import Playlist

        if num is None:
            num = _DEFAULT_NUM

        try:
            cred = self.get_credential()
            if not cred:
                return []

            uin = getattr(cred, 'musicid', 0)
            result = await self._client.user.get_created_songlist(uin, credential=cred)
            playlists = []

            items = getattr(result, 'playlists', [])[:num]

            for item in items:
                playlist_obj = Playlist(
                    dissid=str(item.id),
                    name=safe_get(item, '', 'title', 'name'),
                    image_url=getattr(item, 'picurl', ''),
                )
                playlists.append(playlist_obj)

            return playlists
        except CredentialError as e:
            logger.error("获取创建的歌单失败（凭证失效）: %s: %s", type(e).__name__, e)
            return []
        except NETWORK_ERRORS as e:
            logger.error("获取创建的歌单失败（网络错误）: %s", e, exc_info=True)
            return []
        except (AttributeError, KeyError, TypeError) as e:
            logger.error("获取创建的歌单失败（数据解析错误）: %s", e, exc_info=True)
            return []

    @handle_api_errors("获取收藏的歌单", default=[])
    async def get_fav_songlist(self, num: int = None) -> List:
        """获取我收藏的歌单"""
        from .api import Playlist

        if num is None:
            num = _DEFAULT_NUM

        cred = self.get_credential()
        if not cred:
            return []

        euin = getattr(cred, 'encrypt_uin', '')
        result = await self._client.user.get_fav_songlist(euin, num=num, credential=cred)
        playlists = []

        items = getattr(result, 'playlists', [])[:num]

        for item in items:
            playlist_obj = Playlist(
                dissid=str(item.id),
                name=safe_get(item, '', 'title', 'name'),
                image_url=getattr(item, 'picurl', ''),
            )
            playlists.append(playlist_obj)

        return playlists

    @handle_api_errors("获取喜欢的专辑", default=[])
    async def get_fav_albums(self, num: int = None) -> List:
        """获取我喜欢的专辑"""
        from .api import Album

        if num is None:
            num = _DEFAULT_NUM

        cred = self.get_credential()
        if not cred:
            return []

        euin = getattr(cred, 'encrypt_uin', '')
        result = await self._client.user.get_fav_album(euin, num=num, credential=cred)
        albums = []

        for item in result.albums[:num]:
            singer = self._parse_singers(item)
            album_obj = Album(
                mid=item.mid,
                name=item.name,
                singer=singer,
            )
            albums.append(album_obj)

        return albums

    @handle_api_errors("获取我喜欢歌单ID", default=None)
    async def _get_fav_dirid(self) -> Optional[int]:
        """获取"我喜欢"歌单的 dirid"""
        from .config import FAV_PLAYLIST_NAME

        if self._fav_dirid is not None:
            return self._fav_dirid

        if not self.has_credential():
            return None

        cred = self.get_credential()
        if not cred:
            return None

        musicid = getattr(cred, 'musicid', 0)
        await self._rate_limit("fav_dirid")
        result = await self._client.user.get_created_songlist(musicid, credential=cred)
        playlists = getattr(result, 'playlists', [])

        for p in playlists:
            title = getattr(p, 'title', '')
            if title == FAV_PLAYLIST_NAME:
                self._fav_dirid = getattr(p, 'dirid', None)
                return self._fav_dirid

        return None

    async def _get_song_id_for_fav(self, song_mid: str) -> Optional[int]:
        """获取歌曲 ID（用于收藏/取消收藏的公共逻辑）"""
        if not self.has_credential():
            logger.warning("请先登录")
            return None

        cred = self.get_credential()
        if not cred:
            logger.warning("请先登录")
            return None

        # L1: 使用缓存避免重复查询 mid→song_id
        # B5 修复：限制缓存大小，防止长时间运行的内存缓慢增长
        # R6 修复：_song_id_cache 已在 QQMusicClient.__init__ 中初始化，此处仅读取
        cached_id = self._song_id_cache.get(song_mid)
        if cached_id is not None:
            return cached_id

        await self._rate_limit("fav")

        fav_dirid = await self._get_fav_dirid()
        if fav_dirid is None:
            logger.warning("无法获取我喜欢歌单")
            return None

        info = await self._client.song.get_detail(song_mid)
        if not info or not hasattr(info, 'track'):
            logger.warning("无法获取歌曲详情")
            return None

        track = info.track
        if track is None:
            logger.warning("歌曲详情中 track 为空: mid=%s", song_mid)
            return None

        try:
            song_id = getattr(track, 'id', getattr(track, 'track_id', 0))
        except AttributeError:
            logger.warning("无法从 track 对象获取歌曲ID: %s", type(track))
            return None

        if song_id == 0:
            logger.warning("无法获取歌曲ID")
            return None

        # B5 修复：缓存达到上限时清理最早的条目，防止内存泄漏
        if len(self._song_id_cache) >= self._song_id_cache_max:
            # 删除第一个条目（Python 3.7+ dict 保持插入顺序）
            first_key = next(iter(self._song_id_cache))
            del self._song_id_cache[first_key]
            logger.debug("_song_id_cache 达到上限，驱逐最早条目: %s", first_key)
        self._song_id_cache[song_mid] = song_id
        return song_id

    async def _toggle_fav_song(self, song_mid: str, action: str) -> bool:
        """收藏/取消收藏歌曲的公共逻辑

        Args:
            song_mid: 歌曲 mid
            action: "add" 或 "del"

        Returns:
            是否操作成功
        """
        try:
            if not self.has_credential():
                logger.warning("请先登录")
                return False

            cred = self.get_credential()
            if not cred:
                logger.warning("请先登录")
                return False

            song_id = await self._get_song_id_for_fav(song_mid)
            if song_id is None:
                return False

            fav_dirid = self._fav_dirid
            if fav_dirid is None:
                fav_dirid = await self._get_fav_dirid()
                if fav_dirid is None:
                    return False

            await self._rate_limit("fav")

            if action == "add":
                await self._client.songlist.add_songs(
                    dirid=fav_dirid,
                    song_info=[(song_id, 0)],
                    credential=cred,
                )
                async with self._fav_cache_lock:
                    cache = getattr(self, '_fav_cache', None)
                    if cache is not None and cache.mids is not None:
                        cache.mids.add(song_mid)
            else:
                await self._client.songlist.del_songs(
                    dirid=fav_dirid,
                    song_info=[(song_id, 0)],
                    credential=cred,
                )
                async with self._fav_cache_lock:
                    cache = getattr(self, '_fav_cache', None)
                    if cache is not None and cache.mids is not None:
                        cache.mids.discard(song_mid)
            return True
        except CredentialError as e:
            op_name = "收藏" if action == "add" else "取消收藏"
            logger.error("%s歌曲失败（凭证失效）: %s: %s", op_name, type(e).__name__, e)
            return False
        except NETWORK_ERRORS as e:
            op_name = "收藏" if action == "add" else "取消收藏"
            logger.error("%s歌曲失败（网络错误）: %s", op_name, e)
            return False
        except RateLimitError:
            op_name = "收藏" if action == "add" else "取消收藏"
            logger.warning("%s歌曲失败（限流超时）", op_name)
            return False
        except (AttributeError, KeyError, TypeError) as e:
            op_name = "收藏" if action == "add" else "取消收藏"
            logger.error("%s歌曲失败（数据错误）: %s", op_name, e)
            return False

    async def like_song(self, song_mid: str) -> bool:
        """收藏歌曲（添加到我喜欢）"""
        return await self._toggle_fav_song(song_mid, "add")

    async def unlike_song(self, song_mid: str) -> bool:
        """取消收藏歌曲（从我喜欢移除）"""
        return await self._toggle_fav_song(song_mid, "del")

    async def is_song_liked(self, song_mid: str) -> bool:
        """检查歌曲是否已收藏（使用缓存避免每次拉取完整列表）

        注意：锁已在 QQMusicClient.__init__ 中统一初始化，避免延迟创建导致的竞态条件
        """
        try:
            from .config import FAV_MAX_LOAD_COUNT
            if not self.has_credential():
                return False

            cred = self.get_credential()
            if not cred:
                return False

            # 先在锁内快照缓存，判断是否需要刷新
            need_refresh = False
            async with self._fav_cache_lock:
                cache = getattr(self, '_fav_cache', None)
                now = time.monotonic()
                if cache is None or now - cache.timestamp > 300:
                    need_refresh = True
                else:
                    return song_mid in cache.mids

            # 在锁外执行网络 I/O，避免持锁阻塞其他协程
            if need_refresh:
                try:
                    fav_songs = await self.get_fav_songs(num=FAV_MAX_LOAD_COUNT)
                    new_mids = {s.mid for s in fav_songs}
                except (NETWORK_ERRORS, RateLimitError, CredentialError) as e:
                    # 网络错误时不缓存空集合，保留旧缓存
                    logger.debug("is_song_liked 刷新缓存失败，保留旧缓存: %s", e)
                    async with self._fav_cache_lock:
                        cache = getattr(self, '_fav_cache', None)
                        if cache is not None:
                            return song_mid in cache.mids
                    return False

                async with self._fav_cache_lock:
                    from .api import _FavCache
                    # 如果 get_fav_songs 返回空但旧缓存非空，保留旧 mids（仅更新时间戳），
                    # 避免网络失败返回 [] 被缓存为空集合 300 秒
                    if not new_mids and self._fav_cache and self._fav_cache.mids:
                        old_mids = self._fav_cache.mids
                        self._fav_cache = _FavCache(timestamp=now, mids=old_mids)
                        return song_mid in old_mids
                    self._fav_cache = _FavCache(timestamp=now, mids=new_mids)
                    return song_mid in new_mids

            return False
        except (AttributeError, TypeError) as e:
            logger.debug("is_song_liked 检查失败（数据错误）: %s", e)
            return False