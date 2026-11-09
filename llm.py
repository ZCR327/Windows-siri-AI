# -*- coding: utf-8 -*-
"""LLM 后端：MiniMax(anthropic 协议) + DeepSeek(OpenAI) + 智谱GLM(OpenAI)。

key 从哪来（优先级从高到低）：
  1. 调用方显式传入
  2. 统一密钥库 config/api_keys.json   <- 日常就是这个
  3. 本机 MiniMax 网关 config.yaml（provider.minimax）
  4. 环境变量 MINIMAX_API_KEY / DEEPSEEK_API_KEY / ZHIPU_API_KEY

失败处理：鉴权类/余额类错误（401/402/403）会把该后端**本次进程内**拉黑，
不再每句话都先去撞一次墙 —— 语音助手每句话都要快，不能白白多等一次请求。
"""
import os
import re
import json

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

try:
    import keys as keylib
except Exception:  # pragma: no cover
    keylib = None

try:
    import providers as prov
except Exception:  # pragma: no cover
    prov = None

# 本机 MiniMax 网关配置（可选）：本机已经登录过时可以免填 key。
# 依次尝试：环境变量 MINIMAX_GATEWAY_CFG -> ~/.minimax/config.yaml
LOCAL_CFG_CANDIDATES = (
    os.environ.get("MINIMAX_GATEWAY_CFG", "").strip(),
)
LOCAL_CFG = LOCAL_CFG_CANDIDATES[-1]
DEFAULT_MINIMAX_MODEL = "MiniMax-M3.1-Flash"
DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"
DEFAULT_ZHIPU_MODEL = "glm-4-flash"
DEFAULT_ZHIPU_BASE = "https://open.bigmodel.cn/api/paas/v4"

# 鉴权/余额类错误：撞一次就够了，别每次都试
_FATAL_MARKERS = ("401", "402", "403", "invalid api key", "auth failed",
                  "insufficient_balance", "insufficient balance", "unauthorized")


# ------------------------------------------------------------------ 工具调用
# 两种协议的形状差很多，但**对上层只暴露一种**：都归一成
#   {"text": str, "tool_calls": [{"id","name","arguments"(dict)}], "protocol": "openai"|"anthropic"}
# 上层（agent.py）只按 protocol 拼下一轮的消息，两边写法都在那儿处理。

def _parse_json_args(raw):
    """工具参数：模型给的是 JSON 字符串；解析失败就当空字典（别让一轮对话整个炸掉）。"""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        d = json.loads(raw)
    except Exception:
        return {}
    return d if isinstance(d, dict) else {}


def _openai_tool_payload(model, messages, tools, system, max_tokens, temperature):
    payload = {
        "model": model,
        "messages": ([{"role": "system", "content": system}] if system else []) + list(messages),
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    headers = {"content-type": "application/json", "authorization": "Bearer "}
    return payload, headers


def _parse_openai_tool_reply(data):
    choices = data.get("choices") or []
    msg = (choices[0].get("message") or {}) if choices else {}
    calls = []
    for tc in (msg.get("tool_calls") or []):
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        if not name:
            continue
        calls.append({"id": tc.get("id") or "", "name": name,
                      "arguments": _parse_json_args(fn.get("arguments"))})
    return (msg.get("content") or "").strip(), calls


def _anthropic_tool_payload(model, messages, tools, system, max_tokens, temperature):
    payload = {"model": model, "max_tokens": max_tokens,
               "messages": list(messages), "temperature": temperature}
    if system:
        payload["system"] = system
    if tools:
        payload["tools"] = [{
            "name": t.get("function", {}).get("name"),
            "description": t.get("function", {}).get("description", ""),
            "input_schema": t.get("function", {}).get("parameters")
                           or {"type": "object", "properties": {}},
        } for t in tools]
    headers = {"content-type": "application/json", "anthropic-version": "2023-06-01"}
    return payload, headers


def _parse_anthropic_tool_reply(data):
    texts = []
    calls = []
    for block in (data.get("content") or []):
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            texts.append(block.get("text") or "")
        elif kind == "tool_use":
            inp = block.get("input")
            calls.append({"id": block.get("id") or "", "name": block.get("name") or "",
                          "arguments": inp if isinstance(inp, dict) else {}})
    return "".join(texts).strip(), calls


def _store_key(name):
    if keylib is None:
        return ""
    try:
        return keylib.get(name)
    except Exception:
        return ""


def _parse_minimax_yaml(path):
    """从一个 yaml 里抠出 minimax 段的 apiKey / baseURL。"""
    out = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            txt = f.read()
    except Exception:
        return out
    m = re.search(r"minimax:(.*?)(?=\n\S)", txt, re.S)
    block = m.group(1) if m else txt
    km = re.search(r"apiKey:\s*(\S+)", block)
    bm = re.search(r"baseURL:\s*(\S+)", block)
    if km:
        out["apiKey"] = km.group(1).strip().strip('"').strip("'")
    if bm:
        out["baseURL"] = bm.group(1).strip().strip('"').strip("'")
    return out


def _read_minimax_local(path=None):
    """从本机 MiniMax 网关配置里抠出 apiKey / baseURL（都没有就当没有）。"""
    paths = [path] if path else list(LOCAL_CFG_CANDIDATES)
    for p in paths:
        if not p:
            continue
        out = _parse_minimax_yaml(p)
        if out:
            return out
    return {}


class MinimaxBackend(object):
    """anthropic /messages 协议；404 时自动回落到 OpenAI /chat/completions。"""

    name = "minimax"

    def __init__(self, model=None, api_key=None, base_url=None, timeout=120):
        local = _read_minimax_local()
        self._explicit = (api_key or "").strip()
        self._local = local
        self.api_key = (self._explicit or _store_key("minimax")
                        or local.get("apiKey") or os.environ.get("MINIMAX_API_KEY", ""))
        self.base_url = (base_url or local.get("baseURL") or "https://api.minimaxi.com/anthropic/v1").rstrip("/")
        self.model = model or DEFAULT_MINIMAX_MODEL
        self.timeout = timeout

    def refresh(self):
        """重新解析 key —— 在 set_key.py 里换 key 后不用重启助手。"""
        self.api_key = (self._explicit or _store_key("minimax")
                        or self._local.get("apiKey") or os.environ.get("MINIMAX_API_KEY", ""))
        return self.api_key

    def available(self):
        return bool(self.api_key)

    def chat(self, messages, system=None, max_tokens=1024, temperature=0.6):
        headers = {
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": self.api_key,
            "authorization": "Bearer " + self.api_key,
        }
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": messages,
            "temperature": temperature,
        }
        if system:
            body["system"] = system
        try:
            r = requests.post(self.base_url + "/messages", headers=headers, json=body,
                              timeout=self.timeout)
        except Exception as e:
            raise RuntimeError("minimax request failed: %s" % e)
        if r.status_code < 400:
            return self._parse_anthropic(r.json())
        # **只有 404 才回落**到 OpenAI 兼容协议。
        # 401/402/400 是真实错误（余额不足、key 失效、模型名不对）——
        # 回落一次只会把真正的错误盖掉：之前 402 被回落的 404 顶掉，
        # 面板上就显示成「404 地址不对」，害我查错方向。
        if r.status_code != 404:
            raise RuntimeError("minimax http %s: %s" % (r.status_code, r.text[:200]))
        obody = {
            "model": self.model,
            "messages": ([{"role": "system", "content": system}] if system else []) + list(messages),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        try:
            r2 = requests.post(self.base_url + "/chat/completions", headers=headers,
                                json=obody, timeout=self.timeout)
        except Exception as e:
            raise RuntimeError("minimax fallback failed: %s" % e)
        if r2.status_code >= 400:
            raise RuntimeError("minimax /messages 404; fallback http %s: %s"
                               % (r2.status_code, r2.text[:200]))
        return self._parse_openai(r2.json())

    @staticmethod
    def _parse_anthropic(data):
        c = data.get("content")
        if isinstance(c, list):
            return "".join(b.get("text", "") for b in c if isinstance(b, dict))
        if isinstance(data.get("completion"), str):
            return data["completion"]
        if isinstance(data.get("choices"), list):
            return MinimaxBackend._parse_openai(data)
        raise RuntimeError("minimax unexpected payload: %s" % json.dumps(data)[:200])

    @staticmethod
    def _parse_openai(data):
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("minimax empty choices")
        msg = choices[0].get("message") or {}
        return (msg.get("content") or "").strip()

    def chat_tools(self, messages, tools=None, system=None, max_tokens=1024, temperature=0.6):
        """MiniMax 走 Anthropic 协议；404 时回落到 OpenAI 兼容协议（和 chat() 一样的规矩）。"""
        if not self.available():
            raise RuntimeError("minimax: no api key")
        headers = {
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": self.api_key,
            "authorization": "Bearer " + self.api_key,
        }
        payload, _ = _anthropic_tool_payload(self.model, messages, tools, system,
                                             max_tokens, temperature)
        try:
            r = requests.post(self.base_url + "/messages", headers=headers, json=payload,
                              timeout=self.timeout)
        except Exception as e:
            raise RuntimeError("minimax request failed: %s" % e)
        if r.status_code < 400:
            text, calls = _parse_anthropic_tool_reply(r.json())
            return {"text": text, "tool_calls": calls, "protocol": "anthropic"}
        if r.status_code != 404:
            raise RuntimeError("minimax http %s: %s" % (r.status_code, r.text[:200]))
        opayload, oheaders = _openai_tool_payload(self.model, messages, tools, system,
                                                  max_tokens, temperature)
        oheaders["authorization"] = "Bearer " + self.api_key
        try:
            r2 = requests.post(self.base_url + "/chat/completions", headers=oheaders,
                               json=opayload, timeout=self.timeout)
        except Exception as e:
            raise RuntimeError("minimax fallback failed: %s" % e)
        if r2.status_code >= 400:
            raise RuntimeError("minimax /messages 404; fallback http %s: %s"
                               % (r2.status_code, r2.text[:200]))
        text, calls = _parse_openai_tool_reply(r2.json())
        return {"text": text, "tool_calls": calls, "protocol": "openai"}


class ClaudeBackend(object):
    """anthropic /messages 协议（Claude）。

    和 MiniMax 是同一种协议实现，但**没有 OpenAI 回落** ——
    Anthropic 官方没有 /chat/completions 端点，回落了也只会再失败一次。
    """

    name = "claude"

    def __init__(self, name=None, model=None, api_key=None, base_url=None,
                 timeout=120, env="ANTHROPIC_API_KEY"):
        self.name = name or "claude"
        self._explicit = (api_key or "").strip()
        self._env = env
        self.api_key = (self._explicit or _store_key(self.name)
                        or os.environ.get(self._env, ""))
        self.base_url = (base_url or "https://api.anthropic.com/v1").rstrip("/")
        self.model = model or ""
        self.timeout = timeout

    def refresh(self):
        self.api_key = (self._explicit or _store_key(self.name)
                        or os.environ.get(self._env, ""))
        return self.api_key

    def available(self):
        return bool(self.api_key)

    def chat(self, messages, system=None, max_tokens=1024, temperature=0.6):
        if not self.available():
            raise RuntimeError("%s: no api key" % self.name)
        headers = {
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": self.api_key,
        }
        body = {"model": self.model, "max_tokens": max_tokens,
                "messages": messages, "temperature": temperature}
        if system:
            body["system"] = system
        try:
            r = requests.post(self.base_url + "/messages", headers=headers, json=body,
                              timeout=self.timeout)
        except Exception as e:
            raise RuntimeError("%s request failed: %s" % (self.name, e))
        if r.status_code >= 400:
            raise RuntimeError("%s http %s: %s" % (self.name, r.status_code, r.text[:200]))
        return MinimaxBackend._parse_anthropic(r.json())

    def chat_tools(self, messages, tools=None, system=None, max_tokens=1024, temperature=0.6):
        """Anthropic 协议的工具调用（tool_use）。"""
        if not self.available():
            raise RuntimeError("%s: no api key" % self.name)
        payload, headers = _anthropic_tool_payload(self.model, messages, tools, system,
                                                   max_tokens, temperature)
        headers["x-api-key"] = self.api_key
        try:
            r = requests.post(self.base_url + "/messages", headers=headers, json=payload,
                              timeout=self.timeout)
        except Exception as e:
            raise RuntimeError("%s request failed: %s" % (self.name, e))
        if r.status_code >= 400:
            raise RuntimeError("%s http %s: %s" % (self.name, r.status_code, r.text[:200]))
        text, calls = _parse_anthropic_tool_reply(r.json())
        return {"text": text, "tool_calls": calls, "protocol": "anthropic"}


class OpenAIBackend(object):
    """OpenAI /chat/completions 兼容协议的通用后端（DeepSeek / 智谱 共用）。"""

    name = "openai"

    def __init__(self, name=None, model=None, api_key=None, base_url=None,
                 timeout=120, store_name=None, env=None):
        self.name = name or "openai"
        self._store_name = store_name or self.name
        self._env = env or ("%s_API_KEY" % self.name.upper())
        self._explicit = (api_key or "").strip()
        self.api_key = self._explicit or _store_key(self._store_name) or os.environ.get(self._env, "")
        self.base_url = (base_url or "").rstrip("/")
        self.model = model or ""
        self.timeout = timeout

    def refresh(self):
        """重新解析 key —— set_key.py 换 key 后不用重启助手。"""
        self.api_key = self._explicit or _store_key(self._store_name) or os.environ.get(self._env, "")
        return self.api_key

    def available(self):
        return bool(self.api_key)

    def chat(self, messages, system=None, max_tokens=1024, temperature=0.6):
        if not self.available():
            raise RuntimeError("%s: no api key" % self.name)
        payload = {
            "model": self.model,
            "messages": ([{"role": "system", "content": system}] if system else []) + list(messages),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        headers = {
            "content-type": "application/json",
            "authorization": "Bearer " + self.api_key,
        }
        try:
            r = requests.post(self.base_url + "/chat/completions", headers=headers,
                              json=payload, timeout=self.timeout)
        except Exception as e:
            raise RuntimeError("%s request failed: %s" % (self.name, e))
        if r.status_code >= 400:
            raise RuntimeError("%s http %s: %s" % (self.name, r.status_code, r.text[:200]))
        return MinimaxBackend._parse_openai(r.json())

    def chat_tools(self, messages, tools=None, system=None, max_tokens=1024, temperature=0.6):
        """OpenAI 兼容协议的工具调用（tool_calls）。"""
        if not self.available():
            raise RuntimeError("%s: no api key" % self.name)
        payload, headers = _openai_tool_payload(self.model, messages, tools, system,
                                                 max_tokens, temperature)
        headers["authorization"] = "Bearer " + self.api_key
        try:
            r = requests.post(self.base_url + "/chat/completions", headers=headers,
                              json=payload, timeout=self.timeout)
        except Exception as e:
            raise RuntimeError("%s request failed: %s" % (self.name, e))
        if r.status_code >= 400:
            raise RuntimeError("%s http %s: %s" % (self.name, r.status_code, r.text[:200]))
        text, calls = _parse_openai_tool_reply(r.json())
        return {"text": text, "tool_calls": calls, "protocol": "openai"}


class DeepSeekBackend(OpenAIBackend):
    """OpenAI 兼容协议（DeepSeek）。"""

    def __init__(self, model=None, api_key=None, base_url=None, timeout=120):
        OpenAIBackend.__init__(self, name="deepseek", model=model or DEFAULT_DEEPSEEK_MODEL,
                               api_key=api_key, base_url=base_url or "https://api.deepseek.com",
                               timeout=timeout, store_name="deepseek", env="DEEPSEEK_API_KEY")


class ZhipuBackend(OpenAIBackend):
    """OpenAI 兼容协议（智谱 GLM）。"""

    def __init__(self, model=None, api_key=None, base_url=None, timeout=120):
        OpenAIBackend.__init__(self, name="zhipu", model=model or DEFAULT_ZHIPU_MODEL,
                               api_key=api_key, base_url=base_url or DEFAULT_ZHIPU_BASE,
                               timeout=timeout, store_name="zhipu", env="ZHIPU_API_KEY")


def _registry():
    return (prov.PROVIDERS if prov is not None else {})


MODELS_URL = dict((n, p.get("models_url")) for n, p in _registry().items()
                  if p.get("models_url"))


def _models_headers(provider, key):
    """各家 /models 端点的鉴权头不一样：OpenAI 系用 Bearer、Anthropic 用 x-api-key、
    Gemini 原生端点用 x-goog-api-key。**都带上 Bearer**，省得为一家写分支。
    """
    p = _registry().get(provider) or {}
    style = p.get("models_auth") or ("x-api-key" if p.get("protocol") == "anthropic"
                                    else "bearer")
    h = {"authorization": "Bearer " + key, "content-type": "application/json"}
    if style == "x-api-key":
        h["x-api-key"] = key
    elif style == "x-goog-api-key":
        h["x-goog-api-key"] = key
    return h


def list_models(provider, api_key=None, timeout=20, url=None):
    """从服务商自己的 /models 端点拉一份当前可用的模型清单。

    返回 (models, error)：models 是模型 id 列表，失败时是空列表 + 原因。
    面板上「拉取最新」按钮用它 —— 新模型上线不用等发版。
    """
    if requests is None:
        return [], "requests module missing"
    url = url or _registry().get(provider, {}).get("models_url") or MODELS_URL.get(provider)
    if not url:
        return [], "unknown provider: %s" % provider
    key = api_key
    if not key and keylib is not None:
        try:
            key = keylib.get(provider)
        except Exception:
            key = None
    if not key:
        return [], "no api key for %s" % provider
    headers = _models_headers(provider, key)
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
    except Exception as e:
        return [], str(e)
    if r.status_code != 200:
        return [], "HTTP %s" % r.status_code
    try:
        j = r.json()
    except Exception:
        return [], "bad json"
    data = None
    if isinstance(j, dict):
        for keyname in ("data", "models"):
            if isinstance(j.get(keyname), list):
                data = j[keyname]
                break
    if not isinstance(data, list):
        return [], "unexpected payload"
    out = []
    for it in data:
        mid = None
        if isinstance(it, dict):
            mid = it.get("id") or it.get("name") or it.get("model")
        elif it:
            mid = it
        if mid:
            mid = str(mid)
            # Gemini 的原生清单里是 "models/gemini-xxx"，去掉前缀
            if mid.startswith("models/"):
                mid = mid[len("models/"):]
            out.append(mid)
    return sorted(set(out)), ""


def make_backend(name, lcfg=None):
    """按注册表造一个后端实例。

    助手和面板的「后端自检」都走这一条路 —— 两边算出来的后端一定一致，
    不会出现「面板说能用、助手说不能用」。
    """
    p = _registry().get(name)
    if not p:
        return None
    lcfg = lcfg or {}
    model = lcfg.get("%s_model" % name) or p.get("default_model")
    api_key = lcfg.get("%s_api_key" % name) or ""
    base_url = lcfg.get("%s_base_url" % name) or p.get("base_url")
    if name == "minimax":
        return MinimaxBackend(model=model, api_key=api_key, base_url=base_url)
    if p.get("protocol") == "anthropic":
        return ClaudeBackend(name=name, model=model, api_key=api_key, base_url=base_url,
                             env=p.get("env"))
    return OpenAIBackend(name=name, model=model, api_key=api_key, base_url=base_url,
                         store_name=name, env=p.get("env"))


class LLM(object):
    """按配置顺序串行尝试各后端，第一个成功的返回。

    鉴权/余额类错误会把该后端在本次进程内拉黑（不再重复撞墙）。
    """

    def __init__(self, cfg):
        self._broken = {}     # backend name -> 拉黑原因
        self._build((cfg or {}).get("llm") or {})

    def _build(self, lcfg):
        """按配置顺序重建后端列表。

        llm.enabled 里关掉的后端**根本不建** —— 连一次请求都不会发出去。
        """
        registry = _registry()
        # 用户排过的顺序在前；没排过的新厂商补在**后面**（不打乱已有顺序）
        order = [n for n in (lcfg.get("order") or []) if n in registry]
        for n in registry:
            if n not in order:
                order.append(n)
        enabled = lcfg.get("enabled")
        if not isinstance(enabled, dict):
            enabled = {}
        # enabled 里没写的厂商 = **默认关** —— 新加进来的厂商不该被自动拉起来
        # （还没填 key，发出去也是白费）。
        default_on = set(prov.DEFAULT_ENABLED) if prov is not None else set()
        self.backends = []
        for name in order:
            if not enabled.get(name, name in default_on):
                continue
            b = make_backend(name, lcfg)
            if b is not None:
                self.backends.append(b)

    def apply_config(self, cfg):
        """配置热重载后调用 —— 模型/后端顺序改了**立刻生效**，不用重启助手。

        余额/鉴权黑名单只在「模型和地址都没变」时保留：换了模型就该重新试一次，
        否则一次 402 会把新模型也连坐掉。
        """
        lcfg = (cfg or {}).get("llm") or {}
        old = {b.name: (b.model, b.base_url) for b in self.backends}
        self._build(lcfg)
        for b in self.backends:
            if old.get(b.name) != (b.model, b.base_url):
                self._broken.pop(b.name, None)

        try:
            print('[llm] backends -> %s'
                  % (', '.join(b.name for b in self.backends) or '(none)'))
        except Exception:
            pass

    def chat(self, messages, system=None, max_tokens=1024, temperature=0.6):
        errs = []
        for b in self.backends:
            if b.name in self._broken:
                errs.append("%s: skipped (%s)" % (b.name, self._broken[b.name]))
                continue
            try:
                b.refresh()
            except Exception:
                pass
            if not b.available():
                errs.append("%s: no key" % b.name)
                continue
            try:
                text = b.chat(messages, system=system, max_tokens=max_tokens,
                              temperature=temperature)
                if text and text.strip():
                    return text.strip()
                errs.append("%s: empty reply" % b.name)
            except Exception as e:
                msg = str(e)
                low = msg.lower()
                if any(mk in low for mk in _FATAL_MARKERS):
                    self._broken[b.name] = "auth/balance error (disabled for this session)"
                errs.append("%s: %s" % (b.name, msg))
        raise RuntimeError(" | ".join(errs) if errs else "no backend")

    def chat_tools(self, messages, tools=None, system=None, max_tokens=1024,
                   temperature=0.6, backend=None):
        """带工具的一次请求。

        返回 {"text", "tool_calls", "protocol", "backend"}。
        backend 不为空 = 强制用这个后端。**工具对话的后续轮次必须回到同一个模型**，
        否则 tool_call id 对不上、结果无处安放 —— 所以下一轮要把 backend 传回来。
        """
        errs = []
        if backend:
            pool = [b for b in self.backends if b.name == backend]
            if not pool:
                raise RuntimeError("backend %s is not available" % backend)
        else:
            pool = list(self.backends)
        for b in pool:
            if b.name in self._broken and not backend:
                errs.append("%s: skipped (%s)" % (b.name, self._broken[b.name]))
                continue
            try:
                b.refresh()
            except Exception:
                pass
            if not b.available():
                errs.append("%s: no key" % b.name)
                continue
            try:
                res = b.chat_tools(messages, tools=tools, system=system,
                                   max_tokens=max_tokens, temperature=temperature)
            except Exception as e:
                msg = str(e)
                low = msg.lower()
                if any(mk in low for mk in _FATAL_MARKERS):
                    self._broken[b.name] = "auth/balance error (disabled for this session)"
                errs.append("%s: %s" % (b.name, msg))
                continue
            res = res or {}
            res["backend"] = b.name
            res.setdefault("text", "")
            res.setdefault("tool_calls", [])
            res.setdefault("protocol", "openai")
            if res["tool_calls"] or res["text"]:
                return res
            errs.append("%s: empty reply" % b.name)
        raise RuntimeError(" | ".join(errs) if errs else "no backend")
