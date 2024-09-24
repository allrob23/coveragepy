# Licensed under the Apache License: http://www.apache.org/licenses/LICENSE-2.0
# For details: https://github.com/nedbat/coveragepy/blob/master/NOTICE.txt

"""Execute files of Python code."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import inspect
import marshal
import os
import struct
import sys

from importlib.machinery import ModuleSpec
from types import CodeType, ModuleType
from typing import Any

from coverage import env
from coverage.exceptions import CoverageException, _ExceptionDuringRun, NoCode, NoSource
from coverage.files import canonical_filename, python_reported_file
from coverage.misc import isolate_module
from coverage.python import get_python_source

os = isolate_module(os)


PYC_MAGIC_NUMBER = importlib.util.MAGIC_NUMBER

class DummyLoader:
    """A shim for the pep302 __loader__, emulating pkgutil.ImpLoader.

    Currently only implements the .fullname attribute
    """
    def __init__(self, fullname: str, *_args: Any) -> None:
        self.fullname = fullname


def find_module(
    modulename: str,
) -> tuple[str | None, str, ModuleSpec]:
    """Find the module named `modulename`.

    Returns the file path of the module, the name of the enclosing
    package, and the spec.
    """
    try:
        spec = importlib.util.find_spec(modulename)
    except ImportError as err:
        raise NoSource(str(err)) from err
    if not spec:
        raise NoSource(f"No module named {modulename!r}")
    pathname = spec.origin
    packagename = spec.name
    if spec.submodule_search_locations:
        mod_main = modulename + ".__main__"
        spec = importlib.util.find_spec(mod_main)
        if not spec:
            raise NoSource(
                f"No module named {mod_main}; " +
                f"{modulename!r} is a package and cannot be directly executed",
            )
        pathname = spec.origin
        packagename = spec.name
    packagename = packagename.rpartition(".")[0]
    return pathname, packagename, spec


class PyRunner:
    """Multi-stage execution of Python code.

    This is meant to emulate real Python execution as closely as possible.

    """
    def __init__(self, args: list[str], as_module: bool = False) -> None:
        self.args = args
        self.as_module = as_module

        self.arg0 = args[0]
        self.package: str | None = None
        self.modulename: str | None = None
        self.pathname: str | None = None
        self.loader: DummyLoader | None = None
        self.spec: ModuleSpec | None = None

    def prepare(self) -> None:
        """Set sys.path properly.

        This needs to happen before any importing, and without importing anything.
        """
        path0: str | None
        if self.as_module:
            path0 = os.getcwd()
        elif os.path.isdir(self.arg0):
            # Running a directory means running the __main__.py file in that
            # directory.
            path0 = self.arg0
        else:
            path0 = os.path.abspath(os.path.dirname(self.arg0))

        if os.path.isdir(sys.path[0]):
            # sys.path fakery.  If we are being run as a command, then sys.path[0]
            # is the directory of the "coverage" script.  If this is so, replace
            # sys.path[0] with the directory of the file we're running, or the
            # current directory when running modules.  If it isn't so, then we
            # don't know what's going on, and just leave it alone.
            top_file = inspect.stack()[-1][0].f_code.co_filename
            sys_path_0_abs = os.path.abspath(sys.path[0])
            top_file_dir_abs = os.path.abspath(os.path.dirname(top_file))
            sys_path_0_abs = canonical_filename(sys_path_0_abs)
            top_file_dir_abs = canonical_filename(top_file_dir_abs)
            if sys_path_0_abs != top_file_dir_abs:
                path0 = None

        else:
            # sys.path[0] is a file. Is the next entry the directory containing
            # that file?
            if sys.path[1] == os.path.dirname(sys.path[0]):
                # Can it be right to always remove that?
                del sys.path[1]

        if path0 is not None:
            sys.path[0] = python_reported_file(path0)

    def _prepare2(self) -> None:
        """Do more preparation to run Python code.

        Includes finding the module to run and adjusting sys.argv[0].
        This method is allowed to import code.

        """
        if self.as_module:
            self.modulename = self.arg0
            pathname, self.package, self.spec = find_module(self.modulename)
            if self.spec is not None:
                self.modulename = self.spec.name
            self.loader = DummyLoader(self.modulename)
            assert pathname is not None
            self.pathname = os.path.abspath(pathname)
            self.args[0] = self.arg0 = self.pathname
        elif os.path.isdir(self.arg0):
            # Running a directory means running the __main__.py file in that
            # directory.
            for ext in [".py", ".pyc", ".pyo"]:
                try_filename = os.path.join(self.arg0, "__main__" + ext)
                # 3.8.10 changed how files are reported when running a
                # directory.
                try_filename = os.path.abspath(try_filename)
                if os.path.exists(try_filename):
                    self.arg0 = try_filename
                    break
            else:
                raise NoSource(f"Can't find '__main__' module in '{self.arg0}'")

            # Make a spec. I don't know if this is the right way to do it.
            try_filename = python_reported_file(try_filename)
            self.spec = importlib.machinery.ModuleSpec("__main__", None, origin=try_filename)
            self.spec.has_location = True
            self.package = ""
            self.loader = DummyLoader("__main__")
        else:
            self.loader = DummyLoader("__main__")

        self.arg0 = python_reported_file(self.arg0)

    def run(self) -> None:
        """Run the Python code!"""

        self._prepare2()

        # Create a module to serve as __main__
        main_mod = ModuleType("__main__")

        from_pyc = self.arg0.endswith((".pyc", ".pyo"))
        main_mod.__file__ = self.arg0
        if from_pyc:
            main_mod.__file__ = main_mod.__file__[:-1]
        if self.package is not None:
            main_mod.__package__ = self.package
        main_mod.__loader__ = self.loader   # type: ignore[assignment]
        if self.spec is not None:
            main_mod.__spec__ = self.spec

        main_mod.__builtins__ = sys.modules["builtins"]     # type: ignore[attr-defined]

        sys.modules["__main__"] = main_mod

        # Set sys.argv properly.
        sys.argv = self.args

        try:
            # Make a code object somehow.
            if from_pyc:
                code = make_code_from_pyc(self.arg0)
            else:
                code = make_code_from_py(self.arg0)
        except CoverageException:
            raise
        except Exception as exc:
            msg = f"Couldn't run '{self.arg0}' as Python code: {exc.__class__.__name__}: {exc}"
            raise CoverageException(msg) from exc

        # Execute the code object.
        # Return to the original directory in case the test code exits in
        # a non-existent directory.
        cwd = os.getcwd()
        try:
            exec(code, main_mod.__dict__)
        except SystemExit:                          # pylint: disable=try-except-raise
            # The user called sys.exit().  Just pass it along to the upper
            # layers, where it will be handled.
            raise
        except Exception:
            # Something went wrong while executing the user code.
            # Get the exc_info, and pack them into an exception that we can
            # throw up to the outer loop.  We peel one layer off the traceback
            # so that the coverage.py code doesn't appear in the final printed
            # traceback.
            typ, err, tb = sys.exc_info()
            assert typ is not None
            assert err is not None
            assert tb is not None

            # PyPy3 weirdness.  If I don't access __context__, then somehow it
            # is non-None when the exception is reported at the upper layer,
            # and a nested exception is shown to the user.  This getattr fixes
            # it somehow? https://bitbucket.org/pypy/pypy/issue/1903
            getattr(err, "__context__", None)

            # Call the excepthook.
            try:
                assert err.__traceback__ is not None
                err.__traceback__ = err.__traceback__.tb_next
                sys.excepthook(typ, err, tb.tb_next)
            except SystemExit:                      # pylint: disable=try-except-raise
                raise
            except Exception as exc:
                # Getting the output right in the case of excepthook
                # shenanigans is kind of involved.
                sys.stderr.write("Error in sys.excepthook:\n")
                typ2, err2, tb2 = sys.exc_info()
                assert typ2 is not None
                assert err2 is not None
                assert tb2 is not None
                err2.__suppress_context__ = True
                assert err2.__traceback__ is not None
                err2.__traceback__ = err2.__traceback__.tb_next
                sys.__excepthook__(typ2, err2, tb2.tb_next)
                sys.stderr.write("\nOriginal exception was:\n")
                raise _ExceptionDuringRun(typ, err, tb.tb_next) from exc
            else:
                sys.exit(1)
        finally:
            os.chdir(cwd)


def run_python_module(args: list[str]) -> None:
    """Run a Python module, as though with ``python -m name args...``.

    `args` is the argument array to present as sys.argv, including the first
    element naming the module being executed.

    This is a helper for tests, to encapsulate how to use PyRunner.

    """
    runner = PyRunner(args, as_module=True)
    runner.prepare()
    runner.run()


def run_python_file(args: list[str]) -> None:
    """Run a Python file as if it were the main program on the command line.

    `args` is the argument array to present as sys.argv, including the first
    element naming the file being executed.  `package` is the name of the
    enclosing package, if any.

    This is a helper for tests, to encapsulate how to use PyRunner.

    """
    runner = PyRunner(args, as_module=False)
    runner.prepare()
    runner.run()


def make_code_from_py(filename: str) -> CodeType:
    """Get source from `filename` and make a code object of it."""
    try:
        source = get_python_source(filename)
    except (OSError, NoSource) as exc:
        raise NoSource(f"No file to run: '{filename}'") from exc

    code = compile(source, filename, mode="exec", dont_inherit=True)
    return code


def make_code_from_pyc(filename: str) -> CodeType:
    """Get a code object from a .pyc file."""
    try:
        fpyc = open(filename, "rb")
    except OSError as exc:
        raise NoCode(f"No file to run: '{filename}'") from exc

    with fpyc:
        # First four bytes are a version-specific magic number.  It has to
        # match or we won't run the file.
        magic = fpyc.read(4)
        if magic != PYC_MAGIC_NUMBER:
            raise NoCode(f"Bad magic number in .pyc file: {magic!r} != {PYC_MAGIC_NUMBER!r}")

        flags = struct.unpack("<L", fpyc.read(4))[0]
        hash_based = flags & 0x01
        if hash_based:
            fpyc.read(8)    # Skip the hash.
        else:
            # Skip the junk in the header that we don't need.
            fpyc.read(4)    # Skip the moddate.
            fpyc.read(4)    # Skip the size.

        # The rest of the file is the code object we want.
        code = marshal.load(fpyc)
        assert isinstance(code, CodeType)

    return code
