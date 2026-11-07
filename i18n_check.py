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
        ok(len(sec_rows) == 3, "面板上有 3 个密钥输入框", "-> %d" % len(sec_rows))
        for sp, ctrl in sec_rows:
            ctrl.setText("SELFTESTKEY-123456")
        cfg, secrets = dlg._collect()
        ok(isinstance(secrets, dict), "_collect 返回 (cfg, secrets)")
        ok(len(secrets) == 3, "三个密钥都收进了 secrets", "-> %d" % len(secrets))
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


def main():
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

    print("\n%d checks, %d FAIL" % (CHECKS[0], len(FAILS)))
    if FAILS:
        for f in FAILS:
            print("  - %s" % f)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
