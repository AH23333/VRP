"""内置 LLM 梳理循环——配置与 OpenAI-compatible 客户端（构想v7 §13 G 批次，G1）。

- 单协议：POST {base_url}/chat/completions（OpenAI-compatible，D2）
- stdlib urllib 实现，零第三方依赖（D3）
- 传输层重试：429/5xx 退避 ≤2，不消耗验证重试额度（§13.4）
- key 任何形态不落盘：留痕一律走 LlmConfig.echo() 掩码（D5）
- 响应剥离容错（D8）：围栏剥离 + 首 {/[ 到末 }/] 提取，失败抛 ValueError
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_TIMEOUT = 180
DEFAULT_MAX_OUTPUT_TOKENS = 16384
TRANSPORT_RETRIES = 2
_RETRY_BACKOFF_S = 2.0

_MIN_EXAMPLE = (
    "{\n"
    '  "llm": {\n'
    '    "base_url": "https://api.openai.com/v1",\n'
    '    "api_key_env": "VRP_LLM_API_KEY",\n'
    '    "model": "gpt-4.1"\n'
    "  }\n"
    "}"
)


class LlmConfig:
    """解析后的 LLM 配置；api_key 只存在于内存，留痕用 echo()。"""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        timeout: int,
        max_output_tokens: int,
        api_key_source: str,
    ) -> None:
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.max_output_tokens = max_output_tokens
        self.api_key_source = api_key_source

    def echo(self) -> dict:
        """脱敏回显（留痕用）：key 永远只出现掩码。"""
        return {
            "base_url": self.base_url,
            "model": self.model,
            "timeout": self.timeout,
            "max_output_tokens": self.max_output_tokens,
            "api_key": "***",
            "api_key_source": self.api_key_source,
        }


def load_config(repo: Path, env: dict | None = None) -> LlmConfig:
    """读 <repo>/.vrp/config.json 并校验；缺键诚实报错并给最小示例（§13.3）。"""
    path = Path(repo) / ".vrp" / "config.json"
    if not path.exists():
        raise ValueError(f"config missing: {path} - create it with:\n{_MIN_EXAMPLE}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"config unreadable: {path}: {exc}") from exc
    llm = raw.get("llm") if isinstance(raw, dict) else None
    if not isinstance(llm, dict):
        raise ValueError(f"config missing 'llm' object: {path} - minimal example:\n{_MIN_EXAMPLE}")
    base_url = llm.get("base_url")
    if not isinstance(base_url, str) or not base_url.startswith(("http://", "https://")):
        raise ValueError("config llm.base_url must be an http(s) URL")
    model = llm.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("config llm.model must be a non-empty string")
    timeout = llm.get("timeout", DEFAULT_TIMEOUT)
    max_output_tokens = llm.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS)
    for name, value in (("timeout", timeout), ("max_output_tokens", max_output_tokens)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"config llm.{name} must be a positive integer")
    env_map = os.environ if env is None else env
    api_key = ""
    source = ""
    if isinstance(llm.get("api_key"), str) and llm["api_key"]:
        api_key = llm["api_key"]
        source = "inline"
    elif isinstance(llm.get("api_key_env"), str) and llm["api_key_env"]:
        var = llm["api_key_env"]
        api_key = env_map.get(var, "")
        if not api_key:
            raise ValueError(
                f"config llm.api_key_env references unset environment variable: {var}"
            )
        source = f"env:{var}"
    else:
        raise ValueError(
            "config llm needs an API key: set 'api_key_env' (recommended) or 'api_key'"
        )
    return LlmConfig(base_url.rstrip("/"), model, api_key, timeout, max_output_tokens, source)


def _post_json(url: str, headers: dict, body: dict, timeout: int) -> tuple[int, str]:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def _parse_response(text: str) -> dict:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"llm response is not valid JSON: {str(exc)[:200]}") from exc
    try:
        choice = data["choices"][0]
        content = choice["message"]["content"]
        finish_reason = choice.get("finish_reason")
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"llm response missing choices[0].message.content: {exc}") from exc
    if not isinstance(content, str):
        raise ValueError("llm response content is not a string")
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return {"content": content, "finish_reason": finish_reason, "usage": usage}


def chat(
    config: LlmConfig,
    messages: list,
    transport=None,
    sleep=None,
) -> dict:
    """单发调用；返回 {"content", "finish_reason", "usage"}。

    transport(url, headers, body, timeout) -> (status, text) 可注入（测试）。
    429/5xx 退避重试 ≤TRANSPORT_RETRIES；其余 4xx/网络错误诚实抛 ValueError。
    """
    post = transport or _post_json
    pause = time.sleep if sleep is None else sleep
    url = f"{config.base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": config.model,
        "messages": messages,
        "max_tokens": config.max_output_tokens,
    }
    last = ""
    for attempt in range(TRANSPORT_RETRIES + 1):
        try:
            status, text = post(url, headers, body, config.timeout)
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                text = exc.read().decode("utf-8", "replace")
            except Exception:
                text = ""
        except (urllib.error.URLError, OSError) as exc:
            raise ValueError(f"llm request failed: {url}: {exc}") from exc
        if status == 200:
            return _parse_response(text)
        last = f"HTTP {status}: {text[:500]}"
        if (status == 429 or status >= 500) and attempt < TRANSPORT_RETRIES:
            pause(_RETRY_BACKOFF_S * (attempt + 1))
            continue
        raise ValueError(f"llm request rejected: {last}")
    raise ValueError(f"llm request failed after {TRANSPORT_RETRIES + 1} attempts: {last}")


def extract_json(content: str):
    """响应剥离（D8）：markdown 围栏剥离 + 首 {/[ 到末 }/] 提取。

    失败抛 ValueError——调用方计一次尝试（等同拒收，消耗验证重试额度）。
    """
    text = content.strip()
    if "```" in text:
        blocks = re.findall(r"```[a-zA-Z0-9_-]*\n(.*?)```", text, re.DOTALL)
        if blocks:
            text = max(blocks, key=len).strip()
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        raise ValueError(f"no JSON object/array found in response: {content[:200]!r}")
    ends = [i for i in (text.rfind("}"), text.rfind("]")) if i >= 0]
    if not ends:
        raise ValueError(f"no JSON object/array end found in response: {content[:200]!r}")
    candidate = text[min(starts) : max(ends) + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"response JSON unparseable: {exc}; excerpt: {candidate[:200]!r}"
        ) from exc
