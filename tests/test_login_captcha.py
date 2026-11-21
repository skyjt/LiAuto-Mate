"""滑块求解纯函数回归：合成图、模板匹配、拖拽轨迹（不启动浏览器）。"""

from __future__ import annotations

import base64
import io
import json
import random
import time
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from liam.auth.session_store import SessionStore
from liam.core.db import Database
from liam.core.secrets import SecretBox
from liam.login.captcha import (
    _correlate2d,
    build_drag_track,
    decode_piece,
    find_hole,
    locate_dark_slot,
    locate_slot,
)
from liam.login.flow import LoginFlow


def synthetic(seed: int = 0, hole: tuple[int, int, int, int] = (210, 90, 52, 52)):
    """生成带纹理背景 + 压暗缺口 + 同内容拼图块。"""
    rng = np.random.default_rng(seed)
    height, width = 240, 360
    yy, xx = np.mgrid[0:height, 0:width]
    bg = 90 + 60 * (xx / width) + 30 * (yy / height) + rng.normal(0, 8, (height, width))
    bg = np.clip(bg, 0, 255)
    x0, y0, w, h = hole
    piece = bg[y0:y0 + h, x0:x0 + w].copy()
    mask = np.zeros((h, w), bool)
    mask[3:h - 3, 3:w - 3] = True
    mask[1:h - 1, 8:w - 8] = True
    piece = np.clip(piece + rng.normal(0, 2, piece.shape), 0, 255)
    dark = bg.copy()
    dark[y0:y0 + h, x0:x0 + w][mask] *= 0.22
    dark = np.clip(dark + rng.normal(0, 2, dark.shape), 0, 255)
    return dark, piece, mask, (x0 + w / 2, y0 + h / 2)


def test_correlate2d_matches_naive():
    image = np.arange(24, dtype=float).reshape(4, 6)
    kernel = np.array([[1.0, 2.0], [3.0, 4.0]])
    expected = np.array([
        [sum(image[y + i, x + j] * kernel[i, j]
             for i in range(2) for j in range(2))
         for x in range(5)]
        for y in range(3)
    ])
    assert np.allclose(_correlate2d(image, kernel), expected)


def test_find_hole_finds_dark_slot():
    bg, _, _, center = synthetic()
    holes = find_hole(bg)
    assert holes
    assert min(abs(h["cx"] - center[0]) for h in holes) < 4
    assert min(abs(h["cy"] - center[1]) for h in holes) < 4


@pytest.mark.parametrize("target_w", [0, 46, 52, 58])
def test_locate_slot_handles_scale_estimates(target_w):
    bg, piece, mask, center = synthetic()
    slot = locate_slot(bg, piece, mask, target_w=target_w,
                       target_h=target_w, expected_y=center[1])
    assert slot is not None
    assert abs(slot.cx - center[0]) < 4
    assert abs(slot.cy - center[1]) < 4


def test_locate_dark_slot_without_piece():
    bg, _, _, center = synthetic()
    slot = locate_dark_slot(bg, expected_y=center[1])
    assert slot is not None
    assert abs(slot.cx - center[0]) < 4
    assert slot.source == "dark"


def test_locate_slot_ignores_piece_drawn_at_its_start():
    """若背景画布把悬浮拼图也画进去，不能把起点自匹配当成缺口。"""
    bg, piece, mask, center = synthetic()
    y0 = int(center[1] - piece.shape[0] / 2)
    x0 = 8
    bg[y0:y0 + piece.shape[0], x0:x0 + piece.shape[1]][mask] = piece[mask]
    slot = locate_slot(bg, piece, mask, target_w=52, target_h=52,
                       expected_y=center[1],
                       exclude_cx=x0 + piece.shape[1] / 2,
                       exclude_cy=center[1])
    assert slot is not None
    assert abs(slot.cx - center[0]) < 4


def test_locate_slot_rejects_flat_image():
    bg = np.full((120, 200), 180.0)
    piece = np.full((30, 30), 180.0)
    mask = np.ones((30, 30), bool)
    assert locate_slot(bg, piece, mask, target_w=30, target_h=30) is None


def test_decode_piece_crops_transparent_padding():
    rgba = np.zeros((80, 100, 4), np.uint8)
    rgba[20:60, 10:50, 0] = 200
    rgba[20:60, 10:50, 3] = 255
    buffer = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buffer, "PNG")
    decoded = decode_piece(buffer.getvalue())
    assert decoded is not None
    gray, mask, frac = decoded
    assert gray.shape == mask.shape == (40, 40)
    assert frac == pytest.approx((0.1, 0.25, 0.4, 0.5))
    assert mask.all()


def test_build_drag_track_ends_at_target():
    points = build_drag_track(123.4, rng=random.Random(1))
    assert 30 <= len(points) <= 50
    assert points[-1] == (123.4, 0.0)
    assert all(abs(y) <= 1.0 for _, y in points)
    assert max(abs(p[0]) for p in points) < 130


def test_build_drag_track_zero_distance():
    assert build_drag_track(0.0) == [(0.0, 0.0)]


def _png_data_url(array: np.ndarray, mode: str) -> str:
    buffer = io.BytesIO()
    Image.fromarray(array, mode).save(buffer, "PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


class _FakeFrame:
    def __init__(self, get_imgs: dict, state: dict) -> None:
        self.get_imgs = get_imgs
        self.state = state

    def evaluate(self, script: str):
        if "pieceData" in script:
            return json.dumps(self.get_imgs)
        if "codeInput" in script:
            return json.dumps(self.state)
        if "document.querySelector" in script:
            return True
        return None

    def is_detached(self) -> bool:
        return False


class _FakePage:
    def __init__(self, frame: _FakeFrame) -> None:
        self.main_frame = frame
        self.frames = [frame]
        self.url = "https://account.lixiang.com/login/app/livis/identifier"


class _FakeCDP:
    def __init__(self, state: dict) -> None:
        self.state = state
        self.events: list[tuple[str, dict]] = []

    def send(self, method: str, params: dict) -> None:
        self.events.append((method, params))
        if method == "Input.dispatchTouchEvent" and params.get("type") == "touchEnd":
            self.state["ok"] = True
        if method == "Input.dispatchMouseEvent" and params.get("type") == "mouseReleased":
            self.state["ok"] = True


def test_solve_once_dispatches_drag_to_detected_slot(tmp_path, monkeypatch):
    bg, piece, mask, _ = synthetic()
    rgba = np.zeros((*piece.shape, 4), np.uint8)
    rgba[..., 0] = rgba[..., 1] = rgba[..., 2] = piece.astype(np.uint8)
    rgba[..., 3] = np.where(mask, 255, 0)
    state = {"widget": True, "ok": False, "fail": False, "detecting": False,
             "countdown": False, "sent": True, "codeInput": False,
             "text": "", "url": ""}
    get_imgs = {
        "canvas": _png_data_url(bg.astype(np.uint8), "L"),
        "cvr": {"x": 20, "y": 50, "w": 360, "h": 240},
        "piece": None,
        "pieceData": _png_data_url(rgba, "RGBA"),
        "pieceNat": {"w": 52, "h": 52},
        # 拼图可见中心与缺口同高：50 + 116 - 26
        "pr": {"x": 20, "y": 140, "w": 52, "h": 52},
        "sub": {"x": 20, "y": 140, "w": 52, "h": 52},
        "slider": {"x": 20, "y": 300, "w": 40, "h": 30},
    }
    frame = _FakeFrame(get_imgs, state)
    page = _FakePage(frame)
    cdp = _FakeCDP(state)
    monkeypatch.setattr(
        "liam.login.flow.time",
        SimpleNamespace(sleep=lambda _s: None, time=time.time, strftime=time.strftime))

    db = Database(tmp_path / "login.db")
    flow = LoginFlow(SessionStore(db, SecretBox.load_or_create(tmp_path)), headless=True)
    try:
        ok, why = flow._solve_once(page, cdp)
    finally:
        db.close()
    assert ok is True and why == "solved"
    moves = [params for method, params in cdp.events
             if method == "Input.dispatchTouchEvent" and params["type"] == "touchMove"]
    assert moves
    # 滑块起点 20+20，缺口中心 236，拼图中心 46 → dx=210。
    assert abs(moves[-1]["touchPoints"][0]["x"] - 250) < 2
    assert any(method == "Input.dispatchTouchEvent" and params["type"] == "touchEnd"
               for method, params in cdp.events)
