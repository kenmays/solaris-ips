#!/usr/bin/python

# CDDL HEADER START
#
# The contents of this file are subject to the terms of the
# Common Development and Distribution License (the "License").
# You may not use this file except in compliance with the License.
#
# You can obtain a copy of the license at usr/src/OPENSOLARIS.LICENSE
# or http://www.opensolaris.org/os/licensing.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# When distributing Covered Code, include this CDDL HEADER in each
# file and include the License file at usr/src/OPENSOLARIS.LICENSE.
# If applicable, add the following below this CDDL HEADER, with the
# fields enclosed by brackets "[]" replaced with your own identifying
# information: Portions Copyright [yyyy] [name of copyright owner]
#
# CDDL HEADER END
#

# Copyright (c) 2008, 2010, Oracle and/or its affiliates. All rights reserved.

import baseline
import ConfigParser
import copy
import difflib
import errno
import gettext
import hashlib
import logging
import os
import pprint
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import platform
import pwd
import re
import textwrap

EmptyI = tuple()
EmptyDict = dict()

# relative to our proto area
path_to_pub_util = "../../src/util/publish"

#
# These are initialized by pkg5testenv.setup_environment.
#
g_proto_area = "TOXIC"
# User's value for TEMPDIR
g_tempdir = "/tmp"

g_debug_output = False
if "DEBUG" in os.environ:
        g_debug_output = True

#
# XXX?
#
gettext.install("pkg", "/usr/share/locale")

OUTPUT_DOTS = 0         # Dots ...
OUTPUT_VERBOSE = 1      # Verbose
OUTPUT_PARSEABLE = 2    # Machine readable

class TestStopException(Exception):
        """An exception used to signal that all testing should cease.
        This is a framework-internal exception that tests should not
        raise"""
        pass

class TestSkippedException(Exception):
        """An exception used to signal that a test was skipped.
        Should be initialized with a string giving a more detailed
        reason.  Test cases can raise this to the framework
        that some prerequisite of the test is unsatisfied.  A string
        explaining the error should be passed at construction.  """
        def __str__(self):
                return "Test Skipped: " + " ".join(self.args)



#
# Errors for which the traceback is likely not useful.
#
import pkg.depotcontroller as depotcontroller
import pkg.portable as portable
import pkg.client.api
import pkg.client.progress

# Version test suite is known to work with.
PKG_CLIENT_NAME = "pkg"
CLIENT_API_VERSION = 47

ELIDABLE_ERRORS = [ TestSkippedException, depotcontroller.DepotStateException ]

class Pkg5CommonException(AssertionError):
        def __init__(self, com = ""):
                Pkg5TestCase.failureException.__init__(self, com)

        topdivider = \
        ",---------------------------------------------------------------------\n"
        botdivider = \
        "`---------------------------------------------------------------------\n"
        def format_comment(self, comment):
                if comment is not None:
                        comment = comment.expandtabs()
                        comm = ""
                        for line in comment.splitlines():
                                line = line.strip()
                                if line == "":
                                        continue
                                comm += "  " + line + "\n"
                        return comm + "\n"
                else:
                        return "<no comment>\n\n"

        def format_output(self, command, output):
                str = "  Output Follows:\n"
                str += self.topdivider
                if command is not None:
                        str += "| $ " + command + "\n"

                if output is None or output == "":
                        str += "| <no output>\n"
                else:
                        for line in output.split("\n"):
                                str += "| " + line.rstrip() + "\n"
                str += self.botdivider
                return str

        def format_debug(self, output):
                str = "  Debug Buffer Follows:\n"
                str += self.topdivider

                if output is None or output == "":
                        str += "| <no debug buffer>\n"
                else:
                        for line in output.split("\n"):
                                str += "| " + line.rstrip() + "\n"
                str += self.botdivider
                return str


class AssFailException(Pkg5CommonException):
        def __init__(self, comment = None, debug=None):
                Pkg5CommonException.__init__(self, comment)
                self.__comment = comment
                self.__debug = debug

        def __str__(self):
                str = ""
                if self.__comment is None:
                        str += Exception.__str__(self)
                else:
                        str += self.format_comment(self.__comment)
                if self.__debug is not None and self.__debug != "":
                        str += self.format_debug(self.__debug)
                return str


class DebugLogHandler(logging.Handler):
        """This class is a special log handler to redirect logger output to
        the test case class' debug() method.
        """

        def __init__(self, test_case):
                self.test_case = test_case
                logging.Handler.__init__(self)

        def emit(self, record):
                self.test_case.debug(record)

def setup_logging(test_case):
        # Ensure logger messages output by unit tests are redirected
        # to debug output so they are not shown by default.
        from pkg.client import global_settings
        log_handler = DebugLogHandler(test_case)
        global_settings.info_log_handler = log_handler
        global_settings.error_log_handler = log_handler


class Pkg5TestCase(unittest.TestCase):

        # Needed for compatability
        failureException = AssertionError

        bogus_url = "test.invalid"
        __debug_buf = ""

        def __init__(self, methodName='runTest'):
                super(Pkg5TestCase, self).__init__(methodName)
                self.__test_root = None
                self.__pid = os.getpid()
                self.__pwd = os.getcwd()
                self.__didteardown = False
                setup_logging(self)

        def __str__(self):
                return "%s.py %s.%s" % (self.__class__.__module__,
                    self.__class__.__name__, self._testMethodName)

        #
        # Uses property() to implements test_root as a read-only attribute.
        #
        test_root = property(fget=lambda self: self.__test_root)

        def __get_ro_data_root(self):
                if not self.__test_root:
                        return None
                return os.path.join(self.__test_root, "ro_data")
        
        ro_data_root = property(fget=__get_ro_data_root)

        def cmdline_run(self, cmdline, comment="", coverage=True, exit=0,
            handle=False, out=False, prefix="", raise_error=True, su_wrap=None,
            stderr=False):
                wrapper = ""
                if coverage:
                        wrapper = self.coverage_cmd
                su_wrap, su_end = self.get_su_wrapper(su_wrap=su_wrap)

                cmdline = "%s%s%s %s%s" % (prefix, su_wrap, wrapper,
                    cmdline, su_end)
                self.debugcmd(cmdline)

                newenv = os.environ.copy()
                if coverage:
                        newenv.update(self.coverage_env)

                p = subprocess.Popen(cmdline,
                    env=newenv,
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE)

                if handle:
                        # Do nothing more.
                        return p
                self.output, self.errout = p.communicate()
                retcode = p.returncode
                self.debugresult(retcode, exit, self.output)
                if self.errout != "":
                        self.debug(self.errout)

                if raise_error and retcode == 99:
                        raise TracebackException(cmdline, self.output +
                            self.errout, comment)

                if not isinstance(exit, list):
                        exit = [exit]

                if raise_error and retcode not in exit:
                        raise UnexpectedExitCodeException(cmdline,
                            exit, retcode, self.output + self.errout,
                            comment)

                if out:
                        if stderr:
                                return retcode, self.output, self.errout
                        return retcode, self.output
                return retcode

        def debug(self, s):
                s = str(s)
                for x in s.splitlines():
                        if g_debug_output:
                                print >> sys.stderr, "# %s" % x
                        self.__debug_buf += x + "\n"

        def debugcmd(self, cmdline):
                wrapper = textwrap.TextWrapper(initial_indent="$ ",
                    subsequent_indent="\t",
                    break_long_words=False,
                    break_on_hyphens=False)
                res = wrapper.wrap(cmdline.strip())
                self.debug(" \\\n".join(res))

        def debugfilecreate(self, content, path):
                lines = content.splitlines()
                if lines == []:
                        lines = [""]
                if len(lines) > 1:
                        ins = " [+%d lines...]" % (len(lines) - 1)
                else:
                        ins = ""
                self.debugcmd(
                    "echo '%s%s' > %s" % (lines[0], ins, path))

        def debugresult(self, retcode, expected, output):
                if output.strip() != "":
                        self.debug(output.strip())
                if not isinstance(expected, list):
                        expected = [expected]
                if retcode is None or retcode != 0 or \
                    retcode not in expected:
                        self.debug("[exited %s, expected %s]" %
                            (retcode, ", ".join(str(e) for e in expected)))

        def get_debugbuf(self):
                return self.__debug_buf

        def get_su_wrapper(self, su_wrap=None):
                if su_wrap:
                        if su_wrap == True:
                                su_wrap = get_su_wrap_user()
                        cov_env = " ".join(
                            ("%s=%s" % e for e in self.coverage_env.items()))
                        su_wrap = "su %s -c 'LD_LIBRARY_PATH=%s %s " % \
                            (su_wrap, os.getenv("LD_LIBRARY_PATH", ""), cov_env)
                        su_end = "'"
                else:
                        su_wrap = ""
                        su_end = ""
                return su_wrap, su_end

        def getTeardownFunc(self):
                return (self, self.tearDown)

        def getSetupFunc(self):
                return (self, self.setUp)

        def setUp(self):
                self.__test_root = os.path.join(g_tempdir,
                    "ips.test.%d" % self.__pid)
                self.__didtearDown = False
                try:
                        os.makedirs(self.__test_root, 0755)
                except OSError, e:
                        if e.errno != errno.EEXIST:
                                raise e
                test_relative = os.path.sep.join(["..", "..", "src", "tests"])
                test_src = os.path.join(g_proto_area, test_relative)
                shutil.copytree(os.path.join(test_src, "ro_data"),
                    self.ro_data_root)
                #
                # TMPDIR affects the behavior of mkdtemp and mkstemp.
                # Setting this here should ensure that tests will make temp
                # files and dirs inside the test directory rather than
                # polluting /tmp.
                #
                os.environ["TMPDIR"] = self.__test_root
                tempfile.tempdir = self.__test_root
                setup_logging(self)

                self.configure_rcfile( "%s/usr/share/lib/pkg/pkglintrc" %
                    g_proto_area,
                    {"info_classification_path":
                    "%s/usr/share/package-manager/data/opensolaris.org.sections" %
                    g_proto_area}, self.test_root, section="pkglint")

        def impl_tearDown(self):
                # impl_tearDown exists so that we can ensure that this class's
                # teardown is actually called.  Sometimes, subclasses will
                # implement teardown but forget to call the superclass teardown.
                if self.__didteardown:
                        return
                self.__didteardown = True
                try:
                        os.chdir(self.__pwd)
                except OSError:
                        # working directory of last resort.
                        os.chdir(g_tempdir)

                #
                # Kill depots before blowing away test dir-- otherwise
                # the depot can race with the shutil.rmtree()
                #
                if hasattr(self, "killalldepots"):
                        try:
                                self.killalldepots()
                        except Exception, e:
                                print >> sys.stderr, str(e)

                #
                # We have some sloppy subclasses which don't call the superclass
                # setUp-- in which case the dir might not exist.  Tolerate it.
                #
                if self.__test_root is not None and \
                    os.path.exists(self.__test_root):
                        shutil.rmtree(self.__test_root)

        def tearDown(self):
                # In reality this call does nothing.
                unittest.TestCase.tearDown(self)

                self.impl_tearDown()

        def run(self, result=None):
                if result is None:
                        result = self.defaultTestResult()
                pwd = os.getcwd()
                result.startTest(self)
                testMethod = getattr(self, self._testMethodName)
                if getattr(result, "coverage", None) is not None:
                        self.coverage_cmd, self.coverage_env = result.coverage
                else:
                        self.coverage_cmd, self.coverage_env = "", {}
                try:
                        needtodie = False
                        try:
                                self.setUp()
                        except KeyboardInterrupt:
                                # Try hard to make sure we've done a teardown.
                                try:
                                        self.tearDown()
                                except:
                                        pass
                                self.impl_tearDown()
                                raise TestStopException
                        except:
                                # teardown could fail too, esp. if setup failed...
                                try:
                                        self.tearDown()
                                except:
                                        pass
                                # Try hard to make sure we've done a teardown.
                                self.impl_tearDown()
                                result.addError(self, sys.exc_info())
                                return

                        ok = False
                        error_added = False
                        try:
                                testMethod()
                                ok = True
                        except self.failureException:
                                result.addFailure(self, sys.exc_info())
                        except KeyboardInterrupt:
                                # Try hard to make sure we've done a teardown.
                                needtodie = True
                        except:
                                error_added = True
                                result.addError(self, sys.exc_info())

                        try:
                                self.tearDown()
                        except KeyboardInterrupt:
                                needtodie = True
                        except:
                                # Try hard to make sure we've done a teardown.
                                self.impl_tearDown()
                                # Make sure we don't mark this error'd twice.
                                if not error_added:
                                        result.addError(self, sys.exc_info())
                                ok = False

                        if needtodie:
                                try:
                                        self.impl_tearDown()
                                except:
                                        pass
                                raise TestStopException

                        if ok:
                                result.addSuccess(self)
                finally:
                        result.stopTest(self)
                        # make sure we restore our directory if it still exists.
                        try:
                                os.chdir(pwd)
                        except OSError, e:
                                # If directory doesn't exist anymore it doesn't
                                # matter.
                                if e.errno != errno.ENOENT:
                                        raise

        #
        # The following are utility functions for use by testcases.
        #
        def c_compile(self, prog_text, opts, outputfile):
                """Given a C program (as a string), compile it into the
                executable given by outputfile.  Outputfile should be
                given as a relative path, and will be located below the
                test prefix path.  Additional compiler options should be
                passed in 'opts'.  Suitable for compiling small test
                programs."""

                #
                # We use a series of likely compilers.  At present we support
                # this testing with SunStudio.
                #
                if os.path.dirname(outputfile) != "":
                        try:
                                os.makedirs(os.path.dirname(outputfile))
                        except OSError, e:
                                if e.errno != errno.EEXIST:
                                        raise
                c_fd, c_path = tempfile.mkstemp(suffix=".c",
                    dir=self.test_root)
                c_fh = os.fdopen(c_fd, "w")
                c_fh.write(prog_text)
                c_fh.close()

                found = False
                outpath = os.path.join(self.test_root, outputfile)
                compilers = ["/usr/bin/cc", "cc", "$CC"]
                for compiler in compilers:
                        cmd = [compiler, "-o", outpath]
                        cmd.extend(opts)
                        cmd.append(c_path)
                        try:
                                # Make sure to use shell=True so that env.
                                # vars and $PATH are evaluated.
                                self.debugcmd(" ".join(cmd))
                                s = subprocess.Popen(" ".join(cmd),
                                    shell=True,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT)
                                sout, serr = s.communicate()
                                rc = s.returncode
                                if rc != 0 and rc != 127:
                                        try: os.remove(outpath)
                                        except OSError: pass
                                        try: os.remove(c_path)
                                        except OSError: pass
                                        raise RuntimeError(
                                            "Compile failed: %s --> %d\n%s" % \
                                            (cmd, rc, sout))
                                if rc == 127:
                                        self.debug("[%s not found]" % compiler)
                                        continue
                                # so rc == 0
                                found = True
                                break
                        except OSError:
                                continue
                try:
                        os.remove(c_path)
                except OSError:
                        pass
                if not found:
                        raise TestSkippedException(
                            "No suitable Sun Studio compiler found. "
                            "Tried: %s.  Try setting $CC to a valid"
                            "compiler." % compilers)


        def make_misc_files(self, files, prefix=None, mode=0644):
                """ Make miscellaneous text files.  Files can be a
                single relative pathname, a list of relative pathnames,
                or a hash mapping relative pathnames to specific contents.
                If file contents are not specified, the pathname of the
                file is placed into the file as default content. """

                outpaths = []
                #
                # If files is a string, make it a list.  Then, if it is
                # a list, simply turn it into a dict where each file's
                # contents is its own name, so that we get some uniqueness.
                #
                if isinstance(files, basestring):
                        files = [files]

                if isinstance(files, list):
                        nfiles = {}
                        for f in files:
                                nfiles[f] = f
                        files = nfiles

                if prefix is None:
                        prefix = self.test_root
                else:
                        assert(not prefix.startswith(os.pathsep))
                        prefix = os.path.join(self.test_root, prefix)

                for f, content in files.items():
                        assert not f.startswith("/"), \
                            ("%s: misc file paths must be relative!" % f)
                        path = os.path.join(prefix, f)
                        if not os.path.exists(os.path.dirname(path)):
                                os.makedirs(os.path.dirname(path))
                        self.debugfilecreate(content, path)
                        file_handle = open(path, 'wb')
                        if isinstance(content, unicode):
                                content = content.encode("utf-8")
                        file_handle.write(content)
                        file_handle.close()
                        os.chmod(path, mode)
                        outpaths.append(path)
                return outpaths

        def make_manifest(self, content, manifest_dir="manifests"):
                # Trim to ensure nice looking output.
                content = content.strip()

                # Place inside of test prefix.
                manifest_dir = os.path.join(self.test_root,
                    manifest_dir)

                if not os.path.exists(manifest_dir):
                        os.makedirs(manifest_dir)
                t_fd, t_path = tempfile.mkstemp(prefix="mfst.", dir=manifest_dir)
                t_fh = os.fdopen(t_fd, "w")
                t_fh.write(content)
                t_fh.close()
                self.debugfilecreate(content, t_path)
                return t_path

        @staticmethod
        def calc_file_hash(pth):
                # Find the hash of the file.
                fh = open(pth, "rb")
                s = fh.read()
                fh.close()
                hsh = hashlib.sha1()
                hsh.update(s)
                return hsh.hexdigest()

        def reduceSpaces(self, string):
                """Reduce runs of spaces down to a single space."""
                return re.sub(" +", " ", string)

        def assertEqualDiff(self, expected, actual):
                """Compare two strings."""

                if not isinstance(expected, basestring):
                        expected = pprint.pformat(expected)
                if not isinstance(actual, basestring):
                        actual = pprint.pformat(actual)

                self.assertEqual(expected, actual,
                    "Actual output differed from expected output.\n" +
                    "\n".join(difflib.unified_diff(
                        expected.splitlines(), actual.splitlines(),
                        "Expected output", "Actual output", lineterm="")))

        def configure_rcfile(self, rcfile, config, test_root, section="DEFAULT",
            suffix=""):
                """Reads the provided rcfile file, setting key/value
                pairs in the provided section those from the 'config'
                dictionary. The new config file is written to the supplied
                test_root, returning the name of that new file.

                Used to set keys to point to paths beneath our test_root,
                which would otherwise be shipped as hard-coded paths, relative
                to /.
                """

                new_rcfile = file("%s/%s%s" % (test_root, os.path.basename(rcfile),
                    suffix), "w")

                conf = ConfigParser.SafeConfigParser()
                conf.readfp(open(rcfile))

                for key in config:
                        conf.set(section, key, config[key])

                conf.write(new_rcfile)
                return new_rcfile.name


class _Pkg5TestResult(unittest._TextTestResult):
        baseline = None
        machsep = "|"

        def __init__(self, stream, output, baseline, bailonfail=False,
            show_on_expected_fail=False, archive_dir=None):
                unittest.TestResult.__init__(self)
                self.stream = stream
                self.output = output
                self.baseline = baseline
                self.success = []
                self.mismatches = []
                self.bailonfail = bailonfail
                self.show_on_expected_fail = show_on_expected_fail
                self.archive_dir = archive_dir

        def getDescription(self, test):
                return str(test)

        # Override the unittest version of this so that success is
        # considered "matching the baseline"
        def wasSuccessful(self):
                return len(self.mismatches) == 0

        def dobailout(self, test):
                """ Pull the ejection lever.  Stop execution, doing as
                much forcible cleanup as possible. """
                inst, tdf = test.getTeardownFunc()
                try:
                        tdf()
                except Exception, e:
                        print >> sys.stderr, str(e)
                        pass

                if getattr(test, "persistent_setup", None):
                        try:
                                test.reallytearDown()
                        except Exception, e:
                                print >> sys.stderr, str(e)
                                pass

                if hasattr(inst, "killalldepots"):
                        try:
                                inst.killalldepots()
                        except Exception, e:
                                print >> sys.stderr, str(e)
                                pass
                raise TestStopException()

        def fmt_parseable(self, match, actual, expected):
                if match == baseline.BASELINE_MATCH:
                        mstr = "MATCH"
                else:
                        mstr = "MISMATCH"
                return "%s|%s|%s" % (mstr, actual, expected)


        @staticmethod
        def fmt_prefix_with(instr, prefix):
                res = ""
                for s in instr.splitlines():
                        res += "%s%s\n" % (prefix, s)
                return res

        @staticmethod
        def fmt_box(instr, title, prefix=""):
                trailingdashes = (50 - len(title)) * "-"
                res = "\n.---" + title + trailingdashes + "\n"
                for s in instr.splitlines():
                        if s.strip() == "":
                                continue
                        res += "| %s\n" % s
                res += "`---" + len(title) * "-" + trailingdashes
                return _Pkg5TestResult.fmt_prefix_with(res, prefix)

        def do_archive(self, test, info):
                assert self.archive_dir
                if not os.path.exists(self.archive_dir):
                        os.makedirs(self.archive_dir, mode=0755)

                archive_path = os.path.join(self.archive_dir,
                    "%d" % os.getpid())
                if not os.path.exists(archive_path):
                        os.makedirs(archive_path, mode=0755)
                archive_path = os.path.join(archive_path, test.id())
                if g_debug_output:
                        self.stream.writeln("# Archiving to %s" % archive_path)

                if os.path.exists(test.test_root):
                        shutil.copytree(test.test_root, archive_path,
                            symlinks=True)
                else:
                        # If the test has failed without creating its directory,
                        # make it manually, so that we have a place to write out
                        # ERROR_INFO.
                        os.makedirs(archive_path, mode=0755)

                f = open(os.path.join(archive_path, "ERROR_INFO"), "w")
                f.write("------------------DEBUG LOG---------------\n")
                f.write(test.get_debugbuf())
                if info is not None:
                        f.write("\n\n------------------EXCEPTION---------------\n")
                        f.write(info)
                f.close()

        def addSuccess(self, test):
                unittest.TestResult.addSuccess(self, test)

                # If we're debugging, we'll have had output since we
                # announced the name of the test, so restate it.
                if g_debug_output:
                        self.statename(test)

                errinfo = self.format_output_and_exc(test, None)

                bresult = self.baseline.handleresult(str(test), "pass")
                expected = self.baseline.expectedresult(str(test))
                if self.output == OUTPUT_PARSEABLE:
                        res = self.fmt_parseable(bresult, "pass", expected)

                elif self.output == OUTPUT_VERBOSE:
                        if bresult == baseline.BASELINE_MATCH:
                                res = "match pass"
                        else:
                                res = "MISMATCH pass (expected: %s)" % \
                                    expected
                                res = self.fmt_box(errinfo,
                                    "Successful Test", "# ")
                else:
                        assert self.output == OUTPUT_DOTS
                        res = "."

                if self.output != OUTPUT_DOTS:
                        self.stream.writeln(res)
                else:
                        self.stream.write(res)
                self.success.append(test)

                if bresult == baseline.BASELINE_MISMATCH:
                        self.mismatches.append(test)

                if bresult == baseline.BASELINE_MISMATCH and self.archive_dir:
                        self.do_archive(test, None)

                # Bail out completely if the 'bail on fail' flag is set
                # but iff the result disagrees with the baseline.
                if self.bailonfail and bresult == baseline.BASELINE_MISMATCH:
                        self.dobailout(test)


        def addError(self, test, err):
                errtype, errval = err[:2]
                # for a few special exceptions, we delete the traceback so
                # as to elide it.  use only when the traceback itself
                # is not likely to be useful.
                if errtype in ELIDABLE_ERRORS:
                        unittest.TestResult.addError(self, test,
                            (err[0], err[1], None))
                else:
                        unittest.TestResult.addError(self, test, err)

                # If we're debugging, we'll have had output since we
                # announced the name of the test, so restate it.
                if g_debug_output:
                        self.statename(test)

                errinfo = self.format_output_and_exc(test, err)

                bresult = self.baseline.handleresult(str(test), "error")
                expected = self.baseline.expectedresult(str(test))
                if self.output == OUTPUT_PARSEABLE:
                        if errtype in ELIDABLE_ERRORS:
                                res = self.fmt_parseable(bresult, "ERROR", expected)
                                res += "\n# %s\n" % str(errval).strip()
                        else:
                                res = self.fmt_parseable(bresult, "ERROR", expected)
                                res += "\n"
                                if bresult == baseline.BASELINE_MISMATCH \
                                   or self.show_on_expected_fail:
                                        res += self.fmt_prefix_with(errinfo, "# ")

                elif self.output == OUTPUT_VERBOSE:
                        if bresult == baseline.BASELINE_MATCH:
                                b = "match"
                        else:
                                b = "MISMATCH"

                        if errtype in ELIDABLE_ERRORS:
                                res = "%s ERROR\n" % b
                                res += "#\t%s" % str(errval)
                        else:
                                res = "%s ERROR\n" % b
                                if bresult == baseline.BASELINE_MISMATCH \
                                   or self.show_on_expected_fail:
                                        res += self.fmt_box(errinfo,
                                            "Error Information", "# ")

                elif self.output == OUTPUT_DOTS:
                        if bresult == baseline.BASELINE_MATCH:
                                res = "e"
                        else:
                                res = "E"

                if self.output == OUTPUT_DOTS:
                        self.stream.write(res)
                else:
                        self.stream.writeln(res)

                if bresult == baseline.BASELINE_MISMATCH:
                        self.mismatches.append(test)

                # Check to see if we should archive this baseline mismatch.
                if bresult == baseline.BASELINE_MISMATCH and self.archive_dir:
                        self.do_archive(test, self._exc_info_to_string(err, test))

                # Bail out completely if the 'bail on fail' flag is set
                # but iff the result disagrees with the baseline.
                if self.bailonfail and bresult == baseline.BASELINE_MISMATCH:
                        self.dobailout(test)

        def format_output_and_exc(self, test, error):
                res = ""
                dbgbuf = test.get_debugbuf()
                if dbgbuf != "":
                        res += dbgbuf
                if error is not None:
                        res += self._exc_info_to_string(error, test)
                return res

        def addFailure(self, test, err):
                unittest.TestResult.addFailure(self, test, err)

                bresult = self.baseline.handleresult(str(test), "fail")
                expected = self.baseline.expectedresult(str(test))

                # If we're debugging, we'll have had output since we
                # announced the name of the test, so restate it.
                if g_debug_output:
                        self.statename(test)

                errinfo = self.format_output_and_exc(test, err)

                if self.output == OUTPUT_PARSEABLE:
                        res = self.fmt_parseable(bresult, "FAIL", expected)
                        res += "\n"
                        if bresult == baseline.BASELINE_MISMATCH \
                           or self.show_on_expected_fail:
                                res += self.fmt_prefix_with(errinfo, "# ")
                elif self.output == OUTPUT_VERBOSE:
                        if bresult == baseline.BASELINE_MISMATCH:
                                res = "MISMATCH FAIL (expected: %s)" % expected
                        else:
                                res = "match FAIL (expected: FAIL)"

                        if bresult == baseline.BASELINE_MISMATCH \
                           or self.show_on_expected_fail:
                                res += self.fmt_box(errinfo,
                                    "Failure Information", "# ")

                elif self.output == OUTPUT_DOTS:
                        if bresult == baseline.BASELINE_MATCH:
                                res = "f"
                        else:
                                res = "F"

                if self.output == OUTPUT_DOTS:
                        self.stream.write(res)
                else:
                        self.stream.writeln(res)

                if bresult == baseline.BASELINE_MISMATCH:
                        self.mismatches.append(test)

                # Check to see if we should archive this baseline mismatch.
                if bresult == baseline.BASELINE_MISMATCH and self.archive_dir:
                        self.do_archive(test, self._exc_info_to_string(err, test))

                # Bail out completely if the 'bail on fail' flag is set
                # but iff the result disagrees with the baseline.
                if self.bailonfail and bresult == baseline.BASELINE_MISMATCH:
                        self.dobailout(test)

        def addPersistentSetupError(self, test, err):
                errtype, errval = err[:2]

                errinfo = self.format_output_and_exc(test, err)

                res = "# ERROR during persistent setup for %s\n" % test.id()
                res += "# As a result, all test cases in this class will " \
                    "result in errors."

                if errtype in ELIDABLE_ERRORS:
                        res += "#   " + str(errval)
                else:
                        res += self.fmt_box(errinfo, \
                            "Persistent Setup Error Information", "# ")
                self.stream.writeln(res)

        def addPersistentTeardownError(self, test, err):
                errtype, errval = err[:2]

                errinfo = self.format_output_and_exc(test, err)

                res = "# ERROR during persistent teardown for %s\n" % test.id()
                if errtype in ELIDABLE_ERRORS:
                        res += "#   " + str(errval)
                else:
                        res += self.fmt_box(errinfo, \
                            "Persistent Teardown Error Information", "# ")
                self.stream.writeln(res)

        def statename(self, test, prefix=""):
                name = self.getDescription(test)
                if self.output == OUTPUT_VERBOSE:
                        name = name.ljust(65) + "  "
                elif self.output == OUTPUT_PARSEABLE:
                        name += "|"
                elif self.output == OUTPUT_DOTS:
                        return
                self.stream.write(name)

        def startTest(self, test):
                unittest.TestResult.startTest(self, test)
                test.debug("_" * 75)
                test.debug("Start:   %s" % \
                    self.getDescription(test))
                if test._testMethodDoc is not None:
                        docs = ["  " + x.strip() \
                            for x in test._testMethodDoc.splitlines()]
                        while len(docs) > 0 and docs[-1] == "":
                                del docs[-1]
                        for x in docs:
                                test.debug(x)
                test.debug("_" * 75)
                test.debug("")

                if not g_debug_output:
                        self.statename(test)

        def printErrors(self):
                self.stream.writeln()
                self.printErrorList('ERROR', self.errors)
                self.printErrorList('FAIL', self.failures)

        def printErrorList(self, flavour, errors):
                for test, err in errors:
                        self.stream.writeln(self.separator1)
                        self.stream.writeln("%s: %s" %
                            (flavour, self.getDescription(test)))
                        self.stream.writeln(self.separator2)
                        self.stream.writeln("%s" % err)

class Pkg5TestRunner(unittest.TextTestRunner):
        """TestRunner for test suites that we want to be able to compare
        against a result baseline."""
        baseline = None

        def __init__(self, baseline, stream=sys.stderr, output=OUTPUT_DOTS,
            timing_file=None, bailonfail=False, coverage=None,
            show_on_expected_fail=False, archive_dir=None):
                """Set up the test runner"""
                # output is one of OUTPUT_DOTS, OUTPUT_VERBOSE, OUTPUT_PARSEABLE
                super(Pkg5TestRunner, self).__init__(stream)
                self.baseline = baseline
                self.output = output
                self.timing_file = timing_file
                self.bailonfail = bailonfail
                self.coverage = coverage
                self.show_on_expected_fail = show_on_expected_fail
                self.archive_dir = archive_dir

        def _makeResult(self):
                return _Pkg5TestResult(self.stream, self.output, self.baseline,
                    bailonfail=self.bailonfail,
                    show_on_expected_fail=self.show_on_expected_fail,
                    archive_dir=self.archive_dir)

        @staticmethod
        def __write_timing_info(stream, suite_name, class_list, method_list):
                if not class_list and not method_list:
                        return
                tot = 0
                print >> stream, "Tests run for '%s' Suite, " \
                    "broken down by class:\n" % suite_name
                for secs, cname in class_list:
                        print >> stream, "%6.2f %s.%s" % (secs, suite_name, cname)
                        tot += secs
                        for secs, mcname, mname in method_list:
                                if mcname != cname:
                                        continue
                                print >> stream, \
                                    "    %6.2f %s" % (secs, mname)
                        print >> stream
                print >> stream, "%6.2f Total time\n" % tot
                print >> stream, "=" * 60
                print >> stream, "\nTests run for '%s' Suite, " \
                    "sorted by time taken:\n" % suite_name
                for secs, cname, mname in method_list:
                        print >> stream, "%6.2f %s %s" % (secs, cname, mname)
                print >> stream, "%6.2f Total time\n" % tot
                print >> stream, "=" * 60
                print >> stream, ""

        def _do_timings(self, test):
                timing = {}
                lst = []
                suite_name = None
                for t in test._tests:
                        for (sname, cname, mname), secs in t.timing.items():
                                lst.append((secs, cname, mname))
                                if cname not in timing:
                                        timing[cname] = 0
                                timing[cname] += secs
                                suite_name = sname
                lst.sort()
                clst = sorted((secs, cname) for cname, secs in timing.items())

                if self.timing_file:
                        try:
                                fh = open(self.timing_file, "ab+")
                                opened = True
                        except KeyboardInterrupt:
                                raise TestStopException()
                        except Exception:
                                fh = sys.stderr
                                opened = False
                        self.__write_timing_info(fh, suite_name, clst, lst)
                        if opened:
                                fh.close()

        def run(self, test):
                "Run the given test case or test suite."
                result = self._makeResult()

                startTime = time.time()
                result.coverage = self.coverage
                try:
                        test.run(result)
                finally:
                        stopTime = time.time()
                        timeTaken = stopTime - startTime

                        run = result.testsRun
                        if run > 0:
                                if self.output != OUTPUT_VERBOSE:
                                        result.printErrors()
                                        self.stream.writeln("# " + result.separator2)
                                self.stream.writeln("\n# Ran %d test%s in %.3fs" %
                                    (run, run != 1 and "s" or "", timeTaken))
                                self.stream.writeln()
                        if not result.wasSuccessful():
                                self.stream.write("FAILED (")
                                success, failed, errored, mismatches = map(len,
                                    (result.success, result.failures, result.errors,
                                        result.mismatches))
                                self.stream.write("successes=%d, " % success)
                                self.stream.write("failures=%d, " % failed)
                                self.stream.write("errors=%d, " % errored)
                                self.stream.write("mismatches=%d" % mismatches)
                                self.stream.writeln(")")

                        self._do_timings(test)
                return result


class Pkg5TestSuite(unittest.TestSuite):
        """Test suite that extends unittest.TestSuite to handle persistent
        tests.  Persistent tests are ones that are able to only call their
        setUp/tearDown functions once per class, instead of before and after
        every test case.  Aside from actually running the test it defers the
        majority of its work to unittest.TestSuite.

        To make a test class into a persistent one, add this class
        variable declaration:
                persistent_setup = True
        """

        def __init__(self, tests=()):
                unittest.TestSuite.__init__(self, tests)
                self.timing = {}

                # The site module deletes the function to change the
                # default encoding so a forced reload of sys has to
                # be done at least once.
                reload(sys)

        def cleanup_and_die(self, inst, info):
                print >> sys.stderr, \
                    "\nCtrl-C: Attempting cleanup during %s" % info

                if hasattr(inst, "killalldepots"):
                        print >> sys.stderr, "Killing depots..."
                        inst.killalldepots()
                print >> sys.stderr, "Stopping tests..."
                raise TestStopException()

        def run(self, result):
                self.timing = {}
                inst = None
                tdf = None
                try:
                        persistent_setup = getattr(self._tests[0],
                            "persistent_setup", False)
                except IndexError:
                        # No tests; that's ok.
                        return

                # This is needed because the import of some modules (such as
                # pygtk or pango) causes the default encoding for Python to be
                # changed which can can cause tests to succeed when they should
                # fail due to unicode issues:
                #     https://bugzilla.gnome.org/show_bug.cgi?id=132040
                default_utf8 = getattr(self._tests[0], "default_utf8", False)
                if not default_utf8:
                        # Now reset to the default a standard Python
                        # distribution uses.
                        sys.setdefaultencoding("ascii")
                else:
                        sys.setdefaultencoding("utf-8")

                def setUp_donothing():
                        pass

                def tearDown_donothing():
                        pass

                def setUp_dofail():
                        raise TestSkippedException(
                            "Persistent setUp Failed, skipping test.")

                if persistent_setup:
                        setUpFailed = False

                        # Save a reference to the tearDown func and neuter
                        # normal per-test-function teardown.
                        inst, tdf = self._tests[0].getTeardownFunc()
                        inst.reallytearDown = tdf
                        inst.tearDown = tearDown_donothing

                        if result.coverage:
                                inst.coverage_cmd, inst.coverage_env = result.coverage
                        else:
                                inst.coverage_cmd, inst.coverage_env = "", {}

                        try:
                                inst.setUp()
                        except KeyboardInterrupt:
                                self.cleanup_and_die(inst, "persistent setup")
                        except:
                                result.addPersistentSetupError(inst, sys.exc_info())
                                setUpFailed = True
                                # XXX do cleanup?

                        # If the setUp function didn't work, then cause
                        # every test case to fail.
                        if setUpFailed:
                                inst.setUp = setUp_dofail
                        else:
                                inst.setUp = setUp_donothing

                for test in self._tests:
                        if result.shouldStop:
                                break
                        real_test_name = test._testMethodName
                        suite_name = test._Pkg5TestCase__suite_name
                        cname = test.__class__.__name__

                        # Populate test with the data from the instance
                        # already constructed, but update the method name.
                        # We need to do this so that we have all the state
                        # that the object is populated with when setUp() is
                        # called (depot controller list, etc).
                        if persistent_setup:
                                name = test._testMethodName
                                doc = test._testMethodDoc
                                test = copy.copy(inst)
                                test._testMethodName = name
                                test._testMethodDoc = doc

                        test_start = time.time()
                        test(result)
                        test_end = time.time()
                        self.timing[suite_name, cname, real_test_name] = \
                            test_end - test_start
                if persistent_setup:
                        try:
                                inst.reallytearDown()
                        except KeyboardInterrupt:
                                self.cleanup_and_die(inst, "persistent teardown")
                        except:
                                result.addPersistentTeardownError(inst, sys.exc_info())

                # Try to ensure that all depots have been nuked.
                if hasattr(inst, "killalldepots"):
                        inst.killalldepots()



def get_su_wrap_user():
        for u in ["noaccess", "nobody"]:
                try:
                        pwd.getpwnam(u)
                        return u
                except (KeyError, NameError):
                        pass
        raise RuntimeError("Unable to determine user for su.")

class DepotTracebackException(Pkg5CommonException):
        def __init__(self, logfile, output):
                Pkg5CommonException.__init__(self)
                self.__logfile = logfile
                self.__output = output

        def __str__(self):
                str = "During this test, a depot Traceback was detected.\n"
                str += "Log file: %s.\n" % self.__logfile
                str += "Log file output is:\n"
                str += self.format_output(None, self.__output)
                return str

class TracebackException(Pkg5CommonException):
        def __init__(self, command, output=None, comment=None, debug=None):
                Pkg5CommonException.__init__(self)
                self.__command = command
                self.__output = output
                self.__comment = comment
                self.__debug = debug

        def __str__(self):
                if self.__comment is None and self.__output is None:
                        return (Exception.__str__(self))

                str = ""
                str += self.format_comment(self.__comment)
                str += self.format_output(self.__command, self.__output)
                if self.__debug is not None and self.__debug != "":
                        str += self.format_debug(self.__debug)
                return str

class UnexpectedExitCodeException(Pkg5CommonException):
        def __init__(self, command, expected, got, output=None, comment=None):
                Pkg5CommonException.__init__(self)
                self.__command = command
                self.__output = output
                self.__expected = expected
                self.__got = got
                self.__comment = comment

        def __str__(self):
                if self.__comment is None and self.__output is None:
                        return (Exception.__str__(self))

                str = ""
                str += self.format_comment(self.__comment)

                str += "  Invoked: %s\n" % self.__command
                str += "  Expected exit status: %s.  Got: %d." % \
                    (self.__expected, self.__got)

                str += self.format_output(self.__command, self.__output)
                return str

        @property
        def exitcode(self):
                return self.__got

class PkgSendOpenException(Pkg5CommonException):
        def __init__(self, com = ""):
                Pkg5CommonException.__init__(self, com)

class CliTestCase(Pkg5TestCase):
        bail_on_fail = False

        def setUp(self):
                Pkg5TestCase.setUp(self)

                self.image_dir = None
                self.img_path = os.path.join(self.test_root, "image")
                os.environ["PKG_IMAGE"] = self.img_path
                self.image_created = False

        def tearDown(self):
                Pkg5TestCase.tearDown(self)

        def get_img_path(self):
                return self.img_path

        def get_img_api_obj(self, cmd_path=None, img_path=None):
                if not img_path:
                        img_path = self.img_path
                from pkg.client import global_settings
                progresstracker = pkg.client.progress.NullProgressTracker()
                if not cmd_path:
                        cmd_path = os.path.join(img_path, "pkg")
                old_val = global_settings.client_args
                global_settings.client_args[0] = cmd_path
                res = pkg.client.api.ImageInterface(img_path,
                    CLIENT_API_VERSION, progresstracker, lambda x: False,
                    PKG_CLIENT_NAME)
                global_settings.client_args = old_val
                return res

        def image_create(self, repourl, prefix="test", variants=EmptyDict,
            destroy=True):
                """A convenience wrapper for callers that only need basic image
                creation functionality.  This wrapper creates a full (as opposed
                to user) image using the pkg.client.api and returns the related
                API object."""

                assert self.img_path
                assert self.img_path != "/"

                if destroy:
                        self.image_destroy()
                os.mkdir(self.img_path)

                progtrack = pkg.client.progress.NullProgressTracker()
                api_inst = pkg.client.api.image_create(PKG_CLIENT_NAME,
                    CLIENT_API_VERSION, self.img_path,
                    pkg.client.api.IMG_TYPE_ENTIRE, False, repo_uri=repourl,
                    prefix=prefix, progtrack=progtrack, variants=variants)
                shutil.copy("%s/usr/bin/pkg" % g_proto_area,
                    os.path.join(self.img_path, "pkg"))
                self.image_created = True
                return api_inst

        def pkg_image_create(self, repourl, prefix="test", additional_args="",
            exit=0):
                """Executes pkg(1) client to create a full (as opposed to user)
                image; returns exit code of client or raises an exception if
                exit code doesn't match 'exit' or equals 99.."""

                assert self.img_path
                assert self.img_path != "/"

                self.image_destroy()
                os.mkdir(self.img_path)
                cmdline = "pkg image-create -F -p %s=%s %s %s" % \
                    (prefix, repourl, additional_args, self.img_path)
                self.debugcmd(cmdline)

                p = subprocess.Popen(cmdline, shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT)
                output = p.stdout.read()
                retcode = p.wait()
                self.debugresult(retcode, 0, output)

                if retcode == 99:
                        raise TracebackException(cmdline, output)
                if retcode != exit:
                        raise UnexpectedExitCodeException(cmdline, 0,
                            retcode, output)
                shutil.copy("%s/usr/bin/pkg" % g_proto_area,
                    os.path.join(self.img_path, "pkg"))
                self.image_created = True
                return retcode

        def image_set(self, imgdir):
                self.debug("image_set: %s" % imgdir)
                self.img_path = imgdir
                os.environ["PKG_IMAGE"] = self.img_path

        def image_destroy(self):
                self.debug("image_destroy %s" % self.img_path)
                # Make sure we're not in the image.
                if os.path.exists(self.img_path):
                        os.chdir(self.test_root)
                        shutil.rmtree(self.img_path)

        def pkg(self, command, exit=0, comment="", prefix="", su_wrap=None,
            out=False, stderr=False, alt_img_path=None):
                pth = self.img_path
                if alt_img_path:
                        pth = alt_img_path
                elif not self.image_created:
                        pth = "%s/usr/bin" % g_proto_area
                cmdline = "%s/pkg %s" % (pth, command)
                return self.cmdline_run(cmdline, exit=exit, comment=comment,
                    prefix=prefix, su_wrap=su_wrap, out=out, stderr=stderr)

        def pkgdepend_resolve(self, args, exit=0, comment=""):
                cmdline = "%s/usr/bin/pkgdepend resolve %s" % (g_proto_area,
                    args)
                return self.cmdline_run(cmdline, comment=comment, exit=exit)

        def pkgdepend_generate(self, args, exit=0, comment=""):
                cmdline = "%s/usr/bin/pkgdepend generate %s" % (g_proto_area,
                    args)
                return self.cmdline_run(cmdline, exit=exit, comment=comment)

        def pkglint(self, args, exit=0, comment="", testrc=True):
                if testrc:
                        rcpath = "%s/pkglintrc" % self.test_root
                        cmdline = "%s/usr/bin/pkglint -f %s %s" % \
                            (g_proto_area, rcpath, args)
                else:
                        cmdline = "%s/usr/bin/pkglint %s" % (g_proto_area, args)
                return self.cmdline_run(cmdline, exit=exit, out=True,
                    comment=comment, stderr=True)

        def pkgrecv(self, server_url=None, command=None, exit=0, out=False,
            comment=""):
                args = []
                if server_url:
                        args.append("-s %s" % server_url)

                if command:
                        args.append(command)

                cmdline = "%s/usr/bin/pkgrecv %s" % (g_proto_area,
                    " ".join(args))
                return self.cmdline_run(cmdline, comment=comment, exit=exit,
                    out=out)

        def pkgrepo(self, command, comment="", exit=0, su_wrap=False):
                cmdline = "%s/usr/bin/pkgrepo %s" % (g_proto_area, command)
                return self.cmdline_run(cmdline, comment=comment, exit=exit,
                    su_wrap=su_wrap)

        def pkgsign(self, depot_url, command, exit=0, comment=""):
                args = []
                if depot_url:
                        args.append("-s %s" % depot_url)

                if command:
                        args.append(command)

                cmdline = "%s/usr/bin/pkgsign %s" % (g_proto_area,
                    " ".join(args))
                return self.cmdline_run(cmdline, comment=comment, exit=exit)

        def pkgsend(self, depot_url="", command="", exit=0, comment=""):
                args = []
                if depot_url:
                        args.append("-s " + depot_url)

                if command:
                        args.append(command)

                prefix = "cd %s;" % self.test_root
                cmdline = "%s/usr/bin/pkgsend %s" % (g_proto_area, 
                    " ".join(args))

                retcode, out = self.cmdline_run(cmdline, comment=comment,
                    exit=exit, out=True, prefix=prefix, raise_error=False)
                errout = self.errout

                cmdop = command.split(' ')[0]
                if cmdop in ("open", "append") and retcode == 0:
                        out = out.rstrip()
                        assert out.startswith("export PKG_TRANS_ID=")
                        arr = out.split("=")
                        assert arr
                        out = arr[1]
                        os.environ["PKG_TRANS_ID"] = out
                        self.debug("$ export PKG_TRANS_ID=%s" % out)
                        # retcode != 0 will be handled below

                published = None
                if (cmdop == "close" and retcode == 0) or cmdop == "publish":
                        os.environ["PKG_TRANS_ID"] = ""
                        self.debug("$ export PKG_TRANS_ID=")
                        for l in out.splitlines():
                                if l.startswith("pkg:/"):
                                        published = l
                                        break
                elif (cmdop == "generate" and retcode == 0):
                        published = out

                if retcode == 99:
                        raise TracebackException(cmdline, out, comment)

                if retcode != exit:
                        raise UnexpectedExitCodeException(cmdline, exit,
                            retcode, out + errout, comment)

                return retcode, published

        def pkgsend_bulk(self, depot_url, commands, exit=0, comment="",
            no_catalog=False, refresh_index=False):
                """ Send a series of packaging commands; useful  for quickly
                    doing a bulk-load of stuff into the repo.  All commands are
                    expected to work; if not, the transaction is abandoned.  If
                    'exit' is set, then if none of the actions triggers that
                    exit code, an UnexpectedExitCodeException is raised.

                    A list containing the fmris of any packages that were
                    published as a result of the commands executed will be
                    returned; it will be empty if none were. """

                if isinstance(commands, (list, tuple)):
                        commands = "".join(commands)

                extra_opts = []
                if no_catalog:
                        extra_opts.append("--no-catalog")
                extra_opts = " ".join(extra_opts)

                plist = []
                try:
                        accumulate = []
                        current_fmri = None
                        retcode = None

                        for line in commands.split("\n"):
                                line = line.strip()

                                # pkgsend_bulk can't be used w/ import or
                                # generate.
                                assert not line.startswith("import"), \
                                    "pkgsend_bulk cannot be used with import"
                                assert not line.startswith("generate"), \
                                    "pkgsend_bulk cannot be used with generate"

                                if line == "":
                                        continue
                                if line.startswith("add"):
                                        self.assert_(current_fmri != None,
                                            "Missing open in pkgsend string")
                                        accumulate.append(line[4:])
                                        continue

                                if current_fmri: # send any content seen so far (can be 0)
                                        fd, f_path = tempfile.mkstemp(dir=self.test_root)
                                        for l in accumulate:
                                                os.write(fd, "%s\n" % l)
                                        os.close(fd)
                                        try:
                                                cmd = "publish %s -d %s %s " \
                                                    "%s" % (extra_opts,
                                                    self.test_root,
                                                    current_fmri, f_path)
                                                current_fmri = None
                                                accumulate = []
                                                retcode, published = \
                                                    self.pkgsend(depot_url, cmd)
                                                if retcode == 0 and published:
                                                        plist.append(published)
                                        except:
                                                os.remove(f_path)
                                                raise
                                        os.remove(f_path)
                                if line.startswith("open"):
                                        current_fmri = line[5:].strip()

                        if exit == 0 and refresh_index:
                                self.pkgrepo("-s %s refresh --no-catalog" %
                                    depot_url)
                except UnexpectedExitCodeException, e:
                        if e.exitcode != exit:
                                raise
                        retcode = e.exitcode

                if retcode != exit:
                        raise UnexpectedExitCodeException(line, exit, retcode,
                            self.output + self.errout)

                return plist

        def merge(self, args=EmptyI, exit=0):
                pub_utils = os.path.join(g_proto_area, path_to_pub_util)
                prog = os.path.join(pub_utils, "merge.py")
                cmd = "%s %s" % (prog, " ".join(args))
                self.cmdline_run(cmd, exit=exit)

        def copy_repository(self, src, dest, pub_map):
                """Copies the packages from the src repository to a new
                destination repository that will be created at dest.  In
                addition, any packages from the src_pub will be assigned
                to the dest_pub during the copy.  The new repository will
                not have a catalog or search indices, so a depot server
                pointed at the new repository must be started with the
                --rebuild option.
                """

                # Preserve destination repository's configuration if it exists.
                dest_cfg = os.path.join(dest, "pkg5.repository")
                dest_cfg_data = None
                if os.path.exists(dest_cfg):
                        with open(dest_cfg, "rb") as f:
                                dest_cfg_data = f.read()
                shutil.rmtree(dest, True)
                os.makedirs(dest, mode=0755)

                # Ensure config is written back out.
                if dest_cfg_data:
                        with open(dest_cfg, "wb") as f:
                                f.write(dest_cfg_data)

                def copy_manifests(src_root, dest_root):
                        # Now copy each manifest and replace any references to
                        # the old publisher with that of the new publisher as
                        # they are copied.
                        src_pkg_root = os.path.join(src_root, "pkg")
                        dest_pkg_root = os.path.join(dest_root, "pkg")
                        for stem in os.listdir(src_pkg_root):
                                src_pkg_path = os.path.join(src_pkg_root, stem)
                                dest_pkg_path = os.path.join(dest_pkg_root,
                                    stem)
                                for mname in os.listdir(src_pkg_path):
                                        # Ensure destination manifest directory
                                        # exists.
                                        if not os.path.isdir(dest_pkg_path):
                                                os.makedirs(dest_pkg_path,
                                                    mode=0755)

                                        msrc = open(os.path.join(src_pkg_path,
                                            mname), "rb")
                                        mdest = open(os.path.join(dest_pkg_path,
                                            mname), "wb")
                                        for l in msrc:
                                                if l.find("pkg://") == -1:
                                                        mdest.write(l)
                                                        continue
                                                nl = l
                                                for src_pub in pub_map:
                                                        nl = nl.replace(
                                                            src_pub,
                                                            pub_map[src_pub])
                                                mdest.write(nl)
                                        msrc.close()
                                        mdest.close()

                src_pub_root = os.path.join(src, "publisher")
                if os.path.exists(src_pub_root):
                        dest_pub_root = os.path.join(dest, "publisher")
                        for pub in os.listdir(src_pub_root):
                                if pub not in pub_map:
                                        continue
                                src_root = os.path.join(src_pub_root, pub)
                                dest_root = os.path.join(dest_pub_root,
                                    pub_map[pub])
                                for entry in os.listdir(src_root):
                                        # Skip the catalog, index, and pkg
                                        # directories as they will be copied
                                        # manually.
                                        if entry not in ("catalog", "index",
                                            "pkg", "tmp", "trans"):
                                                spath = os.path.join(src_root,
                                                    entry)
                                                dpath = os.path.join(dest_root,
                                                    entry)
                                                shutil.copytree(spath, dpath)
                                                continue
                                        if entry != "pkg":
                                                continue
                                        copy_manifests(src_root, dest_root)

        def get_img_manifest_cache_dir(self, pfmri, img_path=None):
                """Returns the path to the manifest for the given fmri."""

                img = self.get_img_api_obj(img_path=img_path).img

                if not pfmri.publisher:
                        # Allow callers to not specify a fully-qualified FMRI
                        # if it can be asssumed which publisher likely has
                        # the package.
                        pubs = [
                            p.prefix
                            for p in img.gen_publishers(inc_disabled=True)
                        ]
                        assert len(pubs) == 1
                        pfmri.publisher = pubs[0]
                return img.get_manifest_dir(pfmri)

        def get_img_manifest_path(self, pfmri, img_path=None):
                """Returns the path to the manifest for the given fmri."""

                img = self.get_img_api_obj(img_path=img_path).img

                if not pfmri.publisher:
                        # Allow callers to not specify a fully-qualified FMRI
                        # if it can be asssumed which publisher likely has
                        # the package.
                        pubs = [
                            p.prefix
                            for p in img.gen_publishers(inc_disabled=True)
                        ]
                        assert len(pubs) == 1
                        pfmri.publisher = pubs[0]
                return img.get_manifest_path(pfmri)

        def get_img_manifest(self, pfmri, img_path=None):
                """Retrieves the client's cached copy of the manifest for the
                given package FMRI and returns it as a string.  Callers are
                responsible for all error handling."""

                mpath = self.get_img_manifest_path(pfmri, img_path=img_path)
                with open(mpath, "rb") as f:
                        return f.read()

        def write_img_manifest(self, pfmri, mdata, img_path=None):
                """Overwrites the client's cached copy of the manifest for the
                given package FMRI using the provided string.  Callers are
                responsible for all error handling."""

                if not img_path:
                        img_path = self.get_img_path()

                mpath = self.get_img_manifest_path(pfmri, img_path=img_path)
                mdir = self.get_img_manifest_cache_dir(pfmri, img_path=img_path)

                # Dump the manifest directory for the package to ensure any
                # cached information related to it is gone.
                shutil.rmtree(mdir, True)
                self.assert_(not os.path.exists(mdir))
                os.makedirs(mdir, mode=0755)

                # Finally, write the new manifest.
                with open(mpath, "wb") as f:
                        f.write(mdata)

        def validate_fsobj_attrs(self, act, target=None, img_path=None):
                """Used to verify that the target item's mode, attrs, timestamp,
                etc. match as expected.  The actual"""

                if act.name not in ("file", "dir"):
                        return

                if not img_path:
                        img_path = self.get_img_path()
                if not target:
                        target = act.attrs["path"]

                fpath = os.path.join(img_path, target)
                lstat = os.lstat(fpath)

                # Verify owner.
                expected = portable.get_user_by_name(act.attrs["owner"], None,
                    False)
                actual = lstat.st_uid
                self.assertEqual(expected, actual)

                # Verify group.
                expected = portable.get_group_by_name(act.attrs["group"], None,
                    False)
                actual = lstat.st_gid
                self.assertEqual(expected, actual)

                # Verify mode.
                expected = int(act.attrs["mode"], 8)
                actual = stat.S_IMODE(lstat.st_mode)
                self.assertEqual(expected, actual)

        def validate_html_file(self, fname, exit=0, comment="",
            options="-quiet -utf8"):
                cmdline = "tidy %s %s" % (options, fname)
                return self.cmdline_run(cmdline, comment=comment,
                    coverage=False, exit=exit)

        def create_repo(self, repodir, properties=EmptyDict, version=None):
                """ Convenience routine to help subclasses create a package
                    repository.  Returns a pkg.server.repository.Repository
                    object. """

                # Note that this must be deferred until after PYTHONPATH
                # is set up.
                import pkg.server.repository as sr
                try:
                        repo = sr.repository_create(repodir,
                            properties=properties, version=version)
                        self.debug("created repository %s" % repodir)
                except sr.RepositoryExistsError:
                        # Already exists.
                        repo = sr.Repository(root=repodir,
                            properties=properties)
                return repo

        def get_repo(self, repodir, read_only=False):
                """ Convenience routine to help subclasses retrieve a
                    pkg.server.repository.Repository object for a given
                    path. """

                # Note that this must be deferred until after PYTHONPATH
                # is set up.
                import pkg.server.repository as sr
                return sr.Repository(read_only=read_only, root=repodir)

        def prep_depot(self, port, repodir, logpath, refresh_index=False,
            debug_features=EmptyI, properties=EmptyI, start=False):
                """ Convenience routine to help subclasses prepare
                    depots.  Returns a depotcontroller. """

                # Note that this must be deferred until after PYTHONPATH
                # is set up.
                import pkg.depotcontroller as depotcontroller

                self.debug("prep_depot: set depot port %d" % port)
                self.debug("prep_depot: set depot repository %s" % repodir)
                self.debug("prep_depot: set depot log to %s" % logpath)

                dc = depotcontroller.DepotController(
                    wrapper_start=self.coverage_cmd.split(),
                    env=self.coverage_env)
                dc.set_depotd_path(g_proto_area + "/usr/lib/pkg.depotd")
                dc.set_depotd_content_root(g_proto_area + "/usr/share/lib/pkg")
                for f in debug_features:
                        dc.set_debug_feature(f)
                dc.set_repodir(repodir)
                dc.set_logpath(logpath)
                dc.set_port(port)

                for section in properties:
                        for prop, val in properties[section].iteritems():
                                dc.set_property(section, prop, val)
                if refresh_index:
                        dc.set_refresh_index()

                if start:
                        # If the caller requested the depot be started, then let
                        # the depot process create the repository.
                        dc.start()
                        self.debug("depot on port %s started" % port)
                else:
                        # Otherwise, create the repository with the assumption
                        # that the caller wants that at the least, but doesn't
                        # need the depot server (yet).
                        self.create_repo(repodir, properties=properties)
                return dc

        def wait_repo(self, repodir, timeout=5.0):
                """Wait for the specified repository to complete its current
                operations before continuing."""

                check_interval = 0.20
                time.sleep(check_interval)

                begintime = time.time()
                ready = False
                while (time.time() - begintime) <= timeout:
                        status = self.get_repo(repodir).get_status()
                        rdata = status.get("repository", {})
                        repo_status = rdata.get("status", "")
                        if repo_status == "online":
                                for pubdata in rdata.get("publishers",
                                    {}).values():
                                        if pubdata.get("status", "") != "online":
                                                ready = False
                                                break
                                else:
                                        # All repository stores were ready.
                                        ready = True

                        if not ready:
                                time.sleep(check_interval)
                        else:
                                break

                if not ready:
                        raise RuntimeError("Repository readiness "
                            "timeout exceeded.")

        def _api_install(self, api_obj, pkg_list, **kwargs):
                self.debug("install %s" % " ".join(pkg_list))
                api_obj.plan_install(pkg_list, **kwargs)
                self._api_finish(api_obj)

        def _api_uninstall(self, api_obj, pkg_list, **kwargs):
                self.debug("uninstall %s" % " ".join(pkg_list))
                api_obj.plan_uninstall(pkg_list, False, **kwargs)
                self._api_finish(api_obj)

        def _api_image_update(self, api_obj, **kwargs):
                self.debug("planning update")
                api_obj.plan_update_all(**kwargs)
                self._api_finish(api_obj)

        def _api_finish(self, api_obj):
                api_obj.prepare()
                api_obj.execute_plan()
                api_obj.reset()


class ManyDepotTestCase(CliTestCase):

        def __init__(self, methodName="runTest"):
                super(ManyDepotTestCase, self).__init__(methodName)
                self.dcs = {}

        def setUp(self, publishers, debug_features=EmptyI, start_depots=False):
                CliTestCase.setUp(self)

                self.debug("setup: %s" % self.id())
                self.debug("creating %d repo(s)" % len(publishers))
                self.debug("publishers: %s" % publishers)
                self.debug("debug_features: %s" % list(debug_features))
                self.dcs = {}

                for n, pub in enumerate(publishers):
                        i = n + 1
                        testdir = os.path.join(self.test_root)

                        try:
                                os.makedirs(testdir, 0755)
                        except OSError, e:
                                if e.errno != errno.EEXIST:
                                        raise e

                        depot_logfile = os.path.join(testdir,
                            "depot_logfile%d" % i)

                        props = { "publisher": { "prefix": pub } }

                        # We pick an arbitrary base port.  This could be more
                        # automated in the future.
                        repodir = os.path.join(testdir, "repo_contents%d" % i)
                        self.dcs[i] = self.prep_depot(12000 + i, repodir,
                            depot_logfile, debug_features=debug_features,
                            properties=props, start=start_depots)

        def check_traceback(self, logpath):
                """ Scan logpath looking for tracebacks.
                    Raise a DepotTracebackException if one is seen.
                """
                self.debug("check for depot tracebacks in %s" % logpath)
                logfile = open(logpath, "r")
                output = logfile.read()
                for line in output.splitlines():
                        if line.find("Traceback") > -1:
                                raise DepotTracebackException(logpath, output)

        def restart_depots(self):
                self.debug("restarting %d depot(s)" % len(self.dcs))
                for i in sorted(self.dcs.keys()):
                        dc = self.dcs[i]
                        self.debug("stopping depot at url: %s" % dc.get_depot_url())
                        dc.stop()
                        self.debug("starting depot at url: %s" % dc.get_depot_url())
                        dc.start()

        def killall_sighandler(self, signum, frame):
                print >> sys.stderr, \
                    "Ctrl-C: I'm killing depots, please wait.\n"
                print self
                self.signalled = True

        def killalldepots(self):
                self.signalled = False
                self.debug("killalldepots: %s" % self.id())

                oldhdlr = signal.signal(signal.SIGINT, self.killall_sighandler)

                try:
                        check_dc = []
                        for i in sorted(self.dcs.keys()):
                                dc = self.dcs[i]
                                if not dc.started:
                                        continue
                                check_dc.append(dc)
                                path = dc.get_repodir()
                                self.debug("stopping depot at url: %s, %s" % \
                                    (dc.get_depot_url(), path))

                                status = 0
                                try:
                                        status = dc.kill()
                                except Exception:
                                        pass

                                if status:
                                        self.debug("depot: %s" % status)

                        for dc in check_dc:
                                try:
                                        self.check_traceback(dc.get_logpath())
                                except Exception:
                                        pass
                finally:
                        signal.signal(signal.SIGINT, oldhdlr)

                self.dcs = {}
                if self.signalled:
                        raise KeyboardInterrupt("Ctrl-C while killing depots.")

        def tearDown(self):
                self.debug("ManyDepotTestCase.tearDown: %s" % self.id())

                self.killalldepots()
                CliTestCase.tearDown(self)

        def run(self, result=None):
                if result is None:
                        result = self.defaultTestResult()
                CliTestCase.run(self, result)


class SingleDepotTestCase(ManyDepotTestCase):

        def setUp(self, debug_features=EmptyI, publisher="test",
            start_depot=False):
                ManyDepotTestCase.setUp(self, [publisher],
                    debug_features=debug_features, start_depots=start_depot)
                self.backup_img_path = None

        def __get_dc(self):
                if self.dcs:
                        return self.dcs[1]
                else:
                        return None

        @property
        def durl(self):
                return self.dc.get_depot_url()

        @property
        def rurl(self):
                return self.dc.get_repo_url()

        # dc is a readonly property which is an alias for self.dcs[1],
        # for convenience of writing test cases.
        dc = property(fget=__get_dc)

        def create_sub_image(self, repourl, prefix="test", variants=EmptyDict):
                if not self.backup_img_path:
                        self.backup_img_path = self.img_path
                self.image_set(os.path.join(self.img_path, "sub"))
                self.image_create(repourl, prefix, variants, destroy=False)


class SingleDepotTestCaseCorruptImage(SingleDepotTestCase):
        """ A class which allows manipulation of the image directory that
        SingleDepotTestCase creates. Specifically, it supports removing one
        or more of the files or subdirectories inside an image (publisher,
        cfg_cache, etc...) in a controlled way.

        To add a new directory or file to be corrupted, it will be necessary
        to update corrupt_image_create to recognize a new option in config
        and perform the appropriate action (removing the directory or file
        for example).
        """

        def setUp(self, debug_features=EmptyI, publisher="test",
            start_depot=False):
                SingleDepotTestCase.setUp(self, debug_features=debug_features,
                    publisher=publisher, start_depot=start_depot)

        def tearDown(self):
                self.__uncorrupt_img_path()
                SingleDepotTestCase.tearDown(self)

        def __uncorrupt_img_path(self):
                """ Function which restores the img_path back to the original
                level. """
                if self.backup_img_path:
                        self.img_path = self.backup_img_path

        def corrupt_image_create(self, repourl, config, subdirs, prefix="test",
            destroy = True):
                """ Creates two levels of directories under the original image
                directory. In the first level (called bad), it builds a "corrupt
                image" which means it builds subdirectories the subdirectories
                speicified by subdirs (essentially determining whether a user
                image or a full image will be built). It populates these
                subdirectories with a partial image directory stucture as
                speicified by config. As another subdirectory of bad, it
                creates a subdirectory called final which represents the
                directory the command was actually run from (which is why
                img_path is set to that location). Existing image destruction
                was made optional to allow testing of two images installed next
                to each other (a user and full image created in the same
                directory for example). """
                if not self.backup_img_path:
                        self.backup_img_path = self.img_path
                self.img_path = os.path.join(self.img_path, "bad")
                assert self.img_path
                assert self.img_path and self.img_path != "/"

                if destroy:
                        self.image_destroy()

                for s in subdirs:
                        if s == "var/pkg":
                                cmdline = "pkg image-create -F -p %s=%s %s" % \
                                    (prefix, repourl, self.img_path)
                        elif s == ".org.opensolaris,pkg":
                                cmdline = "pkg image-create -U -p %s=%s %s" % \
                                    (prefix, repourl, self.img_path)
                        else:
                                raise RuntimeError("Got unknown subdir option:"
                                    "%s\n" % s)

                        self.debugcmd(cmdline)

                        # Run the command to actually create a good image
                        p = subprocess.Popen(cmdline, shell=True,
                                             stdout=subprocess.PIPE,
                                             stderr=subprocess.STDOUT)
                        output = p.stdout.read()
                        retcode = p.wait()
                        self.debugresult(retcode, 0, output)

                        if retcode == 99:
                                raise TracebackException(cmdline, output)
                        if retcode != 0:
                                raise UnexpectedExitCodeException(cmdline, 0,
                                    retcode, output)

                        tmpDir = os.path.join(self.img_path, s)

                        # This is where the actual corruption of the
                        # image takes place. A normal image was created
                        # above and this goes in and removes critical
                        # directories and files.
                        if "publisher_absent" in config or \
                           "publisher_empty" in config:
                                shutil.rmtree(os.path.join(tmpDir, "publisher"))
                        if "known_absent" in config or \
                           "known_empty" in config:
                                shutil.rmtree(os.path.join(tmpDir, "state",
                                    "known"))
                        if "known_empty" in config:
                                os.mkdir(os.path.join(tmpDir, "state", "known"))
                        if "publisher_empty" in config:
                                os.mkdir(os.path.join(tmpDir, "publisher"))
                        if "cfg_cache_absent" in config:
                                os.remove(os.path.join(tmpDir, "pkg5.image"))
                        if "index_absent" in config:
                                shutil.rmtree(os.path.join(tmpDir, "cache",
                                    "index"))
                shutil.copy("%s/usr/bin/pkg" % g_proto_area,
                    os.path.join(self.img_path, "pkg"))

                # Make find root start at final. (See the doc string for
                # more explanation.)
                cmd_path = os.path.join(self.img_path, "final")

                os.mkdir(cmd_path)
                return cmd_path

def eval_assert_raises(ex_type, eval_ex_func, func, *args):
        try:
                func(*args)
        except ex_type, e:
                print str(e)
                if not eval_ex_func(e):
                        raise
        else:
                raise RuntimeError("Function did not raise exception.")
