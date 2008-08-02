#!/usr/bin/python2.4
#
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
# Copyright 2008 Sun Microsystems, Inc.  All rights reserved.
# Use is subject to license terms.
#
# pkg - package system client utility
#
# We use urllib2 for GET and POST operations, but httplib for PUT and DELETE
# operations.
#
# The client is going to maintain an on-disk cache of its state, so that startup
# assembly of the graph is reduced.
#
# Client graph is of the entire local catalog.  As operations progress, package
# states will change.
#
# Deduction operation allows the compilation of the local component of the
# catalog, only if an authoritative repository can identify critical files.
#
# Environment variables
#
# PKG_IMAGE - root path of target image
# PKG_IMAGE_TYPE [entire, partial, user] - type of image
#       XXX or is this in the Image configuration?

import getopt
import gettext
import itertools
import os
import re
import socket
import sys
import traceback
import urllib2
import urlparse

import pkg.client.image as image
import pkg.client.imageplan as imageplan
import pkg.client.filelist as filelist
import pkg.client.progress as progress
import pkg.client.bootenv as bootenv
import pkg.search_errors as search_errors
import pkg.fmri as fmri
import pkg.misc as misc
from pkg.misc import msg, emsg, PipeError
import pkg.version
import pkg

def error(text):
        """Emit an error message prefixed by the command name """

        # This has to be a constant value as we can't reliably get our actual
        # program name on all platforms.
        emsg("pkg: " + text)

def usage(usage_error = None):
        """Emit a usage message and optionally prefix it with a more
            specific error message.  Causes program to exit. """

        if usage_error:
                error(usage_error)

        emsg(_("""\
Usage:
        pkg [options] command [cmd_options] [operands]

Basic subcommands:
        pkg install [-nvq] package...
        pkg uninstall [-nrvq] package...
        pkg list [-aHsuv] [package...]
        pkg image-update [-nvq]
        pkg refresh [--full]
        pkg version
        pkg help

Advanced subcommands:
        pkg info [-lr] [--license] [pkg_fmri_pattern ...]
        pkg search [-lr] [-s server] token
        pkg verify [-fHqv] [pkg_fmri_pattern ...]
        pkg contents [-Hmr] [-o attribute ...] [-s sort_key] [-t action_type ... ]
            pkg_fmri_pattern [...]
        pkg image-create [-FPUz] [--full|--partial|--user] [--zone]
            [-k ssl_key] [-c ssl_cert] -a <prefix>=<url> dir

        pkg set-authority [-P] [-k ssl_key] [-c ssl_cert]
            [-O origin_url] authority
        pkg unset-authority authority ...
        pkg authority [-HP] [authname]
        pkg rebuild-index

Options:
        -R dir

Environment:
        PKG_IMAGE"""))
        sys.exit(2)

# XXX Subcommands to implement:
#        pkg image-set name value
#        pkg image-unset name
#        pkg image-get [name ...]

INCONSISTENT_INDEX_ERROR_MESSAGE = "The search index appears corrupted.  " + \
    "Please rebuild the index with 'pkg rebuild-index'."

PROBLEMATIC_PERMISSIONS_ERROR_MESSAGE = " (Failure of consistent use " + \
    "of pfexec when running pkg commands is often a source of this problem.)"

def get_partial_indexing_error_message(text):
        return "Result of partial indexing found.\n" + \
            "Could not make: " + \
            text + "\nbecause it already exists. " + \
            "Please use 'pkg rebuild-index' " + \
            "to fix this problem."

def list_inventory(img, args):
        all_known = False
        display_headers = True
        upgradable_only = False
        verbose = False
        summary = False

        opts, pargs = getopt.getopt(args, "aHsuv")

        for opt, arg in opts:
                if opt == "-a":
                        all_known = True
                elif opt == "-H":
                        display_headers = False
                elif opt == "-s":
                        summary = True
                elif opt == "-u":
                        upgradable_only = True
                elif opt == "-v":
                        verbose = True

        if summary and verbose:
                usage(_("-s and -v may not be combined"))

        if verbose:
                fmt_str = "%-64s %-10s %s"
        elif summary:
                fmt_str = "%-30s %s"
        else:
                fmt_str = "%-45s %-15s %-10s %s"

        img.load_catalogs(progress.NullProgressTracker())

        try:
                found = False
                for pkg, state in img.inventory(pargs, all_known):
                        if upgradable_only and not state["upgradable"]:
                                continue

                        if not found:
                                if display_headers:
                                        if verbose:
                                                msg(fmt_str % \
                                                    ("FMRI", "STATE", "UFIX"))
                                        elif summary:
                                                msg(fmt_str % \
                                                    ("NAME (AUTHORITY)",
                                                    "SUMMARY"))
                                        else:
                                                msg(fmt_str % \
                                                    ("NAME (AUTHORITY)",
                                                    "VERSION", "STATE", "UFIX"))
                                found = True

                        ufix = "%c%c%c%c" % \
                            (state["upgradable"] and "u" or "-",
                            state["frozen"] and "f" or "-",
                            state["incorporated"] and "i" or "-",
                            state["excludes"] and "x" or "-")

                        if pkg.preferred_authority():
                                auth = ""
                        else:
                                auth = " (" + pkg.get_authority() + ")"

                        if verbose:
                                pf = pkg.get_fmri(img.get_default_authority())
                                msg("%-64s %-10s %s" % (pf, state["state"],
                                    ufix))
                        elif summary:
                                pf = pkg.get_name() + auth

                                m = img.get_manifest(pkg, filtered = True)
                                msg(fmt_str % (pf, m.get("description", "")))

                        else:
                                pf = pkg.get_name() + auth
                                msg(fmt_str % (pf, pkg.get_version(),
                                    state["state"], ufix))


                if not found:
                        if not pargs:
                                if upgradable_only:
                                        error(_("no installed packages have " \
                                            "available updates"))
                                else:
                                        error(_("no packages installed"))
                        return 1
                return 0

        except RuntimeError, e:
                if not found:
                        error(_("no matching packages installed"))
                        return 1

                state = all_known and \
                    image.PKG_STATE_KNOWN or image.PKG_STATE_INSTALLED
                for pat in e.args[0]:
                        error(_("no packages matching '%s' %s") % (pat, state))
                return 1

def get_tracker(quiet = False):
        if quiet:
                progresstracker = progress.QuietProgressTracker()
        else:
                try:
                        progresstracker = \
                            progress.FancyUNIXProgressTracker()
                except progress.ProgressTrackerException:
                        progresstracker = progress.CommandLineProgressTracker()
        return progresstracker



def installed_fmris_from_args(img, args):
        """Helper function to translate client command line arguments
            into a list of installed fmris.  Used by info, contents, verify.

            XXX consider moving into image class
        """
        found = []
        notfound = []
        try:
                for m in img.inventory(args):
                        found.append(m[0])
        except RuntimeError, e:
                notfound = e[0]

        return found, notfound

def verify_image(img, args):
        opts, pargs = getopt.getopt(args, "vfqH")

        quiet = verbose = False
        # for now, always check contents of files
        forever = display_headers = True

        for opt, arg in opts:
                if opt == "-H":
                        display_headers = False
                if opt == "-v":
                        verbose = True
                elif opt == "-f":
                        forever = True
                elif opt == "-q":
                        quiet = True
                        display_headers = False

        if verbose and quiet:
                usage(_("verify: -v and -q may not be combined"))

        progresstracker = get_tracker(quiet)

        img.load_catalogs(progresstracker)

        fmris, notfound = installed_fmris_from_args(img, pargs)

        any_errors = False

        header = False
        for f in fmris:
                pkgerr = False
                for err in img.verify(f, progresstracker,
                    verbose=verbose, forever=forever):
                        #
                        # Eventually this code should probably
                        # move into the progresstracker
                        #
                        if not pkgerr:
                                if display_headers and not header:
                                        msg("%-50s %7s" % ("PACKAGE", "STATUS"))
                                        header = True

                                if not quiet:
                                        msg("%-50s %7s" % (f.get_pkg_stem(),
                                            "ERROR"))
                                pkgerr = True

                        if not quiet:
                                msg("\t%s" % err[0])
                                for x in err[1]:
                                        msg("\t\t%s" % x)
                if verbose and not pkgerr:
                        if display_headers and not header:
                                msg("%-50s %7s" % ("PACKAGE", "STATUS"))
                                header = True
                        msg("%-50s %7s" % (f.get_pkg_stem(), "OK"))

                any_errors = any_errors or pkgerr

        if fmris:
                progresstracker.verify_done()

        if notfound:
                if fmris:
                        emsg()
                emsg(_("""\
pkg: no packages matching the following patterns you specified are
installed on the system.\n"""))
                for p in notfound:
                        emsg("        %s" % p)
                if fmris:
                        if any_errors:
                                msg2 = "See above for\nverification failures."
                        else:
                                msg2 = "No packages failed\nverification."
                        emsg(_("\nAll other patterns matched installed "
                            "packages.  %s" % msg2))
                any_errors = True

        if any_errors:
                return 1
        return 0

def ipkg_is_up_to_date(img):
        """ Test whether SUNWipkg is updated to the latest version
            known to be available for this image """
        #
        # This routine makes the distinction between the "target image"--
        # which we're going to alter, and the "running image", which is to
        # say whatever image appears to contain the version of the pkg
        # command we're running.
        #

        #
        # There are two relevant cases here:
        #     1) Packaging code and image we're updating are the same
        #        image.  (i.e. 'pkg image-update')
        #
        #     2) Packaging code's image and the image we're updating are
        #        different (i.e. 'pkg image-update -R')
        #
        # In general, we care about getting the user to run the
        # most recent packaging code available for their build.  So,
        # if we're not in the liveroot case, we create a new image
        # which represents "/" on the system.
        #

        # always quiet for this part of the code.
        progresstracker = get_tracker(True)

        if not img.is_liveroot():
                newimg = image.Image()
                cmdpath = os.path.join(os.getcwd(), sys.argv[0])
                cmdpath = os.path.realpath(cmdpath)
                cmddir = os.path.dirname(os.path.realpath(cmdpath))
                try:
                        #
                        # Find the path to ourselves, and use that
                        # as a way to locate the image we're in.  It's
                        # not perfect-- we could be in a developer's
                        # workspace, for example.
                        #
                        newimg.find_root(cmddir)
                except ValueError:
                        # We can't answer in this case, so we return True to
                        # let installation proceed.
                        msg(_("No image corresponding to '%s' was located. " \
                            "Proceeding.") % cmdpath)
                        return True
                newimg.load_config()

                # Refresh the catalog, so that we can discover if a new
                # SUNWipkg is available.
                try:
                        newimg.retrieve_catalogs()
                except RuntimeError, failures:
                        display_catalog_failures(failures)
                        error(_("SUNWipkg update check failed."))
                        return False

                # Load catalog.
                newimg.load_catalogs(progresstracker)
                img = newimg

        msg(_("Checking that SUNWipkg (in '%s') is up to date... ") % \
            img.get_root())

        try:
                img.make_install_plan(["SUNWipkg"], progresstracker,
                    filters = [], noexecute = True)
        except RuntimeError:
                return True

        if img.imageplan.nothingtodo():
                return True

        msg(_("WARNING: pkg(5) appears to be out of date, and should be " \
            "updated before\nrunning image-update.\n"))
        msg(_("Please update pkg(5) using 'pfexec pkg install SUNWipkg' " \
            "and then retry\nthe image-update."))
        return False


def image_update(img, args):
        """Attempt to take all installed packages specified to latest
        version."""

        # XXX Authority-catalog issues.
        # XXX Are filters appropriate for an image update?
        # XXX Leaf package refinements.

        opts, pargs = getopt.getopt(args, "b:fnvq")

        force = quiet = noexecute = verbose = False
        for opt, arg in opts:
                if opt == "-n":
                        noexecute = True
                elif opt == "-v":
                        verbose = True
                elif opt == "-b":
                        filelist.FileList.maxbytes_default = int(arg)
                elif opt == "-q":
                        quiet = True
                elif opt == "-f":
                        force = True

        if verbose and quiet:
                usage(_("image-update: -v and -q may not be combined"))

        if pargs:
                usage(_("image-update: command does not take operands " \
                    "('%s')") % " ".join(pargs))

        progresstracker = get_tracker(quiet)
        img.load_catalogs(progresstracker)

        try:
                img.retrieve_catalogs()
        except RuntimeError, failures:
                if display_catalog_failures(failures) == 0:
                        if not noexecute:
                                return 1
                else:
                        if not noexecute:
                                return 3

        # Reload catalog.  This picks up the update from retrieve_catalogs.
        img.load_catalogs(progresstracker)

        #
        # If we can find SUNWipkg and SUNWcs in the target image, then
        # we assume this is a valid opensolaris image, and activate some
        # special case behaviors.
        #
        opensolaris_image = True
        fmris, notfound = installed_fmris_from_args(img, ["SUNWipkg", "SUNWcs"])
        if notfound:
                opensolaris_image = False

        if opensolaris_image and not force:
                if not ipkg_is_up_to_date(img):
                        return 1

        pkg_list = [ ipkg.get_pkg_stem() for ipkg in img.gen_installed_pkgs() ]

        try:
                img.make_install_plan(pkg_list, progresstracker,
                    verbose = verbose, noexecute = noexecute)
        except RuntimeError, e:
                error(_("image-update failed: %s") % e)
                return 1

        assert img.imageplan

        if img.imageplan.nothingtodo():
                msg(_("No updates available for this image."))
                return 0

        if noexecute:
                return 0

        try:
                img.imageplan.preexecute()
        except Exception, e:
                error(_("\nAn unexpected error happened during " \
                    "image-update:"))
                img.cleanup_downloads()
                raise
        try:
                be = bootenv.BootEnv(img.get_root())
        except RuntimeError:
                be = bootenv.BootEnvNull(img.get_root())

        be.init_image_recovery(img)

        if img.is_liveroot():
                error(_("image-update cannot be done on live image"))
                return 1

        try:
                img.imageplan.execute()
                be.activate_image()
                ret_code = 0
        except RuntimeError, e:
                error(_("image-update failed: %s") % e)
                be.restore_image()
                ret_code = 1
        except search_errors.InconsistentIndexException, e:
                error(INCONSISTENT_INDEX_ERROR_MESSAGE)
                ret_code = 1
        except search_errors.PartialIndexingException, e:
                error(get_partial_indexing_error_message(e.cause))
                ret_code = 1
        except search_errors.ProblematicPermissionsIndexException, e:
                error(str(e) + PROBLEMATIC_PERMISSIONS_ERROR_MESSAGE)
                ret_code = 1
        except Exception, e:
                error(_("\nAn unexpected error happened during " \
                    "image-update: %s") % e)
                be.restore_image()
                img.cleanup_downloads()
                raise

        img.cleanup_downloads()
        if ret_code == 0:
                img.cleanup_cached_content()
                
                if opensolaris_image:
                        msg("\n" + "-" * 75)
                        msg(_("NOTE: Please review release notes posted at:\n" \
                            "   http://opensolaris.org/os/project/indiana/" \
                            "resources/rn3/"))
                        msg("-" * 75 + "\n")

        return ret_code


def install(img, args):
        """Attempt to take package specified to INSTALLED state.  The operands
        are interpreted as glob patterns."""

        # XXX Authority-catalog issues.

        opts, pargs = getopt.getopt(args, "nvb:f:q")

        quiet = noexecute = verbose = False
        filters = []
        for opt, arg in opts:
                if opt == "-n":
                        noexecute = True
                elif opt == "-v":
                        verbose = True
                elif opt == "-b":
                        filelist.FileList.maxbytes_default = int(arg)
                elif opt == "-f":
                        filters += [ arg ]
                elif opt == "-q":
                        quiet = True

        if not pargs:
                usage(_("install: at least one package name required"))

        if verbose and quiet:
                usage(_("install: -v and -q may not be combined"))

        progresstracker = get_tracker(quiet)

        img.load_catalogs(progresstracker)

        pkg_list = [ pat.replace("*", ".*").replace("?", ".")
            for pat in pargs ]

        try:
                img.make_install_plan(pkg_list, progresstracker,
                    filters = filters, verbose = verbose, noexecute = noexecute)
        except RuntimeError, e:
                error(_("install failed: %s") % e)
                return 1

        assert img.imageplan

        #
        # The result of make_install_plan is that an imageplan is now filled out
        # for the image.
        #
        if img.imageplan.nothingtodo():
                msg(_("Nothing to install in this image (is this package " \
                    "already installed?)"))
                return 0

        if noexecute:
                return 0

        try:
                img.imageplan.preexecute()
        except Exception, e:
                error(_("\nAn unexpected error happened during " \
                    "install:"))
                img.cleanup_downloads()
                raise

        try:
                be = bootenv.BootEnv(img.get_root())
        except RuntimeError:
                be = bootenv.BootEnvNull(img.get_root())

        try:
                img.imageplan.execute()
                be.activate_install_uninstall()
                ret_code = 0
        except RuntimeError, e:
                error(_("installation failed: %s") % e)
                be.restore_install_uninstall()
                ret_code = 1
        except search_errors.InconsistentIndexException, e:
                error(INCONSISTENT_INDEX_ERROR_MESSAGE)
                ret_code = 1
        except search_errors.PartialIndexingException, e:
                error(get_partial_indexing_error_message(e.cause))
                ret_code = 1
        except search_errors.ProblematicPermissionsIndexException, e:
                error(str(e) + PROBLEMATIC_PERMISSIONS_ERROR_MESSAGE)
                ret_code = 1
        except Exception, e:
                error(_("An unexpected error happened during " \
                    "installation: %s") % e)
                be.restore_install_uninstall()
                img.cleanup_downloads()
                raise

        img.cleanup_downloads()
        if ret_code == 0:
                img.cleanup_cached_content()

        return ret_code


def uninstall(img, args):
        """Attempt to take package specified to DELETED state."""

        opts, pargs = getopt.getopt(args, "nrvq")

        quiet = noexecute = recursive_removal = verbose = False
        for opt, arg in opts:
                if opt == "-n":
                        noexecute = True
                elif opt == "-r":
                        recursive_removal = True
                elif opt == "-v":
                        verbose = True
                elif opt == "-q":
                        quiet = True

        if not pargs:
                usage(_("uninstall: at least one package name required"))

        if verbose and quiet:
                usage(_("uninstall: -v and -q may not be combined"))

        progresstracker = get_tracker(quiet)

        img.load_catalogs(progresstracker)

        ip = imageplan.ImagePlan(img, progresstracker, recursive_removal)

        err = 0

        for ppat in pargs:
                rpat = re.sub("\*", ".*", ppat)
                rpat = re.sub("\?", ".", rpat)

                try:
                        matches = list(img.inventory([ rpat ]))
                except RuntimeError:
                        error(_("'%s' not even in catalog!") % ppat)
                        err = 1
                        continue

                if len(matches) > 1:
                        error(_("'%s' matches multiple packages") % ppat)
                        for k in matches:
                                msg("\t%s" % k[0])
                        err = 1
                        continue

                if len(matches) < 1:
                        error(_("'%s' matches no installed packages") % \
                            ppat)
                        err = 1
                        continue

                # Propose the removal of the first (and only!) match.
                ip.propose_fmri_removal(matches[0][0])

        if err == 1:
                return err

        if verbose:
                msg(_("Before evaluation:"))
                msg(ip)

        try:
                ip.evaluate()
        except imageplan.NonLeafPackageException, e:
                error("""Cannot remove '%s' due to
the following packages that depend on it:""" % e[0])
                for d in e[1]:
                        emsg("  %s" % d)
                return 1

        img.imageplan = ip

        if verbose:
                msg(_("After evaluation:"))
                ip.display()

        assert not ip.nothingtodo()

        if noexecute:
                return 0

        try:
                img.imageplan.preexecute()
        except Exception, e:
                error(_("\nAn unexpected error happened during " \
                    "uninstall"))
                raise

        try:
                be = bootenv.BootEnv(img.get_root())
        except RuntimeError:
                be = bootenv.BootEnvNull(img.get_root())

        try:
                ip.execute()
        except RuntimeError, e:
                error(_("installation failed: %s") % e)
                be.restore_install_uninstall()
                err = 1
        except Exception, e:
                error(_("An unexpected error happened during " \
                    "uninstallation: %s") % e)
                be.restore_install_uninstall()
                raise

        if ip.state == imageplan.EXECUTED_OK:
                be.activate_install_uninstall()
        else:
                be.restore_install_uninstall()

        return err

def freeze(img, args):
        """Attempt to take package specified to FROZEN state, with given
        restrictions.  Package must have been in the INSTALLED state."""
        return 0

def unfreeze(img, args):
        """Attempt to return package specified to INSTALLED state from FROZEN
        state."""
        return 0

def search(img, args):
        """Search through the reverse index databases for the given token."""

        opts, pargs = getopt.getopt(args, "lrs:")

        local = remote = False
        servers = []
        for opt, arg in opts:
                if opt == "-l":
                        local = True
                elif opt == "-r":
                        remote = True
                elif opt == "-s":
                        if not arg.startswith("http://") and \
                            not arg.startswith("https://"):
                                arg = "http://" + arg
                        remote = True
                        servers.append({"origin": arg})

        if not local and not remote:
                local = True

        if not pargs:
                usage()

        searches = []
        if local:
                try:
                        searches.append(img.local_search(pargs))
                except search_errors.NoIndexException, nie:
                        error(str(nie) +
                            "\nPlease try 'pkg rebuild-index' to recreate the " +
                            "index.")
                        return 1
                except search_errors.InconsistentIndexException, iie:
                        error("The search index appears corrupted.  Please "
                            "rebuild the index with 'pkg rebuild-index'.")
                        return 1


        if remote:
                searches.append(img.remote_search(pargs, servers))

        # By default assume we don't find anything.
        retcode = 1

        try:
                first = True
                for index, mfmri, action, value in itertools.chain(*searches):
                        retcode = 0
                        if first:
                                if action and value:
                                        msg("%-10s %-9s %-25s %s" % ("INDEX",
                                            "ACTION", "VALUE", "PACKAGE"))
                                else:
                                        msg("%-10s %s" % ("INDEX", "PACKAGE"))
                                first = False
                        if action and value:
                                msg("%-10s %-9s %-25s %s" % (index, action,
                                    value, fmri.PkgFmri(str(mfmri)
                                    ).get_short_fmri()))
                        else:
                                msg("%-10s %s" % (index, mfmri))

        except RuntimeError, failed:
                emsg("Some servers failed to respond:")
                for auth, err in failed.args[0]:
                        if isinstance(err, urllib2.HTTPError):
                                emsg("    %s: %s (%d)" % \
                                    (auth["origin"], err.msg, err.code))
                        elif isinstance(err, urllib2.URLError):
                                if isinstance(err.args[0], socket.timeout):
                                        emsg("    %s: %s" % \
                                            (auth["origin"], "timeout"))
                                else:
                                        emsg("    %s: %s" % \
                                            (auth["origin"], err.args[0][1]))

                retcode = 4

        return retcode

def info_license(img, mfst, remote):
        for i, license in enumerate(mfst.gen_actions_by_type("license")):
                if i > 0:
                        msg("")

                if remote:
                        misc.gunzip_from_stream(
                            license.get_remote_opener(img, mfst.fmri)(), sys.stdout)
                else:
                        msg(license.get_local_opener(img, mfst.fmri)().read()[:-1])

def info(img, args):
        """Display information about a package or packages.
        """

        display_license = False
        info_local = False
        info_remote = False

        opts, pargs = getopt.getopt(args, "lr", ["license"])
        for opt, arg in opts:
                if opt == "-l":
                        info_local = True
                elif opt == "-r":
                        info_remote = True
                elif opt == "--license":
                        display_license = True

        if not info_local and not info_remote:
                info_local = True
        elif info_local and info_remote:
                usage(_("info: -l and -r may not be combined"))

        if info_remote and not pargs:
                usage(_("info: must request remote info for specific packages"))

        img.load_catalogs(progress.NullProgressTracker())

        err = 0

        if info_local:
                fmris, notfound = installed_fmris_from_args(img, pargs)
                if not fmris and not notfound:
                        error(_("no packages installed"))
                        return 1
        elif info_remote:
                fmris = []
                notfound = []

                # XXX This loop really needs not to be copied from
                # Image.make_install_plan()!
                for p in pargs:
                        try:
                                matches = list(img.inventory([ p ],
                                    all_known = True))
                        except RuntimeError:
                                notfound.append(p)
                                continue

                        pnames = {}
                        pmatch = []
                        npnames = {}
                        npmatch = []
                        for m, state in matches:
                                if m.preferred_authority():
                                        pnames[m.get_pkg_stem()] = 1
                                        pmatch.append(m)
                                else:
                                        npnames[m.get_pkg_stem()] = 1
                                        npmatch.append(m)

                        if len(pnames.keys()) > 1:
                                msg(_("pkg: '%s' matches multiple packages") % \
                                    p)
                                for k in pnames.keys():
                                        msg("\t%s" % k)
                                continue
                        elif len(pnames.keys()) < 1 and len(npnames.keys()) > 1:
                                msg(_("pkg: '%s' matches multiple packages") % \
                                    p)
                                for k in npnames.keys():
                                        msg("\t%s" % k)
                                continue

                        # matches is a list reverse sorted by version, so take
                        # the first; i.e., the latest.
                        if len(pmatch) > 0:
                                fmris.append(pmatch[0])
                        else:
                                fmris.append(npmatch[0])

        manifests = ( img.get_manifest(f, filtered = True) for f in fmris )

        for i, m in enumerate(manifests):
                if i > 0:
                        msg("")

                if display_license:
                        info_license(img, m, info_remote)
                        continue

                authority, name, version = m.fmri.tuple()
                authority = fmri.strip_auth_pfx(authority)
                summary = m.get("description", "")
                if m.fmri.preferred_authority():
                        authority += _(" (preferred)")
                if img.is_installed(m.fmri):
                        state = _("Installed")
                else:
                        state = _("Not installed")

                msg("          Name:", name)
                msg("       Summary:", summary)
                msg("         State:", state)

                # XXX even more info on the authority would be nice?
                msg("     Authority:", authority)
                msg("       Version:", version.release)
                msg(" Build Release:", version.build_release)
                msg("        Branch:", version.branch)
                msg("Packaging Date:", version.get_timestamp().ctime())
                if m.size > (1024 * 1024):
                        msg("          Size: %.1f MB" % \
                            (m.size / float(1024 * 1024)))
                elif m.size > 1024:
                        msg("          Size: %d kB" % (m.size / 1024))
                else:
                        msg("          Size: %d B" % m.size)
                msg("          FMRI:", m.fmri)
                # XXX need to properly humanize the manifest.size
                # XXX add license/copyright info here?

        if notfound:
                err = 1
                if fmris:
                        emsg()
                if info_local:
                        emsg(_("""\
pkg: no packages matching the following patterns you specified are
installed on the system.  Try specifying -r to query remotely:"""))
                elif info_remote:
                        emsg(_("""\
pkg: no packages matching the following patterns you specified were
found in the catalog.  Try relaxing the patterns, refreshing, and/or
examining the catalogs:"""))
                emsg()
                for p in notfound:
                        emsg("        %s" % p)

        return err

def display_contents_results(actionlist, attrs, sort_attrs, action_types,
    display_headers):
        """Print results of a "list" operation """

        # widths is a list of tuples of column width and justification.  Start
        # with the widths of the column headers.
        JUST_UNKN = 0
        JUST_LEFT = -1
        JUST_RIGHT = 1
        widths = [ (len(attr) - attr.find(".") - 1, JUST_UNKN)
            for attr in attrs ]
        lines = []

        for manifest, action in actionlist:
                if action_types and action.name not in action_types:
                        continue
                line = []
                for i, attr in enumerate(attrs):
                        just = JUST_UNKN
                        # As a first approximation, numeric attributes
                        # are right justified, non-numerics left.
                        try:
                                int(action.attrs[attr])
                                just = JUST_RIGHT
                        # attribute is non-numeric or is something like
                        # a list.
                        except (ValueError, TypeError):
                                just = JUST_LEFT
                        # attribute isn't in the list, so we don't know
                        # what it might be
                        except KeyError:
                                pass

                        if attr in action.attrs:
                                a = action.attrs[attr]
                        elif attr == "action.name":
                                a = action.name
                                just = JUST_LEFT
                        elif attr == "action.key":
                                a = action.attrs[action.key_attr]
                                just = JUST_LEFT
                        elif attr == "action.raw":
                                a = action
                                just = JUST_LEFT
                        elif attr == "pkg.name":
                                a = manifest.fmri.get_name()
                                just = JUST_LEFT
                        elif attr == "pkg.fmri":
                                a = manifest.fmri
                                just = JUST_LEFT
                        elif attr == "pkg.shortfmri":
                                a = manifest.fmri.get_short_fmri()
                                just = JUST_LEFT
                        elif attr == "pkg.authority":
                                a = manifest.fmri.get_authority()
                                just = JUST_LEFT
                        else:
                                a = ""

                        line.append(a)

                        # XXX What to do when a column's justification
                        # changes?
                        if just != JUST_UNKN:
                                widths[i] = \
                                    (max(widths[i][0], len(str(a))), just)

                if line and [l for l in line if str(l) != ""]:
                        lines.append(line)

        sortidx = 0
        for i, attr in enumerate(attrs):
                if attr == sort_attrs[0]:
                        sortidx = i
                        break

        # Sort numeric columns numerically.
        if widths[sortidx][1] == JUST_RIGHT:
                def key_extract(x):
                        try:
                                return int(x[sortidx])
                        except (ValueError, TypeError):
                                return 0
        else:
                key_extract = lambda x: x[sortidx]

        if display_headers:
                headers = []
                for i, attr in enumerate(attrs):
                        headers.append(str(attr.upper()))
                        widths[i] = \
                            (max(widths[i][0], len(attr)), widths[i][1])

                # Now that we know all the widths, multiply them by the
                # justification values to get positive or negative numbers to
                # pass to the %-expander.
                widths = [ e[0] * e[1] for e in widths ]
                fmt = ("%%%ss " * len(widths)) % tuple(widths)

                msg((fmt % tuple(headers)).rstrip())
        else:
                fmt = "%s\t" * len(widths)
                fmt.rstrip("\t")

        for line in sorted(lines, key = key_extract):
                msg((fmt % tuple(line)).rstrip())

def list_contents(img, args):
        """List package contents.

        If no arguments are given, display for all locally installed packages.
        With -H omit headers and use a tab-delimited format; with -o select
        attributes to display; with -s, specify attributes to sort on; with -t,
        specify which action types to list."""

        # XXX Need remote-info option, to request equivalent information
        # from repository.

        opts, pargs = getopt.getopt(args, "Ho:s:t:mfr")

        valid_special_attrs = [ "action.name", "action.key", "action.raw",
            "pkg.name", "pkg.fmri", "pkg.shortfmri", "pkg.authority",
            "pkg.size" ]

        display_headers = True
        display_raw = False
        display_nofilters = False
        remote = False
        local = False
        attrs = []
        sort_attrs = []
        action_types = []
        for opt, arg in opts:
                if opt == "-H":
                        display_headers = False
                elif opt == "-o":
                        attrs.extend(arg.split(","))
                elif opt == "-s":
                        sort_attrs.append(arg)
                elif opt == "-t":
                        action_types.extend(arg.split(","))
                elif opt == "-r":
                        remote = True
                elif opt == "-m":
                        display_raw = True
                elif opt == "-f":
                        # Undocumented, for now.
                        display_nofilters = True

        if not remote and not local:
                local = True
        elif local and remote:
                usage(_("contents: -l and -r may not be combined"))

        if remote and not pargs:
                usage(_("contents: must request remote contents for specific " \
                   "packages"))

        if display_raw:
                display_headers = False
                attrs = [ "action.raw" ]

                invalid = set(("-H", "-o", "-t")). \
                    intersection(set([x[0] for x in opts]))

                if len(invalid) > 0:
                        usage(_("contents: -m and %s may not be specified " \
                            "at the same time") % invalid.pop())

        for a in attrs:
                if a.startswith("action.") and not a in valid_special_attrs:
                        usage(_("Invalid attribute '%s'") % a)

                if a.startswith("pkg.") and not a in valid_special_attrs:
                        usage(_("Invalid attribute '%s'") % a)

        img.load_catalogs(progress.NullProgressTracker())

        err = 0

        if local:
                fmris, notfound = installed_fmris_from_args(img, pargs)
                if not fmris and not notfound:
                        error(_("no packages installed"))
                        return 1
        elif remote:
                fmris = []
                notfound = []

                # XXX This loop really needs not to be copied from
                # Image.make_install_plan()!
                for p in pargs:
                        try:
                                matches = list(img.inventory([ p ],
                                    all_known = True))
                        except RuntimeError:
                                notfound.append(p)
                                continue

                        pnames = {}
                        pmatch = []
                        npnames = {}
                        npmatch = []
                        for m, state in matches:
                                if m.preferred_authority():
                                        pnames[m.get_pkg_stem()] = 1
                                        pmatch.append(m)
                                else:
                                        npnames[m.get_pkg_stem()] = 1
                                        npmatch.append(m)

                        if len(pnames.keys()) > 1:
                                msg(_("pkg: '%s' matches multiple packages") % \
                                    p)
                                for k in pnames.keys():
                                        msg("\t%s" % k)
                                continue
                        elif len(pnames.keys()) < 1 and len(npnames.keys()) > 1:
                                msg(_("pkg: '%s' matches multiple packages") % \
                                    p)
                                for k in npnames.keys():
                                        msg("\t%s" % k)
                                continue

                        # matches is a list reverse sorted by version, so take
                        # the first; i.e., the latest.
                        if len(pmatch) > 0:
                                fmris.append(pmatch[0])
                        else:
                                fmris.append(npmatch[0])

        #
        # If the user specifies no specific attrs, and no specific
        # sort order, then we fill in some defaults.
        #
        if not attrs:
                # XXX Possibly have multiple exclusive attributes per column?
                # If listing dependencies and files, you could have a path/fmri
                # column which would list paths for files and fmris for
                # dependencies.
                attrs = [ "path" ]

        if not sort_attrs:
                # XXX reverse sorting
                # Most likely want to sort by path, so don't force people to
                # make it explicit
                if "path" in attrs:
                        sort_attrs = [ "path" ]
                else:
                        sort_attrs = attrs[:1]

        filt = not display_nofilters
        manifests = ( img.get_manifest(f, filtered = filt) for f in fmris )

        actionlist = [ (m, a)
                    for m in manifests
                    for a in m.actions ]

        if fmris:
                display_contents_results(actionlist, attrs, sort_attrs,
                    action_types, display_headers)

        if notfound:
                err = 1
                if fmris:
                        emsg()
                if local:
                        emsg(_("""\
pkg: no packages matching the following patterns you specified are
installed on the system.  Try specifying -r to query remotely:"""))
                elif remote:
                        emsg(_("""\
pkg: no packages matching the following patterns you specified were
found in the catalog.  Try relaxing the patterns, refreshing, and/or
examining the catalogs:"""))
                emsg()
                for p in notfound:
                        emsg("        %s" % p)

        return err

def display_catalog_failures(failures):
        total, succeeded = failures.args[1:3]
        msg(_("pkg: %s/%s catalogs successfully updated:") % (succeeded, total))

        for auth, err in failures.args[0]:
                if isinstance(err, urllib2.HTTPError):
                        emsg("   %s: %s - %s" % \
                            (err.filename, err.code, err.msg))
                elif isinstance(err, urllib2.URLError):
                        if err.args[0][0] == 8:
                                emsg("    %s: %s" % \
                                    (urlparse.urlsplit(
                                        auth["origin"])[1].split(":")[0],
                                    err.args[0][1]))
                        else:
                                if isinstance(err.args[0], socket.timeout):
                                        emsg("    %s: %s" % \
                                            (auth["origin"], "timeout"))
                                else:
                                        emsg("    %s: %s" % \
                                            (auth["origin"], err.args[0][1]))
                else:
                        emsg("   ", err)

        return succeeded

def catalog_refresh(img, args):
        """Update image's catalogs."""

        # XXX will need to show available content series for each package
        full_refresh = False
        opts, pargs = getopt.getopt(args, "", ["full"])
        for opt, arg in opts:
                if opt == "--full":
                        full_refresh = True

        if pargs:
                usage(_("refresh: command does not take operands ('%s')") %
                      " ".join(pargs))
        
        # Ensure Image directory structure is valid.
        if not os.path.isdir("%s/catalog" % img.imgdir):
                img.mkdirs()

        # Loading catalogs allows us to perform incremental update
        img.load_catalogs(get_tracker())

        try:
                img.retrieve_catalogs(full_refresh)
        except RuntimeError, failures:
                if display_catalog_failures(failures) == 0:
                        return 1
                else:
                        return 3
        else:
                return 0

def authority_set(img, args):
        """pkg set-authority [-P] [-k ssl_key] [-c ssl_cert]
            [-O origin_url] authority"""

        preferred = False
        ssl_key = None
        ssl_cert = None
        origin_url = None

        opts, pargs = getopt.getopt(args, "Pk:c:O:")
        for opt, arg in opts:
                if opt == "-P":
                        preferred = True
                if opt == "-k":
                        ssl_key = arg
                if opt == "-c":
                        ssl_cert = arg
                if opt == "-O":
                        origin_url = arg

        if len(pargs) != 1:
                usage(
                    _("pkg: set-authority: one and only one authority " \
                        "may be set"))

        auth = pargs[0]

        if ssl_key:
                ssl_key = os.path.abspath(ssl_key)
                if not os.path.exists(ssl_key):
                        error(_("set-authority: SSL key file '%s' does not " \
                            "exist") % ssl_key)
                        return 1

        if ssl_cert:
                ssl_cert = os.path.abspath(ssl_cert)
                if not os.path.exists(ssl_cert):
                        error(_("set-authority: SSL key cert '%s' does not " \
                            "exist") % ssl_cert)
                        return 1


        if not img.has_authority(auth) and origin_url == None:
                error(_("set-authority: must define origin URL for new " \
                    "authority"))
                return 1

        elif not img.has_authority(auth) and not misc.valid_auth_prefix(auth):
                error(_("set-authority: authority name has invalid characters"))
                return 1

        if origin_url and not misc.valid_auth_url(origin_url):
                error(_("set-authority: authority URL is invalid"))
                return 1

        try:
                img.set_authority(auth, origin_url = origin_url,
                        ssl_key = ssl_key, ssl_cert = ssl_cert)
        except RuntimeError, e:
                error(_("set-authority failed: %s") % e)
                return 1

        if preferred:
                img.set_preferred_authority(auth)

        return 0

def authority_unset(img, args):
        """pkg unset-authority authority ..."""

        # is this an existing authority in our image?
        # if so, delete it
        # if not, error
        preferred_auth = img.get_default_authority()

        if len(args) == 0:
                usage()

        for a in args:
                if not img.has_authority(a):
                        error(_("unset-authority: no such authority: %s") \
                            % a)
                        return 1

                if a == preferred_auth:
                        error(_("unset-authority: removal of preferred " \
                            "authority not allowed."))
                        return 1

                img.delete_authority(a)

        return 0

def authority_list(img, args):
        """pkg authorities"""
        omit_headers = False
        preferred_only = False
        preferred_authority = img.get_default_authority()

        opts, pargs = getopt.getopt(args, "HP")
        for opt, arg in opts:
                if opt == "-H":
                        omit_headers = True
                if opt == "-P":
                        preferred_only = True

        if len(pargs) == 0:
                if not omit_headers:
                        msg("%-35s %s" % ("AUTHORITY", "URL"))

                if preferred_only:
                        auths = [img.get_authority(preferred_authority)]
                else:
                        auths = img.gen_authorities()

                for a in auths:
                        # summary list
                        pfx, url, ssl_key, ssl_cert, dt = img.split_authority(a)

                        if not preferred_only and pfx == preferred_authority:
                                pfx += " (preferred)"
                        msg("%-35s %s" % (pfx, url))
        else:
                img.load_catalogs(get_tracker())

                for a in pargs:
                        if not img.has_authority(a):
                                error(_("authority: no such authority: %s") \
                                    % a)
                                return 1

                        # detailed print
                        auth = img.get_authority(a)
                        pfx, url, ssl_key, ssl_cert, dt = \
                            img.split_authority(auth)

                        if dt:
                                dt = dt.ctime()

                        msg("")
                        msg("      Authority:", pfx)
                        msg("     Origin URL:", url)
                        msg("        SSL Key:", ssl_key)
                        msg("       SSL Cert:", ssl_cert)
                        msg("Catalog Updated:", dt)

        return 0

def image_create(img, args):
        """Create an image of the requested kind, at the given path.  Load
        catalog for initial authority for convenience.

        At present, it is legitimate for a user image to specify that it will be
        deployed in a zone.  An easy example would be a program with an optional
        component that consumes global zone-only information, such as various
        kernel statistics or device information."""

        # XXX Long options support

        imgtype = image.IMG_USER
        is_zone = False
        ssl_key = None
        ssl_cert = None
        auth_name = None
        auth_url = None

        opts, pargs = getopt.getopt(args, "FPUza:k:c:",
            ["full", "partial", "user", "zone", "authority="])

        for opt, arg in opts:
                if opt == "-F" or opt == "--full":
                        imgtype = image.IMG_ENTIRE
                if opt == "-P" or opt == "--partial":
                        imgtype = image.IMG_PARTIAL
                if opt == "-U" or opt == "--user":
                        imgtype = image.IMG_USER
                if opt == "-z" or opt == "--zone":
                        is_zone = True
                if opt == "-k":
                        ssl_key = arg
                if opt == "-c":
                        ssl_cert = arg
                if opt == "-a" or opt == "--authority":
                        try:
                                auth_name, auth_url = arg.split("=", 1)
                        except ValueError:
                                usage(_("image-create requires authority "
                                    "argument to be of the form "
                                    "'<prefix>=<url>'."))

        if len(pargs) != 1:
                usage(_("image-create requires a single image directory path"))

        if ssl_key:
                ssl_key = os.path.abspath(ssl_key)
                if not os.path.exists(ssl_key):
                        msg(_("pkg: set-authority: SSL key file '%s' does " \
                            "not exist") % ssl_key)
                        return 1

        if ssl_cert:
                ssl_cert = os.path.abspath(ssl_cert)
                if not os.path.exists(ssl_cert):
                        msg(_("pkg: set-authority: SSL key cert '%s' does " \
                            "not exist") % ssl_cert)
                        return 1

        if not auth_name and not auth_url:
                usage("image-create requires an authority argument")

        if not auth_name or not auth_url:
                usage(_("image-create requires authority argument to be of "
                    "the form '<prefix>=<url>'."))

        if auth_name.startswith(fmri.PREF_AUTH_PFX):
                error(_("image-create requires that a prefix not match: %s"
                        % fmri.PREF_AUTH_PFX))
                return 1

        if not misc.valid_auth_prefix(auth_name):
                error(_("image-create: authority prefix has invalid " \
                    "characters"))
                return 1

        if not misc.valid_auth_url(auth_url):
                error(_("image-create: authority URL is invalid"))
                return 1

        try:
                img.set_attrs(imgtype, pargs[0], is_zone, auth_name, auth_url,
                    ssl_key = ssl_key, ssl_cert = ssl_cert)
        except OSError, e:
                error(_("cannot create image at %s: %s") % \
                    (pargs[0], e.args[1]))
                return 1

        try:
                img.retrieve_catalogs()
        except RuntimeError, failures:
                if display_catalog_failures(failures) == 0:
                        return 1
                else:
                        return 3
        else:
                return 0


def rebuild_index(img, pargs):
        """pkg rebuild-index

        Forcibly rebuild the search indexes. Will remove existing indexes
        and build new ones from scratch."""
        quiet = False

        if pargs:
                usage(_("rebuild-index: command does not take operands " \
                    "('%s')") % " ".join(pargs))

        try:
                img.rebuild_search_index(get_tracker(quiet))
        except search_errors.InconsistentIndexException, iie:
                error(INCONSISTENT_INDEX_ERROR_MESSAGE)
                return 1
        except search_errors.ProblematicPermissionsIndexException, ppie:
                error(str(ppie) + PROBLEMATIC_PERMISSIONS_ERROR_MESSAGE)
                return 1

def main_func():
        img = image.Image()

        # XXX /usr/lib/locale is OpenSolaris-specific.
        gettext.install("pkg", "/usr/lib/locale")

        try:
                opts, pargs = getopt.getopt(sys.argv[1:], "R:")
        except getopt.GetoptError, e:
                usage(_("illegal global option -- %s") % e.opt)

        if pargs == None or len(pargs) == 0:
                usage()

        subcommand = pargs[0]
        del pargs[0]

        socket.setdefaulttimeout(
            int(os.environ.get("PKG_CLIENT_TIMEOUT", "30"))) # in seconds

        # Override default MAX_TIMEOUT_COUNT if a value has been specified
        # in the environment.
        timeout_max = misc.MAX_TIMEOUT_COUNT
        misc.MAX_TIMEOUT_COUNT = int(os.environ.get("PKG_TIMEOUT_MAX",
            timeout_max))

        if subcommand == "image-create":
                try:
                        ret = image_create(img, pargs)
                except getopt.GetoptError, e:
                        usage(_("illegal %s option -- %s") % \
                            (subcommand, e.opt))
                return ret
        elif subcommand == "version":
                if pargs:
                        usage(_("version: command does not take operands " \
                            "('%s')") % " ".join(pargs))
                msg(pkg.VERSION)
                return 0
        elif subcommand == "help":
                try:
                        usage()
                except SystemExit:
                        return 0

        for opt, arg in opts:
                if opt == "-R":
                        mydir = arg

        if "mydir" not in locals():
                try:
                        mydir = os.environ["PKG_IMAGE"]
                except KeyError:
                        mydir = os.getcwd()

        try:
                img.find_root(mydir)
        except ValueError:
                error(_("'%s' is not an install image") % mydir)
                return 1

        img.load_config()

        try:
                if subcommand == "refresh":
                        return catalog_refresh(img, pargs)
                elif subcommand == "list":
                        return list_inventory(img, pargs)
                elif subcommand == "image-update":
                        return image_update(img, pargs)
                elif subcommand == "install":
                        return install(img, pargs)
                elif subcommand == "uninstall":
                        return uninstall(img, pargs)
                elif subcommand == "freeze":
                        return freeze(img, pargs)
                elif subcommand == "unfreeze":
                        return unfreeze(img, pargs)
                elif subcommand == "search":
                        return search(img, pargs)
                elif subcommand == "info":
                        return info(img, pargs)
                elif subcommand == "contents":
                        return list_contents(img, pargs)
                elif subcommand == "verify":
                        return verify_image(img, pargs)
                elif subcommand == "set-authority":
                        return authority_set(img, pargs)
                elif subcommand == "unset-authority":
                        return authority_unset(img, pargs)
                elif subcommand == "authority":
                        return authority_list(img, pargs)
                elif subcommand == "rebuild-index":
                        return rebuild_index(img, pargs)
                else:
                        usage(_("unknown subcommand '%s'") % subcommand)

        except getopt.GetoptError, e:
                usage(_("illegal %s option -- %s") % (subcommand, e.opt))


#
# Establish a specific exit status which means: "python barfed an exception"
# so that we can more easily detect these in testing of the CLI commands.
#
if __name__ == "__main__":
        try:
                ret = main_func()
        except SystemExit, e:
                raise e
        except (PipeError, KeyboardInterrupt):
                # We don't want to display any messages here to prevent possible
                # further broken pipe (EPIPE) errors.
                sys.exit(1)
        except misc.TransferTimedOutException:
                msg(_("Maximum number of timeouts exceeded during download."))
                sys.exit(1)
        except:
                traceback.print_exc()
                error(
                    "\n\nThis is an internal error, please let the " + \
                    "developers know about this\nproblem by filing " + \
                    "a bug at http://defect.opensolaris.org and including " + \
                    "the\nabove traceback and the output of 'pkg version'.")
                sys.exit(99)
        sys.exit(ret)
