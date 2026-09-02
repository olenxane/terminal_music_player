"""配置文件加载与解析"""
from __future__ import annotations
import os
import copy
from dataclasses import dataclass, field
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None


DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.yaml")

BAND_FREQS = ["31Hz", "62Hz", "125Hz", "250Hz", "500Hz", "1kHz",
              "2kHz", "4kHz", "8kHz", "16kHz"]
BAND_CENTER_HZ = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
DEFAULT_Q_VALUES = [1.0, 1.0, 1.0, 1.0, 2.5, 1.0, 1.0, 1.0, 4.0, 1.0]

class ConfigError(Exception):
    pass


def _minimal_yaml_load(text: str) -> dict:
    """极简 YAML 兜底解析器（仅当 PyYAML 不可用时使用）。
    """
    raise ConfigError(
        "未安装 PyYAML，无法解析配置文件。请先运行: pip install pyyaml"
    )


def load_raw_config(path: str) -> dict:
    if not os.path.exists(path):
        raise ConfigError(f"配置文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    if yaml is not None:
        data = yaml.safe_load(text)
    else:
        data = _minimal_yaml_load(text)
    if not isinstance(data, dict):
        raise ConfigError("配置文件格式错误：根节点必须是一个映射(dict)")
    return data


@dataclass
class ThemeColors:
    key: str
    name: str
    primary: str
    secondary: str
    accent: str
    text: str
    dim: str
    bg_bar: str
    spectrum_gradient: list
    lyric_current: str
    lyric_context: str


@dataclass
class EqualizerConfig:
    enabled: bool
    bands_db: list  # 10 个 float, 单位 dB, 顺序与 BAND_FREQS 一致
    q_values: list  # 10 个 float, 每个频段的 Q 值
    preset: str = "custom"


@dataclass
class LyricsConfig:
    fuzzy_match: bool = True
    fuzzy_threshold: float = 0.55
    offset_ms: int = 0
    context_lines: int = 2
    lyrics_dir: str = ""


@dataclass
class SpectrumConfig:
    bars: int = 48
    fft_size: int = 2048
    smoothing: float = 0.6
    min_db: float = -60
    max_db: float = 0
    style: str = "blocks"
    height: int = 4  # 频谱行数，1~5
    height_scale: float = 1.0  # 高度缩放因子，<1 变矮 >1 变高
    zhanwei_char: str = "▁"  # 未填充格位的占位字符，默认 ▁
    enabled: bool = True  # 是否默认显示频谱


@dataclass
class PlaybackConfig:
    default_volume: float = 0.8
    fade_ms: int = 150
    playlist_mode: str = "sequential"
    ui_fps: int = 20
    ui_width: int = 50


@dataclass
class LoudnessConfig:
    enabled: bool = False
    target_lufs: float = -16.0


@dataclass
class VbeConfig:
    enabled: bool = False
    gain_db: float = -3.0


@dataclass
class LimiterConfig:
    enabled: bool = False
    threshold_db: float = -1.0
    release_ms: float = 50.0


@dataclass
class DspConfig:
    loudness: LoudnessConfig = field(default_factory=LoudnessConfig)
    vbe: VbeConfig = field(default_factory=VbeConfig)
    limiter: LimiterConfig = field(default_factory=LimiterConfig)


@dataclass
class OnlineMusicConfig:
    qq_quality: int = 320          # 128 或 320
    wy_quality: str = "exhigh"     # standard/higher/exhigh/lossless/hires


@dataclass
class AppConfig:
    music_dirs: list
    equalizer: EqualizerConfig
    lyrics: LyricsConfig
    spectrum: SpectrumConfig
    playback: PlaybackConfig
    dsp: DspConfig
    theme: ThemeColors
    all_themes: dict
    raw: dict
    path: str
    online_music: OnlineMusicConfig = field(default_factory=OnlineMusicConfig)

    def theme_names(self):
        return list(self.all_themes.keys())


def _build_theme(key: str, raw: dict) -> ThemeColors:
    return ThemeColors(
        key=key,
        name=raw.get("name", key),
        primary=raw.get("primary", "#ffffff"),
        secondary=raw.get("secondary", "#cccccc"),
        accent=raw.get("accent", "#ffcc00"),
        text=raw.get("text", "#e0e0e0"),
        dim=raw.get("dim", "#666666"),
        bg_bar=raw.get("bg_bar", "#202020"),
        spectrum_gradient=raw.get("spectrum_gradient", ["#666666", "#aaaaaa", "#ffffff"]),
        lyric_current=raw.get("lyric_current", "bold #ffffff"),
        lyric_context=raw.get("lyric_context", "#888888"),
    )


def _resolve_eq_bands(eq_raw: dict) -> list:
    preset = eq_raw.get("preset", "custom")
    presets = eq_raw.get("presets", {})
    if preset != "custom" and preset in presets:
        vals = presets[preset]
    else:
        bands_map = eq_raw.get("bands", {})
        vals = [float(bands_map.get(f, 0)) for f in BAND_FREQS]
    vals = [float(v) for v in vals]
    if len(vals) != 10:
        raise ConfigError("均衡器必须恰好包含 10 个频段的数值")
    return vals


def _resolve_eq_q_values(eq_raw: dict) -> list:
    q_map = eq_raw.get("q_values", {})
    vals = [float(q_map.get(f, dq)) for f, dq in zip(BAND_FREQS, DEFAULT_Q_VALUES)]
    return vals


def _resolve_music_dirs(raw: dict) -> list:
    """兼容新旧两种写法：
    - 新版: music_dirs: [路径1, 路径2, ...]
    - 旧版: music_dir: "单个路径"（字符串）
    返回去重后的路径列表（原始顺序保留，不在此处校验是否存在）"""
    dirs = []
    multi = raw.get("music_dirs")
    if isinstance(multi, list):
        dirs.extend(str(d).strip() for d in multi if str(d).strip())
    single = raw.get("music_dir")
    if isinstance(single, str) and single.strip():
        dirs.append(single.strip())
    # 去重，保留顺序
    seen = set()
    result = []
    for d in dirs:
        key = os.path.abspath(os.path.expanduser(d))
        if key not in seen:
            seen.add(key)
            result.append(d)
    return result


def save_music_dirs(path: str, music_dirs: list) -> None:
    """把音频目录列表写回配置文件，仅更新 music_dirs 字段（保留其余配置和注释无法完全保留，
    因为 yaml.safe_load 不保留注释；此函数用最小侵入方式重写整份配置）。
    """
    if yaml is None:
        raise ConfigError("未安装 PyYAML，无法写入配置文件。请先运行: pip install pyyaml")
    raw = load_raw_config(path) if os.path.exists(path) else {}
    raw["music_dirs"] = list(music_dirs)
    raw.pop("music_dir", None)  # 迁移到新字段，避免新旧同时存在造成混淆
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, allow_unicode=True, sort_keys=False, default_flow_style=False)


def load_config(path: str = DEFAULT_CONFIG_PATH) -> AppConfig:
    raw = load_raw_config(path)

    eq_raw = raw.get("equalizer", {}) or {}
    equalizer = EqualizerConfig(
        enabled=bool(eq_raw.get("enabled", True)),
        bands_db=_resolve_eq_bands(eq_raw),
        q_values=_resolve_eq_q_values(eq_raw),
        preset=eq_raw.get("preset", "custom"),
    )

    lyr_raw = raw.get("lyrics", {}) or {}
    lyrics = LyricsConfig(
        fuzzy_match=bool(lyr_raw.get("fuzzy_match", True)),
        fuzzy_threshold=float(lyr_raw.get("fuzzy_threshold", 0.55)),
        offset_ms=int(lyr_raw.get("offset_ms", 0)),
        context_lines=int(lyr_raw.get("context_lines", 2)),
        lyrics_dir=raw.get("lyrics_dir", "") or "",
    )

    spec_raw = raw.get("spectrum", {}) or {}
    spectrum = SpectrumConfig(
        bars=int(spec_raw.get("bars", 48)),
        fft_size=int(spec_raw.get("fft_size", 2048)),
        smoothing=float(spec_raw.get("smoothing", 0.6)),
        min_db=float(spec_raw.get("min_db", -60)),
        max_db=float(spec_raw.get("max_db", 0)),
        style=spec_raw.get("style", "blocks"),
        height=max(1, min(5, int(spec_raw.get("height", 4)))),
        height_scale=max(0.1, float(spec_raw.get("height_scale", 1.0))),
        zhanwei_char=spec_raw.get("zhanwei_char", "▁") or "▁",
        enabled=bool(spec_raw.get("enabled", True)),
    )

    pb_raw = raw.get("playback", {}) or {}
    playback = PlaybackConfig(
        default_volume=float(pb_raw.get("default_volume", 0.8)),
        fade_ms=int(pb_raw.get("fade_ms", 150)),
        playlist_mode=pb_raw.get("playlist_mode", "sequential"),
        ui_fps=int(pb_raw.get("ui_fps", 20)),
        ui_width=max(30, int(pb_raw.get("ui_width", 50))),
    )

    dsp_raw = raw.get("dsp", {}) or {}
    loud_raw = dsp_raw.get("loudness", {}) or {}
    vbe_raw = dsp_raw.get("vbe", {}) or {}
    lim_raw = dsp_raw.get("limiter", {}) or {}
    dsp = DspConfig(
        loudness=LoudnessConfig(
            enabled=bool(loud_raw.get("enabled", False)),
            target_lufs=float(loud_raw.get("target_lufs", -16.0)),
        ),
        vbe=VbeConfig(
            enabled=bool(vbe_raw.get("enabled", False)),
            gain_db=float(vbe_raw.get("gain_db", -3.0)),
        ),
        limiter=LimiterConfig(
            enabled=bool(lim_raw.get("enabled", False)),
            threshold_db=float(lim_raw.get("threshold_db", -1.0)),
            release_ms=float(lim_raw.get("release_ms", 50.0)),
        ),
    )

    themes_raw = raw.get("themes", {}) or {}
    all_themes = {k: _build_theme(k, v) for k, v in themes_raw.items()}
    theme_key = raw.get("theme", "neon")
    if theme_key not in all_themes:
        if all_themes:
            theme_key = list(all_themes.keys())[0]
        else:
            raise ConfigError("配置文件中未定义任何主题(themes)")

    music_dirs = _resolve_music_dirs(raw)

    om_raw = raw.get("online_music", {}) or {}
    online_music = OnlineMusicConfig(
        qq_quality=int(om_raw.get("qq_quality", 320)),
        wy_quality=om_raw.get("wy_quality", "exhigh"),
    )

    return AppConfig(
        music_dirs=music_dirs,
        equalizer=equalizer,
        lyrics=lyrics,
        spectrum=spectrum,
        playback=playback,
        dsp=dsp,
        theme=all_themes[theme_key],
        all_themes=all_themes,
        raw=raw,
        path=path,
        online_music=online_music,
    )


def reload_config(cfg: AppConfig) -> AppConfig:
    """重新从磁盘加载配置（用于运行时热重载均衡器/主题）"""
    return load_config(cfg.path)
