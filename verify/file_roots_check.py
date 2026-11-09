# -*- coding: utf-8 -*-
"""Self check: the extra-roots access control (agent.file_roots).

Why this file exists
--------------------
The file tools used to be hard-locked to the working folder.  The user asked
for the whole D: drive, so the lock became configurable:

  * agent.file_roots = ""            -> working folder only (the default)
  * agent.file_roots = "D:"          -> the whole drive
  * agent.file_roots = "D:;E:"       -> several roots, separated by ";"

Two things that are easy to get wrong and are checked here:

  1. a bare drive letter ("D:") must become the DRIVE ROOT.  os.path.abspath
     resolves "D:" to the *current directory on D:* -- which, in a session
     workspace, is exactly the wrong place (and a path that leaks the
     session dir into the model's context).
  2. the default must stay closed: with no file_roots, a D: path is refused,
     and another drive is always refused.

No real microphone, no real window, no network: everything is a plain
function call on the path logic.
"""
import _bootstrap  # noqa: F401  (repo root on sys.path)
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import tools as T

FAILS = []


def check(label, cond, extra=""):
    if cond:
        print("ok   %s" % label)
    else:
        FAILS.append(label)
        print("FAIL %s %s" % (label, extra))


def main():
    cfg_open = {"agent": {"work_dir": "", "file_roots": "D:"}}
    cfg_shut = {"agent": {"work_dir": "", "file_roots": ""}}

    extras = T.extra_roots(cfg_open)
    check("a bare drive letter becomes the drive root",
          bool(extras) and extras[0].endswith(os.sep) and len(extras[0]) == 3,
          "-> %s" % extras)

    roots = T.allowed_roots(cfg_open)
    check("the working folder is still a root", len(roots) >= 2, "-> %s" % roots)

    dpath = T._safe_path(cfg_open, "D:" + os.sep)
    check("the drive root itself is allowed", dpath.upper().startswith("D:"), "-> %s" % dpath)

    try:
        T._safe_path(cfg_open, "C:" + os.sep + "Windows")
        check("another drive is refused", False)
    except T.ToolError:
        check("another drive is refused", True)

    try:
        T._safe_path(cfg_shut, "D:" + os.sep)
        check("without file_roots the drive is refused", False)
    except T.ToolError:
        check("without file_roots the drive is refused", True)

    cfg_multi = {"agent": {"work_dir": "", "file_roots": "D:;E:"}}
    check("several roots are parsed",
          len(T.extra_roots(cfg_multi)) == 2, "-> %s" % T.extra_roots(cfg_multi))

    wd = T.work_dir(cfg_open)
    check("a relative path still resolves under the working folder",
          T._safe_path(cfg_open, "notes.txt").startswith(wd))

    print("")
    if FAILS:
        print("%d FAILED" % len(FAILS))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
