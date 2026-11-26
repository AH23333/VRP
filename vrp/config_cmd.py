"""vrp config 子命令：LLM 配置向导 / 掩码回显 / 连通性测试。

- 交互式三问（base_url/model/api_key）写 <repo>/.vrp/config.json；
  非 TTY（stdin 管道/重定向）诚实 SystemExit 不挂起
- 默认环境变量模式：key 不落盘，config 只写 api_key_env 名；
  --store-key：key 明文写入 llm.api_key（打印安全警告）
- --show：回显当前配置，key 恒掩码（env 显已设置/未设置，明文显 ***）
- --test：load_config + 最小 ping（max_tokens=1），错误分类人话
  （401=key / 404=base_url / 超时 / 网络不通 / 未配置），rc=0 ok / 1 失败
- doctor_llm_section(repo)：doctor 挂接面（未配置/已配置+ping 结果）
- 红线：key 任何形态不回显 stdout——向导指引/回显/测试输出一律掩码
  （D5 延续；raise 消息英文=库约定，print 引导面英文）
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.error
from getpass import getpass
from pathlib import Path

from . import llm_client

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
API_KEY_ENV = "VRP_LLM_API_KEY"
PING_MESSAGES = [{"role": "user", "content": "ping"}]


def _config_path(repo: Path) -> Path:
    return Path(repo) / ".vrp" / "config.json"


def _resolve_repo(value) -> Path:
    repo = Path(value).resolve()
    if not repo.is_dir():
        raise SystemExit(f"not a directory: {repo}")
    return repo


def _classify_error(exc: Exception) -> tuple[str, str]:
    """LLM 错误分类：(英文码, 英文人话)。

    双形态都吃：chat() 包装后的 ValueError（"HTTP 401"/"timed out"/
    "llm request failed" 文本）与原始 urllib/socket 异常——--test 与
    doctor 挂接面同口径。
    """
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (401, 403):
            return ("auth", f"authentication failed (HTTP {exc.code}) - the API key is invalid, expired, or lacks permission; check the key")
        if exc.code == 404:
            return ("base_url", "endpoint not found (HTTP 404) - base_url is probably wrong; OpenAI-compatible services usually end with /v1")
        return ("http", f"server returned HTTP {exc.code} - check the service status or retry later")
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return ("timeout", "request timed out - the network is slow or the server is unresponsive; retry later")
    if isinstance(exc, urllib.error.URLError):
        reason = str(exc.reason)
        if "timed out" in reason:
            return ("timeout", "request timed out - the network is slow or the server is unresponsive; retry later")
        return ("network", f"network unreachable - check network/proxy/firewall: {reason}")
    if isinstance(exc, OSError):
        return ("network", f"network error - check network/proxy/firewall: {exc}")
    text = str(exc)
    if "config missing" in text or "missing 'llm'" in text:
        return ("unconfigured", "not configured yet - run 'vrp config <repo>' first")
    if "config unreadable" in text:
        return ("unconfigured", f"config file unreadable - run 'vrp config <repo>' to rebuild: {text}")
    if "unset environment variable" in text:
        return ("unset_env", f"the configured environment variable is not set - set the key environment variable first: {text}")
    if "needs an API key" in text:
        return ("unconfigured", "config lacks an API key - run 'vrp config <repo>' first")
    for code in (401, 403):
        if f"HTTP {code}" in text:
            return ("auth", f"authentication failed (HTTP {code}) - the API key is invalid, expired, or lacks permission; check the key")
    if "HTTP 404" in text:
        return ("base_url", "endpoint not found (HTTP 404) - base_url is probably wrong; OpenAI-compatible services usually end with /v1")
    if "timed out" in text or "timeout" in text.lower():
        return ("timeout", "request timed out - the network is slow or the server is unresponsive; retry later")
    if "llm request failed" in text:
        return ("network", f"network unreachable - check network/proxy/firewall: {text[:200]}")
    return ("unknown", text[:300])


def _ping_config(config: llm_client.LlmConfig) -> llm_client.LlmConfig:
    """最小 ping 配置：max_tokens=1（连通性探测不烧 token）。"""
    return llm_client.LlmConfig(
        config.base_url,
        config.model,
        config.api_key,
        config.timeout,
        1,
        config.api_key_source,
        config.thinking,
    )


def cmd_config(args) -> int:
    repo = _resolve_repo(args.repo)
    if getattr(args, "show", False):
        return _show(repo)
    if getattr(args, "test", False):
        return _test(repo)
    return _interactive(repo, store_key=bool(getattr(args, "store_key", False)))


def _interactive(repo: Path, store_key: bool) -> int:
    if not sys.stdin.isatty():
        print(
            "vrp config needs an interactive terminal, but stdin is not a TTY"
            " (prompting over a pipe/redirect would hang - aborted).\n"
            f"Run it in a real terminal: vrp config \"{repo}\"\n"
            f"Or create {_config_path(repo)} by hand (keys: see the shape the vrp config wizard writes),\n"
            "or finish the interactive setup first and then use --show / --test non-interactively.",
            file=sys.stderr,
        )
        raise SystemExit("vrp config requires an interactive terminal (stdin is not a TTY)")
    print("vrp config - LLM configuration wizard (OpenAI-compatible POST {base_url}/chat/completions)")
    base_url = input(f"base_url [enter = default {DEFAULT_BASE_URL}]: ").strip() or DEFAULT_BASE_URL
    if not base_url.startswith(("http://", "https://")):
        raise SystemExit(f"base_url must be an http(s) URL, got: {base_url}")
    model = input(f"model [enter = default {DEFAULT_MODEL}]: ").strip() or DEFAULT_MODEL
    api_key = getpass("api_key (input not echoed): ").strip()
    if store_key and not api_key:
        raise SystemExit("--store-key needs a non-empty api_key; nothing was written")
    _write_config(repo, base_url, model, api_key if store_key else "")
    path = _config_path(repo)
    print(f"Wrote {path}")
    if store_key:
        print(
            "Security warning: the api_key is stored in plaintext inside config.json (--store-key) -\n"
            "any account/process/backup that can read this file can see it.\n"
            f"Safer: remove the llm.api_key field and use the {API_KEY_ENV} environment variable instead."
        )
    else:
        if not api_key:
            print(f"Hint: no key entered - config saved, but set the {API_KEY_ENV} environment variable first to pass --test.")
        print(
            f"Next step: set your key as the {API_KEY_ENV} environment variable (the key is never written to any file):\n"
            f'  Windows (persists for future terminals): setx {API_KEY_ENV} "sk-your-key"\n'
            f"  POSIX (current terminal only):          export {API_KEY_ENV}=sk-your-key"
        )
    print(f'Verify connectivity: vrp config "{repo}" --test')
    return 0


def _write_config(repo: Path, base_url: str, model: str, api_key: str) -> None:
    """写 <repo>/.vrp/config.json。

    键序：base_url, model, api_key（--store-key）或 api_key_env（默认），
    既有其它字段（timeout/max_output_tokens/thinking、顶层键）保留。
    模式切换剥除另一源（换模式不留双源静默分歧）；既有文件坏 JSON 时
    向导重建（诚实覆写——不吞用户的可恢复字段：坏文件本就不可解析）。
    """
    path = _config_path(repo)
    existing: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            loaded = None
        if isinstance(loaded, dict):
            existing = loaded
    llm_old = existing.get("llm")
    kept = {}
    if isinstance(llm_old, dict):
        kept = {
            k: v
            for k, v in llm_old.items()
            if k not in ("base_url", "model", "api_key", "api_key_env")
        }
    llm: dict = {"base_url": base_url, "model": model}
    if api_key:
        llm["api_key"] = api_key
    else:
        llm["api_key_env"] = API_KEY_ENV
    llm.update(kept)
    payload = dict(existing)
    payload["llm"] = llm
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _show(repo: Path) -> int:
    path = _config_path(repo)
    if not path.exists():
        print(f"Not configured yet: {path} does not exist - run 'vrp config \"{repo}\"' first")
        return 1
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"Config file unreadable: {path}: {exc} - run 'vrp config \"{repo}\"' to rebuild")
        return 1
    if not isinstance(raw, dict):
        print(f"Config root is not a JSON object - run 'vrp config \"{repo}\"' to rebuild")
        return 1
    llm = raw.get("llm")
    if not isinstance(llm, dict):
        print(f"Config lacks a 'llm' object - run 'vrp config \"{repo}\"' to rebuild")
        return 1
    safe_llm = dict(llm)
    inline = safe_llm.get("api_key")
    has_inline = isinstance(inline, str) and bool(inline)
    if has_inline:
        safe_llm["api_key"] = "***"
    env_var = safe_llm.get("api_key_env")
    if isinstance(env_var, str) and env_var:
        state = "set" if os.environ.get(env_var) else "unset"
        safe_llm["api_key_env"] = f"{env_var} (env {state})"
    safe = dict(raw)
    safe["llm"] = safe_llm
    print(f"== vrp config --show ({path}) ==")
    print(json.dumps(safe, ensure_ascii=False, indent=2, default=str))
    if has_inline:
        print("Note: the api_key is stored in plaintext inside config.json - consider removing llm.api_key and using the environment variable instead.")
    return 0


def _test(repo: Path) -> int:
    path = _config_path(repo)
    if not path.exists():
        print(f"Not configured yet: {path} does not exist - run 'vrp config \"{repo}\"' first")
        return 1
    try:
        config = llm_client.load_config(repo)
    except ValueError as exc:
        code, detail = _classify_error(exc)
        print(f"[fail] config load failed ({code}): {detail}")
        return 1
    started = time.monotonic()
    try:
        response = llm_client.chat(_ping_config(config), PING_MESSAGES)
    except (ValueError, OSError) as exc:
        code, detail = _classify_error(exc)
        print(f"[fail] LLM ping failed ({code}): {detail}")
        return 1
    elapsed = time.monotonic() - started
    usage = response.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    print("[ok] LLM ping ok")
    print(f"  model:    {config.model}")
    print(f"  base_url: {config.base_url}")
    print(f"  api_key:  *** (source: {config.api_key_source})")
    print(f"  took:     {elapsed:.2f}s")
    print(f"  usage:    {json.dumps(usage, ensure_ascii=False)}")
    return 0


def doctor_llm_section(repo: Path, call=None) -> dict:
    """doctor 挂接面：未配置 → unconfigured + hint；已配置 → 最小 ping 结果。

    call(config, messages) 可注入（测试不打真 API）；默认 llm_client.chat。
    """
    repo = Path(repo)
    if not _config_path(repo).exists():
        return {"status": "unconfigured", "hint": "vrp config"}
    try:
        config = llm_client.load_config(repo)
    except ValueError as exc:
        code, detail = _classify_error(exc)
        return {"status": "invalid_config", "error": code, "detail": detail, "hint": "vrp config"}
    started = time.monotonic()
    try:
        response = (call or llm_client.chat)(_ping_config(config), PING_MESSAGES)
    except (ValueError, OSError) as exc:
        code, detail = _classify_error(exc)
        return {
            "status": "error",
            "error": code,
            "detail": detail,
            "model": config.model,
            "base_url": config.base_url,
        }
    usage = response.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    return {
        "status": "ok",
        "model": config.model,
        "base_url": config.base_url,
        "latency_ms": round((time.monotonic() - started) * 1000),
        "usage": usage,
    }
