# 安全策略

## 受支持版本

| 版本 | 是否支持 |
| --- | --- |
| PyPI 最新发布版 | ✅ |
| `main` 分支 | ✅ |
| 更早版本 | ❌（请升级） |

## 报告安全漏洞

若你认为 NetEase MusicBox 存在安全漏洞，请负责任地私下报告。**不要**在公开 GitHub Issue 中披露敏感安全问题。

**推荐渠道**

在本仓库使用 [GitHub 私密漏洞报告](https://github.com/darknessomi/musicbox/security/advisories/new)。

请尽量提供：

- 问题描述与影响范围
- 复现步骤（PoC、命令或截图）
- 受影响版本或 commit
- 运行环境（操作系统、Python 版本、安装方式）
- 修复或缓解建议（可选）

我们争取在 **7 个工作日内**确认收到，并与报告者协商公开披露时间。在修复完成前，请勿公开细节。

**不在范围内**（不视为 MusicBox 自身漏洞）：

- 上游 [NeteaseCloudMusicApiEnhanced/api-enhanced](https://github.com/neteasecloudmusicapienhanced/api-enhanced) 或网易云官方服务的问题
- 网易云账号封禁、版权限制、地区播放限制
- 用户本机配置不当（例如 home 目录权限过宽）
- 针对终端用户的社会工程学攻击

## 安全模型

MusicBox 是**本地命令行客户端**，不会在局域网暴露 HTTP 服务。播放控制通过**本机 Unix 域套接字**通信，套接字权限为 `0600`，运行时目录为 `0700`（按 UID 隔离）。

### 本地敏感数据

登录后，会话与状态会写入本地磁盘。请像保管凭据一样保护这些路径：

| 路径（默认） | 内容 |
| --- | --- |
| `~/.local/share/netease-musicbox/cookie.txt` | 网易云会话 Cookie |
| `~/.local/share/netease-musicbox/database.json` | 用户信息、歌单、播放状态 |
| `~/.netease-musicbox/config.json` | 用户配置（可能含代理） |
| `~/.local/share/netease-musicbox/musicbox.log` | 应用日志 |

若设置了 `XDG_DATA_HOME` / `XDG_CONFIG_HOME`，路径遵循 [XDG 目录规范](https://specifications.freedesktop.org/basedir-spec/basedir-spec-latest.html)，位于 `netease-musicbox/` 下。

### 认证方式

- 登录**仅支持**网易云音乐 App 扫码；不支持账号密码登录。
- 能读取 `cookie.txt` 的本地用户，在 Cookie 失效或被撤销前，可冒用你的网易云会话。
- 共享或弃用机器时，请执行 `musicbox auth logout` 或删除本地数据。

### 网络

- MusicBox 通过 HTTPS 调用网易云相关 API（内置 API 客户端）。
- 流量可能包含账号标识、播放元数据与流媒体 URL。
- 非中国大陆用户常配置 `http_proxy` / `https_proxy`；请确保信任代理提供方。

### Daemon 与 CLI

- 后台 daemon（`musicboxd`）仅监听本机 Unix 套接字（`musicboxd-<uid>.sock`）。
- 在多用户系统上，其他本地用户通常无法连接，除非共享同一 UID 或提权。
- 除非清楚文件归属与 Cookie 暴露风险，否则不要以 root 运行 MusicBox。

## 用户建议

- 保持 MusicBox 与依赖更新（`uv tool upgrade netease-musicbox` 或从源码重装）。
- 限制 `~/.netease-musicbox/` 及 XDG 数据/配置目录权限（目录 `chmod 700`，敏感文件必要时 `chmod 600`）。
- 勿将 `cookie.txt`、日志或扫码登录输出粘贴到公开聊天或 Issue。
- 使用第三方 Agent/Skill 前请评估风险；它们继承你的 shell 权限，可读取本地 Cookie。

## 依赖安全

依赖安全问题通过日常维护与 CI（`ruff`、`ty`、`pytest`）跟进。供应链方面，请优先从 PyPI 或本官方仓库安装，避免使用未验证的分叉包。
