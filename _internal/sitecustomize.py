# -*- coding: utf-8 -*-
import os
from datetime import datetime

try:
    with open(os.path.join(os.path.dirname(__file__), "_probe", "sitecustomize.log"), "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat()} loaded from {__file__}\n")
except Exception as exc:
    with open(r"D:\软件\_probe\sitecustomize_err.log", "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat()} {exc!r}\n")
