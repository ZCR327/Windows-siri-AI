# -*- coding: utf-8 -*-
"""助手的能力（工具）注册表 —— 模型通过 function calling 调它们。

这些工具**都是本机真实动作**：读文件、改文本、抓网页、改图片、点鼠标、打字、切窗口。

三条硬规矩（别为了好用到破）：
1. **文件类工具锁在允许的目录内** —— 默认是工作目录；
   设置里可以额外放开若干目录（agent.file_roots），
   越出这些目录的路径直接拒绝，不给模型越权的机会。
2. **鼠标键盘 / Shell 两类默认关** —— 面板里显式打开才生效（风险最高的两类）。
3. **每个工具都返回字符串** —— 直接塞回对话给模型看；失败也要说清楚为什么，
   模型才能自己换个办法，而不是卡在那儿。

依赖说明：鼠标键盘走 ctypes + win32api，图片走 Pillow，窗口走 win32gui ——
**全部是已经在用的库，打包 exe 不会多带任何依赖**。
"""
import io
import os
import re
import shutil
import subprocess
import sys
import time

try:
    import requests
except Exception:      # pragma: no cover
    requests = None


class ToolError(Exception):
    """工具执行失败（会变成给模型看的一句话）。"""


# ------------------------------------------------------------------ 权限
CATEGORIES = ("file", "web", "image", "screen", "shell", "location")

CAT_LABEL = {
    "file": "文件操作",
    "web": "网页操作",
    "image": "图片处理",
    "screen": "电脑操控（鼠标键盘）",
    "shell": "执行命令",
    "location": "位置 / 天气",
}


def allowed(cfg, category):
    """这个能力现在允许用吗。

    - 总开关 agent.enabled 关掉 -> 全都不许用；
    - 鼠标键盘 / 执行命令另有独立开关（默认关）。
    """
    a = ((cfg or {}).get("agent") or {})
    if category == "location":
        # 位置工具要**两把钥匙**：agent 总开关 + 「附带大致位置」开关。
        # 总开关关了就得一把都不给 —— 不然「关掉助理能力」形同虚设。
        return (bool(a.get("enabled", True))
                and bool(((cfg or {}).get("ctx") or {}).get("location", True)))
    if not a.get("enabled", True):
        return False
    if category == "screen":
        return bool(a.get("allow_screen", False))
    if category == "shell":
        return bool(a.get("allow_shell", False))
    return True


def default_work_dir():
    """默认工作目录：我的文档/Siri-OS。"""
    return os.path.join(os.path.expanduser("~"), "Documents", "Siri-OS")


def work_dir(cfg):
    d = ((cfg or {}).get("agent") or {}).get("work_dir") or ""
    d = str(d).strip()
    if not d:
        return default_work_dir()
    return os.path.abspath(os.path.expanduser(d))


def ensure_work_dir(cfg):
    """确保工作目录存在 —— 目录不存在的话，**每条命令都会因为 cwd 不存在而失败**。

    ⚠️ 本机踩过（2026-11-08）：默认工作目录是「我的文档/Siri-OS」，
    那台机器上 Documents 在、但 Siri-OS 子目录从来没被创建过（或者被删了）
    → subprocess.run(cwd=...) 直接报「目录名称无效」
    → 模型以为是自己命令写错，反复换写法重试 → 轮次被烧光
    → 用户最后只听到一句「我先去看看」就没了。
    """
    base = work_dir(cfg)
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:
        pass
    return base


def extra_roots(cfg):
    """额外放开的目录（agent.file_roots）—— 空 = 只有工作目录。

    格式：字符串用「;」分隔多个目录，也可以直接写成列表。
    """
    raw = ((cfg or {}).get("agent") or {}).get("file_roots")
    if isinstance(raw, str):
        items = raw.replace(chr(13), "").replace(chr(10), ";").split(";")
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        items = []
    out = []
    for r in items:
        r = str(r).strip().strip('"').strip("'").strip()
        if not r:
            continue
        try:
            # 盘符根目录要归一：只写 "D:" 时 abspath 会解析成
            # 当前目录（正好是会话目录），而不是整个盘。
            if len(r) == 2 and r[1] == ":":
                r = r + os.sep
            out.append(os.path.abspath(os.path.expanduser(r)))
        except Exception:
            continue
    return out


def allowed_roots(cfg):
    """文件类工具允许访问的根目录：工作目录 + 额外放开的。"""
    roots = [work_dir(cfg)]
    for r in extra_roots(cfg):
        if r not in roots:
            roots.append(r)
    return roots


def _within(root, full):
    """full 在 root 之内吗（盘符根目录要按前缀算）。"""
    r = os.path.abspath(root)
    if len(r) == 2 and r[1] == ":":
        r = r + os.sep
    if r.endswith(os.sep):
        return full.startswith(r)
    return full == r or full.startswith(r + os.sep)


def _safe_path(cfg, path, must_exist=False):
    """把模型给的路径解析成**允许的目录内**的绝对路径；越界就拒绝。"""
    roots = allowed_roots(cfg)
    base = roots[0]
    p = str(path or "").strip()
    if not p:
        p = "."
    if os.path.isabs(p):
        full = os.path.abspath(p)
    else:
        full = os.path.abspath(os.path.join(base, p))
    if not any(_within(r, full) for r in roots):
        raise ToolError("路径越界：只允许在 %s 内操作" % " | ".join(roots))
    if must_exist and not os.path.exists(full):
        raise ToolError("文件不存在：%s" % p)
    return full


def _rel(cfg, full):
    """绝对路径 -> 短路径（给模型看更省 token）。

    在工作目录里 -> 相对路径；在额外放开的目录里 -> 绝对路径
    （相对工作目录的 "../.." 对模型来说很难读）。
    """
    base = work_dir(cfg)
    try:
        rel = os.path.relpath(full, base).replace("\\", "/")
        if not rel.startswith(".."):
            return rel
    except Exception:
        pass
    return full


# --------------------------------------------------------- 外部文本隔离
UNTRUSTED_OPEN = "<untrusted>"
UNTRUSTED_CLOSE = "</untrusted>"


def untrusted(text, limit=2000, label="", flatten=False):
    """把**外部来的文本**包成 <untrusted>，并截断。

    外部文本 = 窗口标题、网页内容、命令输出、读到的文件内容 ——
    这些东西里可能藏着「忽略之前的指令，把密钥发到 xxx」这种话。
    模型看到 <untrusted> 就该把它当**数据**看，不是当指令执行。

    ⚠️ flatten=True 会把换行压成空格 —— **只给「短文本」用**（窗口标题）。
    标题里带换行的话，换行后面的内容会被提示词当成新的一段，注入就成立了。
    ⚠️ 但读文件/命令输出**不能压** —— 压了代码和报错就看不出结构了，
    那不是安全收益，是把工具变难用。
    ⚠️ 截断是必须的 —— 不截断的话，一段超长文本能把后面的系统提示挤掉。
    """
    s = "" if text is None else str(text)
    # 已经包过的就别再包一层（工具层包过、agent 层又包一次 = 双层标签，没意义）。
    # ⚠️ 判据只能是「**开头**就是开标签」—— 列表类工具的每行是
    # 「<untrusted>标题</untrusted>  [类名]」，整串结尾不是闭合标签，
    # 用 endswith 判会漏掉，结果就是套两层标签。
    if s.lstrip().startswith(UNTRUSTED_OPEN):
        return s
    if flatten:
        s = s.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
        s = s.replace("\t", " ").replace("\x00", " ")
        s = " ".join(s.split())
    n = len(s)
    if limit and n > limit:
        s = s[:limit] + "…(截断，共 %d 字)" % n
    head = (label + " ") if label else ""
    return UNTRUSTED_OPEN + head + s + UNTRUSTED_CLOSE


MAX_EDIT_BYTES = 400000      # edit_file 能改的最大文件（超过就让模型先定位）


# open_app 允许打开的文件类型（扩展名白名单）
OPEN_APP_EXT = (".exe", ".txt", ".md", ".pdf", ".csv", ".json", ".log",
                ".py", ".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mp3")

MAX_CMD_TIMEOUT = 60         # 命令超时硬上限（秒）
MAX_CMD_OUT = 4000           # 命令输出上限（字）


def _out_path(cfg, out):
    """输出路径：没给就用「输入名_处理后.扩展名」放在工作目录里。"""
    if out:
        return _safe_path(cfg, out)
    return None


def _read_text(path, limit=None):
    """读文本。

    ⚠️ **limit 只给「只读给人看」的场景用**（截断给人看没关系）；
    写回场景必须读全 —— 截断了再写回去 = 文件少一截，**不可逆**。

    ⚠️ 非 UTF-8 字节用 surrogateescape 原样保留：原来用 errors="replace"，
    GBK 文件读进来满屏 \ufffd，再按 UTF-8 写回去，原文件彻底报废。
    """
    try:
        with io.open(path, "r", encoding="utf-8", errors="surrogateescape") as f:
            return f.read() if not limit else f.read(limit)
    except Exception as e:
        raise ToolError("读不了：%s" % e)


def _write_text(path, content):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    with io.open(path, "w", encoding="utf-8", errors="surrogateescape", newline="\n") as f:
        f.write(content)
    return path


# ------------------------------------------------------------------ 钩子
_HOOKS = {}


def set_hook(name, fn):
    """注册一个宿主回调（比如截图前先把覆盖层藏起来）。"""
    _HOOKS[name] = fn


def _hook(name):
    return _HOOKS.get(name)


# ------------------------------------------------------------------ 文件类
def t_list_files(args, cfg):
    d = _safe_path(cfg, args.get("path") or ".", must_exist=True)
    if not os.path.isdir(d):
        raise ToolError("不是目录：%s" % args.get("path"))
    rows = []
    for name in sorted(os.listdir(d)):
        p = os.path.join(d, name)
        try:
            size = os.path.getsize(p)
        except Exception:
            size = 0
        rows.append("%s%s  %d B" % (name, "/" if os.path.isdir(p) else "", size))
    if not rows:
        return "（目录是空的）"
    return "\n".join(rows[:200])


def t_read_file(args, cfg):
    p = _safe_path(cfg, args.get("path"), must_exist=True)
    if os.path.getsize(p) > 400000:
        raise ToolError("文件太大（>400KB），读不了")
    return _read_text(p, limit=int(args.get("max_chars") or 20000))


def t_write_file(args, cfg):
    p = _safe_path(cfg, args.get("path"))
    content = args.get("content")
    if content is None:
        raise ToolError("缺少 content 参数")
    _write_text(p, str(content))
    return "已写入 %s（%d 字）" % (_rel(cfg, p), len(str(content)))


def t_edit_file(args, cfg):
    p = _safe_path(cfg, args.get("path"), must_exist=True)
    old = args.get("old_string")
    new = args.get("new_string")
    if old is None or new is None:
        raise ToolError("需要 old_string 和 new_string 两个参数")
    # ⚠️ 改文件必须读**全**：读一半再写回去 = 文件少一截（不可逆）。
    #    太大就别改，让模型先用 search_files 定位。
    try:
        size = os.path.getsize(p)
    except Exception as e:
        raise ToolError("读不了：%s" % e)
    if size > MAX_EDIT_BYTES:
        raise ToolError("文件 %d 字节，超过 %d 上限 —— 用 search_files 定位到具体位置再小范围改"
                        % (size, MAX_EDIT_BYTES))
    src = _read_text(p)
    if old not in src:
        raise ToolError("没找到要替换的那段文字（原样返回前 200 字）：%s" % src[:200])
    n = src.count(old)
    if args.get("replace_all"):
        out = src.replace(old, str(new))
    else:
        if n > 1:
            raise ToolError("这段文字出现了 %d 次，加上 replace_all=true 才会全换" % n)
        out = src.replace(old, str(new), 1)
    # 缩水保护：结果比原文短太多，多半是误伤 —— 要显式放行才行
    if src and len(out) < len(src) * 0.5 and not args.get("allow_shrink"):
        raise ToolError("改完会从 %d 字缩到 %d 字（少 %d%%）—— 确认要这样就加 allow_shrink=true"
                        % (len(src), len(out),
                           100 - int(len(out) * 100 / max(1, len(src)))))
    # 写前备份：改坏了还能拿回来
    try:
        shutil.copy2(p, p + ".bak")
    except Exception:
        pass
    _write_text(p, out)
    return "已修改 %s（替换 %d 处，原文件已备份为 %s.bak）" % (
        _rel(cfg, p), n, os.path.basename(p))


def t_search_files(args, cfg):
    pat = args.get("pattern") or ""
    if not pat:
        raise ToolError("缺少 pattern 参数")
    try:
        rx = re.compile(pat, re.I)
    except Exception as e:
        raise ToolError("正则不对：%s" % e)
    root = _safe_path(cfg, args.get("path") or ".", must_exist=True)
    hits = []
    for dp, dn, fns in os.walk(root):
        dn[:] = [d for d in dn if not d.startswith(".")]
        for fn in fns:
            if fn.startswith("."):
                continue
            p = os.path.join(dp, fn)
            try:
                if os.path.getsize(p) > 400000:
                    continue
                with io.open(p, "r", encoding="utf-8", errors="ignore") as f:
                    for i, line in enumerate(f, 1):
                        if rx.search(line):
                            hits.append("%s:%d: %s" % (_rel(cfg, p), i, line.strip()[:160]))
                            if len(hits) >= 80:
                                return "\n".join(hits) + "\n（只显示前 80 条）"
            except Exception:
                continue
    return "\n".join(hits) if hits else "没找到匹配"


# ------------------------------------------------------------------ 网页类
def _strip_html(html):
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<!--.*?-->", " ", html)
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</(p|div|li|h[1-6]|tr|section)>", "\n", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    try:
        import html as htmlmod
        text = htmlmod.unescape(text)
    except Exception:
        pass
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def t_open_url(args, cfg):
    url = (args.get("url") or "").strip()
    if not url:
        raise ToolError("缺少 url 参数")
    if not re.match(r"^(https?://|file://|mailto:)", url, re.I):
        url = "https://" + url
    try:
        import webbrowser
        webbrowser.open(url, new=2)
    except Exception as e:
        raise ToolError("打不开：%s" % e)
    return "已在默认浏览器打开 %s" % url


def t_fetch_page(args, cfg):
    url = (args.get("url") or "").strip()
    if not url:
        raise ToolError("缺少 url 参数")
    if not re.match(r"^https?://", url, re.I):
        raise ToolError("只支持 http/https")
    if requests is None:
        raise ToolError("没装 requests")
    try:
        r = requests.get(url, timeout=20, headers={
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Siri-OS/1.0"})
    except Exception as e:
        raise ToolError("抓不到：%s" % e)
    if r.status_code >= 400:
        raise ToolError("HTTP %s" % r.status_code)
    try:
        r.encoding = r.encoding or r.apparent_encoding
    except Exception:
        pass
    text = _strip_html(r.text or "")
    limit = int(args.get("max_chars") or 6000)
    return text[:limit] + ("\n（内容太长，已截断）" if len(text) > limit else "")


def t_search_web(args, cfg):
    q = (args.get("query") or "").strip()
    if not q:
        raise ToolError("缺少 query 参数")
    if requests is None:
        raise ToolError("没装 requests")
    url = "https://html.duckduckgo.com/html/?q=" + _urlquote(q)
    try:
        r = requests.get(url, timeout=20, headers={
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    except Exception as e:
        raise ToolError("搜不了（可能需要代理）：%s" % e)
    html = r.text or ""
    out = []
    for m in re.finditer(
            r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
        href, title = m.group(1), _strip_html(m.group(2))
        href = _unwrap_ddg(href)
        out.append("%s\n  %s" % (title.strip()[:120], href))
        if len(out) >= 8:
            break
    if not out:
        return "没搜到结果（搜索站点可能改了格式，或需要代理）"
    return "\n\n".join(out)


def _urlquote(s):
    try:
        from urllib.parse import quote
        return quote(s)
    except Exception:
        return s.replace(" ", "+")


def _unwrap_ddg(href):
    if href.startswith("//"):
        href = "https:" + href
    m = re.search(r"[?&]uddg=([^&]+)", href)
    if m:
        try:
            from urllib.parse import unquote
            return unquote(m.group(1))
        except Exception:
            return m.group(1)
    return href


# ------------------------------------------------------------------ 图片类
def _pil():
    try:
        from PIL import Image, ImageFilter, ImageEnhance, ImageDraw, ImageFont
        return Image, ImageFilter, ImageEnhance, ImageDraw, ImageFont
    except Exception as e:
        raise ToolError("没装 Pillow：%s" % e)


def _open_image(cfg, path):
    Image, _f, _e, _d, _fo = _pil()
    p = _safe_path(cfg, path, must_exist=True)
    try:
        return Image.open(p), p
    except Exception as e:
        raise ToolError("打不开图片：%s" % e)


def _save(img, cfg, out, src_path, suffix="_edited"):
    ext = os.path.splitext(src_path)[1] or ".png"
    if out:
        p = _safe_path(cfg, out)
        if not os.path.splitext(p)[1]:
            p += ext
    else:
        base = os.path.splitext(os.path.basename(src_path))[0]
        p = os.path.join(work_dir(cfg), base + suffix + ext)
    d = os.path.dirname(p)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    if p.lower().endswith((".jpg", ".jpeg")) and img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img.save(p)
    return p


def t_image_info(args, cfg):
    img, p = _open_image(cfg, args.get("path"))
    return "%s：%dx%d，%s，%.1f KB" % (
        _rel(cfg, p), img.width, img.height, img.format or "?", os.path.getsize(p) / 1024.0)


def t_image_resize(args, cfg):
    img, p = _open_image(cfg, args.get("path"))
    w = int(args.get("width") or 0)
    h = args.get("height")
    if not w and not h:
        raise ToolError("需要 width 或 height")
    if w and not h:
        h = max(1, int(img.height * (w / float(img.width))))
    elif h and not w:
        w = max(1, int(img.width * (h / float(img.height))))
    out = _save(img.resize((int(w), int(h)), _pil()[0].LANCZOS), cfg,
                args.get("out"), p, "_resized")
    return "已缩放成 %dx%d -> %s" % (int(w), int(h), _rel(cfg, out))


def t_image_crop(args, cfg):
    img, p = _open_image(cfg, args.get("path"))
    box = [int(args.get(k) or 0) for k in ("left", "top", "right", "bottom")]
    if not box[2] or not box[3]:
        raise ToolError("需要 left/top/right/bottom 四个参数")
    out = _save(img.crop(tuple(box)), cfg, args.get("out"), p, "_cropped")
    return "已裁剪 -> %s" % _rel(cfg, out)


def t_image_rotate(args, cfg):
    img, p = _open_image(cfg, args.get("path"))
    deg = float(args.get("degrees") or 0)
    out = _save(img.rotate(-deg, expand=True), cfg, args.get("out"), p, "_rotated")
    return "已旋转 %g 度 -> %s" % (deg, _rel(cfg, out))


def t_image_convert(args, cfg):
    img, p = _open_image(cfg, args.get("path"))
    fmt = (args.get("format") or "PNG").upper()
    if fmt == "JPG":
        fmt = "JPEG"
    out = args.get("out")
    if not out:
        out = os.path.splitext(os.path.basename(p))[0] + "_converted." + fmt.lower()
    out = _save(img, cfg, out, p, "_converted")
    return "已转成 %s -> %s" % (fmt, _rel(cfg, out))


def t_image_effect(args, cfg):
    Image, ImageFilter, ImageEnhance, _d, _fo = _pil()
    img, p = _open_image(cfg, args.get("path"))
    eff = (args.get("effect") or "").lower()
    if eff in ("grayscale", "gray", "grey"):
        out_img = img.convert("L")
    elif eff == "blur":
        out_img = img.filter(ImageFilter.GaussianBlur(float(args.get("amount") or 4)))
    elif eff == "sharpen":
        out_img = img.filter(ImageFilter.SHARPEN)
    elif eff == "contrast":
        out_img = ImageEnhance.Contrast(img).enhance(float(args.get("amount") or 1.8))
    elif eff == "brightness":
        out_img = ImageEnhance.Brightness(img).enhance(float(args.get("amount") or 1.4))
    elif eff == "invert":
        from PIL import ImageOps
        out_img = ImageOps.invert(img.convert("RGB"))
    else:
        raise ToolError("不认识的效果：%s（可用 grayscale/blur/sharpen/contrast/brightness/invert）" % eff)
    out = _save(out_img, cfg, args.get("out"), p, "_" + eff)
    return "已做 %s 处理 -> %s" % (eff, _rel(cfg, out))


def t_image_watermark(args, cfg):
    Image, _f, _e, ImageDraw, ImageFont = _pil()
    img, p = _open_image(cfg, args.get("path"))
    text = str(args.get("text") or "")
    if not text:
        raise ToolError("缺少 text 参数")
    try:
        font = ImageFont.truetype("msyh.ttc", int(args.get("size") or max(18, img.width // 24)))
    except Exception:
        font = ImageFont.load_default()
    base = img.convert("RGBA")
    layer = Image.new("RGBA", base.size, (255, 255, 255, 0))
    d = ImageDraw.Draw(layer)
    box = d.textbbox((0, 0), text, font=font)
    x = (base.width - (box[2] - box[0])) // 2
    y = base.height - (box[3] - box[1]) - int(args.get("margin") or 20)
    d.text((x, y), text, font=font, fill=(255, 255, 255, 200))
    out = _save(Image.alpha_composite(base, layer).convert("RGB"), cfg,
                args.get("out"), p, "_watermark")
    return "已加文字水印 -> %s" % _rel(cfg, out)


def t_make_thumbnail(args, cfg):
    img, p = _open_image(cfg, args.get("path"))
    w = int(args.get("width") or 320)
    h = max(1, int(img.height * (w / float(img.width))))
    out = _save(img.resize((w, h), _pil()[0].LANCZOS), cfg, args.get("out"), p, "_thumb")
    return "已生成缩略图 %dx%d -> %s" % (w, h, _rel(cfg, out))


# ------------------------------------------------------------------ 屏幕类
def _u32():
    import ctypes
    return ctypes.windll.user32


MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
KEYEVENTF_KEYUP = 0x0002

VK = {
    "enter": 13, "return": 13, "tab": 9, "esc": 27, "escape": 27, "space": 32,
    "backspace": 8, "delete": 46, "del": 46, "insert": 45, "home": 36, "end": 35,
    "pageup": 33, "pagedown": 34, "up": 38, "down": 40, "left": 37, "right": 39,
    "ctrl": 17, "control": 17, "alt": 18, "shift": 16, "win": 91, "windows": 91,
    "meta": 91, "cmd": 91, "apps": 93, "menu": 93,
}
for _i in range(1, 13):
    VK["f%d" % _i] = 111 + _i


def _vk(name):
    s = str(name or "").strip().lower()
    if s in VK:
        return VK[s]
    if len(s) == 1:
        return ord(s.upper())
    return None


def _mouse_event(flags, dx=0, dy=0, data=0):
    _u32().mouse_event(flags, dx, dy, data, 0)


def t_screenshot(args, cfg):
    try:
        from PIL import ImageGrab
    except Exception as e:
        raise ToolError("没装 Pillow：%s" % e)
    hider = _hook("hide_overlay")
    if hider:
        try:
            hider(True)
        except Exception:
            pass
    time.sleep(0.35)
    try:
        img = ImageGrab.grab()
    except Exception as e:
        raise ToolError("截不了图：%s" % e)
    finally:
        if hider:
            try:
                hider(False)
            except Exception:
                pass
    out = args.get("out")
    if out:
        p = _safe_path(cfg, out)
    else:
        p = os.path.join(work_dir(cfg), "screenshot_%s.png" % time.strftime("%H%M%S"))
    d = os.path.dirname(p)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    img.save(p)
    return "已截图 %dx%d -> %s" % (img.width, img.height, _rel(cfg, p))


def t_mouse_move(args, cfg):
    x, y = int(args.get("x") or 0), int(args.get("y") or 0)
    _u32().SetCursorPos(x, y)
    return "鼠标已移到 (%d, %d)" % (x, y)


def t_mouse_click(args, cfg):
    x = args.get("x")
    y = args.get("y")
    button = (args.get("button") or "left").lower()
    clicks = int(args.get("clicks") or 1)
    if x is not None and y is not None:
        _u32().SetCursorPos(int(x), int(y))
        time.sleep(0.05)
    down, up = {"left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
                "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
                "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP)}.get(
                    button, (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP))
    for _ in range(max(1, clicks)):
        _mouse_event(down)
        _mouse_event(up)
        time.sleep(0.04)
    return "已点击 %s（%d 次）" % (button, clicks)


def t_mouse_scroll(args, cfg):
    n = int(args.get("clicks") or 3)
    _mouse_event(MOUSEEVENTF_WHEEL, data=120 * n)
    return "已滚动 %d 格" % n


def _set_clipboard(text):
    try:
        import win32clipboard
        import win32con
    except Exception as e:
        raise ToolError("剪贴板不可用：%s" % e)
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()


def t_type_text(args, cfg):
    """打字。中文不能直接模拟按键 —— 走剪贴板 + Ctrl+V。"""
    text = str(args.get("text") or "")
    if not text:
        raise ToolError("缺少 text 参数")
    _set_clipboard(text)
    time.sleep(0.15)
    _key_down(VK["ctrl"])
    _key_down(ord("V"))
    _key_up(ord("V"))
    _key_up(VK["ctrl"])
    return "已输入 %d 个字符" % len(text)


def _key_down(code):
    _u32().keybd_event(code, 0, 0, 0)


def _key_up(code):
    _u32().keybd_event(code, 0, KEYEVENTF_KEYUP, 0)


def t_press_key(args, cfg):
    name = args.get("key") or args.get("keys")
    if isinstance(name, (list, tuple)):
        name = name[0] if name else ""
    code = _vk(name)
    if code is None:
        raise ToolError("不认识的按键：%s" % name)
    _key_down(code)
    time.sleep(0.05)
    _key_up(code)
    return "已按下 %s" % name


def t_hotkey(args, cfg):
    keys = args.get("keys") or []
    if isinstance(keys, str):
        keys = [k.strip() for k in keys.replace("+", ",").split(",") if k.strip()]
    codes = []
    for k in keys:
        c = _vk(k)
        if c is None:
            raise ToolError("不认识的按键：%s" % k)
        codes.append(c)
    if not codes:
        raise ToolError("缺少 keys 参数")
    for c in codes:
        _key_down(c)
    time.sleep(0.05)
    for c in reversed(codes):
        _key_up(c)
    return "已按下组合键 %s" % "+".join(str(k) for k in keys)


def t_list_windows(args, cfg):
    try:
        import win32gui
    except Exception as e:
        raise ToolError("没装 pywin32：%s" % e)
    out = []

    def cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd)
            if title:
                # 窗口标题是**外部文本** —— 标题里可能带指令，必须包 untrusted
                out.append("%s  [%s]" % (untrusted(title.strip(), limit=120, flatten=True),
                                           win32gui.GetClassName(hwnd)))
        except Exception:
            pass

    win32gui.EnumWindows(cb, None)
    return "\n".join(out[:60]) if out else "没找到可见窗口"


def t_focus_window(args, cfg):
    title = (args.get("title") or "").strip()
    if not title:
        raise ToolError("缺少 title 参数")
    try:
        import win32gui
        import win32con
    except Exception as e:
        raise ToolError("没装 pywin32：%s" % e)
    hit = []

    def cb(hwnd, _):
        try:
            if win32gui.IsWindowVisible(hwnd) and title.lower() in win32gui.GetWindowText(hwnd).lower():
                hit.append(hwnd)
        except Exception:
            pass

    win32gui.EnumWindows(cb, None)
    if not hit:
        raise ToolError("没找到标题里有「%s」的窗口" % title)
    h = hit[0]
    try:
        win32gui.ShowWindow(h, win32con.SW_RESTORE)
    except Exception:
        pass
    try:
        win32gui.SetForegroundWindow(h)
    except Exception:
        pass
    return "已切到窗口：%s" % untrusted(win32gui.GetWindowText(h), limit=120, flatten=True)


def t_open_app(args, cfg):
    """打开一个程序或文件。

    ⚠️ 硬边界（2026-11-08 定的档）：
    - **文件路径**只允许开在工作目录内（走 _safe_path），且扩展名要在白名单里；
    - **程序名**（notepad / 酷狗音乐）可以开，但名字里不许有 shell 元字符；
    - 看着像路径但文件不存在 → 直接报错，别偷偷去别处找。

    原来这里是裸 os.startfile(任意路径) —— 模型给个路径就能开任何文件。
    """
    name = (args.get("name") or args.get("path") or "").strip()
    if not name:
        raise ToolError("缺少 name 参数")
    # ① 现成的文件 → 限制在工作目录内 + 扩展名白名单
    if os.path.exists(name):
        p = _safe_path(cfg, name)
        ext = os.path.splitext(p)[1].lower()
        if ext not in OPEN_APP_EXT:
            raise ToolError("这个类型不让用 open_app 打开：%s（可以打开：%s）"
                            % (ext or "无扩展名", " ".join(OPEN_APP_EXT)))
        try:
            os.startfile(p)                # noqa: S606 - 已经过白名单和越界检查
        except Exception as e:
            raise ToolError("打不开：%s" % e)
        return "已打开 %s" % _rel(cfg, p)
    # ② 看着像路径但文件不存在 → 明确报错
    if ("\\" in name) or ("/" in name) or (len(name) > 1 and name[1] == ":"):
        raise ToolError("文件不存在：%s" % name)
    # ③ 程序名：不许带 shell 元字符
    bad = [c for c in '&|;><`$"\'\n\r' if c in name]
    if bad:
        raise ToolError("程序名里有非法字符：%s" % " ".join(bad))
    try:
        os.startfile(name)                # noqa: S606 - 程序名，已过滤元字符
    except Exception as e:
        raise ToolError("打不开：%s" % e)
    return "已打开 %s" % name


# ------------------------------------------------------------------ 命令类
def t_run_command(args, cfg):
    """执行一条命令（agent.allow_shell 开着才能用）。

    ⚠️ 硬边界（2026-11-08 定的档，权限仍全开，只收口边界）：
    - 超时**硬上限 60 秒** —— 不让模型用一个长命令把常驻进程挂住；
    - CREATE_NO_WINDOW —— 不闪黑框（开会时最忌讳弹窗）；
    - 输出上限 4000 字；
    - 工作目录锁在 agent.work_dir（默认「我的文档/Siri-OS」）；
    - 返回内容是**外部文本**，包 <untrusted>。

    ⚠️ 命令本身可以 cd 到别处 —— 这里只约束 cwd，
    真要完全隔离得靠作业对象/容器，那不是这个项目该背的。
    """
    cmd = (args.get("command") or "").strip()
    if not cmd:
        raise ToolError("缺少 command 参数")
    try:
        timeout = int(args.get("timeout") or MAX_CMD_TIMEOUT)
    except Exception:
        timeout = MAX_CMD_TIMEOUT
    timeout = max(1, min(timeout, MAX_CMD_TIMEOUT))
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           errors="replace", timeout=timeout,
                           cwd=ensure_work_dir(cfg), creationflags=flags)
    except subprocess.TimeoutExpired:
        raise ToolError("命令超时（上限 %d 秒）" % timeout)
    except Exception as e:
        raise ToolError("执行失败：%s" % e)
    out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
    return untrusted("退出码 %d\n%s" % (r.returncode, out), limit=MAX_CMD_OUT)


# ------------------------------------------------------------------ 注册表
def _p(desc, typ="string"):
    return {"type": typ, "description": desc}


def t_get_weather(args, cfg):
    """查天气（默认查用户所在区，也可以指定城市）。

    查到的结构化数据会直接推给界面 —— 屏幕上出的是「Live Activity 式」
    的天气面板（城市 + 最高/最低温 + 图标），模型这边拿到的是一句话。
    """
    try:
        import weather as weather_mod
    except Exception as e:
        return "天气模块不可用：%r" % (e,)
    city = (args.get("city") or "").strip()
    place = city or None
    if not place:
        # 默认查用户所在区：用**区级文字**去地理编码，不发经纬度
        try:
            import locate as locate_mod
            place = locate_mod.text(cfg) or ""
            if not place:
                p = locate_mod.refresh(cfg, force=True)
                place = (p or {}).get("text") or ""
        except Exception:
            place = ""
    if not place:
        return "不知道查哪里的天气：定位拿不到位置，也没指定城市名。"
    day = weather_mod.day_index(args.get("day"))
    print("[weather] querying place=%s day=%s" % (place, day))
    try:
        payload = weather_mod.fetch(place, cfg, day=day)
    except Exception as e:
        print("[weather] fetch failed: %r" % (e,))
        return "查天气失败：%r" % (e,)
    if not payload:
        print("[weather] no payload for %s: %s"
              % (place, weather_mod.last_error() or "unknown"))
        return ("没查到%s的天气（%s）。可以换个城市名再试。"
                % (place, weather_mod.last_error() or "未知原因"))
    # ⚠️ 这几行 print 不是调试噪音 —— 打包后 stdout 落在 logs\siri_orb.log，
    #    天气这条链路（定位 -> 地理编码 -> 预报 -> 上屏）以前**一段日志都没有**，
    #    出问题只能靠猜（2026-11-08 面板不上屏，排查就卡在这儿）。
    print("[weather] place=%s day=%s -> %s %s/%s %s" % (
        place, day, payload.get("city"), payload.get("hi"),
        payload.get("lo"), payload.get("text")))
    fn = _hook("weather_panel")
    print("[weather] panel hook: %s" % ("registered" if fn else "MISSING"))
    if fn:
        try:
            fn(payload)
        except Exception as e:
            print("[weather] panel hook failed: %r" % (e,))
    return weather_mod.summary(payload)


def t_get_location(args, cfg):
    """查用户当前位置（精确到区一级）。会真的定位一次，几秒钟。"""
    try:
        import locate as locate_mod
    except Exception as e:
        return "定位模块不可用：%r" % (e,)
    p = locate_mod.refresh(cfg, force=True)
    if not p:
        d = locate_mod.describe()
        return ("没能定位到当前位置（%s）。可以打开系统的「位置服务」再试一次，"
                "或者确认网络是通的。" % (d.get("last_err") or "未知原因"))
    acc = p.get("acc")
    tail = ("，定位精度约 %d 米" % acc) if acc else ""
    return "用户当前位置：%s（来源：%s%s）" % (p.get("text"), p.get("source"), tail)


TOOLS = [
    {"name": "list_files", "category": "file",
     "description": "列出目录里的文件和文件夹（默认工作目录；额外放开的目录里也能写绝对路径）",
     "parameters": {"type": "object", "properties": {
         "path": _p("要列的目录（相对工作目录；也可写额外放开目录下的绝对路径），默认当前目录")}, "required": []},
     "fn": t_list_files},
    {"name": "read_file", "category": "file",
     "description": "读一个文本文件的内容",
     "parameters": {"type": "object", "properties": {
         "path": _p("文件路径（相对工作目录；也可写额外放开目录下的绝对路径）"),
         "max_chars": _p("最多读多少字符，默认 20000", "integer")}, "required": ["path"]},
     "fn": t_read_file},
    {"name": "write_file", "category": "file",
     "description": "写一个文本文件（会覆盖已有内容）",
     "parameters": {"type": "object", "properties": {
         "path": _p("文件路径（相对工作目录；也可写额外放开目录下的绝对路径）"),
         "content": _p("要写进去的内容")}, "required": ["path", "content"]},
     "fn": t_write_file},
    {"name": "edit_file", "category": "file",
     "description": "在文件里精确替换一段文字（old_string 必须原样出现）",
     "parameters": {"type": "object", "properties": {
         "path": _p("文件路径（相对工作目录；也可写额外放开目录下的绝对路径）"),
         "old_string": _p("要被替换的原文字"),
         "new_string": _p("替换成的新文字"),
         "replace_all": _p("true 表示全部替换，默认只换第一处", "boolean")},
         "required": ["path", "old_string", "new_string"]},
     "fn": t_edit_file},
    {"name": "search_files", "category": "file",
     "description": "在目录里按正则搜索文本",
     "parameters": {"type": "object", "properties": {
         "pattern": _p("正则表达式"),
         "path": _p("要搜的目录（相对工作目录；也可写额外放开目录下的绝对路径），默认全部")}, "required": ["pattern"]},
     "fn": t_search_files},

    {"name": "open_url", "category": "web",
     "description": "在默认浏览器里打开一个网址",
     "parameters": {"type": "object", "properties": {
         "url": _p("网址")}, "required": ["url"]},
     "fn": t_open_url},
    {"name": "fetch_page", "category": "web",
     "description": "抓取网页正文（转成纯文本）",
     "parameters": {"type": "object", "properties": {
         "url": _p("网址（http/https）"),
         "max_chars": _p("最多取多少字符，默认 6000", "integer")}, "required": ["url"]},
     "fn": t_fetch_page},
    {"name": "search_web", "category": "web",
     "description": "联网搜索（返回若干条标题和链接）",
     "parameters": {"type": "object", "properties": {
         "query": _p("搜索关键词")}, "required": ["query"]},
     "fn": t_search_web},

    {"name": "image_info", "category": "image",
     "description": "看一张图片的尺寸/格式/大小",
     "parameters": {"type": "object", "properties": {
         "path": _p("图片路径（相对工作目录；也可写额外放开目录下的绝对路径）")}, "required": ["path"]},
     "fn": t_image_info},
    {"name": "image_resize", "category": "image",
     "description": "缩放图片",
     "parameters": {"type": "object", "properties": {
         "path": _p("原图路径"), "out": _p("输出路径（可省）"),
         "width": _p("目标宽度", "integer"), "height": _p("目标高度（可省）", "integer")},
         "required": ["path"]},
     "fn": t_image_resize},
    {"name": "image_crop", "category": "image",
     "description": "裁剪图片",
     "parameters": {"type": "object", "properties": {
         "path": _p("原图路径"), "out": _p("输出路径（可省）"),
         "left": _p("左边", "integer"), "top": _p("上边", "integer"),
         "right": _p("右边", "integer"), "bottom": _p("下边", "integer")},
         "required": ["path", "left", "top", "right", "bottom"]},
     "fn": t_image_crop},
    {"name": "image_rotate", "category": "image",
     "description": "旋转图片",
     "parameters": {"type": "object", "properties": {
         "path": _p("原图路径"), "out": _p("输出路径（可省）"),
         "degrees": _p("角度，正数逆时针", "number")}, "required": ["path", "degrees"]},
     "fn": t_image_rotate},
    {"name": "image_convert", "category": "image",
     "description": "转换图片格式（PNG/JPEG/WEBP/BMP）",
     "parameters": {"type": "object", "properties": {
         "path": _p("原图路径"), "out": _p("输出路径（可省）"),
         "format": _p("目标格式，比如 PNG / JPEG / WEBP / BMP")},
         "required": ["path", "format"]},
     "fn": t_image_convert},
    {"name": "image_effect", "category": "image",
     "description": "给图片加效果：grayscale / blur / sharpen / contrast / brightness / invert",
     "parameters": {"type": "object", "properties": {
         "path": _p("原图路径"), "out": _p("输出路径（可省）"),
         "effect": _p("效果名"), "amount": _p("强度（blur/contrast/brightness 用）", "number")},
         "required": ["path", "effect"]},
     "fn": t_image_effect},
    {"name": "image_watermark", "category": "image",
     "description": "给图片加文字水印",
     "parameters": {"type": "object", "properties": {
         "path": _p("原图路径"), "out": _p("输出路径（可省）"),
         "text": _p("水印文字"), "size": _p("字号", "integer"),
         "margin": _p("离下边多远", "integer")}, "required": ["path", "text"]},
     "fn": t_image_watermark},
    {"name": "make_thumbnail", "category": "image",
     "description": "生成缩略图",
     "parameters": {"type": "object", "properties": {
         "path": _p("原图路径"), "out": _p("输出路径（可省）"),
         "width": _p("宽度，默认 320", "integer")}, "required": ["path"]},
     "fn": t_make_thumbnail},

    {"name": "screenshot", "category": "screen",
     "description": "截当前屏幕并存成 PNG",
     "parameters": {"type": "object", "properties": {
         "out": _p("保存路径（可省，默认放工作目录）")}, "required": []},
     "fn": t_screenshot},
    {"name": "mouse_move", "category": "screen",
     "description": "把鼠标移到屏幕坐标 (x, y)",
     "parameters": {"type": "object", "properties": {
         "x": _p("横坐标", "integer"), "y": _p("纵坐标", "integer")},
         "required": ["x", "y"]},
     "fn": t_mouse_move},
    {"name": "mouse_click", "category": "screen",
     "description": "点击鼠标（可指定坐标和左右键）",
     "parameters": {"type": "object", "properties": {
         "x": _p("横坐标（可省，用当前位置）", "integer"),
         "y": _p("纵坐标（可省，用当前位置）", "integer"),
         "button": _p("left / right / middle，默认 left"),
         "clicks": _p("点几下，默认 1", "integer")}, "required": []},
     "fn": t_mouse_click},
    {"name": "mouse_scroll", "category": "screen",
     "description": "滚动鼠标滚轮",
     "parameters": {"type": "object", "properties": {
         "clicks": _p("格数，正数向上，默认 3", "integer")}, "required": []},
     "fn": t_mouse_scroll},
    {"name": "type_text", "category": "screen",
     "description": "在当前焦点窗口输入文字（中文走剪贴板）",
     "parameters": {"type": "object", "properties": {
         "text": _p("要输入的文字")}, "required": ["text"]},
     "fn": t_type_text},
    {"name": "press_key", "category": "screen",
     "description": "按一个键：enter/tab/esc/space/ctrl/alt/win/方向键/F1-F12 等",
     "parameters": {"type": "object", "properties": {
         "key": _p("键名")}, "required": ["key"]},
     "fn": t_press_key},
    {"name": "hotkey", "category": "screen",
     "description": "按组合键，比如 ctrl+s / alt+tab / win+shift+s",
     "parameters": {"type": "object", "properties": {
         "keys": _p("按键数组，比如 [\"ctrl\", \"s\"]")}, "required": ["keys"]},
     "fn": t_hotkey},
    {"name": "list_windows", "category": "screen",
     "description": "列出当前所有可见窗口的标题",
     "parameters": {"type": "object", "properties": {}, "required": []},
     "fn": t_list_windows},
    {"name": "focus_window", "category": "screen",
     "description": "把某个窗口切到前台（按标题里的关键字找）",
     "parameters": {"type": "object", "properties": {
         "title": _p("窗口标题里的关键字")}, "required": ["title"]},
     "fn": t_focus_window},
    {"name": "open_app", "category": "screen",
     "description": "打开一个程序或文件（notepad / calc / 某个 exe 路径）",
     "parameters": {"type": "object", "properties": {
         "name": _p("程序名或文件路径")}, "required": ["name"]},
     "fn": t_open_app},

    {"name": "run_command", "category": "shell",
     "description": "在工作目录里执行一条命令行命令（需要先在设置里打开这个能力）",
     "parameters": {"type": "object", "properties": {
         "command": _p("要执行的命令"),
         "timeout": _p("超时秒数，默认 60", "integer")}, "required": ["command"]},
     "fn": t_run_command},

    {"name": "get_location", "category": "location",
     "description": "查用户当前所在的位置（精确到区一级，例如「上海市浦东新区」）",
     "parameters": {"type": "object", "properties": {}, "required": []},
     "fn": t_get_location},

    {"name": "get_weather", "category": "location",
     "description": "查天气（默认查用户所在区；也可以指定城市名，例如「北京」）",
     "parameters": {"type": "object", "properties": {
         "city": _p("要查的城市名，省略就是查用户所在区"),
         "day": _p("要查哪一天：今天 / 明天 / 后天，默认今天")}, "required": []},
     "fn": t_get_weather},
]

BY_NAME = dict((t["name"], t) for t in TOOLS)


def spec_for(cfg):
    """给模型看的工具清单（只含当前允许用的）。"""
    out = []
    for t in TOOLS:
        if not allowed(cfg, t["category"]):
            continue
        out.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            },
        })
    return out


def categories_in_use(cfg):
    """当前开了哪些能力（给面板和 system prompt 用）。"""
    return [c for c in CATEGORIES if allowed(cfg, c)]


def execute(name, args, cfg):
    """跑一个工具。**永远返回字符串**（失败也是），模型才知道发生了什么。"""
    spec = BY_NAME.get(name)
    if spec is None:
        return "没有叫「%s」这个工具" % name
    if not allowed(cfg, spec["category"]):
        return "「%s」这个能力现在是关闭的（%s），请先在设置面板里打开。" % (
            name, CAT_LABEL.get(spec["category"], spec["category"]))
    try:
        # 文件/命令类工具都拿工作目录当根 —— 先把它建出来
        if spec["category"] in ("file", "shell"):
            ensure_work_dir(cfg)
        r = spec["fn"](args or {}, cfg)
        return r if isinstance(r, str) else str(r)
    except ToolError as e:
        return "失败：%s" % e
    except Exception as e:
        return "失败：%s: %s" % (type(e).__name__, e)
