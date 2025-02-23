# SPDX-License-Identifier: LGPL-2.1+

import ast
import contextlib
import copy
import enum
import fcntl
import functools
import importlib
import importlib.resources
import itertools
import logging
import os
import pwd
import re
import resource
import stat
import tempfile
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, TypeVar

from mkosi.types import PathString

T = TypeVar("T")
V = TypeVar("V")


def dictify(f: Callable[..., Iterator[tuple[T, V]]]) -> Callable[..., dict[T, V]]:
    def wrapper(*args: Any, **kwargs: Any) -> dict[T, V]:
        return dict(f(*args, **kwargs))

    return functools.update_wrapper(wrapper, f)


@dictify
def read_env_file(path: Path) -> Iterator[tuple[str, str]]:
    with path.open() as f:
        for line_number, line in enumerate(f, start=1):
            line = line.rstrip()
            if not line or line.startswith("#"):
                continue
            if (m := re.match(r"([A-Z][A-Z_0-9]+)=(.*)", line)):
                name, val = m.groups()
                if val and val[0] in "\"'":
                    val = ast.literal_eval(val)
                yield name, val
            else:
                logging.info(f"{path}:{line_number}: bad line {line!r}")


def read_os_release(root: Path = Path("/")) -> dict[str, str]:
    filename = root / "etc/os-release"
    if not filename.exists():
        filename = root / "usr/lib/os-release"

    return read_env_file(filename)


def format_rlimit(rlimit: int) -> str:
    limits = resource.getrlimit(rlimit)
    soft = "infinity" if limits[0] == resource.RLIM_INFINITY else str(limits[0])
    hard = "infinity" if limits[1] == resource.RLIM_INFINITY else str(limits[1])
    return f"{soft}:{hard}"


def sort_packages(packages: Iterable[str]) -> list[str]:
    """Sorts packages: normal first, paths second, conditional third"""

    m = {"(": 2, "/": 1}
    return sorted(packages, key=lambda name: (m.get(name[0], 0), name))


def flatten(lists: Iterable[Iterable[T]]) -> list[T]:
    """Flatten a sequence of sequences into a single list."""
    return list(itertools.chain.from_iterable(lists))


class INVOKING_USER:
    uid = int(os.getenv("SUDO_UID") or os.getenv("PKEXEC_UID") or os.getuid())
    gid = int(os.getenv("SUDO_GID") or os.getgid())
    invoked_as_root = (uid == 0)

    @classmethod
    def init(cls) -> None:
        name = cls.name()
        home = cls.home()
        logging.debug(f"Running as user '{name}' ({cls.uid}:{cls.gid}) with home {home}.")

    @classmethod
    def is_running_user(cls) -> bool:
        return cls.uid == os.getuid()

    @classmethod
    @functools.lru_cache(maxsize=1)
    def name(cls) -> str:
        return pwd.getpwuid(cls.uid).pw_name

    @classmethod
    @functools.lru_cache(maxsize=1)
    def home(cls) -> Path:
        return Path(f"~{cls.name()}").expanduser()


@contextlib.contextmanager
def chdir(directory: PathString) -> Iterator[None]:
    old = Path.cwd()

    if old == directory:
        yield
        return

    try:
        os.chdir(directory)
        yield
    finally:
        os.chdir(old)


def make_executable(path: Path) -> None:
    st = path.stat()
    os.chmod(path, st.st_mode | stat.S_IEXEC)


def try_import(module: str) -> None:
    try:
        importlib.import_module(module)
    except ModuleNotFoundError:
        pass


@contextlib.contextmanager
def flock(path: Path) -> Iterator[int]:
    fd = os.open(path, os.O_CLOEXEC|os.O_RDONLY)
    try:
        fcntl.fcntl(fd, fcntl.FD_CLOEXEC)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        os.close(fd)


@contextlib.contextmanager
def scopedenv(env: Mapping[str, Any]) -> Iterator[None]:
    old = copy.deepcopy(os.environ)
    os.environ |= env

    # python caches the default temporary directory so when we might modify TMPDIR we have to make sure it
    # gets recalculated (see https://docs.python.org/3/library/tempfile.html#tempfile.tempdir).
    tempfile.tempdir = None

    try:
        yield
    finally:
        os.environ = old
        tempfile.tempdir = None


class StrEnum(enum.Enum):
    def __str__(self) -> str:
        assert isinstance(self.value, str)
        return self.value

    # Used by enum.auto() to get the next value.
    @staticmethod
    def _generate_next_value_(name: str, start: int, count: int, last_values: Sequence[str]) -> str:
        return name.replace("_", "-")

    @classmethod
    def values(cls) -> list[str]:
        return list(map(str, cls))


def one_zero(b: bool) -> str:
    return "1" if b else "0"


@contextlib.contextmanager
def umask(mask: int) -> Iterator[None]:
    old = os.umask(mask)
    try:
        yield
    finally:
        os.umask(old)


def is_power_of_2(x: int) -> bool:
    return x > 0 and (x & x - 1 == 0)


@contextlib.contextmanager
def resource_path(mod: ModuleType) -> Iterator[Path]:
    t = importlib.resources.files(mod)
    with importlib.resources.as_file(t) as p:
        yield p
