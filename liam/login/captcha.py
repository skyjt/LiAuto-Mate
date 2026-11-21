"""滑块验证码求解（纯函数，便于离线测试）。

浏览器交互（Playwright/CDP）留在 ``flow.py``；这里只处理图像与轨迹：

* ``find_hole``             沿用早期 spike 的暗色缺口检测，作为兜底。
* ``locate_slot``           拼图块模板匹配 + 缺口候选校验，输出缺口中心。
* ``build_drag_track``      生成带缓动与抖动的拖拽轨迹。

匹配使用掩码归一化互相关（NCC）：对拼图块透明区域以外的像素做相关，
对整体明暗变化不敏感，因此即使服务端把缺口压暗也能匹配到正确位置。
"""

from __future__ import annotations

import math
import random
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

# numpy/Pillow 属于可选 ``login`` 依赖：模块顶层不导入，保证没有登录组件时
# 看板仍能启动；只有真正求解滑块或运行测试时才需要它们。
if TYPE_CHECKING:
    import numpy as np


@dataclass(frozen=True)
class SlotMatch:
    """一个候选缺口（坐标单位为背景图像像素）。"""

    cx: float
    cy: float
    width: float
    height: float
    score: float = 0.0
    source: str = "dark"


# ---------------------------------------------------------------------------
# 暗色缺口检测（保留原 spike 配方，作为模板匹配失败时的兜底）
# ---------------------------------------------------------------------------
def find_hole(bg: np.ndarray) -> list[dict]:
    """返回背景图中尺寸/亮度符合滑块缺口特征的连通区域。"""
    import numpy as np
    from PIL import Image, ImageFilter

    m = np.asarray(Image.fromarray(bg.astype(np.uint8))
                   .filter(ImageFilter.BoxBlur(3))).astype(float)
    s = np.asarray(Image.fromarray((bg ** 2).clip(0, 255).astype(np.uint8))
                   .filter(ImageFilter.BoxBlur(3))).astype(float)
    std = np.sqrt(np.maximum(s - m ** 2, 0))
    mask = (bg < 75) & (std < 20)
    H, W = mask.shape
    seen = np.zeros_like(mask, bool)
    comps = []
    for y in range(H):
        for x in range(W):
            if mask[y, x] and not seen[y, x]:
                q = deque([(y, x)])
                seen[y, x] = True
                pts = []
                while q:
                    cy, cx = q.popleft()
                    pts.append((cy, cx))
                    for dy in (-1, 0, 1):
                        for dx in (-1, 0, 1):
                            ny, nx = cy + dy, cx + dx
                            if 0 <= ny < H and 0 <= nx < W and mask[ny, nx] and not seen[ny, nx]:
                                seen[ny, nx] = True
                                q.append((ny, nx))
                if 400 < len(pts) < H * W * 0.5:
                    ys = [p[0] for p in pts]
                    xs = [p[1] for p in pts]
                    lum = bg[[p[0] for p in pts], [p[1] for p in pts]].mean()
                    comps.append({
                        "size": len(pts),
                        "x0": min(xs), "x1": max(xs),
                        "y0": min(ys), "y1": max(ys),
                        "cx": (min(xs) + max(xs)) / 2,
                        "cy": (min(ys) + max(ys)) / 2,
                        "mean": float(lum),
                    })
    comps.sort(key=lambda c: -c["size"])
    return [c for c in comps
            if 800 < c["size"] < 6000
            and (c["x1"] - c["x0"]) > 35 and (c["y1"] - c["y0"]) > 35]


# ---------------------------------------------------------------------------
# 模板匹配
# ---------------------------------------------------------------------------
def _correlate2d(image: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    """valid 模式的二维相关（numpy FFT，无 scipy 依赖）。"""
    import numpy as np

    ih, iw = image.shape
    kh, kw = kernel.shape
    shape = (ih + kh - 1, iw + kw - 1)
    image_f = np.fft.rfft2(image.astype(np.float64), s=shape)
    kernel_f = np.fft.rfft2(kernel[::-1, ::-1].astype(np.float64), s=shape)
    full = np.fft.irfft2(image_f * kernel_f, s=shape)
    return full[kh - 1:ih, kw - 1:iw]


def _ncc_map(image: np.ndarray, image_sq: np.ndarray,
             template: np.ndarray, mask: np.ndarray) -> np.ndarray | None:
    """掩码归一化互相关，返回 (H-h+1, W-w+1) 的分数图。"""
    import numpy as np

    ih, iw = image.shape
    th, tw = template.shape
    if th > ih or tw > iw:
        return None
    m = mask.astype(np.float64)
    n = float(m.sum())
    if n < 25:
        return None
    t = template.astype(np.float64)
    mean_t = float((t * m).sum() / n)
    tz = (t - mean_t) * m
    var_t = float((tz * tz).sum())
    if var_t < 1e-3:
        return None
    num = _correlate2d(image, tz)
    sum_i = _correlate2d(image, m)
    sum_i2 = _correlate2d(image_sq, m)
    var_i = sum_i2 - (sum_i * sum_i) / n
    denom = np.sqrt(np.maximum(var_i, 1e-6) * var_t)
    return num / np.maximum(denom, 1e-6)


def _resize_piece(piece: np.ndarray, mask: np.ndarray,
                  width: int, height: int) -> tuple[np.ndarray, np.ndarray]:
    import numpy as np
    from PIL import Image

    p = Image.fromarray(np.clip(piece, 0, 255).astype(np.uint8))
    a = Image.fromarray(np.clip(mask * 255, 0, 255).astype(np.uint8))
    p = p.resize((max(1, width), max(1, height)), Image.Resampling.LANCZOS)
    a = a.resize((max(1, width), max(1, height)), Image.Resampling.LANCZOS)
    return (np.asarray(p, dtype=np.float64),
            np.asarray(a, dtype=np.float64) > 127)


def decode_piece(raw: bytes) -> tuple[np.ndarray, np.ndarray,
                                      tuple[float, float, float, float]] | None:
    """解码拼图块，裁剪透明留白并返回可见区域占比。

    返回值中的 ``frac`` 为 ``(x, y, w, h)`` 相对原图的比例，页面可用它把
    ``<img>`` 的 CSS 矩形换算成可见拼图块的真实尺寸与中心。
    """
    import io

    import numpy as np
    from PIL import Image

    try:
        im = Image.open(io.BytesIO(raw)).convert("RGBA")
        orig_w, orig_h = im.size
        arr = np.asarray(im)
    except Exception:  # noqa: BLE001
        return None
    if not orig_w or not orig_h:
        return None
    gray = (arr[..., 0] * 0.299 + arr[..., 1] * 0.587
            + arr[..., 2] * 0.114).astype(float)
    mask = arr[..., 3] > 64
    if not mask.any():
        mask = np.ones(gray.shape, bool)
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return None
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    frac = (x0 / orig_w, y0 / orig_h, (x1 - x0) / orig_w, (y1 - y0) / orig_h)
    return gray[y0:y1, x0:x1], mask[y0:y1, x0:x1], frac


def _width_candidates(target_w: float, target_h: float,
                      piece_w: int, piece_h: int,
                      holes: list[dict]) -> list[int]:
    """拼图块在背景图上的候选宽度。

    页面 CSS 尺寸给出的 ``target_w`` 通常最准；再围绕它做 ±12% 的搜索。
    暗色缺口的包围盒也作为候选，避免 CSS 尺寸因容器 padding 偏移。
    """
    values: list[float] = []
    if target_w and target_w > 8:
        values.extend(target_w * f for f in (0.88, 0.94, 1.0, 1.06, 1.12))
    if target_h and target_h > 8 and piece_h:
        # 由 CSS 高度反推宽度，兼顾宽高比例误差
        values.append(target_h * (piece_w / piece_h))
    for hole in holes[:4]:
        values.append(float(hole["x1"] - hole["x0"]))
    values = [v for v in values if 18 < v < 160]
    return sorted({round(v) for v in values})


def locate_dark_slot(bg: np.ndarray, expected_y: float | None = None) -> SlotMatch | None:
    """仅用暗色缺口兜底（拼图块下载失败时）。"""
    holes = [h for h in find_hole(bg) if 30 < (h["x1"] - h["x0"]) < 120]
    if not holes:
        return None
    if expected_y is not None:
        pool = [h for h in holes if abs(h["cy"] - expected_y) < max(50, bg.shape[0] * 0.25)]
        pool = pool or holes
    else:
        pool = holes
    h = min(pool, key=lambda z: z["mean"])
    return SlotMatch(cx=float(h["cx"]), cy=float(h["cy"]),
                     width=float(h["x1"] - h["x0"]),
                     height=float(h["y1"] - h["y0"]),
                     score=float(h["mean"]), source="dark")


def locate_slot(bg: np.ndarray, piece: np.ndarray, mask: np.ndarray,
                target_w: float = 0.0, target_h: float = 0.0,
                expected_y: float | None = None,
                exclude_cx: float | None = None,
                exclude_cy: float | None = None) -> SlotMatch | None:
    """在背景图中定位拼图块应落入的缺口。

    ``bg`` 为灰度背景（H×W），``piece`` 为灰度拼图块，``mask`` 为同尺寸
    布尔掩码（True 表示拼图块本身，不是透明背景）。坐标均为背景图像素。

    ``exclude_cx/cy`` 是拼图块当前悬浮位置：如果背景画布把拼图块也画了
    进去，模板会在起点自匹配；有远离起点的暗色缺口时，屏蔽起点附近。
    """
    if bg.ndim != 2 or piece.ndim != 2 or mask.shape != piece.shape:
        return None
    import numpy as np

    holes = find_hole(bg)
    ih, iw = bg.shape
    ph, pw = piece.shape
    if ph < 8 or pw < 8:
        return None
    image = bg.astype(np.float64)
    image_sq = image * image

    best: SlotMatch | None = None
    for tw in _width_candidates(target_w, target_h, pw, ph, holes):
        th = max(1, round(tw * ph / pw))
        if th > ih * 0.8 or tw > iw * 0.8:
            continue
        tmpl, tmpl_mask = _resize_piece(piece, mask, tw, th)
        ncc = _ncc_map(image, image_sq, tmpl, tmpl_mask)
        if ncc is None:
            continue
        if (exclude_cx is not None and exclude_cy is not None
                and any(math.hypot(h["cx"] - exclude_cx, h["cy"] - exclude_cy)
                        > max(30.0, tw * 0.6) for h in holes)):
            # 只有存在远离悬浮位置的暗色缺口时才屏蔽起点，避免误伤 dx≈0。
            xs = np.arange(ncc.shape[1]) + tw / 2
            ys = np.arange(ncc.shape[0]) + th / 2
            near = (((xs[None, :] - exclude_cx) ** 2
                     + (ys[:, None] - exclude_cy) ** 2)
                    < max(12.0, tw * 0.5) ** 2)
            ncc = np.where(near, -1.0, ncc)
        # 纵向先验只做温和惩罚：拼图块可能在顶部，而缺口在任意高度。
        ys, xs = np.unravel_index(np.argmax(ncc), ncc.shape)
        score = float(ncc[ys, xs])
        cx = float(xs) + tw / 2
        cy = float(ys) + th / 2
        if expected_y is not None and ih > 0:
            off = abs(cy - expected_y) / ih
            score -= min(0.15, off * 0.35)
        cand = SlotMatch(cx=cx, cy=cy, width=float(tw), height=float(th),
                         score=score, source="template")
        if best is None or cand.score > best.score:
            best = cand

    usable = [h for h in holes if 30 < (h["x1"] - h["x0"]) < 120]
    dark: SlotMatch | None = None
    if usable:
        if expected_y is not None:
            pool = [h for h in usable if abs(h["cy"] - expected_y) < max(50, ih * 0.25)]
            pool = pool or usable
        else:
            pool = usable
        h = min(pool, key=lambda z: z["mean"])
        dark = SlotMatch(cx=float(h["cx"]), cy=float(h["cy"]),
                         width=float(h["x1"] - h["x0"]),
                         height=float(h["y1"] - h["y0"]),
                         score=float(h["mean"]), source="dark")

    if best is not None and best.score >= 0.55:
        return best
    if best is not None and dark is not None:
        dist = math.hypot(best.cx - dark.cx, best.cy - dark.cy)
        if dist <= max(10.0, best.width * 0.25):
            return SlotMatch(cx=best.cx, cy=best.cy, width=best.width,
                             height=best.height, score=best.score,
                             source="template+dark")
    if dark is not None:
        return dark
    if best is not None and best.score >= 0.28:
        return best
    return None


# ---------------------------------------------------------------------------
# 拖拽轨迹
# ---------------------------------------------------------------------------
def build_drag_track(dx: float, *, steps: int | None = None,
                     rng: random.Random | None = None) -> list[tuple[float, float]]:
    """生成从起点到 ``dx`` 的相对位移轨迹。

    轨迹带缓出、轻微横向抖动和纵向抖动；终点精确落在 ``dx``。
    返回 ``(x, y)`` 相对拖拽起点的偏移列表（不含起点）。
    """
    rng = rng or random.Random()
    if abs(dx) < 0.5:
        return [(dx, 0.0)]
    count = steps or rng.randint(34, 46)
    points: list[tuple[float, float]] = []
    for i in range(1, count + 1):
        t = i / count
        # 先加速后减速，接近真人拖拽
        ease = t * t * (3 - 2 * t)
        wobble = math.sin(t * math.pi * 2.2) * (1 - t) * 0.5
        x = dx * ease + wobble
        y = rng.uniform(-0.9, 0.9) * (1 - t * 0.65)
        points.append((x, y))
    points[-1] = (dx, 0.0)
    return points
