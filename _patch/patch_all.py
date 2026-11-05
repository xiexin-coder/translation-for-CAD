# -*- coding: utf-8 -*-
"""Rebuild Tuyi.exe from Tuyi.original.exe with all local patches applied.

Patches
  1. backend.language_assets  - fuzzy glossary matching (2026-10-28)
                              + writable-DB fallback, TM write errors never
                                abort a translation (2026-10-29 late)
  2. backend.storage          - write lock gets a timeout, never blocks forever
  3. backend.api              - save_config writes directly (no global lock)
  4. backend.providers.deepl_provider - DeepL failures keep the API message
  5. backend.drawings         - log translate_rows tracebacks
  6. desktop.launcher         - startup/hang diagnostics (integrity level,
                                paths, thread stacks, Not-Responding watchdog)
  7. backend.app_meta         - output folder that cannot be written (restricted
                                launch / policy) silently falls back to a
                                writable folder next to the exe (2026-10-29)

Usage:
    python patch_all.py Tuyi.original.exe Tuyi.exe
"""
import marshal
import sys

import pyi_patch

LOG_PATH = r"D:\soft\诊断日志.txt"

STORAGE_PATCH = r'''
# --- never-block write lock (injected 2026-10-29) ---
import os as _os
import sys as _sys
import time as _time

_ORIG_CODE = "__ORIG_CODE_PLACEHOLDER__"
_NS = {
    "__name__": __name__,
    "__package__": __package__,
    "__loader__": __loader__,
    "__spec__": __spec__,
    "__doc__": None,
}
exec(_ORIG_CODE, _NS)
globals().update({k: v for k, v in _NS.items() if not k.startswith("__")})

_LOG = r"__LOG_PATH__"


def _patch_log(msg):
    try:
        with open(_LOG, "a", encoding="utf-8") as handle:
            handle.write("%s storage: %s\n" % (_time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


_patch_log("backend.storage wrapper imported")


class _TimeoutLock:
    """threading.Lock wrapper: waits at most `timeout` seconds, then lets the
    caller continue. Atomic writes stay correct (temp file + os.replace); the
    lock is only a nicety, so a wedged holder must not freeze the app."""

    def __init__(self, inner, timeout=2.0):
        self._inner = inner
        self._timeout = timeout
        self._held = False
        self._depth = 0

    def __enter__(self):
        if self._depth:
            self._depth += 1
            self._held = False
            return self
        self._depth += 1
        try:
            ok = self._inner.acquire(timeout=self._timeout)
        except Exception:
            ok = False
        self._held = bool(ok)
        if not ok:
            _patch_log("write lock busy >%.1fs, writing without it" % self._timeout)
        return self

    def __exit__(self, *exc_info):
        self._depth = max(0, self._depth - 1)
        if self._held:
            self._held = False
            try:
                self._inner.release()
            except Exception:
                pass
        return False


if "_WRITE_LOCK" in _NS:
    _NS["_WRITE_LOCK"] = _TimeoutLock(_NS["_WRITE_LOCK"])
    _WRITE_LOCK = _NS["_WRITE_LOCK"]


# --- fail fast when the output folder is not writable (injected 2026-10-29) ---
# tempfile.mkstemp retries on permission errors; in a restricted process that
# spun for half an hour on 10000 attempts.  Check once, raise a clear message.
_ORIG_AOP = _NS.get("atomic_output_path")
if _ORIG_AOP is not None:
    import contextlib as _ctx
    import os as _aos
    import pathlib as _pl

    @_ctx.contextmanager
    def atomic_output_path(path):
        target = _pl.Path(path)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            raise RuntimeError("创建输出目录失败：%s（%r）" % (target.parent, exc))
        probe = target.parent / (".tuyi_wprobe_%s" % _aos.getpid())
        try:
            with open(probe, "w", encoding="utf-8") as _fh:
                _fh.write("x")
            _aos.remove(probe)
        except Exception as exc:
            _patch_log("输出目录不可写：%r（%r）" % (target.parent, exc))
            raise RuntimeError(
                "无法在输出目录创建文件：%s（%s: %s）。"
                "如果程序是从聊天窗口或文件卡片打开的，它运行在受限权限下，写不了这个目录；"
                "请关闭程序后从资源管理器双击 Tuyi.exe 重新启动。" % (
                    target.parent, type(exc).__name__, exc))
        with _ORIG_AOP(path) as temporary:
            yield temporary

    globals()["atomic_output_path"] = atomic_output_path
    _NS["atomic_output_path"] = atomic_output_path
    _patch_log("atomic_output_path wrapped (fail fast on unwritable folder)")


# --- JSON/bytes writes must not spin on an unwritable folder (injected) ---
# queue._save() runs on the GUI thread while the window closes; 10000 mkstemp
# retries there freeze the window ("Not Responding").  Skip the write instead.
def _dir_writable(directory):
    try:
        probe = _aos.path.join(str(directory), ".tuyi_wprobe_%s" % _aos.getpid())
        with open(probe, "w", encoding="utf-8") as _fh:
            _fh.write("x")
        _aos.remove(probe)
        return True
    except Exception:
        return False


def _wrap_write(name):
    original = _NS.get(name)
    if original is None:
        return

    def wrapper(path, *args, **kwargs):
        target_dir = _aos.path.dirname(_aos.path.abspath(str(path))) or "."
        try:
            _aos.makedirs(target_dir, exist_ok=True)
        except Exception:
            pass
        if not _dir_writable(target_dir):
            _patch_log("%s 跳过：目录不可写 %r" % (name, target_dir))
            return None
        return original(path, *args, **kwargs)

    globals()[name] = wrapper
    _NS[name] = wrapper


_wrap_write("atomic_write_json")
_wrap_write("atomic_write_bytes")
'''

API_PATCH = r'''
# --- hang-proof config save + readable errors (injected 2026-10-29) ---
import json as _json
import os as _os
import sys as _sys
import tempfile as _tempfile
import time as _time

_ORIG_CODE = "__ORIG_CODE_PLACEHOLDER__"
_NS = {
    "__name__": __name__,
    "__package__": __package__,
    "__loader__": __loader__,
    "__spec__": __spec__,
    "__doc__": None,
}
exec(_ORIG_CODE, _NS)
globals().update({k: v for k, v in _NS.items() if not k.startswith("__")})

_LOG = r"__LOG_PATH__"


def _patch_log(msg):
    try:
        with open(_LOG, "a", encoding="utf-8") as handle:
            handle.write("%s api: %s\n" % (_time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


_patch_log("backend.api wrapper imported")


def _write_json_atomic(path, value):
    path = str(path)
    parent = _os.path.dirname(path) or "."
    temporary = path + ".tmp"
    _patch_log("write: begin %r" % path)
    try:
        _os.makedirs(parent, exist_ok=True)
    except Exception as exc:
        _patch_log("write: makedirs failed %r" % (exc,))
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            _json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            try:
                _os.fsync(handle.fileno())
            except Exception as exc:
                _patch_log("write: fsync failed %r" % (exc,))
        _patch_log("write: dumped %s bytes" % _os.path.getsize(temporary))
        last = None
        for attempt in range(8):
            try:
                _os.replace(temporary, path)
                _patch_log("write: replaced on attempt %d" % attempt)
                return
            except OSError as exc:
                last = exc
                _patch_log("write: replace attempt %d failed %r" % (attempt, exc))
                _time.sleep(0.05 * (attempt + 1))
        raise last if last else OSError("replace failed")
    except Exception as exc:
        _patch_log("write: FAILED %r" % (exc,))
        raise
    finally:
        if _os.path.exists(temporary):
            try:
                _os.remove(temporary)
            except Exception:
                pass


_Service = _NS.get("TranslationService")
_CONFIG_PATH = _NS.get("CONFIG_PATH")

_env_config = _os.environ.get("TUYI_CONFIG_PATH")
if _env_config:
    _CONFIG_PATH = _env_config
    _NS["CONFIG_PATH"] = _env_config
    _patch_log("config path overridden: %r" % _env_config)
else:
    # If the profile folder refuses writes (AppContainer, policy, antivirus),
    # fall back to a config file next to the executable so saving still works.
    try:
        with open(_CONFIG_PATH, "a", encoding="utf-8"):
            pass
    except Exception as exc:
        _patch_log("primary config not writable (%r), falling back" % (exc,))
        _fallback = _os.path.join(
            _os.path.dirname(_os.path.abspath(_sys.executable)), ".tuyi_config.json"
        )
        try:
            with open(_fallback, "a", encoding="utf-8"):
                pass
            _CONFIG_PATH = _fallback
            _NS["CONFIG_PATH"] = _fallback
            _patch_log("config path fallback: %r" % _fallback)
        except Exception as exc2:
            _patch_log("fallback config not writable either: %r" % (exc2,))


if _Service is not None and _CONFIG_PATH is not None:
    _patch_log("config path in use: %r" % (_CONFIG_PATH,))

    def save_config(
        self,
        deepl_key="",
        output_dir="",
        provider="deepl",
        azure_key="",
        azure_region="",
        project_package_path=None,
        **extra
    ):
        _patch_log("save_config called key_len=%d provider=%r" % (len(deepl_key or ""), provider))
        config = self.load_config()
        config["deepl_key"] = (deepl_key or "").strip()
        config["provider"] = provider
        config["azure_key"] = (azure_key or "").strip()
        config["azure_region"] = (azure_region or "").strip()
        if project_package_path is not None:
            config["project_package_path"] = str(project_package_path).strip()
        if output_dir:
            config["output_dir"] = output_dir
        for key in ("openai_key", "openai_base", "openai_model", "ollama_host", "ollama_model"):
            if key in extra and extra[key] is not None:
                config[key] = str(extra[key]).strip()
        try:
            config.setdefault("output_dir", self.default_output_dir())
        except Exception:
            pass
        _patch_log("save_config provider=%r key_len=%d" % (provider, len(config["deepl_key"])))
        _write_json_atomic(_CONFIG_PATH, config)

    _Service.save_config = save_config

    # Report the folder the program can really write to, so the settings page
    # and the saved result paths agree with where files actually land.
    _orig_load_config = _Service.load_config

    def load_config(self):
        config = _orig_load_config(self)
        try:
            directory = str((config or {}).get("output_dir") or "")
            resolve = _NS.get("resolve_output_dir")
            if resolve is not None:
                effective = resolve(directory)
                if effective and effective != directory:
                    config["output_dir"] = effective
                    _patch_log("load_config: output dir %r -> %r" % (directory, effective))
        except Exception as exc:
            _patch_log("load_config: output dir check failed %r" % (exc,))
        return config

    _Service.load_config = load_config


def _chinese_detail(exc, fallback="操作失败"):
    """Keep the real reason: walk the exception chain and join the messages."""
    parts = []
    seen = set()
    current = exc
    for _ in range(4):
        if current is None:
            break
        try:
            text = str(current).strip()
        except Exception:
            text = ""
        if text:
            text = text.splitlines()[0][:220]
            if text not in seen and text not in parts:
                seen.add(text)
                parts.append(text)
        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
    if not parts:
        return fallback
    if len(parts) == 1:
        return parts[0]
    return ("%s（%s）" % (parts[0], " / ".join(parts[1:]))) [:300]


_NS["_chinese_detail"] = _chinese_detail
'''

DEEPL_PATCH = r'''
# --- keep the DeepL API message (injected 2026-10-29) ---
_ORIG_CODE = "__ORIG_CODE_PLACEHOLDER__"
_NS = {
    "__name__": __name__,
    "__package__": __package__,
    "__loader__": __loader__,
    "__spec__": __spec__,
    "__doc__": None,
}
exec(_ORIG_CODE, _NS)
globals().update({k: v for k, v in _NS.items() if not k.startswith("__")})

_Provider = _NS.get("DeepLProvider")
if _Provider is not None:
    import time as _time

    def _dbg(msg):
        try:
            with open(r"__LOG_PATH__", "a", encoding="utf-8") as _h:
                _h.write("%s deepl: %s\n" % (_time.strftime("%Y-%m-%d %H:%M:%S"), msg))
        except Exception:
            pass

    _orig_translate = _Provider.translate_text

    def translate_text(self, text, source_lang, target_lang):
        _dbg("translate %r %s->%s" % (text[:30], source_lang, target_lang))
        try:
            result = _orig_translate(self, text, source_lang, target_lang)
            _dbg("translate ok %r" % (result[:30],))
            return result
        except Exception as exc:
            cause = exc.__cause__ or exc.__context__
            _dbg("translate FAILED %s: %s | cause=%r" % (type(exc).__name__, exc, cause))
            detail = str(cause).strip().splitlines()[0] if cause is not None else ""
            message = str(exc).strip() or "DeepL 翻译失败"
            if detail and detail not in message:
                message = "%s（%s）" % (message, detail[:220])
            try:
                error = type(exc)(message)
            except Exception:
                error = _NS["TranslationProviderError"](message)
            try:
                error.retryable = getattr(exc, "retryable", True)
            except Exception:
                pass
            raise error from exc

    _Provider.translate_text = translate_text

    _orig_init = _Provider.__init__

    def __init__(self, api_key=""):
        _dbg("provider init key_len=%d" % len((api_key or "").strip()))
        try:
            _orig_init(self, api_key)
        except Exception as exc:
            _dbg("provider init FAILED %s: %s" % (type(exc).__name__, exc))
            raise

    _Provider.__init__ = __init__
'''

# Appended to the fuzzy glossary patch: keep translations alive when the
# language-assets database (translation memory) cannot be written.
LA_EXTRA = r'''
# --- SQLite never kills a translation (injected 2026-10-29) ---
import os as _la_os
import sys as _la_sys
import time as _la_time
import traceback as _la_traceback

_LA_LOG = r"__LOG_PATH__"


def _la_log(msg):
    try:
        with open(_LA_LOG, "a", encoding="utf-8") as _h:
            _h.write("%s language_assets: %s\n" % (_la_time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def _dir_writable(path):
    try:
        probe = _la_os.path.join(str(path), ".tuyi_write_probe")
        with open(probe, "w", encoding="utf-8") as _h:
            _h.write("x")
        _la_os.remove(probe)
        return True, ""
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)


_LA = _NS["LanguageAssets"]
_la_orig_init = _LA.__init__


def _la_init(self, database_path=None):
    target = database_path if database_path else _NS.get("DATABASE_PATH")
    try:
        import pathlib as _pl

        target_path = _pl.Path(target)
        parent = target_path.parent
        if not parent.exists():
            ok, why = False, "目录不存在"
        else:
            ok, why = _dir_writable(parent)
        if not ok:
            _la_log("数据库目录不可写（%s），改用程序目录：%s" % (why, parent))
            alt_parent = _pl.Path(_la_sys.executable).resolve().parent
            alt_ok, alt_why = _dir_writable(alt_parent)
            if alt_ok:
                database_path = alt_parent / ".tuyi_language_assets.sqlite3"
                _la_log("数据库改为：%s" % database_path)
            else:
                import tempfile as _tf

                tmp_ok, tmp_why = _dir_writable(_tf.gettempdir())
                if tmp_ok:
                    database_path = _pl.Path(_tf.gettempdir()) / ".tuyi_language_assets.sqlite3"
                    _la_log("程序目录也不可写（%s），数据库改到临时目录：%s" % (alt_why, database_path))
                else:
                    _la_log("临时目录也不可写（%s），保持原路径" % tmp_why)
    except Exception as exc:
        _la_log("数据库路径检查失败（忽略）：%r" % (exc,))
    try:
        _la_orig_init(self, database_path)
        _la_log("数据库就绪：%s" % getattr(self, "database_path", "?"))
    except Exception:
        _la_log("数据库初始化失败：\n%s" % _la_traceback.format_exc())
        raise


_LA.__init__ = _la_init


def _la_safe_write(name):
    original = getattr(_LA, name)

    def wrapper(self, *args, **kwargs):
        try:
            return original(self, *args, **kwargs)
        except Exception:
            _la_log("%s 写入失败（已忽略，不影响译文输出）：\n%s" % (name, _la_traceback.format_exc()))
            return None

    wrapper.__name__ = name
    setattr(_LA, name, wrapper)


for _la_name in ("record_memory", "record_usage"):
    try:
        _la_safe_write(_la_name)
    except Exception as exc:
        _la_log("包装 %s 失败：%r" % (_la_name, exc))
'''

DRAWINGS_PATCH = r'''
# --- log translate_rows failures (injected 2026-10-29) ---
import time as _dr_time
import traceback as _dr_traceback

_ORIG_CODE = "__ORIG_CODE_PLACEHOLDER__"
_NS = {
    "__name__": __name__,
    "__package__": __package__,
    "__loader__": __loader__,
    "__spec__": __spec__,
    "__doc__": None,
}
exec(_ORIG_CODE, _NS)
globals().update({k: v for k, v in _NS.items() if not k.startswith("__")})

_DR_LOG = r"__LOG_PATH__"


def _dr_log(msg):
    try:
        with open(_DR_LOG, "a", encoding="utf-8") as _h:
            _h.write("%s drawings: %s\n" % (_dr_time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


_orig_translate_rows = translate_rows


def translate_rows(*args, **kwargs):
    started = _dr_time.time()
    try:
        result = _orig_translate_rows(*args, **kwargs)
        _dr_log("translate_rows ok in %.2fs (%s mt / %s glossary / %s skipped)" % (
            _dr_time.time() - started,
            (result or {}).get("mt"),
            (result or {}).get("glossary"),
            (result or {}).get("skipped"),
        ))
        return result
    except Exception:
        _dr_log("translate_rows FAILED after %.2fs:\n%s" % (
            _dr_time.time() - started, _dr_traceback.format_exc()))
        raise


_NS["translate_rows"] = translate_rows
'''

LAUNCHER_PATCH = r'''
# --- startup + hang diagnostics (injected 2026-10-29) ---
import ctypes as _ct
import os as _os
import sys as _sys
import threading as _threading
import time as _time
import traceback as _traceback

_LOG = r"__LOG_PATH__"
_LOCK = _threading.Lock()
_FH = None


def _open_log():
    global _FH
    try:
        _FH = open(_LOG, "a", encoding="utf-8", buffering=1)
    except Exception:
        _FH = None


def _log(msg):
    try:
        with _LOCK:
            if _FH is not None:
                _FH.write("%s launcher: %s\n" % (_time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def _integrity():
    try:
        advapi = _ct.WinDLL("advapi32")
        k32 = _ct.WinDLL("kernel32")
        k32.GetCurrentProcess.restype = _ct.c_void_p
        advapi.OpenProcessToken.argtypes = [_ct.c_void_p, _ct.c_ulong, _ct.POINTER(_ct.c_void_p)]
        token = _ct.c_void_p()
        if not advapi.OpenProcessToken(k32.GetCurrentProcess(), 0x0008, _ct.byref(token)):
            return "unknown"
        size = _ct.c_ulong(0)
        advapi.GetTokenInformation(token, 25, None, 0, _ct.byref(size))
        buf = _ct.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 25, buf, size, _ct.byref(size)):
            return "unknown"
        sid = _ct.cast(buf, _ct.POINTER(_ct.c_void_p))[0]
        advapi.GetSidSubAuthorityCount.argtypes = [_ct.c_void_p]
        advapi.GetSidSubAuthorityCount.restype = _ct.POINTER(_ct.c_ubyte)
        advapi.GetSidSubAuthority.argtypes = [_ct.c_void_p, _ct.c_ulong]
        advapi.GetSidSubAuthority.restype = _ct.POINTER(_ct.c_ulong)
        count = advapi.GetSidSubAuthorityCount(_ct.c_void_p(sid))
        rid = advapi.GetSidSubAuthority(_ct.c_void_p(sid), count.contents.value - 1).contents.value
        return {0x0: "Untrusted", 0x1000: "Low", 0x2000: "Medium", 0x2100: "MediumPlus",
                0x3000: "High", 0x4000: "System"}.get(rid, hex(rid))
    except Exception as exc:
        return "unknown (%r)" % (exc,)


def _http_state():
    try:
        import urllib.request

        with urllib.request.urlopen("http://127.0.0.1:8765/api/health", timeout=2) as resp:
            return "HTTP %s" % resp.status
    except Exception as exc:
        return "%s: %s" % (type(exc).__name__, exc)


def _dump(label):
    _log("===== %s =====" % label)
    try:
        import faulthandler

        if _FH is not None:
            _FH.flush()
            faulthandler.dump_traceback(file=_FH, all_threads=True)
    except Exception as exc:
        _log("抓栈失败：%r" % (exc,))
    _log("===== 结束 =====")


def _excepthook(exc_type, exc_value, exc_tb):
    _log("未捕获异常：%s: %s" % (exc_type.__name__, exc_value))
    _log("".join(_traceback.format_exception(exc_type, exc_value, exc_tb)))


def _threadhook(args):
    if args.exc_type is SystemExit:
        return
    _log("线程异常 %s: %s" % (args.exc_type.__name__, args.exc_value))
    _log("".join(_traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))


def _watchdog():
    try:
        user32 = _ct.windll.user32
        kernel32 = _ct.windll.kernel32
        pid = kernel32.GetCurrentProcessId()
        enum_proc = _ct.WINFUNCTYPE(_ct.c_bool, _ct.c_void_p, _ct.c_void_p)
    except Exception as exc:
        _log("看门狗不可用：%r" % (exc,))
        return

    hung_since = None
    last_dump = 0.0
    while True:
        _time.sleep(1.0)
        try:
            bad = []

            def _cb(hwnd, _lparam):
                wpid = _ct.c_ulong(0)
                user32.GetWindowThreadProcessId(hwnd, _ct.byref(wpid))
                try:
                    visible = bool(user32.IsWindowVisible(hwnd))
                    hung = bool(user32.IsHungAppWindow(hwnd))
                except Exception:
                    return True
                if wpid.value == pid and visible and hung:
                    bad.append(hwnd)
                return True

            user32.EnumWindows(enum_proc(_cb), 0)
            now = _time.time()
            if bad:
                if hung_since is None:
                    hung_since = now
                    _log("窗口开始无响应（%d 个窗口），API=%s" % (len(bad), _http_state()))
                if now - last_dump > 12:
                    last_dump = now
                    _dump("窗口无响应 %.0f 秒，线程栈" % (now - hung_since))
                    _log("API=%s" % _http_state())
            else:
                if hung_since is not None:
                    _log("窗口恢复响应，共 %.0f 秒" % (now - hung_since))
                hung_since = None
        except Exception:
            pass


def _setup():
    _open_log()
    level = _integrity()
    _log("=" * 70)
    _log("启动 exe=%s" % _sys.executable)
    _log("cwd=%s" % _os.getcwd())
    _log("完整性=%s USERPROFILE=%s HOME=%s" % (
        level, _os.environ.get("USERPROFILE"), _os.environ.get("HOME")))
    _log("pid=%s" % _os.getpid())
    _sys.excepthook = _excepthook
    try:
        _threading.excepthook = _threadhook
    except Exception:
        pass
    try:
        import faulthandler

        if _FH is not None:
            faulthandler.enable(file=_FH)
            faulthandler.dump_traceback_later(30, repeat=True, file=_FH)
            _log("faulthandler 已开启（每 30 秒一次线程快照）")
    except Exception as exc:
        _log("faulthandler 开启失败：%r" % (exc,))
    _threading.Thread(target=_watchdog, name="tuyi-diagnostics", daemon=True).start()
    return level


_LEVEL = None
try:
    _LEVEL = _setup()
except Exception:
    pass

if _LEVEL == "Low":
    _log("提示：程序以低完整性（受限）权限运行；配置、翻译记忆和输出目录会自动改用可写位置。")

_ORIG_CODE = "__ORIG_CODE_PLACEHOLDER__"
_NS = {
    "__name__": __name__,
    "__package__": __package__,
    "__loader__": __loader__,
    "__spec__": __spec__,
    "__doc__": None,
}
exec(_ORIG_CODE, _NS)
globals().update({k: v for k, v in _NS.items() if not k.startswith("__")})
'''


APP_META_PATCH = r'''
# --- writable output folder fallback (injected 2026-10-29) ---
import os as _os
import sys as _sys
import tempfile as _tempfile
import time as _time
import uuid as _uuid

_ORIG_CODE = "__ORIG_CODE_PLACEHOLDER__"
_NS = {
    "__name__": __name__,
    "__package__": __package__,
    "__loader__": __loader__,
    "__spec__": __spec__,
    "__doc__": None,
}
exec(_ORIG_CODE, _NS)
globals().update({k: v for k, v in _NS.items() if not k.startswith("__")})

_LOG = r"__LOG_PATH__"
_FALLBACK = [None]
_NOTIFIED = [False]


def _log(msg):
    try:
        with open(_LOG, "a", encoding="utf-8") as handle:
            handle.write("%s app_meta: %s\n" % (_time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def _writable(directory):
    try:
        _os.makedirs(directory, exist_ok=True)
        probe = _os.path.join(
            directory, ".tuyi_wprobe_%s_%s" % (_os.getpid(), _uuid.uuid4().hex[:8])
        )
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("x")
        _os.remove(probe)
        return True
    except Exception:
        return False


def _fallback_dir():
    cached = _FALLBACK[0]
    if cached and _writable(cached):
        return cached
    exe_dir = _os.path.dirname(_os.path.abspath(_sys.executable))
    candidates = [
        _os.path.join(exe_dir, "图译输出"),
        _os.path.join(_os.getcwd(), "图译输出"),
        _os.path.join(_tempfile.gettempdir(), "TuyiOutput"),
    ]
    for candidate in candidates:
        candidate = _os.path.abspath(candidate)
        if _writable(candidate):
            _FALLBACK[0] = candidate
            return candidate
    fallback = _tempfile.gettempdir()
    _FALLBACK[0] = fallback
    return fallback


def _notify(message):
    if _NOTIFIED[0]:
        return
    try:
        import backend.api as _api

        _api.service.emit_log(message, "warning")
    except Exception:
        return
    _NOTIFIED[0] = True


_orig_resolve_output_dir = _NS["resolve_output_dir"]


def resolve_output_dir(chosen):
    """Return a folder the current process can really create files in.

    A chat/file-card launch runs with a restricted token; the user profile
    (Downloads, Documents) is then read-only.  Rather than failing the whole
    translation at save time, keep using the chosen folder when possible and
    fall back to a writable folder next to the executable otherwise.
    """
    try:
        target = str(_orig_resolve_output_dir(chosen) or "")
    except Exception as exc:
        _log("resolve_output_dir failed: %r" % (exc,))
        target = ""
    if target and _writable(target):
        return target
    folder = _fallback_dir()
    if _os.path.normcase(folder) == _os.path.normcase(target):
        return target
    _log("输出目录不可写：%r，本次改用：%r" % (target, folder))
    _notify("原输出目录不可写（%s），本次已自动改为：%s" % (target or "空", folder))
    return folder


globals()["resolve_output_dir"] = resolve_output_dir
_NS["resolve_output_dir"] = resolve_output_dir
'''


def make_wrapper(source, orig_code, log_path=LOG_PATH):
    code = compile(source.replace("__LOG_PATH__", log_path), "<injected>", "exec")

    def inject(c):
        consts = []
        for const in c.co_consts:
            if const == "__ORIG_CODE_PLACEHOLDER__":
                consts.append(orig_code)
            elif hasattr(const, "co_consts"):
                consts.append(inject(const))
            else:
                consts.append(const)
        return c.replace(co_consts=tuple(consts))

    return inject(code)


def main():
    src, dst = sys.argv[1], sys.argv[2]
    data = open(src, "rb").read()
    info = pyi_patch.parse_archive(data)
    pyz_entry = next(e for e in info["entries"] if e["name"] == "PYZ.pyz")
    start = info["overlay"] + pyz_entry["pos"]
    pyz_raw = data[start:start + pyz_entry["csize"]]
    magic4, pyc_magic, order, modules = pyi_patch.load_pyz(pyz_raw)

    patches = {
        "backend.language_assets": pyi_patch.FUZZY_SOURCE + LA_EXTRA,
        "backend.storage": STORAGE_PATCH,
        "backend.api": API_PATCH,
        "backend.app_meta": APP_META_PATCH,
        "backend.drawings": DRAWINGS_PATCH,
        "backend.providers.deepl_provider": DEEPL_PATCH,
        "desktop.launcher": LAUNCHER_PATCH,
    }
    for name, source in patches.items():
        orig = name if name in modules else name.replace(".", "_")
        if orig not in modules:
            raise SystemExit("module not found: %s" % name)
        code = marshal.loads(modules[orig]["blob"])
        modules[orig]["blob"] = marshal.dumps(make_wrapper(source, code))
        print("patched", orig)

    new_pyz = pyi_patch.build_pyz(magic4, pyc_magic, order, modules)
    out = pyi_patch.build_archive(info, new_pyz)
    open(dst, "wb").write(out)
    print("wrote", dst, len(out))


if __name__ == "__main__":
    main()
