# -*- coding: utf-8 -*-
"""中英双语自检（离线，不碰麦克风、不碰屏幕）。

把「三层语言」的所有断言写成可跑的检查，改完 i18n / cfgstore / voice /
settings 之后跑一遍就知道有没有漏：

    python tools\siri_orb\i18n_check.py

退出码 0 = 全过；非 0 = 有 FAIL。
"""
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cfgstore
import i18n
import voice as voice_mod

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
    print("\n== %s ==" % title)


def _check_rebuild():
    """离屏跑一次「改界面语言 -> 整块重建」，看旧控件有没有留下来。

    ⚠ 2026-11-07 实测踩过：旧写法只拆顶层布局项，**嵌套布局里的控件**
    （底部那一行按钮）会留下来继续画在屏幕上 —— 中文按钮压在英文按钮上。
    判据用控件数，不靠肉眼看图。
    """
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5 import QtCore, QtWidgets
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    import settings as S
    S.SettingsDialog._backend_check = lambda self: None   # 自检不发真实请求
    dlg = S.SettingsDialog()
    n0 = len(dlg.findChildren(QtWidgets.QPushButton))
    counts = [n0]
    for lang in ("en", "zh"):          # 中 -> 英 -> 中，两轮重建
        dlg.cfg["ui_lang"] = lang
        dlg._rebuild_for_lang()
        app.processEvents()
        counts.append(len(dlg.findChildren(QtWidgets.QPushButton)))
    orphans = [w for w in app.topLevelWidgets()
               if w is not dlg and w.isVisible()
               and w.__class__.__name__ not in ("QMenu", "QToolTip")]
    print("    按钮数 %s；可见孤儿顶层控件 %d" % (" -> ".join(map(str, counts)), len(orphans)))
    try:
        dlg.hide()
    except Exception:
        pass
    return (counts == [n0] * 3) and (len(orphans) == 0)


def _check_new_features():
    """面板扩容（2026-11-07）带来的新能力，固化成回归断言。

    覆盖四件事：
      1) llm.enabled 关掉的后端**根本不建** —— 一次请求都不发出去；
      2) 首次运行自动生成 config.json —— 别人 clone 下来直接能跑；
      3) 密钥永远不进 config.json —— 只进密钥库；
      4) 新旋钮有默认值，而且真的被渲染代码读走。
    """
    import shutil
    import tempfile
    import time as _time

    # ---- 1. 后端过滤 ----
    import llm as llm_mod
    lcfg = {"order": ["minimax", "deepseek", "zhipu"],
            "enabled": {"minimax": False, "deepseek": True, "zhipu": False}}
    L = llm_mod.LLM({"llm": lcfg})
    names = [b.name for b in L.backends]
    ok("minimax" not in names, "关掉的 MiniMax 不建后端", "-> %s" % names)
    ok("zhipu" not in names, "关掉的 智谱 不建后端", "-> %s" % names)
    ok("deepseek" in names, "开着的 DeepSeek 还在", "-> %s" % names)
    ok(len(names) == 1, "只剩 1 个后端", "-> %s" % names)

    # ---- 2. 首次运行自动生成 config.json ----
    tmpdir = tempfile.mkdtemp(prefix="siri_cfg_")
    try:
        old_path = cfgstore.CONFIG_PATH
        fake = os.path.join(tmpdir, "config.json")
        cfgstore.CONFIG_PATH = fake
        ok(not os.path.exists(fake), "起始状态：没有 config.json")
        cfgstore.load()
        ok(os.path.exists(fake), "首次运行自动落了一份 config.json")
        try:
            with io.open(fake, "r", encoding="utf-8") as f:
                data = json.load(f)
            ok(isinstance(data, dict), "自动生成的配置是 dict")
            ok(data.get("ui_lang") in ("zh", "en"), "自动生成的配置带默认值 ui_lang",
               "-> %r" % (data.get("ui_lang") if isinstance(data, dict) else None))
        except Exception as e:
            ok(False, "自动生成的 config.json 能解析", "-> %s" % e)
    finally:
        cfgstore.CONFIG_PATH = old_path
        shutil.rmtree(tmpdir, ignore_errors=True)

    # ---- 3. 密钥不进 config.json ----
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5 import QtCore, QtGui, QtWidgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    import settings as S
    S.SettingsDialog._backend_check = lambda self: None
    dlg = S.SettingsDialog()
    try:
        sec_rows = [(sp, c) for sp, c in dlg.rows if sp.get("type") == "secret"]
        ok(len(sec_rows) == len(cfgstore.provider_order()),
           "每家厂商一个密钥输入框", "-> %d" % len(sec_rows))
        for sp, ctrl in sec_rows:
            ctrl.setText("SELFTESTKEY-123456")
        cfg, secrets = dlg._collect()
        ok(isinstance(secrets, dict), "_collect 返回 (cfg, secrets)")
        ok(len(secrets) == len(cfgstore.provider_order()),
           "所有密钥都收进了 secrets", "-> %d" % len(secrets))
        ok(secrets.get("minimax") == "SELFTESTKEY-123456",
           "密钥名对得上", "-> %r" % sorted(secrets))
        for sp, _c in sec_rows:
            ok(cfgstore.get_path(cfg, sp["key"]) is None,
               "config 里没有密钥项: %s" % sp["key"])
        blob = json.dumps(cfg, ensure_ascii=False)
        ok("SELFTESTKEY" not in blob, "config 序列化后不含密钥明文")
        for sp, ctrl in sec_rows:
            ctrl.clear()
    finally:
        try:
            dlg.hide()
        except Exception:
            pass

    # ---- 4. 新旋钮有默认值 ----
    ok(cfgstore.get_path(cfgstore.DEFAULTS, "caption_font_size") == 14, "字幕字号默认 14")
    ok(cfgstore.get_path(cfgstore.DEFAULTS, "glow_intensity") == 1.0, "边光亮度默认 1.0")
    ok(cfgstore.get_path(cfgstore.DEFAULTS, "show_tray_icon") is True, "默认显示托盘图标")
    ok("autostart" in cfgstore.DEFAULTS, "默认有开机自启项")
    ok(cfgstore.get_path(cfgstore.DEFAULTS, "sim_rate") == 4.2, "模拟语速默认 4.2")
    ok(cfgstore.get_path(cfgstore.DEFAULTS, "llm.enabled.minimax") is True, "默认 MiniMax 开")
    ok(cfgstore.get_path(cfgstore.DEFAULTS, "llm.enabled.deepseek") is True, "默认 DeepSeek 开")
    ok(cfgstore.get_path(cfgstore.DEFAULTS, "llm.enabled.zhipu") is True, "默认 智谱 开")
    ok("系统" in cfgstore.TABS, "有「系统」页签")
    ok(cfgstore.TAB_EN.get("系统") == "System", "「系统」有英文页签名")

    # ---- 5. 边光亮度真的乘进渲染 ----
    try:
        import numpy as _np
        import siri_orb
        ov = siri_orb.Overlay(dict(cfgstore.DEFAULTS))
        ov.resize(1440, 900)
        ov.state = "listening"
        ov.press_time = None
        try:
            ov._cfg_mtime = os.path.getmtime(cfgstore.CONFIG_PATH)
        except Exception:
            pass
        alphas = {}
        for cap in (1.0, 0.5):
            ov.cfg["glow_intensity"] = cap
            ov.glow = 1.0
            ov._cfg_checked = _time.time()
            ov._trig_checked = _time.time()
            ov._tick_body()
            img = ov.glow_img
            ok(img is not None, "glow_intensity=%.1f 渲染出了图" % cap)
            if img is None:
                continue
            img = img.convertToFormat(QtGui.QImage.Format_RGBA8888)
            ptr = img.bits()
            ptr.setsize(img.byteCount())
            arr = _np.frombuffer(ptr, dtype=_np.uint8).reshape(img.height(), img.width(), 4)
            alphas[cap] = float(arr[..., 3].mean())
        if 1.0 in alphas and 0.5 in alphas:
            ratio = alphas[0.5] / max(1e-6, alphas[1.0])
            ok(0.35 < ratio < 0.65, "边光亮度 0.5 -> 平均 alpha 约为一半",
               "-> ratio=%.3f (1.0=%.2f, 0.5=%.2f)" % (ratio, alphas[1.0], alphas[0.5]))
        else:
            ok(False, "边光亮度对比有数据")
    except Exception as e:
        ok(False, "边光亮度真的乘进渲染", "-> %s" % e)

    # ---- 6. 字幕字号真的进 QFont ----
    try:
        import siri_orb
        ov = siri_orb.Overlay(dict(cfgstore.DEFAULTS))
        ov.caption = "Hello, this is a test of the caption font size."
        ov.state = "listening"
        sizes = {}
        for fs in (14, 24):
            ov.cfg["caption_font_size"] = fs
            g = ov.caption_geom()
            sizes[fs] = g[-1].pointSize()
        ok(sizes.get(14) == 14, "字幕字号 14 -> QFont 14", "-> %r" % sizes)
        ok(sizes.get(24) == 24, "字幕字号 24 -> QFont 24", "-> %r" % sizes)
    except Exception as e:
        ok(False, "字幕字号真的进 QFont", "-> %s" % e)


def _check_model_picker():
    """模型选择器：8 家后端、清单全、下拉对得上、面板分组和友好名。

    有 key 的 3 家清单是**实测**的（服务端 /models + 逐个真实请求验活）；
    其余几家抄官方文档 —— 面板 hint 里写明了这一点，不假装验过。
    """
    import llm as llm_mod
    import providers as prov
    names = list(prov.ORDER)
    ok(len(names) == len(prov.PROVIDERS), "注册表家数 == ORDER 家数", "-> %d" % len(names))
    ok(hasattr(cfgstore, "MODEL_CHOICES"), "cfgstore 有 MODEL_CHOICES")
    ok(set(cfgstore.MODEL_CHOICES) == set(names), "每家服务商都有模型清单",
       "-> %s" % sorted(cfgstore.MODEL_CHOICES))
    total = 0
    for p in names:
        lst = cfgstore.MODEL_CHOICES[p]
        total += len(lst)
        ok(len(lst) >= 5, "%s 模型数 >= 5" % p, "-> %d" % len(lst))
        ok(len(set(lst)) == len(lst), "%s 清单无重复" % p)
        ok(all(isinstance(m, str) and m.strip() for m in lst),
           "%s 清单全是非空字符串" % p)
        d = prov.PROVIDERS[p].get("default_model")
        ok(d in lst, "%s 默认模型在清单里" % p, "-> %r" % d)
        kinds = sorted([s["type"] for s in cfgstore.SPEC if s.get("provider") == p])
        ok(kinds == ["bool", "choice", "secret", "text"],
           "%s 有 启用/密钥/模型/地址 四个控件" % p, "-> %s" % kinds)
    ok(total >= 90, "所有家加起来 >= 90 个模型", "-> %d" % total)

    for spec in cfgstore.SPEC:
        pname = spec.get("provider")
        if not pname or spec["type"] != "choice":
            continue
        ok(spec.get("choices") == cfgstore.MODEL_CHOICES.get(pname),
           "%s 下拉内容 == 清单" % spec["key"])
        ok(spec.get("editable") is True, "%s 允许手输清单外的模型" % spec["key"])
        d = cfgstore.get_path(cfgstore.DEFAULTS, spec["key"])
        ok(d in cfgstore.MODEL_CHOICES[pname], "%s 默认值在清单里" % spec["key"],
           "-> %r" % d)

    ok(hasattr(llm_mod, "list_models"), "llm 有 list_models()")
    ok(hasattr(llm_mod, "make_backend"), "llm 有 make_backend()")
    ok(set(getattr(llm_mod, "MODELS_URL", {})) == set(names),
       "llm 每家都有 /models 地址")
    try:
        models, err = llm_mod.list_models("deepseek")
        if models:
            ok(len(models) >= 2, "DeepSeek /models 拉到 >= 2 个模型", "-> %d" % len(models))
        else:
            ok(True, "DeepSeek /models 本次没拉到（离线可跳过）", "-> %s" % err)
    except Exception as e:
        ok(True, "list_models 调用异常（离线可跳过）", "-> %s" % e)

    # 面板上：8 个模型下拉 + 8 个「拉取最新」按钮 + 8 个厂商分组 + 友好名优先级
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5 import QtWidgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    import settings as S
    S.SettingsDialog._backend_check = lambda self: None
    # 断言按中文标签找东西 -> 必须**强制中文界面**，否则发布副本里
    # make_release_images 留下的 ui_lang=en 的 config.json 会让断言全挂。
    _orig_load = cfgstore.load

    def _load_zh(path=None):
        c = _orig_load(path)
        c["ui_lang"] = "zh"
        return c

    cfgstore.load = _load_zh
    try:
        dlg = S.SettingsDialog()
    finally:
        cfgstore.load = _orig_load
    try:
        want = i18n.t("zh", "settings.models.refresh")
        nbtn = len([b for b in dlg.findChildren(QtWidgets.QPushButton) if b.text() == want])
        ok(nbtn == len(names), "模型下拉旁每家一个「拉取最新」按钮", "-> %d" % nbtn)
        combos = 0
        for spec, ctrl in dlg.rows:
            pname = spec.get("provider")
            if not pname or spec["type"] != "choice":
                continue
            combos += 1
            got = [ctrl.itemText(i) for i in range(ctrl.count())]
            ok(got == cfgstore.MODEL_CHOICES[pname],
               "%s 下拉里的项 == 清单" % spec["key"], "-> %d 项" % len(got))
        ok(combos == len(names), "每家厂商一个模型下拉", "-> %d" % combos)

        titles = [g.title() for g in dlg.findChildren(QtWidgets.QGroupBox)]
        for pname in names:
            lab = cfgstore.provider_label(pname, "zh")
            ok(lab in titles, "面板上有「%s」分组" % lab)

        order_rows = [(sp, c) for sp, c in dlg.rows if sp.get("type") == "order"]
        ok(len(order_rows) == 1, "有 1 个后端优先级控件")
        if order_rows:
            _sp, ctrl = order_rows[0]
            texts = [ctrl.list.item(i).text() for i in range(ctrl.list.count())]
            vals = ctrl.value()
            ok(len(vals) == len(names), "优先级列表项数 == 厂商数", "-> %d" % len(vals))
            ok(set(vals) == set(names), "优先级存的是厂商代号", "-> %s" % vals)
            ok(texts != vals, "优先级显示的不是代号")
            for pname in names:
                ok(cfgstore.provider_label(pname, "zh") in texts,
                   "优先级里有 %s 的友好名" % pname)
    finally:
        try:
            dlg.hide()
        except Exception:
            pass


def _check_agent():
    """助理能力（操作权限）：配置 / 面板 / 工具 / 协议 都要对得上。"""
    import tools as toolmod
    import agent as agent_mod
    import llm as llm_mod

    # ---------------- 配置层
    a = cfgstore.DEFAULTS.get("agent")
    ok(isinstance(a, dict), "默认配置里有 agent 段")
    if isinstance(a, dict):
        ok(a.get("enabled") is False, "助理能力默认是关的")
        ok(a.get("allow_screen") is False, "鼠标键盘默认是关的")
        ok(a.get("allow_shell") is False, "执行命令默认是关的")
        ok("work_dir" in a, "agent 段有 work_dir")
    ok("助理" in cfgstore.TABS, "页签里有「助理」")
    ok(cfgstore.TAB_EN.get("助理") == "Assistant", "「助理」有英文页签名")
    agent_specs = [s for s in cfgstore.SPEC if s.get("tab") == "助理"]
    ok(len(agent_specs) == 6, "助理页有 6 个旋钮", "-> %d" % len(agent_specs))
    keys = sorted(s["key"] for s in agent_specs)
    want_keys = sorted(["agent.enabled", "agent.work_dir", "agent.allow_screen", "agent.file_roots",
                         "agent.allow_shell", "agent.max_rounds"])
    ok(keys == want_keys, "助理页的旋钮键名对", "-> %s" % keys)
    for s in agent_specs:
        ok(bool(s.get("label")) and bool(s.get("en_label")), "%s 有中英标签" % s["key"])
        ok(bool(s.get("hint")) and bool(s.get("en_hint")), "%s 有中英提示" % s["key"])

    # ---------------- 权限
    off = {"agent": {"enabled": True}}
    ok(toolmod.allowed(off, "file"), "开了总开关 -> 文件能用")
    ok(toolmod.allowed(off, "web"), "开了总开关 -> 网页能用")
    ok(toolmod.allowed(off, "image"), "开了总开关 -> 图片能用")
    ok(not toolmod.allowed(off, "screen"), "鼠标键盘默认还是关的")
    ok(not toolmod.allowed(off, "shell"), "执行命令默认还是关的")
    on = {"agent": {"enabled": True, "allow_screen": True, "allow_shell": True}}
    ok(toolmod.allowed(on, "screen"), "显式打开 -> 鼠标键盘能用")
    ok(toolmod.allowed(on, "shell"), "显式打开 -> 执行命令能用")
    ok(not toolmod.allowed({"agent": {"enabled": False}}, "file"),
       "总开关关着 -> 什么都不能用")
    ok(not toolmod.allowed({"agent": {"enabled": False}}, "location"),
       "总开关关着 -> 位置工具也不能用")
    ok(toolmod.allowed({"agent": {"enabled": True}, "ctx": {"location": True}},
                        "location"), "两把钥匙都开 -> 位置工具能用")
    ok(not toolmod.allowed({"agent": {"enabled": True}, "ctx": {"location": False}},
                            "location"), "位置开关关着 -> 位置工具也不能用")

    # ---------------- 文件工具锁死工作目录
    cfg = {"agent": {"enabled": True, "work_dir": ""}}
    base = toolmod.work_dir(cfg)
    ok(base.endswith(os.path.join("Documents", "Siri-OS")),
       "默认工作目录是 我的文档/Siri-OS", "-> %s" % base)
    try:
        toolmod._safe_path(cfg, os.path.join("C:\\", "Windows", "System32", "cmd.exe"))
        ok(False, "越界路径必须被拒绝")
    except toolmod.ToolError:
        ok(True, "越界路径被拒绝")
    inside = toolmod._safe_path(cfg, "note.txt")
    ok(inside.startswith(base), "工作目录内的路径放行", "-> %s" % inside)

    # ---------------- 工具清单
    ok(len(toolmod.TOOLS) == 29, "工具一共 29 个", "-> %d" % len(toolmod.TOOLS))
    tnames = [t["name"] for t in toolmod.TOOLS]
    ok(len(set(tnames)) == len(tnames), "工具名不重复")
    for cat in toolmod.CATEGORIES:
        ok(any(t["category"] == cat for t in toolmod.TOOLS), "有 %s 类工具" % cat)
    spec = toolmod.spec_for(off)
    ok(all(s["function"]["name"] in tnames for s in spec), "给模型的清单里没有野名字")
    ok(len(toolmod.spec_for({"agent": {"enabled": False}})) == 0,
       "总开关关着 -> 给模型的清单是空的")
    ok(not any(s["function"]["name"] == "mouse_click" for s in toolmod.spec_for(off)),
       "鼠标键盘没开 -> 不出现在给模型的清单里")

    # ---------------- 协议层
    for cls in (llm_mod.OpenAIBackend, llm_mod.ClaudeBackend, llm_mod.MinimaxBackend):
        ok(hasattr(cls, "chat_tools"), "%s 有 chat_tools()" % cls.__name__)
    ok(hasattr(llm_mod.LLM, "chat_tools"), "LLM 有 chat_tools()")
    r1 = llm_mod._parse_openai_tool_reply({
        "choices": [{"message": {"content": "hi", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "read_file",
                          "arguments": "{\"path\": \"a.txt\"}"}}]}}]})
    ok(r1[0] == "hi", "OpenAI 协议能解析出正文")
    ok(bool(r1[1]) and r1[1][0]["name"] == "read_file", "OpenAI 协议能解析出工具调用")
    ok(r1[1][0]["arguments"].get("path") == "a.txt", "工具参数被解析成字典")
    r2 = llm_mod._parse_anthropic_tool_reply({
        "content": [{"type": "text", "text": "hey"},
                    {"type": "tool_use", "id": "t1", "name": "list_files", "input": {}}]})
    ok(r2[0] == "hey", "Anthropic 协议能解析出正文")
    ok(bool(r2[1]) and r2[1][0]["name"] == "list_files", "Anthropic 协议能解析出 tool_use")
    ok(r2[1][0]["arguments"] == {}, "tool_use 的 input 是字典")

    # ---------------- agent 模块
    ok(hasattr(agent_mod, "run_agent"), "agent 有 run_agent()")
    ok(agent_mod.run_agent({"agent": {"enabled": False}}, None, "hi") is None,
       "助理没开 -> run_agent 返回 None（回落到普通聊天）")
    cap = agent_mod.build_capability_prompt({"agent": {"enabled": True}})
    ok(bool(cap), "能生成能力说明")
    ok("工具" in cap, "能力说明里提到工具")


def _check_context():
    """上下文：对话记忆 + 电脑状态。"""
    import context as ctx_mod
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5 import QtWidgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    import settings as S
    S.SettingsDialog._backend_check = lambda self: None
    S.SettingsDialog._do_save = lambda self: None      # 不写活配置

    # ---------------- 配置层
    c = cfgstore.DEFAULTS.get("ctx")
    ok(isinstance(c, dict), "默认配置里有 ctx 段")
    if isinstance(c, dict):
        ok("enabled" in c, "ctx 段有 enabled")
        ok("history_turns" in c, "ctx 段有 history_turns")
        ok("sysinfo" in c, "ctx 段有 sysinfo")
        ok("location" in c, "ctx 段有 location")
        ok("location_ttl_min" in c, "ctx 段有 location_ttl_min")
    ok("上下文" in cfgstore.TABS, "页签里有「上下文」")
    ok(cfgstore.TAB_EN.get("上下文") == "Context", "「上下文」有英文页签名")
    specs = [s for s in cfgstore.SPEC if s.get("tab") == "上下文"]
    ok(len(specs) == 5, "上下文页有 5 个旋钮", "-> %d" % len(specs))
    for s in specs:
        ok(bool(s.get("label")) and bool(s.get("en_label")), "%s 有中英标签" % s["key"])
        ok(bool(s.get("hint")) and bool(s.get("en_hint")), "%s 有中英提示" % s["key"])

    # ---------------- 对话记忆
    conv = ctx_mod.Conversation()
    conv.add_turn("问一", "答一")
    conv.add_turn("问二", "答二")
    ok(len(conv.history(6)) == 4, "两轮 = 4 条消息", "-> %d" % len(conv.history(6)))
    ok(len(conv.history(1)) == 2, "只记 1 轮 = 2 条")
    ok(ctx_mod.is_clear_command("清空上下文"), "认得清空指令")
    ok(not ctx_mod.is_clear_command("把那个改短点"), "普通句子不误触发")

    # ---------------- 交替整形（Anthropic 要求）
    n = ctx_mod.normalize_roles([{"role": "user", "content": "a"},
                                 {"role": "user", "content": "b"},
                                 {"role": "assistant", "content": "c"}])
    ok(len(n) == 2, "连续两条 user 被合并", "-> %d" % len(n))

    # ---------------- 电脑状态
    cfg = {"ctx": {"enabled": True, "history_turns": 6, "sysinfo": True,
                   "location": True, "location_ttl_min": 30},
           "agent": {"enabled": True, "work_dir": ""}}
    s1 = ctx_mod.system_context(cfg)
    ok(bool(s1), "能生成电脑状态块")
    ok("屏幕分辨率" in s1 or "工作目录" in s1, "状态块里有屏幕/目录信息")
    ok(ctx_mod.system_context({"ctx": {"sysinfo": False}}) == "",
       "关掉 sysinfo 就不生成状态块")
    sp = ctx_mod.compose_system(cfg, extra="能力说明")
    ok("能力说明" in sp, "compose_system 把附加内容拼进去了")

    # ---------------- 大致位置（区级）
    ok(ctx_mod.location_enabled({"ctx": {"location": True}}), "位置开关默认开")
    ok(not ctx_mod.location_enabled({"ctx": {"location": False}}), "位置开关能关")
    ok(ctx_mod.location_context({"ctx": {"location": False}}) == "",
       "关掉位置就不带位置")
    ok(ctx_mod.location_context(cfg) == "",
       "没有缓存时位置块是空的（不阻塞、不联网）")

    # ---------------- 面板上真的有这一页
    dlg = S.SettingsDialog()
    try:
        ok("上下文" in dlg._tab_widgets, "面板上有「上下文」页签")
        rows = [(sp2, ctrl) for sp2, ctrl in dlg.rows if sp2.get("tab") == "上下文"]
        ok(len(rows) == 5, "面板「上下文」页有 5 个控件", "-> %d" % len(rows))
    finally:
        try:
            dlg.hide()
        except Exception:
            pass


def _check_auto_enable():
    """填了某家厂商的 key -> 那一家的「启用」自动打开。

    ⚠ 测试时把 config.json 指向临时目录、把密钥库写入换成空操作 ——
       不能拿真密钥库做实验。
    """
    import shutil
    import tempfile
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5 import QtWidgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    import settings as S
    import keys as keylib
    S.SettingsDialog._backend_check = lambda self: None
    dlg = S.SettingsDialog()
    tmpdir = tempfile.mkdtemp(prefix="siri_cfg_")
    old_path = cfgstore.CONFIG_PATH
    old_set = keylib.set_key
    try:
        cfgstore.CONFIG_PATH = os.path.join(tmpdir, "config.json")
        keylib.set_key = lambda n, v: None
        rows = dict((sp["key"], c) for sp, c in dlg.rows if sp.get("type") == "secret")
        ctrl = rows.get("api_key.claude")
        ok(ctrl is not None, "面板上有 Claude 的密钥输入框")
        if ctrl is None:
            return
        chk = None
        for sp, c in dlg.rows:
            if sp.get("key") == "llm.enabled.claude" and sp.get("type") == "bool":
                chk = c
        ok(chk is not None, "面板上有 Claude 的启用开关")
        if chk is None:
            return
        ok(chk.isChecked() is False, "Claude 默认是关的")
        ctrl.setText("SELFTEST-KEY-123456")
        dlg._do_save()
        ok(chk.isChecked() is True, "填了 key 之后「启用」自动打开")
        cfg = cfgstore.load(cfgstore.CONFIG_PATH)
        ok(cfgstore.get_path(cfg, "llm.enabled.claude") is True,
           "config.json 里 Claude 也被启用了")
        ok(cfgstore.get_path(cfg, "api_key.claude") is None,
           "密钥没写进 config.json")
    finally:
        keylib.set_key = old_set
        cfgstore.CONFIG_PATH = old_path
        shutil.rmtree(tmpdir, ignore_errors=True)
        try:
            dlg.hide()
        except Exception:
            pass

def main():
    # ---- 守卫：活配置不许被自检改动 ----
    live_path = cfgstore.CONFIG_PATH
    try:
        with io.open(live_path, "rb") as f:
            live_before = f.read()
    except Exception:
        live_before = None
    violations = []
    _orig_save = cfgstore.save

    def _safe_save(cfg, path=None):
        target = path or cfgstore.CONFIG_PATH
        if target == live_path:
            violations.append(target)
            return True
        return _orig_save(cfg, path)

    cfgstore.save = _safe_save
    sec("1. SPEC 字段完整性（每个旋钮都要有英文）")
    for spec in cfgstore.SPEC:
        key = spec.get("key")
        ok(bool(spec.get("label")), "label 存在: %s" % key)
        ok(bool(spec.get("en_label")), "en_label 存在: %s" % key)
        ok(bool(spec.get("en_hint")), "en_hint 存在: %s" % key)
        if spec.get("unit"):
            ok(bool(spec.get("en_unit")), "en_unit 存在: %s" % key)

    sec("2. 每个旋钮都能在 DEFAULTS 里找到默认值")
    for spec in cfgstore.SPEC:
        key = spec["key"]
        if spec.get("type") == "secret":
            continue          # 密钥在独立密钥库里，本来就不该进 DEFAULTS
        v = cfgstore.get_path(cfgstore.DEFAULTS, key)
        ok(v is not None, "DEFAULTS 有 %s" % key, "-> %r" % (v,))

    sec("2b. 密钥条目：只在密钥库，不进 DEFAULTS")
    for spec in cfgstore.SPEC:
        if spec.get("type") != "secret":
            continue
        key = spec["key"]
        ok(bool(spec.get("secret_name")), "secret 条目有 secret_name: %s" % key)
        ok(cfgstore.get_path(cfgstore.DEFAULTS, key) is None,
           "DEFAULTS 里没有密钥项: %s" % key)
    ok(not any(str(k).startswith("api_key") for k in cfgstore.DEFAULTS),
       "DEFAULTS 顶层没有 api_key 键")

    sec("3. 页签名中英齐全")
    for tab in cfgstore.TABS:
        ok(tab in cfgstore.TAB_EN, "TAB_EN 有 %s" % tab)
        ok(bool(cfgstore.TAB_EN.get(tab)), "TAB_EN[%s] 非空" % tab)

    sec("4. 界面文案中英不同（不能漏翻）")
    for key, pair in i18n.STRINGS.items():
        ok(len(pair) == 2, "STRINGS[%s] 是二元组" % key)
        ok(pair[0] and pair[1], "STRINGS[%s] 两边都非空" % key)
        ok(pair[0] != pair[1], "STRINGS[%s] 中英不同" % key)

    sec("5. 状态文字中英齐全")
    for st in ("idle", "listening", "thinking", "speaking"):
        ok(st in i18n.STATE_TEXT, "STATE_TEXT 有 %s" % st)
    ok(i18n.state_text("zh", "listening") == "聆听中", "zh 状态字正确")
    ok(i18n.state_text("en", "listening") == "Listening", "en 状态字正确")
    ok(i18n.state_text("en", "thinking") == "Thinking", "en thinking 正确")
    ok(i18n.state_text("en", "speaking") == "Speaking", "en speaking 正确")

    sec("6. system_prompt：语言中性 + 按回复语言追加规则")
    base_cfg = {"system_prompt": "你是助手。", "reply_lang": "zh"}
    p_zh = i18n.build_system_prompt(base_cfg)
    p_en = i18n.build_system_prompt({"system_prompt": "你是助手。", "reply_lang": "en"})
    p_both = i18n.build_system_prompt({"system_prompt": "你是助手。", "reply_lang": "both"})
    ok("你是助手。" in p_zh, "zh 保留用户提示词")
    ok("你是助手。" in p_en, "en 保留用户提示词")
    ok("你是助手。" in p_both, "both 保留用户提示词")
    ok("用中文回答。" in p_zh, "zh 追加中文规则")
    ok("Reply in English." in p_en, "en 追加英文规则")
    ok("中文" in p_both and "English" in p_both, "both 追加双语规则")
    ok("中文回答" not in p_en, "en 规则里没有中文要求")
    ok("Reply in English" not in p_zh, "zh 规则里没有英文要求")
    ok(i18n.reply_lang({"reply_lang": "en"}) == "en", "reply_lang 读取 en")
    ok(i18n.reply_lang({"reply_lang": "both"}) == "both", "reply_lang 读取 both")
    ok(i18n.reply_lang({}) == "zh", "reply_lang 默认 zh")
    ok(i18n.ui_lang({"ui_lang": "en"}) == "en", "ui_lang 读取 en")
    ok(i18n.ui_lang({}) == "zh", "ui_lang 默认 zh")

    sec("7. 本地指令：中/英/对照三种说法")
    zh = voice_mod.try_local_command("今天几号", lang="zh")
    en = voice_mod.try_local_command("what date is it", lang="en")
    both = voice_mod.try_local_command("今天几号", lang="both")
    ok(zh and zh.startswith("今天是"), "zh 日期指令", "-> %r" % zh)
    ok(en and "Today is" in en, "en 日期指令", "-> %r" % en)
    ok(both and "中文：" in both and "English:" in both, "both 两段都有", "-> %r" % both)
    ok(i18n.has_cjk(zh or ""), "zh 段含汉字")
    ok(not i18n.has_cjk(en or ""), "en 段不含汉字")

    t_zh = voice_mod.try_local_command("现在几点", lang="zh")
    t_en = voice_mod.try_local_command("what time is it", lang="en")
    ok(t_zh and "点" in t_zh, "zh 时间指令", "-> %r" % t_zh)
    ok(t_en and ("AM" in t_en or "PM" in t_en), "en 时间指令带 AM/PM", "-> %r" % t_en)

    ok(voice_mod.try_local_command("今天几号", lang="en") is None,
       "en 模式不认中文指令（交给模型）")
    ok(voice_mod.try_local_command("what date is it", lang="zh") is None,
       "zh 模式不认英文指令（交给模型）")
    ok(voice_mod.try_local_command("", lang="zh") is None, "空串返回 None")

    sec("8. 中英混排拆分")
    zh_part, en_part = i18n.split_bilingual("中文：你好\n\nEnglish: Hello there")
    ok("你好" in zh_part, "拆出中文段", "-> %r" % zh_part)
    ok("Hello" in en_part, "拆出英文段", "-> %r" % en_part)
    ok(i18n.looks_bilingual("中文：你好\n\nEnglish: Hello there"),
       "带「中文：/ English:」标记的是真双语")
    ok(not i18n.looks_bilingual("第一步，先把目录建出来\nStep 2: run the command"),
       "混排的多步骤回答不算真双语（要整段念）")
    ok(not i18n.looks_bilingual("纯中文的一段回答。"), "纯中文不算双语")

    sec("9. 听写语言：auto 展开成两种、崩溃不重试")
    i18n.set_last_stt_lang("en-US")
    ok(i18n.stt_langs({"stt": {"language": "auto"}}) == ["en-US", "zh-CN"],
       "auto 优先上次成功的语言")
    i18n.set_last_stt_lang("zh-CN")
    ok(i18n.stt_langs({"stt": {"language": "auto"}}) == ["zh-CN", "en-US"],
       "auto 默认先中文")
    ok(i18n.stt_langs({"stt": {"language": "en-US"}}) == ["en-US"],
       "指定语言只有一种")
    ok(i18n.stt_langs({"stt": {"language": "zh-CN"}}) == ["zh-CN"],
       "指定中文只有一种")

    sec("10. 配置文件读写（原子写 + 新键齐全）")
    cfg = cfgstore.load()
    ok(cfg.get("ui_lang") in ("zh", "en"), "config 里有 ui_lang", "-> %r" % cfg.get("ui_lang"))
    ok(cfg.get("reply_lang") in ("zh", "en", "both"), "config 里有 reply_lang",
       "-> %r" % cfg.get("reply_lang"))
    ok((cfg.get("stt") or {}).get("language") in ("auto", "zh-CN", "en-US"),
       "config 里 stt.language 合法",
       "-> %r" % (cfg.get("stt") or {}).get("language"))
    ok("中文" not in (cfg.get("system_prompt") or ""),
       "system_prompt 是语言中性的",
       "-> %r" % (cfg.get("system_prompt") or "")[:60])

    sec("11. 播报声线自动切换（只切换，不出声）")
    sp = voice_mod.Speaker(voice_hint="Huihui")
    d0 = sp._desc()
    ok("Chinese" in d0, "默认声线是中文", "-> %s" % d0)
    sp._pick_by_lang("en")
    d1 = sp._desc()
    ok("English" in d1, "切到英文 -> 英文声线", "-> %s" % d1)
    sp._pick_by_lang("zh")
    d2 = sp._desc()
    ok("Chinese" in d2, "切回中文 -> 中文声线", "-> %s" % d2)

    sec("12. 切换界面语言后旧控件不能残留（2026-11-07 修的残影 bug）")
    try:
        ok(_check_rebuild(), "重建后控件数不增长（没有新旧按钮叠在一起）")
    except Exception as e:
        ok(False, "重建回归检查跑起来了", "-> %s" % e)

    sec("13. 面板扩容的新功能回归（后端过滤 / 首次运行 / 密钥隔离 / 新旋钮）")
    try:
        _check_new_features()
    except Exception as e:
        ok(False, "新功能回归检查跑起来了", "-> %s" % e)

    sec("14. 模型选择器（多家后端 / 清单 / 下拉 / 分组 / 友好名）")
    try:
        _check_model_picker()
    except Exception as e:
        ok(False, "模型选择器回归检查跑起来了", "-> %s" % e)

    sec("15. 填了 key 自动启用该厂商")
    try:
        _check_auto_enable()
    except Exception as e:
        ok(False, "自动启用回归检查跑起来了", "-> %s" % e)

    sec("16. 助理能力（操作权限 / 工具 / 协议）")
    try:
        _check_agent()
    except Exception as e:
        ok(False, "助理能力回归检查跑起来了", "-> %s" % e)

    sec("17. 上下文（对话记忆 + 电脑状态）")
    try:
        _check_context()
    except Exception as e:
        ok(False, "上下文回归检查跑起来了", "-> %s" % e)

    # ---- 守卫：自检**不许改活配置** ----
    # 面板一保存就会覆盖真配置；自检里造了面板就必须挡住。
    # 之前就出过「自检跑完，agent 三个开关莫名变成 True」这类事。
    try:
        cfgstore.save = _orig_save
    except Exception:
        pass
    try:
        with io.open(live_path, "rb") as f:
            live_after = f.read()
    except Exception:
        live_after = live_before
    ok(live_after == live_before, "自检期间活配置一个字都没被改",
       "-> %d 次写活配置" % len(violations))
    if violations:
        for v in violations[:5]:
            print("       写活配置: %s" % v)

    print("\n%d checks, %d FAIL" % (CHECKS[0], len(FAILS)))
    if FAILS:
        for f in FAILS:
            print("  - %s" % f)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
