from __future__ import annotations

import json
import os

import httpx
import pytest

from dealradar.config import load_env
from dealradar.llm_client import ChatClient, LLMBadJSON, LLMError, LLMFatal

CFG = {"base_url": "https://api.deepseek.com", "model": "deepseek-flash", "temperature": 0,
       "max_tokens": 4000, "timeout_seconds": 60, "extra_body": {"thinking": {"type": "disabled"}}}


def ok(content, finish="stop"):
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content},
                                                  "finish_reason": finish}],
                                     "usage": {"prompt_tokens": 100, "completion_tokens": 20}})


def make(responses, **kw):
    """responses 依次返回；元素可以是 Response 或要抛出的异常。"""
    seen, waits = [], []
    queue = list(responses)

    def handler(req):
        seen.append(req)
        r = queue.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    c = ChatClient(CFG, "dummy-test-key", transport=httpx.MockTransport(handler), sleep=waits.append, **kw)
    return c, seen, waits


def test_request_shape(cfg):
    c, seen, _ = make([ok('{"events": []}')])
    assert c.chat_json([{"role": "user", "content": "hi json"}]) == {"events": []}
    req = seen[0]
    assert str(req.url) == "https://api.deepseek.com/chat/completions"
    assert req.headers["Authorization"] == "Bearer dummy-test-key"
    body = json.loads(req.content)
    assert body["model"] == "deepseek-flash" and body["temperature"] == 0 and body["max_tokens"] == 4000
    assert body["response_format"] == {"type": "json_object"}
    assert body["thinking"] == {"type": "disabled"}  # extra_body 原样并入
    assert c.http.timeout.read == 60
    assert c.usage == {"prompt_tokens": 100, "completion_tokens": 20}


def test_llm_yaml(cfg):
    assert cfg.llm["provider"] == "deepseek"
    assert cfg.llm["base_url"] == "https://api.deepseek.com"
    assert cfg.llm["model"] == "deepseek-flash"
    assert cfg.llm["max_tokens"] >= 4000
    assert cfg.llm["extra_body"] == {"thinking": {"type": "disabled"}}
    assert cfg.llm["api_key_env"] == "DEEPSEEK_API_KEY"
    assert cfg.llm["batch_size"] == 15 and cfg.llm["temperature"] == 0


def test_bad_json_retried_once():
    c, seen, _ = make([ok("not json {"), ok('{"events": [1]}')])
    assert c.chat_json([]) == {"events": [1]}
    assert len(seen) == 2


def test_bad_json_twice_raises():
    c, seen, _ = make([ok("nope"), ok('["a list, not an object"]')])
    with pytest.raises(LLMBadJSON):
        c.chat_json([])
    assert len(seen) == 2


def test_truncated_output_is_retried():
    # 被 max_tokens 截断：即使碰巧是合法 JSON 也不信，重试一次
    c, seen, _ = make([ok('{"events": []}', finish="length"), ok('{"events": [2]}')])
    assert c.chat_json([]) == {"events": [2]}
    assert len(seen) == 2


def test_truncated_twice_fails():
    c, seen, _ = make([ok('{"events": [', finish="length"), ok('{"events": [', finish="length")])
    with pytest.raises(LLMBadJSON):
        c.chat_json([])
    assert len(seen) == 2


def test_empty_content_with_only_reasoning_is_bad_json():
    r = httpx.Response(200, json={"choices": [{"message": {"content": "", "reasoning_content": "thinking..."},
                                               "finish_reason": "stop"}]})
    c, seen, _ = make([r, ok("{}")])
    assert c.chat_json([]) == {} and len(seen) == 2


def test_missing_content_counts_as_bad_json():
    c, _, _ = make([httpx.Response(200, json={"choices": []}), ok("{}")])
    assert c.chat_json([]) == {}


@pytest.mark.parametrize("status", [429, 500, 503])
def test_retry_with_backoff_then_success(status):
    c, seen, waits = make([httpx.Response(status), httpx.Response(status), ok("{}")])
    assert c.chat_json([]) == {}
    assert len(seen) == 3 and waits == [2.0, 4.0]


def test_gives_up_after_two_retries():
    c, seen, waits = make([httpx.Response(503)] * 3)
    with pytest.raises(LLMError) as e:
        c.chat_json([])
    assert not isinstance(e.value, LLMFatal)
    assert len(seen) == 3 and len(waits) == 2


def test_retry_after_header_respected():
    c, _, waits = make([httpx.Response(429, headers={"Retry-After": "9"}), ok("{}")])
    c.chat_json([])
    assert waits == [9.0]


def test_network_error_retried():
    c, seen, waits = make([httpx.ReadTimeout("slow"), ok("{}")])
    assert c.chat_json([]) == {}
    assert len(seen) == 2 and waits == [2.0]


@pytest.mark.parametrize("status", [401, 402, 403])
def test_auth_and_balance_errors_are_fatal(status):
    c, seen, waits = make([httpx.Response(status, json={"error": {"message": "Insufficient Balance"}})])
    with pytest.raises(LLMFatal) as e:
        c.chat_json([])
    assert "Insufficient Balance" in str(e.value) and len(seen) == 1 and waits == []


def test_other_4xx_not_retried():
    c, seen, _ = make([httpx.Response(400, json={"error": {"message": "bad"}})])
    with pytest.raises(LLMError):
        c.chat_json([])
    assert len(seen) == 1


def test_load_env_reads_deepseek_key(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OTHER_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("# 注释\nANTHROPIC_API_KEY=\n\nDEEPSEEK_API_KEY=dummy-abc123\nexport OTHER_KEY=\"q\"\n", encoding="utf-8")
    load_env(env)
    assert os.environ["DEEPSEEK_API_KEY"] == "dummy-abc123"
    assert os.environ["OTHER_KEY"] == "q"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-env")
    load_env(env)
    assert os.environ["DEEPSEEK_API_KEY"] == "from-env"  # 已有的环境变量不覆盖
