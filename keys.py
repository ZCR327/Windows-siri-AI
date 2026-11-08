# -*- coding: utf-8 -*-
"""统一密钥库：config/api_keys.json

为什么要它：key 散在各个 config 里，最容易出「改了一处、另一处还是旧 key」的坑，
贴文档/贴聊天时也容易漏掉某一处。统一放一个文件：
写只走这里，读也只走这里（llm.py / set_key.py / doctor.py / jina_ocr.py 都走这里）。

文件格式（纯 JSON，键名固定）：
{
  "minimax":  "sk-...",
  "deepseek": "sk-...",
  "zhipu":    "xxx.yyy",
  "jina":     "jina_..."
}
"""
import io
import json
import os

try:
    import paths as pathmod
except Exception:      # pragma: no cover
    pathmod = None

# 打包成 exe 后模块所在目录是**只读解包目录** —— 密钥库必须落在 exe 同级目录
HERE = (pathmod.data_dir() if pathmod else os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(HERE))        # 上级工作区根（兼容旧的「工作区」布局）
ENV_STORE = os.environ.get("SIRI_OS_KEYS", "").strip()


def _pick_store():
    """决定密钥库放哪。

    顺序：环境变量 SIRI_OS_KEYS > 本目录下的 config/ > 上级工作区的 config/。

    关键规则是「**本目录存在 config/ 就用本目录的**」——
    这样把本目录整个拷成独立仓库、或者拷给别人时，
    不会误读到上一级工作区里那份**真的密钥库**。
    """
    if ENV_STORE:
        return ENV_STORE
    local = os.path.join(HERE, "config")
    if pathmod is not None and pathmod.is_frozen():
        # 打包后密钥库固定在 **exe 同级目录/config** ——
        # 不能往上级目录找：那是 exe 所在文件夹的父目录，可能是共享盘/别人的目录
        return os.path.join(local, "api_keys.json")
    if os.path.isdir(local):
        return os.path.join(local, "api_keys.json")
    return os.path.join(ROOT, "config", "api_keys.json")


STORE = _pick_store()

def _names():
    """密钥库里可能有的键 = 所有后端厂商 + jina（读注册表，别写死）。"""
    out = []
    try:
        import providers as prov
        out.extend(prov.ORDER)
    except Exception:
        out.extend(("minimax", "deepseek", "zhipu", "openai"))
    out.append("jina")
    return tuple(out)


NAMES = _names()


def load():
    """读整个密钥库；读不到就当空（不要让一个坏文件把调用方炸了）。"""
    try:
        with io.open(STORE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def get(name):
    v = load().get(name) or ""
    return v.strip() if isinstance(v, str) else ""


def set_key(name, key):
    """只改一个键，其它键原样保留。"""
    d = load()
    d[name] = key
    ddir = os.path.dirname(STORE)
    if ddir and not os.path.isdir(ddir):
        os.makedirs(ddir, exist_ok=True)
    with io.open(STORE, "w", encoding="utf-8") as f:
        f.write(json.dumps(d, ensure_ascii=False, indent=2))
        f.write("\n")
    return True


def summary():
    """给 doctor / 文档用：每个键在不在、多长（**不打印内容**）。"""
    d = load()
    out = {}
    for n in NAMES:
        v = d.get(n) or ""
        out[n] = len(v) if isinstance(v, str) else 0
    return out
