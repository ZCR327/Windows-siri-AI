# -*- coding: utf-8 -*-
"""助理循环自检（离线：不碰真实模型、不碰麦克风、不碰屏幕）。

验三件事：
  1) 对话记忆：记几轮、按配置裁、交替整形、清空指令；
  2) 工具调用循环：OpenAI 协议和 Anthropic 协议两套消息形状都要对；
  3) **真的用 HTTP 打一个本地假模型服务器**，看发出去的报文长什么样
     （tools 数组、tool_choice、鉴权头、路径）。

    python tools\\siri_orb\\test_agent.py

退出码 0 = 全过。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import context as ctx_mod
import agent as agent_mod
import llm as llm_mod
import tools as tool_mod

FAILS = []
CHECKS = [0]


def ok(cond, label, detail=""):
    CHECKS[0] += 1
    if cond:
        print("  PASS  %s" % label)
    else:
        print("  FAIL  %s %s" % (label, detail))
        FAILS.append(label)


def sec(title):
    print("")
    print("== %s ==" % title)


# ---------------------------------------------------------------- 1. 记忆
def check_memory():
    sec("1. 对话记忆")
    c = ctx_mod.Conversation()
    c.add_turn("第一句", "第一答")
    c.add_turn("第二句", "第二答")
    h = c.history(6)
    ok(len(h) == 4, "两轮 = 4 条消息", "-> %d" % len(h))
    ok(h[0]["content"] == "第一句", "第一条是用户那一句")
    h2 = c.history(1)
    ok(len(h2) == 2, "只记 1 轮 -> 2 条", "-> %d" % len(h2))
    ok(h2[0]["content"] == "第二句", "留的是最近那轮")
    c.add("assistant", "只有答话")
    h3 = c.history(6)
    ok(h3[0]["role"] == "user", "开头不是 user 的会被丢掉")
    m = [{"role": "user", "content": "a"}, {"role": "user", "content": "b"},
         {"role": "assistant", "content": "c"}]
    n = ctx_mod.normalize_roles(m)
    ok(len(n) == 2, "连续两条 user 被合并（Anthropic 要求交替）", "-> %d" % len(n))
    ok("a" in n[0]["content"] and "b" in n[0]["content"], "合并后两句都在")
    ok(n[1]["role"] == "assistant", "assistant 那条独立")
    ok(ctx_mod.is_clear_command("清空上下文"), "认得「清空上下文」")
    ok(ctx_mod.is_clear_command("Forget that"), "认得英文的说法")
    ok(not ctx_mod.is_clear_command("今天天气怎么样"), "普通问句不会误触发")
    conv = ctx_mod.get_conversation()
    conv.clear()
    conv.add_turn("x", "y")
    conv.clear()
    ok(len(conv.history(6)) == 0, "清空后没有历史")


# ------------------------------------------------------------ 2. 循环本体
class FakeLLM(object):
    """假模型：按剧本回话。第一轮要调工具，第二轮给最终答案。"""

    def __init__(self, protocol="openai", tool="read_file", args=None, text="干完了"):
        self.protocol = protocol
        self.tool = tool
        self.args = args or {}
        self.text = text
        self.calls = []
        self.seen = []

    def chat_tools(self, messages, tools=None, system=None, backend=None):
        self.seen.append(list(messages))
        self.calls.append({"tools": tools, "system": system, "backend": backend})
        if len(self.calls) == 1:
            return {"text": "", "protocol": self.protocol, "backend": "fake",
                    "tool_calls": [{"id": "t1", "name": self.tool,
                                    "arguments": self.args}]}
        return {"text": self.text, "tool_calls": [], "protocol": self.protocol,
                "backend": "fake"}


def check_loop():
    sec("2. 工具调用循环")
    tmp = tempfile.mkdtemp(prefix="siri_agent_")
    try:
        cfg = {"agent": {"enabled": True, "work_dir": tmp, "allow_screen": False},
               "ctx": {"enabled": True, "history_turns": 6, "sysinfo": True}}
        with io.open(os.path.join(tmp, "note.txt"), "w", encoding="utf-8") as f:
            f.write("hello")

        llm = FakeLLM(tool="read_file", args={"path": "note.txt"})
        out = agent_mod.run_agent(cfg, llm, "读一下 note.txt")
        ok(out == "干完了", "循环跑完给出最终回答", "-> %r" % out)
        ok(len(llm.calls) == 2, "调了两次模型（一次要工具、一次收尾）",
           "-> %d" % len(llm.calls))
        msgs = llm.seen[1]
        tool_msgs = [m for m in msgs if m.get("role") == "tool"]
        ok(len(tool_msgs) == 1, "OpenAI 协议工具结果是 role=tool",
           "-> %d 条" % len(tool_msgs))
        if tool_msgs:
            ok(tool_msgs[0].get("tool_call_id") == "t1", "tool_call_id 对得上")
            ok("hello" in tool_msgs[0].get("content", ""), "文件内容送回模型了")
        asst = [m for m in msgs if m.get("role") == "assistant"]
        ok(bool(asst) and bool(asst[0].get("tool_calls")),
           "assistant 消息带 tool_calls")
        ok(bool(llm.calls[0]["tools"]), "第一次请求带了工具清单")
        sysp = llm.calls[0]["system"] or ""
        ok("工具" in sysp, "系统提示词里有能力说明")
        ok("工作目录" in sysp, "系统提示词里有工作目录")

        llm2 = FakeLLM(protocol="anthropic", tool="list_files", args={})
        out2 = agent_mod.run_agent(cfg, llm2, "列一下文件")
        ok(out2 == "干完了", "Anthropic 协议也跑通", "-> %r" % out2)
        m2 = llm2.seen[1]
        last = m2[-1]
        ok(last.get("role") == "user", "Anthropic 工具结果放在 user 消息里",
           "-> %r" % last.get("role"))
        blocks = last.get("content")
        ok(isinstance(blocks, list) and bool(blocks)
           and blocks[0].get("type") == "tool_result", "Anthropic 工具结果是 tool_result 块")
        if isinstance(blocks, list) and bool(blocks):
            ok(blocks[0].get("tool_use_id") == "t1", "tool_use_id 对得上")
            ok("note.txt" in blocks[0].get("content", ""), "目录清单送回模型了")
        au = [m for m in m2 if m.get("role") == "assistant"]
        ok(bool(au) and isinstance(au[0].get("content"), list)
           and au[0]["content"][0].get("type") == "tool_use", "assistant 消息带 tool_use 块")

        llm3 = FakeLLM(tool="list_files", args={})
        agent_mod.run_agent(cfg, llm3, "现在呢",
                            history=[{"role": "user", "content": "之前问的"},
                                     {"role": "assistant", "content": "之前答的"}])
        ok(llm3.seen[0][0]["content"] == "之前问的", "历史排在当前问题前面")

        llm4 = FakeLLM()
        ok(agent_mod.run_agent({"agent": {"enabled": False}}, llm4, "hi") is None,
           "助理没开 -> 返回 None")
        ok(len(llm4.calls) == 0, "助理没开 -> 一次模型请求都没发")

        # 工具失败也要把失败原因送回模型（模型才知道换个办法）
        llm5 = FakeLLM(tool="read_file", args={"path": "不存在.txt"})
        agent_mod.run_agent(cfg, llm5, "读一个不存在的文件")
        tm5 = [m for m in llm5.seen[1] if m.get("role") == "tool"]
        c5 = tm5[0].get("content", "") if tm5 else ""
        ok("失败" in c5 or "不存在" in c5, "工具失败原因送回模型", "-> %s" % c5[:40])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------- 3. 真实 HTTP 报文
def check_http():
    sec("3. 真实 HTTP 报文（本地假模型服务器）")
    try:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    except Exception as e:
        ok(False, "http.server 可用", "-> %s" % e)
        return
    got = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode("utf-8")
            got["body"] = json.loads(raw)
            got["path"] = self.path
            got["auth"] = self.headers.get("authorization")
            got["xkey"] = self.headers.get("x-api-key")
            if self.path.endswith("/messages"):
                payload = {"content": [{"type": "text", "text": ""},
                                       {"type": "tool_use", "id": "tu_1",
                                        "name": "list_files", "input": {}}]}
            else:
                payload = {"choices": [{"message": {"role": "assistant", "content": "",
                            "tool_calls": [{"id": "call_1", "type": "function",
                                            "function": {"name": "list_files",
                                                         "arguments": "{}"}}]}}]}
            data = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    spec = [{"type": "function", "function": {"name": "list_files",
                "description": "list files",
                "parameters": {"type": "object", "properties": {}}}}]
    try:
        b = llm_mod.OpenAIBackend(name="mock", model="mock-model",
                                   api_key="dummy-local-test-key",
                                   base_url="http://127.0.0.1:%d/v1" % port)
        res = b.chat_tools([{"role": "user", "content": "list"}], tools=spec,
                           system="you are a helper")
        ok(res["protocol"] == "openai", "协议标记是 openai")
        ok(len(res["tool_calls"]) == 1, "解析出 1 个工具调用")
        ok(res["tool_calls"][0]["name"] == "list_files", "工具名对")
        ok(res["tool_calls"][0]["id"] == "call_1", "工具调用 id 对")
        body = got.get("body") or {}
        ok("tools" in body, "请求体带 tools")
        ok(body.get("tool_choice") == "auto", "tool_choice=auto")
        msgs = body.get("messages") or []
        ok(bool(msgs) and msgs[0].get("role") == "system", "system 提示词在最前")
        ok(str(got.get("auth", "")).startswith("Bearer "), "带 Bearer 鉴权头")
        ok(str(got.get("path", "")).endswith("/chat/completions"), "打的是 /chat/completions")

        b2 = llm_mod.ClaudeBackend(name="mockclaude", model="mock-model",
                                    api_key="dummy-local-test-key",
                                    base_url="http://127.0.0.1:%d" % port)
        res2 = b2.chat_tools([{"role": "user", "content": "list"}], tools=spec,
                             system="you are a helper")
        ok(res2["protocol"] == "anthropic", "协议标记是 anthropic")
        ok(len(res2["tool_calls"]) == 1, "Anthropic 解析出 1 个 tool_use")
        ok(res2["tool_calls"][0]["name"] == "list_files", "tool_use 名字对")
        body2 = got.get("body") or {}
        ok("tools" in body2, "Anthropic 请求体带 tools")
        tl = body2.get("tools") or []
        ok(bool(tl) and tl[0].get("name") == "list_files", "tools 里是 name/description/input_schema")
        ok("input_schema" in (tl[0] if tl else {}), "有 input_schema")
        ok(got.get("xkey") == "dummy-local-test-key", "Anthropic 带 x-api-key")
        ok(str(got.get("path", "")).endswith("/messages"), "打的是 /messages")
    finally:
        srv.shutdown()


# ------------------------------------------- 4. 轮次用光 / 工作目录自建
PREAMBLE = "我先去看看文件内容。"
SUMMARY = "总结：文件已经列出来了，一共 3 个。"


class _FakeLoopLLM(object):
    """每轮只回一句开场白 + 一个工具调用（模拟「永远干不完」的多步骤任务）。"""

    def chat_tools(self, messages, tools=None, system=None, max_tokens=1024,
                   temperature=0.6, backend=None):
        return {"backend": "fake", "protocol": "openai", "text": PREAMBLE,
                "tool_calls": [{"id": "1", "name": "no_such_tool", "arguments": {}}]}

    def chat(self, messages, system=None, max_tokens=1024, temperature=0.6):
        return SUMMARY


def check_rounds():
    sec("4. 轮次用光不能拿开场白当答案")
    cfg = {"agent": {"enabled": True, "max_rounds": 3, "allow_shell": False,
                     "allow_screen": False, "work_dir": ""}}
    ag = agent_mod.Agent(cfg, _FakeLoopLLM())
    out = ag.run("列出文件") or ""
    ok(out != PREAMBLE, "没有把中途的开场白当答案", "-> %r" % out)
    ok(SUMMARY in out, "返回的是补问出来的总结", "-> %r" % out)


def check_work_dir():
    sec("5. 工作目录会自动建出来")
    tmp = tempfile.mkdtemp(prefix="siri-wd-")
    try:
        cfg = {"agent": {"enabled": True, "work_dir": os.path.join(tmp, "Siri-OS")}}
        base = tool_mod.work_dir(cfg)
        ok(not os.path.isdir(base), "一开始目录不存在")
        tool_mod.ensure_work_dir(cfg)
        ok(os.path.isdir(base), "ensure_work_dir 之后目录在了")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    check_memory()
    check_loop()
    check_http()
    check_rounds()
    check_work_dir()
    print("")
    print("%d checks, %d FAIL" % (CHECKS[0], len(FAILS)))
    if FAILS:
        for f in FAILS:
            print("  - %s" % f)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
