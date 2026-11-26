"""vrp config 子命令测试：交互向导 / 非 TTY 诚实退出 / --show 掩码 /
--test 三态 / load_config 优先级（env > 明文）/ doctor 挂接面。

全 mock 不打真 API（chat 经 llm_client.chat 模块属性注入，学
test_auto_curate 的 fake 注入模式）。红线：key 任何形态不出现在
stdout（D5 延续）；交互面英文人话、错误码/键名英文。
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

from vrp import config_cmd, llm_client

API_KEY = "sk-test-secret-456"


def _mk_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


def _write_config(repo: Path, llm: dict) -> None:
    (repo / ".vrp").mkdir(exist_ok=True)
    (repo / ".vrp" / "config.json").write_text(
        json.dumps({"llm": llm}, ensure_ascii=False), encoding="utf-8", newline="\n"
    )


def _args(repo: Path, **flags) -> argparse.Namespace:
    base = {"repo": str(repo), "show": False, "test": False, "store_key": False}
    base.update(flags)
    return argparse.Namespace(**base)


class _FakeTty(io.StringIO):
    def isatty(self) -> bool:
        return True


def _fake_interactive(monkeypatch, base_url: str, model: str, key: str) -> None:
    """TTY 伪装 + input/getpass 注入（不打真终端）。"""
    monkeypatch.setattr(sys, "stdin", _FakeTty())
    answers = iter([base_url, model])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(config_cmd, "getpass", lambda prompt="": key)


# ---------- 交互式写入 ----------

def test_interactive_writes_config_env_mode_key_order(tmp_path, monkeypatch, capsys):
    repo = _mk_repo(tmp_path)
    _fake_interactive(monkeypatch, "https://llm.example/v1", "my-model", API_KEY)
    rc = config_cmd.cmd_config(_args(repo))
    assert rc == 0
    raw = json.loads((repo / ".vrp" / "config.json").read_text(encoding="utf-8"))
    assert list(raw["llm"]) == ["base_url", "model", "api_key_env"]
    assert raw["llm"] == {
        "base_url": "https://llm.example/v1",
        "model": "my-model",
        "api_key_env": "VRP_LLM_API_KEY",
    }
    out = capsys.readouterr().out
    assert "setx VRP_LLM_API_KEY" in out  # Windows 环境变量指引
    assert "export VRP_LLM_API_KEY" in out  # POSIX 指引
    assert "--test" in out  # 验证提示
    assert API_KEY not in out  # 红线：key 不进 stdout


def test_interactive_defaults_on_empty_input(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    _fake_interactive(monkeypatch, "", "", API_KEY)
    rc = config_cmd.cmd_config(_args(repo))
    assert rc == 0
    raw = json.loads((repo / ".vrp" / "config.json").read_text(encoding="utf-8"))
    assert raw["llm"]["base_url"] == "https://api.openai.com/v1"
    assert raw["llm"]["model"] == "gpt-4o-mini"
    assert raw["llm"]["api_key_env"] == "VRP_LLM_API_KEY"


def test_interactive_store_key_writes_inline_with_warning(tmp_path, monkeypatch, capsys):
    repo = _mk_repo(tmp_path)
    _fake_interactive(monkeypatch, "https://llm.example/v1", "my-model", API_KEY)
    rc = config_cmd.cmd_config(_args(repo, store_key=True))
    assert rc == 0
    raw = json.loads((repo / ".vrp" / "config.json").read_text(encoding="utf-8"))
    assert list(raw["llm"]) == ["base_url", "model", "api_key"]
    assert raw["llm"]["api_key"] == API_KEY  # 明文落盘是 --store-key 的本意
    out = capsys.readouterr().out
    assert "Security warning" in out
    assert API_KEY not in out  # 明文可以落盘，但 stdout 回显恒掩码


def test_interactive_preserves_extra_fields_and_switches_env_var(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    _write_config(
        repo,
        {"base_url": "http://old/v1", "model": "old", "api_key_env": "OLD_VAR", "timeout": 60},
    )
    _fake_interactive(monkeypatch, "https://new.example/v1", "new-model", API_KEY)
    rc = config_cmd.cmd_config(_args(repo))
    assert rc == 0
    raw = json.loads((repo / ".vrp" / "config.json").read_text(encoding="utf-8"))
    assert raw["llm"]["timeout"] == 60  # 既有扩展字段保留
    assert raw["llm"]["base_url"] == "https://new.example/v1"
    assert raw["llm"]["api_key_env"] == "VRP_LLM_API_KEY"  # 向导统一写标准变量名


def test_interactive_env_mode_strips_stale_inline_key(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    _write_config(repo, {"base_url": "http://old/v1", "model": "old", "api_key": "sk-old-inline"})
    _fake_interactive(monkeypatch, "https://new.example/v1", "new-model", API_KEY)
    rc = config_cmd.cmd_config(_args(repo))
    assert rc == 0
    text = (repo / ".vrp" / "config.json").read_text(encoding="utf-8")
    raw = json.loads(text)
    assert "api_key" not in raw["llm"]  # 换 env 模式剥除遗留明文源
    assert "sk-old-inline" not in text


def test_interactive_rejects_non_http_base_url(tmp_path, monkeypatch):
    repo = _mk_repo(tmp_path)
    _fake_interactive(monkeypatch, "ftp://bad", "m", API_KEY)
    with pytest.raises(SystemExit, match="http"):
        config_cmd.cmd_config(_args(repo))
    assert not (repo / ".vrp" / "config.json").exists()  # 不写半截配置


def test_interactive_non_tty_exits_honestly(tmp_path, monkeypatch, capsys):
    repo = _mk_repo(tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO())  # 非 TTY：isatty() -> False
    with pytest.raises(SystemExit, match="interactive terminal"):
        config_cmd.cmd_config(_args(repo))
    err = capsys.readouterr().err
    assert "TTY" in err  # 人话说明挂起原因与出路
    assert "vrp config" in err
    assert not (repo / ".vrp" / "config.json").exists()


# ---------- --show 掩码 ----------

def test_show_masks_inline_key(tmp_path, capsys):
    repo = _mk_repo(tmp_path)
    _write_config(repo, {"base_url": "http://llm.example/v1", "model": "m", "api_key": API_KEY})
    rc = config_cmd.cmd_config(_args(repo, show=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "***" in out
    assert API_KEY not in out  # 明文存储也掩码显示
    assert "http://llm.example/v1" in out
    assert "plaintext" in out  # 明文存储警告


def test_show_env_status_set_and_unset(tmp_path, capsys, monkeypatch):
    repo = _mk_repo(tmp_path)
    _write_config(
        repo, {"base_url": "http://x/v1", "model": "m", "api_key_env": "VRP_LLM_API_KEY"}
    )
    monkeypatch.setenv("VRP_LLM_API_KEY", API_KEY)
    rc = config_cmd.cmd_config(_args(repo, show=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "(env set)" in out
    assert API_KEY not in out  # env 值也不进输出——只显状态
    monkeypatch.delenv("VRP_LLM_API_KEY")
    rc = config_cmd.cmd_config(_args(repo, show=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "(env unset)" in out
    assert API_KEY not in out


def test_show_missing_config_hints_vrp_config(tmp_path, capsys):
    repo = _mk_repo(tmp_path)
    rc = config_cmd.cmd_config(_args(repo, show=True))
    assert rc == 1
    assert "vrp config" in capsys.readouterr().out


def test_show_bad_json_hints_rebuild(tmp_path, capsys):
    repo = _mk_repo(tmp_path)
    (repo / ".vrp").mkdir()
    (repo / ".vrp" / "config.json").write_text("{nope", encoding="utf-8", newline="\n")
    rc = config_cmd.cmd_config(_args(repo, show=True))
    assert rc == 1
    assert "vrp config" in capsys.readouterr().out


# ---------- --test 三态 ----------

def _ok_response() -> dict:
    return {
        "content": "pong",
        "finish_reason": "stop",
        "usage": {"prompt_tokens": 8, "completion_tokens": 1, "total_tokens": 9},
    }


def test_test_ok(tmp_path, monkeypatch, capsys):
    repo = _mk_repo(tmp_path)
    _write_config(repo, {"base_url": "http://llm.example/v1", "model": "gpt-test", "api_key": API_KEY})
    calls = []

    def fake_chat(config, messages):
        calls.append({"config": config, "messages": messages})
        return _ok_response()

    monkeypatch.setattr(llm_client, "chat", fake_chat)
    rc = config_cmd.cmd_config(_args(repo, test=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "gpt-test" in out
    assert "http://llm.example/v1" in out
    assert "***" in out  # key 掩码 + 来源
    assert "took" in out
    assert "total_tokens" in out  # usage
    assert API_KEY not in out
    # 最小 ping 形态：单条 ping 消息 + max_tokens=1
    assert calls[0]["messages"] == [{"role": "user", "content": "ping"}]
    assert calls[0]["config"].max_output_tokens == 1
    assert calls[0]["config"].api_key == API_KEY  # 调用面拿到真 key


def test_test_http_401_auth_error(tmp_path, monkeypatch, capsys):
    repo = _mk_repo(tmp_path)
    _write_config(repo, {"base_url": "http://llm.example/v1", "model": "m", "api_key": API_KEY})

    def fake_chat(config, messages):
        raise ValueError("llm request rejected: HTTP 401: invalid api key")

    monkeypatch.setattr(llm_client, "chat", fake_chat)
    rc = config_cmd.cmd_config(_args(repo, test=True))
    assert rc == 1
    out = capsys.readouterr().out
    assert "401" in out
    assert "key" in out  # 人话归因：key 错
    assert API_KEY not in out


def test_test_http_404_base_url_error(tmp_path, monkeypatch, capsys):
    """原始 HTTPError 形态（未经 chat 包装）也须正确分类。"""
    repo = _mk_repo(tmp_path)
    _write_config(repo, {"base_url": "http://llm.example/v1", "model": "m", "api_key": API_KEY})

    def fake_chat(config, messages):
        raise urllib.error.HTTPError(
            "http://llm.example/v1/chat/completions", 404, "Not Found", None, None
        )

    monkeypatch.setattr(llm_client, "chat", fake_chat)
    rc = config_cmd.cmd_config(_args(repo, test=True))
    assert rc == 1
    out = capsys.readouterr().out
    assert "404" in out
    assert "base_url" in out  # 人话归因：base_url 错


def test_test_timeout(tmp_path, monkeypatch, capsys):
    repo = _mk_repo(tmp_path)
    _write_config(repo, {"base_url": "http://llm.example/v1", "model": "m", "api_key": API_KEY})

    def fake_chat(config, messages):
        raise ValueError(
            "llm request failed: http://llm.example/v1/chat/completions: <urlopen error timed out>"
        )

    monkeypatch.setattr(llm_client, "chat", fake_chat)
    rc = config_cmd.cmd_config(_args(repo, test=True))
    assert rc == 1
    assert "timed out" in capsys.readouterr().out


def test_test_network_unreachable(tmp_path, monkeypatch, capsys):
    repo = _mk_repo(tmp_path)
    _write_config(repo, {"base_url": "http://llm.example/v1", "model": "m", "api_key": API_KEY})

    def fake_chat(config, messages):
        raise ValueError(
            "llm request failed: http://llm.example/v1/chat/completions:"
            " <urlopen error [Errno 11001] getaddrinfo failed>"
        )

    monkeypatch.setattr(llm_client, "chat", fake_chat)
    rc = config_cmd.cmd_config(_args(repo, test=True))
    assert rc == 1
    assert "network" in capsys.readouterr().out


def test_test_missing_config(tmp_path, capsys):
    repo = _mk_repo(tmp_path)
    rc = config_cmd.cmd_config(_args(repo, test=True))
    assert rc == 1
    assert "vrp config" in capsys.readouterr().out


def test_test_unset_env_var(tmp_path, monkeypatch, capsys):
    repo = _mk_repo(tmp_path)
    _write_config(
        repo, {"base_url": "http://llm.example/v1", "model": "m", "api_key_env": "VRP_LLM_API_KEY"}
    )
    monkeypatch.delenv("VRP_LLM_API_KEY", raising=False)
    rc = config_cmd.cmd_config(_args(repo, test=True))
    assert rc == 1
    out = capsys.readouterr().out
    assert "environment variable" in out
    assert API_KEY not in out


# ---------- load_config 向后兼容 + 优先级（env > 明文） ----------

def test_load_config_env_beats_inline(tmp_path):
    repo = _mk_repo(tmp_path)
    _write_config(
        repo,
        {
            "base_url": "http://x/v1",
            "model": "m",
            "api_key_env": "VRP_TEST_KEY",
            "api_key": "sk-inline",
        },
    )
    config = llm_client.load_config(repo, env={"VRP_TEST_KEY": "sk-from-env"})
    assert config.api_key == "sk-from-env"
    assert config.api_key_source == "env:VRP_TEST_KEY"
    assert config.echo()["api_key"] == "***"  # 留痕恒掩码


def test_load_config_inline_fallback_when_env_unset(tmp_path):
    """双字段 + env 未设 → 回退明文（向后兼容：旧 inline 语义保留）。"""
    repo = _mk_repo(tmp_path)
    _write_config(
        repo,
        {
            "base_url": "http://x/v1",
            "model": "m",
            "api_key_env": "VRP_TEST_KEY",
            "api_key": "sk-inline",
        },
    )
    config = llm_client.load_config(repo, env={})
    assert config.api_key == "sk-inline"
    assert config.api_key_source == "inline"


# ---------- doctor 挂接面 ----------

def test_doctor_llm_section_unconfigured(tmp_path):
    repo = _mk_repo(tmp_path)
    assert config_cmd.doctor_llm_section(repo) == {"status": "unconfigured", "hint": "vrp config"}


def test_doctor_llm_section_ok(tmp_path):
    repo = _mk_repo(tmp_path)
    _write_config(repo, {"base_url": "http://llm.example/v1", "model": "m", "api_key": API_KEY})
    section = config_cmd.doctor_llm_section(repo, call=lambda config, messages: _ok_response())
    assert section["status"] == "ok"
    assert section["model"] == "m"
    assert section["base_url"] == "http://llm.example/v1"
    assert section["latency_ms"] >= 0
    assert section["usage"]["total_tokens"] == 9
    assert API_KEY not in json.dumps(section)  # 挂接面同样零 key


def test_doctor_llm_section_error(tmp_path):
    repo = _mk_repo(tmp_path)
    _write_config(repo, {"base_url": "http://llm.example/v1", "model": "m", "api_key": API_KEY})

    def fake_chat(config, messages):
        raise ValueError("llm request rejected: HTTP 401: invalid api key")

    section = config_cmd.doctor_llm_section(repo, call=fake_chat)
    assert section["status"] == "error"
    assert section["error"] == "auth"
    assert "401" in section["detail"]
