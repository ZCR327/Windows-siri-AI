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

# 本机 MiniMax 网关配置（可选）：本机已经登录过时可以免填 key。
# 依次尝试：环境变量 MINIMAX_GATEWAY_CFG -> ~/.minimax/config.yaml -> Windows 默认安装路径
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
        self.base_url = (base_url or local.get("baseURL") or "https://api.minimaxi.com/v1").rstrip("/")
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
        # 回落到 OpenAI 兼容协议
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
            raise RuntimeError("minimax http %s: %s" % (r2.status_code, r2.text[:200]))
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


MODELS_URL = {
    "minimax": "https://api.minimaxi.com/v1/models",
    "deepseek": "https://api.deepseek.com/models",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4/models",
}


def list_models(provider, api_key=None, timeout=20):
    """从服务商自己的 /models 端点拉一份当前可用的模型清单。

    返回 (models, error)：models 是模型 id 列表，失败时是空列表 + 原因。
    面板上「拉取最新」按钮用它 —— 新模型上线不用等发版。
    """
    if requests is None:
        return [], "requests module missing"
    url = MODELS_URL.get(provider)
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
    headers = {"authorization": "Bearer " + key, "content-type": "application/json"}
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
    data = j.get("data") if isinstance(j, dict) else None
    if not isinstance(data, list):
        return [], "unexpected payload"
    out = []
    for it in data:
        if isinstance(it, dict):
            mid = it.get("id") or it.get("name") or it.get("model")
            if mid:
                out.append(str(mid))
        elif it:
            out.append(str(it))
    return sorted(set(out)), ""


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
        order = lcfg.get("order") or ["minimax", "deepseek", "zhipu"]
        enabled = lcfg.get("enabled") or {}
        self.backends = []
        for name in order:
            if not enabled.get(name, True):
                continue
            if name == "minimax":
                self.backends.append(MinimaxBackend(
                    model=lcfg.get("minimax_model"),
                    api_key=lcfg.get("minimax_api_key"),
                    base_url=lcfg.get("minimax_base_url")))
            elif name == "deepseek":
                self.backends.append(DeepSeekBackend(
                    model=lcfg.get("deepseek_model"),
                    api_key=lcfg.get("deepseek_api_key"),
                    base_url=lcfg.get("deepseek_base_url")))
            elif name == "zhipu":
                self.backends.append(ZhipuBackend(
                    model=lcfg.get("zhipu_model"),
                    api_key=lcfg.get("zhipu_api_key"),
                    base_url=lcfg.get("zhipu_base_url")))

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
