# 终端音乐播放器

一个纯 Python 编写、运行在终端里的音乐播放器，专为 coding 时在 VSCode 底部终端常驻播放而设计😋。

## 功能特性

- **跨平台支持**：Windows 用 `msvcrt`、Unix 用 `termios/tty` 实现非阻塞键盘输入，Linux端目前没有进行真正的使用测试。
- **全格式解码**：通过 ffmpeg 子进程流式解码，支持 mp3 / wav / flac / ogg / m4a / aiff / aac 等格式
- **Windows 免装 ffmpeg**：自动检测系统 PATH，若未找到则回退到 `imageio-ffmpeg` 自带的预编译二进制
- **紧凑界面**：原地刷新，开频谱 ≤7 行、关频谱 ≤3 行，适合 VSCode 底部终端
- **实时歌词**：解析本地 `.lrc` 文件，支持精确匹配与模糊匹配；在线歌曲自动拉取歌词
- **实时频谱**：对 PCM 数据做 FFT，按对数频段分组并做渐变色柱状图渲染（可开关）
- **10 段均衡器**：通过独立 GUI 配置工具调节，终端中按 `r` 键热重载
- **多主题**：内置 15 套主题，可在 GUI 配置工具中自定义
- **在线音乐**：接入 QQ 音乐 / 网易云音乐 / 哔哩哔哩，支持搜索、每日推荐、收藏、歌单、排行榜，扫码登录 / Cookie 导入。
- **在线下载**：播放在线歌曲时按 `d` 一键下载，同时保存歌词、写入标签与封面
- **音质增强链**：虚拟低音 VBE、谐波激励器、声场展宽、LUFS 响度均衡、前瞻软限幅，可独立开关并自定义链路顺序（本部分逻辑实现完全由LLM生成）
- **歌曲选择器**：`Tab` 打开本地曲库搜索过滤，输入即时筛选，回车跳转
- **栈式播放列表**：FIFO 播放队列 + 历史缓冲栈，非常好用的播放列表功能，但是在添加播放列表后退出时记得按esc而不是enter。
- **标题跑马灯**：超长歌名截断停留后滚动显示，参数可在配置中调节
- **按键绑定**：核心快捷键可在配置 / GUI 中改绑，方向键固定
- **播放统计**：神秘的听歌总结，启动report文件夹内的server.py即可

## 安装

```bash
pip install -r requirements.txt
```

需要系统已安装可用的音频输出设备（PortAudio）。Linux 上如果 `sounddevice` 报错，可能需要：

```bash
sudo apt-get install libportaudio2
```

Windows 用户无需手动安装 ffmpeg — `imageio-ffmpeg` 包自带预编译二进制。

## 使用

### 播放器

```bash
# 首次启动会交互式询问音频目录
python main.py

# 强制重新运行音频目录配置向导
python main.py --setup

# 命令行显式指定目录/文件
python main.py /path/to/music
python main.py /path/to/song.m4a

# 使用自定义配置文件
python main.py -c myconfig.yaml
```

### GUI 配置工具

均衡器、主题、频谱参数等复杂配置通过独立 GUI 工具管理，终端中不提供任何配置功能：

```bash
python configure_gui.py
```

包含 7 个标签页：
- **均衡器**：10 段滑杆 + 预设下拉
- **主题**：主题列表 + 颜色编辑 + 实时预览
- **频谱·歌词·播放**：频谱高度/柱数/FFT/平滑、歌词模糊匹配/偏移、默认音量/播放模式/FPS、标题跑马灯
- **音质增强**：LUFS 响度 / VBE 虚拟低音 / 软限幅 / 激励器 / 声场展宽，各自独立开关
- **音频目录**：增删文件夹/文件、独立歌词目录
- **在线音乐**：QQ / 网易云音质、下载目录
- **按键绑定**：核心快捷键改绑（方向键与选择器/在线模式内部按键除外）

保存后写入 `config.yaml`，在播放器中按 `r` 键即可热重载，无需重启。

### 在线音乐

播放器中按 `o` 进入在线模式，选择平台后浏览或搜索：

- **QQ 音乐（功能较完整）**：搜索歌曲、每日推荐、收藏歌曲、我的歌单、排行榜、扫码登录
- **网易云音乐（基础功能）**：搜索歌曲、排行榜、每日推荐、扫码登录
- **哔哩哔哩（功能较完整）**：搜索视频、UP 主搜索、热门、首页推荐、我的收藏、导入 Cookie

QQ音乐密钥自动刷新可能失效，若出现无法使用账号相关功能问题请尝试重新登录手动刷新密钥。


列表内方向键导航，回车播放，`←` 加入播放队列，`Esc` / `o` 返回上一层。  
播放在线歌曲时按 `d` 下载到配置目录（默认 `download_musics/`），歌词与封面一并写入。

## 界面布局

```
行1: ▶ 霜雪千年 · 南北组  01:35/04:29  [随机]  🔊114%   ← 标题行
行2: ━━━━━━━━━━━━━━━━━╸─────────────────────────    ← 进度条
行3: ♪ 在这老街回眸                                     ← 歌词行
行4: ▂▃▄▅▆▇██▇▆▅▄▃▂                                    ← 频谱 (3~5行)
```


## 按键说明

| 按键 | 功能 |
|---|---|
| 空格 | 播放 / 暂停 |
| n / p | 下一首 / 上一首（历史回退） |
| ← / → | 快退 / 快进 5 秒 |
| ↑ / ↓ | 音量 +5% / -5% |
| Tab | 打开 / 关闭歌曲选择器 |
| o | 进入在线音乐模式 |
| s | 切换频谱显示 |
| t | 切换配色主题 |
| m | 切换播放模式 |
| e | 开关谐波激励器 |
| w | 开关声场展宽 |
| d | 下载当前在线歌曲 |
| u | 重新运行音频目录配置向导 |
| r | 热重载 `config.yaml` |
| q | 退出 |

均衡器调节请使用 `python configure_gui.py`。核心按键可在 GUI「按键绑定」页改绑。

### 选择器 / 在线模式内按键

| 按键 | 功能 |
|---|---|
| ↑ / ↓ | 上下移动 |
| ← / → | 加入播放队列 / 对列表项快捷操作 |
| 回车 | 播放 / 进入 |
| 退格 | 删除搜索字符 |
| Esc / Tab / o | 关闭选择器 / 返回上一层 |

## 配置文件说明（`config.yaml`）

- `music_dirs`：音乐库目录列表
- `lyrics_dir`：独立歌词目录（可空，空则与歌曲同目录查找）
- `lyrics`：模糊匹配开关/阈值/偏移量/上下文行数
- `equalizer`：10 频段 dB 增益 + Q 值 + 内置预设
- `spectrum`：柱数/FFT大小/平滑系数/dB范围/高度(1~5)/是否默认显示
- `theme` + `themes`：当前主题名 + 所有主题颜色定义
- `playback`：默认音量/播放模式/UI刷新率/界面宽度/标题跑马灯
- `dsp`：音效链顺序 + 响度均衡/VBE/限幅/激励器/声场展宽参数
- `online_music`：QQ / 网易云 / B 站音质与下载目录
- `keybindings`：核心快捷键绑定

## 项目结构

```
terminal-music-player/
  main.py               # 播放器入口
  configure_gui.py      # GUI 配置工具入口
  config.yaml           # 默认配置
  requirements.txt
  mp/
    config.py            # 配置加载与解析
    equalizer.py         # 10段图形均衡器（biquad 级联，流式处理）
    vbe.py               # 虚拟低音增强 VBE
    exciter.py           # 谐波激励器（高频光泽）
    stereo_widener.py    # 声场展宽（M/S + 可选房间混响）
    loudness.py          # ITU-R BS.1770-4 LUFS 响度测量与补偿
    limiter.py           # 前瞻软限幅器
    ffmpeg_decoder.py    # ffmpeg 子进程流式解码（支持 m4a/AAC）
    spectrum.py          # FFT 频谱分析 + 对数分组 + 平滑
    lyrics.py            # LRC 解析 + 精确/模糊匹配
    metadata.py          # 歌曲元数据读取/写入（mutagen）
    player.py            # 播放引擎（解码线程 + DSP 链 + 环形缓冲 + sounddevice）
    playlist.py          # 播放列表 / 扫描目录 / 队列与历史栈
    ui.py                # rich 紧凑终端界面渲染（含标题跑马灯）
    online_ui.py         # 在线音乐界面渲染
    online_music.py      # QQ / 网易云 / B站 在线音乐管理器
    qr_terminal.py       # 终端二维码渲染（扫码登录）
    stats.py             # 播放统计
    keyinput.py          # 非阻塞键盘输入（msvcrt / termios）
    setup_wizard.py      # 首次启动 / 运行时音频目录配置向导
    logging_utils.py     # 日志路由到 log/error.log
    app.py               # 主程序 / 事件循环
  renderer/            #运行其中的main.py可将程序渲染为独立窗口在桌面上显示
```

## 致敬：
- **qqmusicbox**：使用了该项目提供的部分QQ音乐接口封装
- **bilibili-cli**：使用了该项目提供的bilibili接口封装
- [**NetEase-MusicBox**](https://github.com/darknessomi/musicbox/graphs/contributors)：使用了该项目的基础网易云音乐接口封装。