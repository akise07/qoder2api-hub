"""Central log paths, conservative legacy usage migration, and stream tee."""
import os
import shutil
import sys
import threading
from pathlib import Path


def resolve_log_root(app_root):
    return str(Path(os.environ.get("QD_LOG_DIR") or Path(app_root)/"logs").resolve())


def resolve_usage_dir(app_root):
    explicit = os.environ.get("QD_PROXY_USAGE_DIR")
    return str(Path(explicit).resolve()) if explicit else str(Path(resolve_log_root(app_root))/"usage")


def migrate_legacy_usage(app_root, destination=None):
    """Copy old data before first new start; never overwrite or delete originals.

    Existing destination wins. If destination has any file while source contains
    missing names, report conflicts instead of merging unrelated history.
    """
    old = Path(app_root)/"usage"
    new = Path(destination or resolve_usage_dir(app_root))
    if not old.is_dir() or old.resolve() == new.resolve():
        return {"copied": [], "conflicts": []}
    candidates = [p for p in old.iterdir() if p.is_file() and p.suffix in (".jsonl", ".json")]
    copied, conflicts = [], []
    populated = new.is_dir() and any(new.iterdir())
    for source in candidates:
        target = new/source.name
        if target.exists() or populated:
            conflicts.append(source.name)
            continue
        new.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix+".migrate.tmp")
        try:
            shutil.copy2(source, temporary)
            os.replace(temporary, target)
            copied.append(source.name)
        finally:
            if temporary.exists():
                temporary.unlink()
    return {"copied": copied, "conflicts": conflicts}


class _LogTee:
    """stderr/stdout 的 tee，**任何情况下都不向外抛异常**。

    线程里未捕获异常的打印（threading.excepthook / sys.excepthook）会直接
    写到 sys.stderr。只要这个流自己抛一次（日志文件被关掉、字符无法用 utf-8
    编码、磁盘/句柄出错……），打印异常的代码就会再失败一次，最终变成终端的
    “Exception in threading.excepthook / Exception ignored in thread started
    by / Exception ignored in sys.unraisablehook” 四行刷屏，而真正的错误
    反而看不到。所以写失败就丢弃这一份拷贝，绝不打断调用方。
    """

    def __init__(self, original, path):
        self.original = original
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # backslashreplace：孤立代理字符（surrogateescape 解出来的字节）
        # 写成 \udcXX，不会让写日志变成 UnicodeEncodeError。
        self.file = self.path.open("a", encoding="utf-8",
                                   errors="backslashreplace", buffering=1)
        self.lock = threading.Lock()
        self.encoding = "utf-8"
        self.errors = "backslashreplace"

    def write(self, text):
        with self.lock:
            try:
                self.original.write(text)
            except Exception:
                pass
            try:
                self.file.write(text)
            except Exception:
                pass
        return len(text)

    def flush(self):
        with self.lock:
            try:
                self.original.flush()
            except Exception:
                pass
            try:
                self.file.flush()
            except Exception:
                pass

    def isatty(self):
        return False

    def writable(self):
        return True

    def readable(self):
        return False

    def seekable(self):
        return False

    @property
    def closed(self):
        return False

    def fileno(self):
        return self.original.fileno()


def install_stream_logging(app_root):
    """Only call at server startup, not module import or unit-test bootstrap.

    Launchers may already redirect to these files; QD_LOG_REDIRECTED avoids
    duplicate writes. Direct terminal/container starts retain console output.
    """
    root = Path(resolve_log_root(app_root))
    root.mkdir(parents=True, exist_ok=True)
    if os.environ.get("QD_LOG_REDIRECTED") != "1":
        if not isinstance(sys.stdout, _LogTee):
            sys.stdout = _LogTee(sys.stdout, root/"gateway.log")
        if not isinstance(sys.stderr, _LogTee):
            sys.stderr = _LogTee(sys.stderr, root/"gateway.err.log")
    return str(root)


# 客户端断开长连接时服务端常见错误码：纯连接回收噪音，不需要报告。
_BENIGN_WINERRORS = frozenset((10038, 10053, 10054, 10058, 10060, 10061))
_crash_log_lock = threading.Lock()


def _is_connection_noise(exc_type, exc_value):
    try:
        if isinstance(exc_type, type) and issubclass(exc_type,
                                                     (ConnectionError, TimeoutError)):
            return True
        winerror = getattr(exc_value, "winerror", None)
        return winerror in _BENIGN_WINERRORS
    except Exception:
        return False


def _one_line(value, limit=400):
    try:
        text = str(value).replace("\r", " ").replace("\n", " ")
    except Exception:
        text = "<unprintable>"
    return text[:limit]


def install_crash_logging(app_root):
    """替换默认的线程/不可抛出异常钩子，保证崩溃信息可读。

    默认行为在“异常本身无法格式化”时只会把四行英文刷屏打到终端
    （Exception in threading.excepthook / Exception ignored in thread
    started by / Exception ignored in sys.unraisablehook），真正的
    异常类型与堆栈都会丢失。这里改成：

      * 终端只留一行 `[thread-crash] <线程>: <类型>: <消息>`；
      * 完整堆栈追加到 logs/thread-crash.log（编码失败也不会再抛）；
      * 连接被客户端中断这类回收噪音直接静默。

    必须在 install_stream_logging 之后调用，这样 stderr 已经是 tee。
    """
    import traceback

    root = Path(resolve_log_root(app_root))
    root.mkdir(parents=True, exist_ok=True)
    crash_file = root/"thread-crash.log"

    def _format(exc_type, exc_value, exc_tb):
        try:
            return "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        except BaseException:
            return "%r\n%r\n" % (exc_type, exc_value)

    def _emit(headline, detail):
        try:
            sys.stderr.write(headline + "\n")
            sys.stderr.flush()
        except BaseException:
            pass
        try:
            with _crash_log_lock:
                with crash_file.open("a", encoding="utf-8",
                                     errors="backslashreplace") as fh:
                    fh.write(headline + "\n" + detail + "\n")
        except BaseException:
            pass

    def _thread_hook(args):
        if args.exc_type is SystemExit:
            return
        if _is_connection_noise(args.exc_type, args.exc_value):
            return
        name = args.thread.name if args.thread is not None else "?"
        _emit("[thread-crash] %s: %s: %s"
              % (name, getattr(args.exc_type, "__name__", args.exc_type),
                 _one_line(args.exc_value)),
              _format(args.exc_type, args.exc_value, args.exc_traceback))

    def _unraisable_hook(unraisable):
        _emit("[thread-crash] unraisable in %r: %s: %s"
              % (unraisable.object,
                 getattr(unraisable.exc_type, "__name__", unraisable.exc_type),
                 _one_line(unraisable.exc_value)),
              _format(unraisable.exc_type, unraisable.exc_value,
                      unraisable.exc_traceback))

    threading.excepthook = _thread_hook
    sys.unraisablehook = _unraisable_hook
    return str(crash_file)
