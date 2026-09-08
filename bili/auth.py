"""Bilibili 认证模块（改造自 bilibili-cli 的 auth.py）。

三级凭证获取策略：
1. 已保存凭证文件  {config_dir}/credential.json（可用 configure_paths 配置）
2. 浏览器 Cookie 提取（browser-cookie3，未安装则跳过）
3. 终端扫码登录（QRLoginSession，start/check 两段式）

凭证模式（AuthMode）：
- optional: 只读已保存凭证，不联网校验、不扫浏览器
- read:     已保存凭证 → 联网校验 → 浏览器 Cookie（best effort）
- write:    同 read，但额外要求 bili_jct（写操作 CSRF token）

校验为三态：True=有效 / False=确认失效（清除后走下一级） / None=网络原因无法判断（best effort 返回）。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import subprocess
import sys
import threading
import time
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Literal

import qrcode
import requests
from bilibili_api.utils.network import Credential

from .exceptions import BiliError

logger = logging.getLogger(__name__)

# ---- 凭证存放路径（可由 configure_paths 注入播放器的 online_data/bili 目录）----
_DEFAULT_CONFIG_DIR = Path.home() / ".bilibili-cli"
CREDENTIAL_DIR = _DEFAULT_CONFIG_DIR
CREDENTIAL_FILE = _DEFAULT_CONFIG_DIR / "credential.json"


def configure_paths(config_dir: str | Path | None = None):
    """配置凭证存放目录（线程安全：仅在管理器初始化时调用一次）。"""
    global CREDENTIAL_DIR, CREDENTIAL_FILE
    if config_dir:
        CREDENTIAL_DIR = Path(config_dir)
        CREDENTIAL_FILE = CREDENTIAL_DIR / "credential.json"


# 有效会话所需的最小 Cookie 集合
REQUIRED_COOKIES = {"SESSDATA"}

# 有助于绕过 B站 412 风控的附加 Cookie 字段
EXTRA_COOKIE_FIELDS = ("buvid3", "buvid4", "dedeuserid")

# 凭证 TTL：超过 7 天尝试从浏览器刷新
CREDENTIAL_TTL_DAYS = 7
_CREDENTIAL_TTL_SECONDS = CREDENTIAL_TTL_DAYS * 86400

# 生成的登录二维码 PNG 静区宽度（模块数，标准值 4）
PNG_BORDER = 4

# ---- 扫码登录端点（原生实现；SDK login_v2 已废弃：仓库删除且其解析
# 不适配 B站新版响应——成功响应只回一次性 ticket，Cookie 需另行兑换）----
_QR_GEN = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate?source=main-fe-header"
_QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
_FINGER_SPI = "https://api.bilibili.com/x/frontend/finger/spi"
_QR_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
          "AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/133.0.0.0 Safari/537.36")
_QR_WANT_COOKIES = ("SESSDATA", "bili_jct", "DedeUserID")

AuthMode = Literal["optional", "read", "write"]

# ---- 进程内凭证缓存（避免每首歌都联网校验）----
_cached_credential: Credential | None = None


# ---------------------------------------------------------------------------
# 凭证获取
# ---------------------------------------------------------------------------

def get_credential(mode: AuthMode = "read") -> Credential | None:
    """按模式获取凭证（优先命中进程内缓存）。"""
    require_write = mode == "write"

    # 1. 进程内缓存
    cred = _cached_credential
    if cred is None:
        cred = _load_saved_credential()
    if cred:
        _refresh_cache(cred)

        if mode == "optional":
            return cred
        if require_write and not getattr(cred, "bili_jct", ""):
            # 写操作需要 bili_jct，缓存中的凭证不满足 → 重新走完整流程
            cred = None
        else:
            validation = _validate_credential(cred, require_write=require_write)
            if validation is True:
                return cred
            if validation is None:
                logger.warning("凭证校验因网络原因无法确认，best effort 返回缓存凭证")
                return cred
            if validation is False:
                # 1) 先尝试用长期凭证刷新（可能只是短期凭证过期，会话可救活）
                if getattr(cred, "ac_time_value", ""):
                    ok, msg = refresh_credential(cred)
                    if ok:
                        fresh = _load_saved_credential()
                        if fresh and _validate_credential(fresh, require_write) is True:
                            logger.info("已通过刷新机制续期凭证")
                            return fresh
                    logger.warning("凭证刷新未成功: %s", msg)
                # 2) 宽限期：刚保存的凭证校验失败可能是会话生效延迟（实测分钟级），暂保留
                saved_at = _saved_at()
                if saved_at and time.time() - saved_at < 120:
                    logger.warning("凭证刚保存但校验未通过（可能为会话生效延迟），暂保留")
                    return cred
                logger.warning("已保存凭证已失效，清除")
                clear_credential()
                cred = None

    if mode == "optional":
        return None

    # 2. 浏览器 Cookie 提取
    cred = _extract_browser_credential()
    if cred:
        validation = _validate_credential(cred, require_write=require_write)
        if validation is True:
            logger.info("从本地浏览器提取到有效凭证")
            save_credential(cred)
            return cred
        if validation is None:
            logger.warning("浏览器凭证校验因网络原因无法确认，best effort 返回")
            return cred
        if validation is False:
            logger.warning("浏览器 Cookie 已失效")

    return None


def _refresh_cache(cred: Credential):
    """刷新进程内缓存与 TTL 时间戳（首次命中磁盘凭证时）。"""
    global _cached_credential
    if _cached_credential is None:
        _cached_credential = cred
    if _is_credential_stale():
        logger.info("凭证超过 %d 天，尝试从浏览器刷新", CREDENTIAL_TTL_DAYS)
        fresh = _extract_browser_credential()
        if fresh:
            if _validate_credential(fresh) is True:
                logger.info("已从浏览器刷新凭证")
                save_credential(fresh)


def has_credential() -> bool:
    """是否已有可登录凭证（只查文件，不联网）。"""
    if _cached_credential is not None:
        return True
    cred = _load_saved_credential()
    return cred is not None


def is_logged_in() -> bool:
    """联网校验当前凭证是否有效。"""
    cred = get_credential(mode="read")
    if cred is None:
        return False
    return _validate_credential(cred) is True


def logout() -> bool:
    """清除本地凭证并失效进程内缓存。"""
    global _cached_credential
    _cached_credential = None
    if CREDENTIAL_FILE.exists():
        CREDENTIAL_FILE.unlink()
        logger.info("凭证已移除: %s", CREDENTIAL_FILE)
        return True
    return False


# ---------------------------------------------------------------------------
# 凭证持久化
# ---------------------------------------------------------------------------

def _is_credential_stale() -> bool:
    """凭证文件是否超过 TTL。"""
    if not CREDENTIAL_FILE.exists():
        return False
    try:
        data = json.loads(CREDENTIAL_FILE.read_text())
        saved_at = data.get("saved_at", 0)
        if not saved_at:
            return True  # 旧格式文件，视为过期以补写时间戳
        return (time.time() - saved_at) > _CREDENTIAL_TTL_SECONDS
    except (json.JSONDecodeError, OSError):
        return False


def _load_saved_credential() -> Credential | None:
    """从凭证文件加载。"""
    if not CREDENTIAL_FILE.exists():
        return None
    try:
        data = json.loads(CREDENTIAL_FILE.read_text())
        sessdata = data.get("sessdata", "")
        if not sessdata:
            return None
        return Credential(
            sessdata=sessdata,
            bili_jct=data.get("bili_jct", ""),
            ac_time_value=data.get("ac_time_value", ""),
            buvid3=data.get("buvid3", ""),
            buvid4=data.get("buvid4", ""),
            dedeuserid=data.get("dedeuserid", ""),
        )
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("凭证文件读取失败: %s", e)
        return None


def save_credential(credential: Credential):
    """保存凭证（带时间戳供 TTL 跟踪）。"""
    global _cached_credential
    CREDENTIAL_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "sessdata": credential.sessdata,
        "bili_jct": credential.bili_jct,
        "ac_time_value": credential.ac_time_value or "",
        "buvid3": credential.buvid3 or "",
        "buvid4": credential.buvid4 or "",
        "dedeuserid": credential.dedeuserid or "",
        "saved_at": time.time(),
    }
    CREDENTIAL_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    try:
        CREDENTIAL_FILE.chmod(0o600)
    except OSError:
        pass  # Windows 无 POSIX 权限语义
    _cached_credential = credential
    logger.info("凭证已保存: %s", CREDENTIAL_FILE)


def clear_credential():
    """清除凭证（向后兼容别名，等同 logout）。"""
    logout()


# ---------------------------------------------------------------------------
# 凭证校验（三态）
# ---------------------------------------------------------------------------

def _validate_credential(cred: Credential, require_write: bool = False) -> bool | None:
    """校验凭证有效性。

    返回 True（API 确认有效）/ False（确认无效或字段缺失）/ None（网络原因无法判断）。
    """
    from bilibili_api import user
    from bilibili_api.exceptions import NetworkException

    if not getattr(cred, "sessdata", ""):
        return False
    if require_write and not getattr(cred, "bili_jct", ""):
        return False

    async def _check():
        try:
            await user.get_self_info(cred)
            return True
        except NetworkException:
            return None
        except Exception:
            return False

    try:
        return asyncio.run(_check())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 浏览器 Cookie 提取（browser-cookie3，可选依赖）
# ---------------------------------------------------------------------------

def _extract_browser_credential() -> Credential | None:
    """从本机浏览器提取 Bilibili Cookie。

    在子进程中执行并限制 15 秒超时，避免浏览器运行时 Cookie DB 锁导致挂死。
    browser-cookie3 未安装时直接跳过（可选依赖，不强制）。
    """
    extract_script = '''
import json, sys
try:
    import browser_cookie3 as bc3
except ImportError:
    print(json.dumps({"error": "not_installed"}))
    sys.exit(0)

browsers = [
    ("Chrome", bc3.chrome),
    ("Firefox", bc3.firefox),
    ("Edge", bc3.edge),
    ("Brave", bc3.brave),
]

for name, loader in browsers:
    try:
        cj = loader(domain_name=".bilibili.com")
        cookies = {c.name: c.value for c in cj if "bilibili.com" in (c.domain or "")}
        if "SESSDATA" in cookies:
            print(json.dumps({"browser": name, "cookies": cookies}))
            sys.exit(0)
    except Exception:
        pass

print(json.dumps({"error": "no_cookies"}))
'''

    try:
        result = subprocess.run(
            [sys.executable, "-c", extract_script],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            logger.debug("Cookie 提取子进程失败: %s", result.stderr)
            return None
        output = result.stdout.strip()
        if not output:
            return None
        data = json.loads(output)
        if "error" in data:
            if data["error"] != "not_installed":
                logger.debug("浏览器中未找到 Bilibili Cookie")
            return None

        cookies = data["cookies"]
        if not REQUIRED_COOKIES.issubset(cookies):
            return None
        logger.info("从 %s 提取到 %d 个 Cookie", data["browser"], len(cookies))

        return Credential(
            sessdata=cookies.get("SESSDATA", ""),
            bili_jct=cookies.get("bili_jct", ""),
            ac_time_value=cookies.get("ac_time_value", ""),
            buvid3=cookies.get("buvid3", ""),
            buvid4=cookies.get("buvid4", ""),
            dedeuserid=cookies.get("DedeUserID", ""),
        )
    except subprocess.TimeoutExpired:
        logger.warning("浏览器 Cookie 提取超时（浏览器可能正在运行）")
        return None
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("浏览器 Cookie 解析失败: %s", e)
        return None


# ---------------------------------------------------------------------------
# 扫码登录（start/check 两段式，供播放器后台线程轮询）
# ---------------------------------------------------------------------------


class QRLoginSession:
    """B站扫码登录会话（原生实现，不依赖 bilibili_api.login_v2）。

    B站新版流程（实测抓包确认）：
    1. generate 返回 scan-web 链接 + qrcode_key
    2. poll 轮询：86101 未扫码 / 86090 已扫码 / 86038 过期 / 0 成功
    3. 成功响应的 url 中**不含 Cookie**，只有一次性 ticket；
       需再 GET 该 crossDomain 链接，从原始 Set-Cookie 头换取真实 Cookie
       （requests 的 Cookie jar 有域作用域，必须手动提取值）
    4. finger/spi 获取 buvid3/buvid4 设备指纹一并保存

    用法（播放器后台线程中经 AsyncRunner 调用）：
        session = get_qr_login_session()
        info = await session.start()      # {"qr_link", "qr_png"}
        result = await session.check()    # {"status": waiting|scanned|success|expired|error}
    成功时凭证自动保存。
    """

    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": _QR_UA,
                                      "Referer": "https://www.bilibili.com"})
        self._key: str | None = None

    async def start(self) -> dict:
        """生成登录二维码。返回 {"qr_link": 链接, "qr_png": PNG 字节}。

        PNG 由 qrcode 库本地生成（标准 4 模块静区，可直接保存为图片扫码）。
        终端 ASCII 渲染由显示层（mp/qr_terminal.py）按实际编码能力分档完成，
        本层不做任何编码假设，也不做静默降级。
        """

        def _gen():
            r = self._session.get(_QR_GEN, timeout=10)
            return r.json().get("data") or {}

        data = await asyncio.to_thread(_gen)
        self._key = data.get("qrcode_key", "")
        qr_link = data.get("url", "")
        if not self._key or not qr_link:
            logger.warning("扫码登录未获取到二维码链接")
            return {"qr_link": "", "qr_png": b""}

        import io

        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L,
                           border=PNG_BORDER)
        qr.add_data(qr_link)
        qr.make(fit=True)
        img = qr.make_image()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return {"qr_link": qr_link, "qr_png": buf.getvalue()}

    async def check(self) -> dict:
        """查询一次扫码状态；成功时兑换 ticket → Cookie 并保存凭证。"""

        def _poll():
            r = self._session.get(_QR_POLL, params={"qrcode_key": self._key}, timeout=10)
            return r.json().get("data") or {}

        d = await asyncio.to_thread(_poll)
        code = d.get("code")

        if code == 86101:
            return {"status": "waiting"}
        if code == 86090:
            return {"status": "scanned"}
        if code == 86038:
            self._key = None
            return {"status": "expired"}
        if code != 0:
            return {"status": "waiting"}

        # ---- 登录成功：兑换 ticket → Cookie ----
        def _exchange():
            cross = self._session.get(d.get("url", ""), allow_redirects=False, timeout=10)
            got: dict[str, str] = {}
            for name, value in cross.raw.headers.items():
                if name.lower() != "set-cookie":
                    continue
                c = SimpleCookie()
                try:
                    c.load(value)
                except Exception:
                    cname, _, cval = value.partition("=")
                    if cname.strip() in _QR_WANT_COOKIES:
                        got[cname.strip()] = cval.split(";")[0]
                    continue
                for cn, morsel in c.items():
                    if cn in _QR_WANT_COOKIES and morsel.value:
                        got[cn] = morsel.value
            return got

        cookies = await asyncio.to_thread(_exchange)
        sessdata = cookies.get("SESSDATA", "")
        if not sessdata:
            self._key = None
            logger.warning("票据兑换未取得 SESSDATA（B站响应结构可能又变化）")
            return {"status": "error", "message": "未取得登录 Cookie"}

        # ---- 跟随 gourl 完成登录收尾（浏览器行为；实测未收尾的会话存活期极短）----
        try:
            gourl = ""
            for kv in d.get("url", "").split("?", 1)[1].split("&"):
                k, _, v = kv.partition("=")
                if k == "gourl":
                    from urllib.parse import unquote
                    gourl = unquote(v)

            def _follow():
                if gourl:
                    self._session.get(gourl, timeout=10)

            await asyncio.to_thread(_follow)
        except Exception as e:
            logger.warning("gourl 收尾请求失败（不影响凭证保存）: %s", e)

        buvid3 = buvid4 = ""

        def _spi():
            return self._session.get(_FINGER_SPI, timeout=10).json().get("data") or {}

        try:
            spi = await asyncio.to_thread(_spi)
            buvid3, buvid4 = spi.get("b_3", ""), spi.get("b_4", "")
        except Exception as e:
            logger.warning("获取设备指纹失败: %s", e)

        save_credential(Credential(
            sessdata=sessdata,
            bili_jct=cookies.get("bili_jct", ""),
            dedeuserid=cookies.get("DedeUserID", ""),
            ac_time_value=d.get("refresh_token", ""),
            buvid3=buvid3,
            buvid4=buvid4,
        ))
        self._key = None
        return {"status": "success"}


_qr_session: QRLoginSession | None = None
_qr_session_lock = threading.Lock()


def get_qr_login_session() -> QRLoginSession:
    """获取进程级扫码登录会话单例。"""
    global _qr_session
    if _qr_session is None:
        with _qr_session_lock:
            if _qr_session is None:
                _qr_session = QRLoginSession()
    return _qr_session


# ---------------------------------------------------------------------------
# Cookie 刷新（短期/长期凭证轮换）
#
# B站机制：SESSDATA 等为短期凭证（会过期），登录时下发的 refresh_token
# （即 Credential.ac_time_value）为长期凭证，可换发新短期凭证：
#   1. cookie/info     查询是否需要刷新（返回 refresh + timestamp）
#   2. correspond_path 固定 RSA 公钥加密时间戳 → 访问 correspond/1/<path> 拿 wc
#   3. new_csrf        HMAC-SHA256(key=wc, msg=timestamp 去掉毫秒)
#   4. cookie/refresh  旧 Cookie + csrf + new_csrf → 新短期凭证 + 新 refresh_token
#   5. confirm/refresh 新 csrf + 旧 refresh_token → 作废旧长期凭证
# 任何一步失败都保留旧凭证，不影响现有登录。
# ---------------------------------------------------------------------------

# correspond_path 生成用的固定 RSA 公钥（PKCS#1 v1.5 加密，输出 hex 去掉 0x 前缀）
REFRESH_RSA_PUBKEY_PEM = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDLgd2OAkcGVtoE3ThUREbio0Eg
Uc/prcajMKXvkCKFCWhJYJcLkcM2DKKcSeFpD/j6Boy538YXnR6VhcuUJOhH2x71
nzPjfdTcqMz7djHum0qSZA0AyCBDABUqCrfNgCiJ00Ra7GmRj+YCK1NJEuewlb40
JNrRuoEUXpabUzGB8QIDAQAB
-----END PUBLIC KEY-----"""


def _saved_at() -> float:
    """读取凭证文件的保存时间（无文件返回 0）。"""
    try:
        return float(json.loads(CREDENTIAL_FILE.read_text()).get("saved_at", 0))
    except (OSError, json.JSONDecodeError, ValueError):
        return 0.0


def _credential_session(cred: Credential) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": _QR_UA, "Referer": "https://www.bilibili.com"})
    s.cookies.update({"SESSDATA": cred.sessdata, "bili_jct": cred.bili_jct,
                      "DedeUserID": cred.dedeuserid})
    if getattr(cred, "buvid3", ""):
        s.cookies.set("buvid3", cred.buvid3, domain=".bilibili.com")
    if getattr(cred, "buvid4", ""):
        s.cookies.set("buvid4", cred.buvid4, domain=".bilibili.com")
    return s


def refresh_needed(cred: Credential) -> bool:
    """查询 cookie/info：当前短期凭证是否需要（且可以）刷新。"""
    if not getattr(cred, "ac_time_value", "") or not getattr(cred, "bili_jct", ""):
        return False
    s = _credential_session(cred)
    try:
        r = s.get("https://passport.bilibili.com/x/passport-login/web/cookie/info",
                  params={"csrf": cred.bili_jct}, timeout=10).json()
    except Exception as e:
        logger.warning("查询刷新状态失败: %s", e)
        return False
    return bool((r.get("data") or {}).get("refresh"))


def _correspond_path(ts_ms: int) -> str:
    """RSA/PKCS1 v1.5 加密毫秒时间戳 → hex 字符串（去掉 0x 前缀）。"""
    import rsa as rsa_lib
    pubkey = rsa_lib.PublicKey.load_pkcs1_openssl_pem(REFRESH_RSA_PUBKEY_PEM.encode())
    return hex(rsa_lib.encrypt(str(ts_ms).encode(), pubkey))[2:]


def refresh_credential(cred: Credential) -> tuple[bool, str]:
    """用长期凭证（ac_time_value）换发新短期凭证。

    成功返回 (True, 消息) 且新凭证已保存（nav 校验通过后才覆盖）；
    失败返回 (False, 原因)，旧凭证保持不变。
    """
    if not getattr(cred, "ac_time_value", ""):
        return False, "缺少长期凭证 refresh_token"
    if not getattr(cred, "bili_jct", ""):
        return False, "缺少 bili_jct（无法通过刷新校验）"

    s = _credential_session(cred)

    # 1. 是否需要刷新 + 取时间戳
    info = s.get("https://passport.bilibili.com/x/passport-login/web/cookie/info",
                 params={"csrf": cred.bili_jct}, timeout=10).json()
    if info.get("code") != 0:
        return False, f"cookie/info 失败: code={info.get('code')} {info.get('message', '')}"
    ts = int(info["data"]["timestamp"])
    if not info["data"].get("refresh"):
        return True, "当前凭证无需刷新"

    # 2. correspond_path → wc
    try:
        path = _correspond_path(ts)
    except Exception as e:
        return False, f"correspond_path 生成失败: {e}"
    try:
        wc = (s.get(f"https://www.bilibili.com/correspond/1/{path}", timeout=10)
              .json().get("data") or {}).get("wc", "")
    except Exception as e:
        return False, f"correspond 接口请求失败: {e}"
    if not wc:
        return False, "correspond 接口未返回 wc"

    # 3. new_csrf = HMAC-SHA256(key=wc, msg=timestamp 去掉毫秒尾数)
    new_csrf = hmac.new(wc.encode(), str(ts)[:10].encode(), hashlib.sha256).hexdigest()

    # 4. cookie/refresh 换发新短期凭证
    rr = s.post("https://passport.bilibili.com/x/passport-login/web/cookie/refresh",
                data={"csrf": cred.bili_jct, "new_csrf": new_csrf}, timeout=10)
    rj = rr.json()
    if rj.get("code") != 0:
        return False, f"cookie/refresh 失败: code={rj.get('code')} {rj.get('message', '')}"
    new_cookies = {c.get("name", ""): c.get("value", "")
                   for c in (rj.get("data", {}).get("cookie_info", {}).get("cookies") or [])}
    new_sessdata = new_cookies.get("SESSDATA", "")
    new_jct = new_cookies.get("bili_jct", "")
    if not new_sessdata or not new_jct:
        return False, "refresh 响应未包含新 Cookie"

    # 5. confirm/refresh 作废旧长期凭证（用新 csrf + 旧 refresh_token）
    cf = s.get("https://passport.bilibili.com/x/passport-login/web/confirm/refresh",
               params={"csrf": new_jct, "refresh_token": cred.ac_time_value}, timeout=10).json()
    if cf.get("code") != 0:
        return False, (f"confirm/refresh 失败: code={cf.get('code')} "
                       f"（新 Cookie 已下发但长期凭证未轮换，请留意）")

    new_cred = Credential(
        sessdata=new_sessdata, bili_jct=new_jct,
        dedeuserid=new_cookies.get("DedeUserID", cred.dedeuserid or ""),
        ac_time_value=rj.get("data", {}).get("refresh_token", cred.ac_time_value),
        buvid3=getattr(cred, "buvid3", "") or "",
        buvid4=getattr(cred, "buvid4", "") or "",
    )
    # nav 校验通过才覆盖，失败回滚保留旧凭证
    if _validate_credential(new_cred) is not True:
        return False, "刷新后的凭证校验未通过（已保留旧凭证）"
    save_credential(new_cred)
    return True, "凭证已刷新"


def maybe_auto_refresh():
    """应用启动时的一次性检查：距上次检查超过 24 小时才查询/执行刷新。"""
    state_file = CREDENTIAL_DIR / "refresh_state.json"
    try:
        cred = _load_saved_credential()
        if cred is None or not getattr(cred, "ac_time_value", ""):
            return
        last = 0
        if state_file.exists():
            last = json.loads(state_file.read_text()).get("last_check", 0)
        if time.time() - last < 86400:
            return
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump({"last_check": time.time()}, f)
        if refresh_needed(cred):
            ok, msg = refresh_credential(cred)
            (logger.info if ok else logger.warning)("自动刷新凭证: %s", msg)
    except Exception as e:
        logger.warning("自动刷新凭证失败: %s", e)


# ---------------------------------------------------------------------------
# 备用通道：Netscape cookies.txt 导入（浏览器 Cookie，不含 refresh_token，
# 无法参与刷新轮换，有效期以导出源为准）
# ---------------------------------------------------------------------------

def import_cookies_txt(path: str) -> tuple[bool, str]:
    """从 Netscape cookies.txt 导入 B站 Cookie。nav 校验通过才落盘。"""
    try:
        cookies: dict[str, str] = {}
        for line in open(path, encoding="utf-8"):
            if line.startswith("#") or not line.strip():
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 7 and "bilibili" in parts[0]:
                cookies[parts[5]] = parts[6]
    except OSError as e:
        return False, f"读取失败: {e}"
    if not cookies.get("SESSDATA"):
        return False, "文件中不含 SESSDATA"

    credential = Credential(
        sessdata=cookies["SESSDATA"],
        bili_jct=cookies.get("bili_jct", ""),
        dedeuserid=cookies.get("DedeUserID", ""),
        buvid3=cookies.get("buvid3", ""),
        buvid4=cookies.get("buvid4", ""),
    )
    if _validate_credential(credential) is not True:
        return False, "导入的 Cookie 校验未通过（可能已过期，请重新导出）"
    save_credential(credential)
    return True, "导入成功"
