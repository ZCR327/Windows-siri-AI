# -*- coding: utf-8 -*-
"""把「要联网才有」的重依赖挡出启动路径。

requests 会带来 urllib3 / certifi / idna / charset_normalizer，
光 import 就 **0.33s**（实测，2026-11-10）—— 而助手启动时根本不需要网络。

里面那四个模块（llm / tools / weather / locate）都是这个写法：

    try:
        from netdeps import requests      # 惰性代理
    except Exception:
        requests = None

然后原来怎么用就怎么用：requests.post(...) / requests.get(...) / if not requests: ...

⚠️ 为什么不用模块级 __getattr__（PEP 562）：
   模块级 __getattr__ 只管**外部属性访问**（module.requests），
   模块**内部函数里的裸全局查找**（requests.post）不会走它 ——
   那时就是 NameError: name 'requests' is not defined。
   这个坑是 2026-11-10 打包后端到端验证真实触发的，
   不是看代码看出来的。
   代理对象是**普通名词**，裸全局查找能解析到它 → 自然工作。
"""


class LazyRequests(object):
    """requests 的惰性代理：第一次真正用它时才 import。"""

    _mod = None
    _tried = False

    def _load(self):
        if not self._tried:
            self._tried = True
            try:
                import requests as _r
            except Exception:       # pragma: no cover
                self._mod = None
            else:
                self._mod = _r
        return self._mod

    def __getattr__(self, name):
        mod = self._load()
        if mod is None:
            raise ImportError("requests module is not available")
        return getattr(mod, name)

    def __bool__(self):
        """能用返回 True；被禁用返回 False（让「if not requests」能当作健康检查）。"""
        return self._load() is not None

    __nonzero__ = __bool__


# 各模块里 from netdeps import requests 就拿到这个代理
requests = LazyRequests()
