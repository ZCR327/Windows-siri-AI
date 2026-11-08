# -*- coding: utf-8 -*-
"""8 家后端的调用链路自测（离线 mock + 真实端点探针）。

为什么需要这个文件：本机只有 MiniMax / DeepSeek / 智谱 三家的 key，
Claude / 千问 / Kimi / Gemini / 豆包 没 key —— 但**接线对不对**
（协议、请求头、请求体、响应解析、错误处理）完全可以离线验。

造一个本地 HTTP 服务器，把 8 家都跑一遍：

  1. 正常路径 —— mock 按该协议返回响应，后端能不能解析出文本
  2. 请求形状 —— 服务端记下收到的头和体，断言协议头 / 请求体对不对
  3. 错误路径 —— mock 返回 401 / 402 / 400，断言错误信息里带得上有用的字样

再打一次**真实服务商端点**（用假 key）：期望拿到「鉴权失败」，
而不是「连不上」或「请求体非法」—— 这能证明地址和协议真的对。
（打不通算跳过，不算失败 —— 本机网络时好时坏。）

    python tools/siri_orb/test_providers.py
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import providers as prov

try:
    import requests
except Exception:
    requests = None

FAILS = []
CHECKS = [0]
SKIPS = []


def ok(cond, label, detail=""):
    CHECKS[0] += 1
    if cond:
        print("  PASS  %s" % label)
    else:
        print("  FAIL  %s %s" % (label, detail))
        FAILS.append(label)


def skip(label, detail=""):
    SKIPS.append(label)
    print("  SKIP  %s %s" % (label, detail))


def sec(title):
    print("")
    print("== %s ==" % title)


# ------------------------------------------------------------ mock 服务器
RECEIVED = []          # 服务端收到的请求记录


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass        # 别把请求日志刷屏

    def _send(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path.endswith("/models"):
            self._send(200, {"object": "list", "data": [
                {"id": "mock-model-a"}, {"id": "mock-model-b"}]})
            return
        self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {}
        path = self.path.split("?")[0]
        RECEIVED.append({"path": path, "headers": dict(self.headers), "body": body})
        model = str(body.get("model") or "")
        # 用模型名注入错误：mock-401 / mock-402 / mock-400
        if model.startswith("mock-401"):
            self._send(401, {"error": {"type": "authentication_error",
                                       "message": "invalid api key"}})
            return
        if model.startswith("mock-402"):
            self._send(402, {"error": {"type": "billing_error",
                                       "message": "insufficient balance"}})
            return
        if model.startswith("mock-400"):
            self._send(400, {"error": {"message": "bad request"}})
            return
        parts = path.split("/")
        provider = parts[2] if len(parts) > 2 else "unknown"
        if path.endswith("/messages"):
            self._send(200, {
                "id": "msg_mock", "type": "message", "role": "assistant",
                "model": model,
                "content": [{"type": "text", "text": "MOCK-OK-" + provider}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 2}})
            return
        if path.endswith("/chat/completions"):
            self._send(200, {
                "id": "chatcmpl-mock", "object": "chat.completion",
                "created": 1, "model": model,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant",
                                         "content": "MOCK-OK-" + provider}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2,
                          "total_tokens": 3}})
            return
        self._send(404, {"error": {"message": "not found"}})


def mock_base(port, provider):
    return "http://127.0.0.1:%d/mock/%s/v1" % (port, provider)


def test_one(provider, port):
    """一家后端的完整链路：正常 / 请求形状 / 错误 / /models。"""
    import llm
    p = prov.PROVIDERS[provider]
    lcfg = {
        "%s_model" % provider: p.get("default_model") or "mock-model",
        "%s_api_key" % provider: "MOCK-KEY-123",
        "%s_base_url" % provider: mock_base(port, provider),
    }
    b = llm.make_backend(provider, lcfg)
    ok(b is not None, "%s 能造出后端实例" % provider)
    if b is None:
        return
    ok(b.available(), "%s 认得自己的 key" % provider)
    before = len(RECEIVED)
    txt = b.chat([{"role": "user", "content": "hi"}], system="be brief",
                 max_tokens=32, temperature=0)
    ok(txt == "MOCK-OK-" + provider, "%s 解析出回复文本" % provider, "-> %r" % txt)
    if len(RECEIVED) <= before:
        ok(False, "%s 服务端收到了请求" % provider)
        return
    rec = RECEIVED[-1]
    h = dict((k.lower(), v) for k, v in rec["headers"].items())
    body = rec["body"]
    if p.get("protocol") == "anthropic":
        ok(rec["path"].endswith("/messages"),
           "%s 走 anthropic /messages" % provider, "-> %s" % rec["path"])
        ok(h.get("x-api-key") == "MOCK-KEY-123", "%s 带 x-api-key 头" % provider)
        ok(h.get("anthropic-version") == "2023-06-01",
           "%s 带 anthropic-version 头" % provider)
        ok(body.get("system") == "be brief", "%s system 是单独一个字段" % provider)
        ok(isinstance(body.get("messages"), list), "%s messages 是列表" % provider)
        ok((body.get("messages") or [{}])[0].get("role") == "user",
           "%s 用户消息在 messages 里" % provider)
        ok(body.get("max_tokens") == 32, "%s 带了 max_tokens" % provider)
    else:
        ok(rec["path"].endswith("/chat/completions"),
           "%s 走 OpenAI /chat/completions" % provider, "-> %s" % rec["path"])
        ok(h.get("authorization") == "Bearer MOCK-KEY-123",
           "%s 带 Bearer 鉴权头" % provider)
        roles = [m.get("role") for m in (body.get("messages") or [])]
        ok(roles[:1] == ["system"], "%s system 拼在 messages 头部" % provider,
           "-> %s" % roles)
        ok(roles[1:2] == ["user"], "%s 用户消息排在后面" % provider, "-> %s" % roles)
        ok(body.get("stream") is False, "%s 明确关掉 stream" % provider)

    for tag, code in (("mock-401", 401), ("mock-402", 402), ("mock-400", 400)):
        lcfg["%s_model" % provider] = tag
        b2 = llm.make_backend(provider, lcfg)
        try:
            b2.chat([{"role": "user", "content": "hi"}], max_tokens=8)
            ok(False, "%s 遇到 %d 应该抛错" % (provider, code))
        except Exception as e:
            ok(str(code) in str(e), "%s 错误信息里带上了 %d" % (provider, code),
               "-> %s" % str(e)[:90])

    models, err = llm.list_models(provider, api_key="MOCK-KEY-123",
                                  url=mock_base(port, provider) + "/models")
    ok(models == ["mock-model-a", "mock-model-b"], "%s /models 解析正确" % provider,
       "-> %r %s" % (models, err))


def test_real_endpoints():
    """拿假 key 打真实端点 —— 期望「鉴权失败」，不是「连不上」「请求体非法」。"""
    sec("真实服务商端点探针（假 key，期望回鉴权错）")
    if requests is None:
        skip("没装 requests，跳过真实端点探针")
        return
    for provider, p in prov.PROVIDERS.items():
        url = (p.get("base_url") or "").rstrip("/")
        headers = {"content-type": "application/json"}
        body = {"model": p.get("default_model") or "probe-model",
                "max_tokens": 4,
                "messages": [{"role": "user", "content": "hi"}]}
        if p.get("protocol") == "anthropic":
            url += "/messages"
            headers["anthropic-version"] = "2023-06-01"
            headers["x-api-key"] = "FAKE-PROBE-KEY"
        else:
            url += "/chat/completions"
            headers["authorization"] = "Bearer FAKE-PROBE-KEY"
            body["stream"] = False
        try:
            r = requests.post(url, headers=headers, json=body, timeout=15)
        except Exception as e:
            skip("%s 真实端点连不上（网络问题，不算失败）" % provider,
                 "-> %s" % str(e)[:80])
            continue
        txt = (r.text or "")[:200].lower()
        auth_like = ("api key" in txt or "authentication" in txt
                     or "unauthorized" in txt or "invalid-api-key" in txt
                     or "apikey" in txt)
        if r.status_code in (401, 403):
            ok(True, "%s 真实端点回 %d（鉴权失败，正是预期的）" % (provider, r.status_code))
        elif r.status_code == 200:
            skip("%s 真实端点居然回 200（这个假 key 有效？可疑）" % provider)
        elif auth_like:
            ok(True, "%s 真实端点回 %d 且提示是 key 问题" % (provider, r.status_code))
        elif r.status_code == 404:
            ok(False, "%s 真实端点 404 —— 地址或路径不对" % provider,
               "-> %s" % (r.text or "")[:120])
        elif r.status_code >= 500:
            skip("%s 真实端点 %d（服务端问题，不算失败）" % (provider, r.status_code))
        else:
            ok(False, "%s 真实端点回 %d（不是预期的鉴权错）" % (provider, r.status_code),
               "-> %s" % (r.text or "")[:120])


def main():
    sec("1. 本地 mock：8 家后端的请求链路")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        for provider in prov.ORDER:
            test_one(provider, port)
    finally:
        srv.shutdown()
        srv.server_close()

    test_real_endpoints()

    print("")
    print("%d checks, %d FAIL, %d SKIP" % (CHECKS[0], len(FAILS), len(SKIPS)))
    if FAILS:
        for f in FAILS:
            print("  - %s" % f)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
