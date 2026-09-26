"""每个接口测试使用隔离的缓存和真实检索，环境变量在结束后恢复。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("VAR_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("INDEX_PATH", str(tmp_path / "index.json"))
    for key in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(key, raising=False)

    from fastapi.testclient import TestClient

    from kbqa import server

    monkeypatch.setattr(server, "_service", None)
    with TestClient(server.app) as api_client:
        yield api_client
