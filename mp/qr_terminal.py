"""终端二维码渲染器：原生模块分辨率，模块物理上呈正方形，可被手机扫描。

为什么不用"PNG 重采样到固定列数"的旧方案（QQ 登录此前的方式）：
采样格子与二维码模块边界不对齐，定位图案被破坏，导致结构失真无法识别。

正确做法是 1 个二维码模块 = 1 个字符宽；终端字符"高:宽 ≈ 2:1"，
用半块字符（▀▄█ + 空格）把 2 行模块压进 1 行字符，模块恢复正方形。

渲染档位（按显示端编码能力自动选择）：
1. ▀▄█ 可编码 → 正立紧凑二维码（Windows Terminal / VS Code / UTF-8 环境）
2. 否则返回 "" —— 调用方必须显式提示用户打开 PNG 文件扫码，不允许静默降级
"""
from __future__ import annotations

# PNG 文件用的静区（打开图片扫码需要标准 4 模块静区）
PNG_BORDER = 4
# 终端渲染用的静区（屏幕上 1~2 模块即可）
TERMINAL_BORDER = 2


def can_encode(text: str, encoding: str | None) -> bool:
    """编码能否表示 text。encoding 为空视为不能。"""
    if not encoding:
        return False
    try:
        text.encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


def matrix_from_data(data: str, border: int = TERMINAL_BORDER) -> list[list[bool]]:
    """用 qrcode 库把内容生成模块布尔矩阵（True=黑模块）。"""
    import qrcode

    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L,
                       border=border)
    qr.add_data(data)
    qr.make(fit=True)
    return [list(row) for row in qr.get_matrix()]


def matrix_from_png(png: bytes) -> list[list[bool]]:
    """从二维码 PNG 检测模块网格，重建原生分辨率布尔矩阵。

    步骤：二值化 → 黑色包围盒（去除静区）→ 最小黑色行程 = 1 模块宽
    （时序图案保证存在单模块行程）→ 按模块中心采样 → 定位图案校验。
    """
    from PIL import Image
    import io

    img = Image.open(io.BytesIO(png)).convert("L")
    w, h = img.size
    px = img.load()
    dark = [[px[x, y] < 128 for x in range(w)] for y in range(h)]

    dark_rows = [y for y in range(h) if any(dark[y])]
    if not dark_rows:
        raise ValueError("PNG 中无黑色内容")
    top, bottom = dark_rows[0], dark_rows[-1]
    dark_cols = [x for x in range(w) if any(dark[y][x] for y in range(top, bottom + 1))]
    left, right = dark_cols[0], dark_cols[-1]
    bw, bh = right - left + 1, bottom - top + 1
    if bw != bh:
        raise ValueError(f"二维码区域非正方形: {bw}x{bh}")

    # 最小黑色行程 = 1 模块的像素宽
    min_run = None
    for y in range(top, bottom + 1):
        run = 0
        for x in range(left, right + 1):
            if dark[y][x]:
                run += 1
            elif run:
                min_run = run if min_run is None else min(min_run, run)
                run = 0
        if run:
            min_run = run if min_run is None else min(min_run, run)
    if not min_run:
        raise ValueError("未检测到黑色行程")

    n = round(bw / min_run)
    if not (21 <= n <= 177) or abs(bw - n * min_run) > 2:
        raise ValueError(f"模块网格检测失败: 宽{bw}px / 行程{min_run}px")

    cell = bw / n
    matrix = []
    for r in range(n):
        y = top + int((r + 0.5) * cell)
        matrix.append([dark[y][left + int((c + 0.5) * cell)] for c in range(n)])

    if not _looks_like_qr(matrix):
        raise ValueError("重建矩阵缺少定位图案")
    return matrix


def _looks_like_qr(matrix: list[list[bool]]) -> bool:
    """校验左上/右上/左下三个定位图案（7x7 外环黑、内环白、中心 3x3 黑、分隔白）。"""
    n = len(matrix)

    def finder_ok(r0: int, c0: int, sep_row: int | None, sep_col: int | None) -> bool:
        # 外环四角黑
        for r, c in ((r0, c0), (r0, c0 + 6), (r0 + 6, c0), (r0 + 6, c0 + 6)):
            if not matrix[r][c]:
                return False
        # 中心黑、内环白
        if not matrix[r0 + 3][c0 + 3] or matrix[r0 + 1][c0 + 1]:
            return False
        # 分隔行/列白（右上图案分隔在左侧，左下图案分隔在上方）
        if sep_row is not None and any(matrix[sep_row][c0 + c] for c in range(7)):
            return False
        if sep_col is not None and any(matrix[r0 + r][sep_col] for r in range(7)):
            return False
        return True

    return (finder_ok(0, 0, sep_row=7, sep_col=7)
            and finder_ok(0, n - 7, sep_row=7, sep_col=n - 8)
            and finder_ok(n - 7, 0, sep_row=n - 8, sep_col=7))


def pad_matrix(matrix: list[list[bool]], border: int = TERMINAL_BORDER) -> list[list[bool]]:
    """给矩阵四周补 border 圈白模块（终端显示需要最小静区才易被扫描）。"""
    if border <= 0:
        return [row[:] for row in matrix]
    n = len(matrix)
    width = n + 2 * border
    blank = [False] * width
    rows = [blank[:] for _ in range(border)]
    for row in matrix:
        rows.append([False] * border + list(row) + [False] * border)
    rows += [blank[:] for _ in range(border)]
    return rows


def render_terminal_qr(matrix: list[list[bool]], encoding: str | None = None) -> str:
    """把模块矩阵渲染为终端字符（原生分辨率，▀▄█ 紧凑，自动补静区）。

    encoding 传显示端实际编码（如 rich console 的 .encoding）；
    不支持半块字符时返回 ""，由调用方走 PNG 提示路径。
    """
    if not matrix or not can_encode("▀▄█", encoding):
        return ""
    rows = pad_matrix(matrix, TERMINAL_BORDER)
    if len(rows) % 2:  # 补一行白，凑偶数行
        rows.append([False] * len(rows[0]))

    lines = []
    for y in range(0, len(rows), 2):
        line = "".join(
            "█" if t and b else "▀" if t else "▄" if b else " "
            for t, b in zip(rows[y], rows[y + 1])
        )
        lines.append(line)
    return "\n".join(lines)


def matrix_from_rendered(text: str) -> list[list[bool]]:
    """从 render_terminal_qr 的输出还原矩阵（测试/校验用）。"""
    lines = text.splitlines()
    width = max((len(line) for line in lines), default=0)
    matrix = []
    for line in lines:
        top = [ch in "█▀" for ch in line] + [False] * (width - len(line))
        bottom = [ch in "█▄" for ch in line] + [False] * (width - len(line))
        matrix.append(top)
        matrix.append(bottom)
    return matrix


def trim_light_rows(matrix: list[list[bool]]) -> list[list[bool]]:
    """裁掉矩阵末尾的全白行（渲染时为凑偶数行补的行）。"""
    end = len(matrix)
    while end > 0 and not any(matrix[end - 1]):
        end -= 1
    return matrix[:end]
