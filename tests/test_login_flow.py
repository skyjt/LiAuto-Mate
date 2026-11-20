"""LoginFlow 纯逻辑单测（不起浏览器——状态守卫/限流/码校验）。"""

from __future__ import annotations

import time

import pytest

from liam.auth.session_store import SessionStore
from liam.core.db import Database
from liam.core.secrets import SecretBox
from liam.login.flow import (
    S_DONE,
    S_FAILED,
    S_IDLE,
    S_RUNNING,
    S_WAITING_CODE,
    LoginFlow,
)


@pytest.fixture()
def flow(tmp_path):
    db = Database(tmp_path / "t.db")
    store = SessionStore(db, SecretBox.load_or_create(tmp_path))
    return LoginFlow(store, headless=True)


def test_status_initial(flow):
    st = flow.status()
    assert st["state"] == S_IDLE
    assert st["screenshot"] == ""


def test_submit_code_rejected_when_idle(flow):
    ok, msg = flow.submit_code("123456")
    assert ok is False
    assert "idle" in msg


def test_submit_code_format_check(flow):
    # 伪造 waiting_code 态验码格式
    flow._set(S_WAITING_CODE)
    ok, msg = flow.submit_code("abc")
    assert ok is False
    ok, msg = flow.submit_code("12345")
    assert ok is True


def test_state_transitions_recorded(flow):
    flow._logp("hello")
    flow._set(S_RUNNING)
    flow._set(S_FAILED, "boom")
    st = flow.status()
    assert st["state"] == S_FAILED
    assert st["error"] == "boom"
    assert st["log"][-1].endswith("hello")


def test_find_hole_synthetic():
    import numpy as np
    from liam.login.flow import find_hole
    bg = np.full((100, 300), 200.0)
    bg[40:80, 150:190] = 30.0
    comps = find_hole(bg)
    assert len(comps) == 1
    assert abs(comps[0]["cx"] - 170) < 5
    assert abs(comps[0]["cy"] - 60) < 5
