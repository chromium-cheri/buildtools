#!/usr/bin/env python3
# Copyright 2021 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""This script is used to fetch reclient cfgs."""

import argparse
import glob
import logging
import os
import posixpath
import re
import shutil
import string
import subprocess
import sys

THIS_DIR = os.path.abspath(os.path.dirname(__file__))
CHROMIUM_SRC = os.path.abspath(os.path.join(THIS_DIR,'..','..'))

REPROXY_CFG_HEADER = """# AUTOGENERATED FILE - DO NOT EDIT
# Generated by configure_reclient_cfgs.py
# To edit:
# Update reproxy_cfg_templates/$reproxy_cfg_template
# And run 'gclient sync' or 'gclient runhooks'
"""

AUTO_AUTH_FLAGS = """
# Googler auth flags
automatic_auth=true
gcert_refresh_timeout=20
"""

ADC_AUTH_FLAGS = """
# ADC auth flags
use_application_default_credentials=true
"""

def ClangRevision():
    sys.path.insert(0, os.path.join(CHROMIUM_SRC,
                                    'tools', 'clang', 'scripts'))
    import update
    sys.path.pop(0)
    return update.PACKAGE_VERSION

def NaclRevision():
    nacl_dir = os.path.join(CHROMIUM_SRC, 'native_client')
    # With git submodules, nacl_dir will always exist, regardless if it is
    # cloned or not. To detect if nacl is actually cloned, check content of the
    # repository. We assume that if README.md exists, then the repository is
    # cloned.
    if not os.path.exists(os.path.join(nacl_dir, 'README.md')):
      return None

    if os.path.isdir(os.path.join(nacl_dir, ".git")):
      return subprocess.run(
          ['git', 'log', '-1', '--format=%H'],
          cwd=nacl_dir, shell=os.name == 'nt', text=True, check=True,
          stdout=subprocess.PIPE
      ).stdout.strip()

    # If we're in a work tree without .git directories, we can fallback to
    # the slower method of looking the revision up via `gclient revinfo`.
    gclient_env = os.environ.copy()
    gclient_env['DEPOT_TOOLS_UPDATE'] = '0'
    revinfo = subprocess.run(
        ['gclient', 'revinfo', '--filter=src/native_client'],
        shell=os.name == 'nt',
        text=True,
        check=True,
        stdout=subprocess.PIPE,
        env=gclient_env).stdout.strip()
    try:
      # We expect this format: "src/native_client: {url}@{commit}"
      commit = revinfo.split("@")[1]
      if not re.match('^[0-9a-f]{40,}$', commit):
         raise ValueError("invalid commit hash")
      return commit
    except (IndexError, ValueError):
      logging.warning("Could not parse output of 'gclient revinfo "
                      "--filter=src/native_client': %s",
                      revinfo if revinfo else "<empty>")
      logging.warning("src/native_client seems present, but neither Git "
                      "nor gclient know its revision?")

    return None

class CipdError(Exception):
  """Raised by configure_reclient_cfgs on fatal cipd error."""

class CipdAuthError(CipdError):
  """Raised by configure_reclient_cfgs on cipd auth error."""

def CipdEnsure(pkg_name, ref, directory, quiet):
    print('ensure %s %s in %s' % (pkg_name, ref, directory))
    log_level = 'warning' if quiet else 'info'
    ensure_file = """
$ParanoidMode CheckIntegrity
{pkg} {ref}
""".format(pkg=pkg_name, ref=ref)
    try:
      output = subprocess.check_output(
          ' '.join(['cipd', 'ensure', '-log-level=' + log_level,
                    '-root', directory, '-ensure-file', '-']),
          shell=True, input=ensure_file, stderr=subprocess.STDOUT,
          universal_newlines=True)
      logging.info(output)
    except subprocess.CalledProcessError as e:
      if not IsCipdLoggedIn():
         raise CipdAuthError(e.output) from e
      raise CipdError(e.output) from e

def IsCipdLoggedIn():
    ps = subprocess.run(
       ['cipd', 'auth-info'],
        shell=True, capture_output=True, text=True)
    logging.info(
        "log for http://b/304677840: stdout from cipd auth-info: %s",
        ps.stdout)
    logging.info(
        "log for http://b/304677840: stderr from cipd auth-info: %s",
        ps.stderr)
    return ps.returncode == 0

def RbeProjectFromInstance(instance):
    m = re.fullmatch(r'projects/([-\w]+)/instances/[-\w]+', instance)
    if not m:
        return None
    return m.group(1)

def GenerateReproxyCfg(reproxy_cfg_template, rbe_instance, rbe_project):
    tmpl_path = os.path.join(
        THIS_DIR, 'reproxy_cfg_templates', reproxy_cfg_template)
    logging.info(f'generate reproxy.cfg using {tmpl_path}')
    if not os.path.isfile(tmpl_path):
        logging.warning(f"{tmpl_path} does not exist")
        return False
    with open(tmpl_path) as f:
      reproxy_cfg_tmpl = string.Template(REPROXY_CFG_HEADER+f.read())
    depsscanner_address = 'exec://' + os.path.join(CHROMIUM_SRC,
                                                   'buildtools',
                                                   'reclient',
                                                   'scandeps_server')
    auth_flags = AUTO_AUTH_FLAGS
    if sys.platform.startswith('win'):
      depsscanner_address += ".exe"
      auth_flags = ADC_AUTH_FLAGS
    reproxy_cfg = reproxy_cfg_tmpl.substitute({
      'rbe_instance': rbe_instance,
      'rbe_project': rbe_project,
      'reproxy_cfg_template': reproxy_cfg_template,
      'depsscanner_address': depsscanner_address,
      'auth_flags': auth_flags,
    })
    reproxy_cfg_path = os.path.join(THIS_DIR, 'reproxy.cfg')
    with open(reproxy_cfg_path, 'w') as f:
      f.write(reproxy_cfg)
    return True


def RequestCipdAuthentication():
  """Requests that the user authenticate to access CIPD packages."""

  print('Access to remoteexec config CIPD package requires authentication.')
  print('-----------------------------------------------------------------')
  print()
  print('I\'m sorry for the hassle, but you may need to do a one-time manual')
  print('authentication. Please run:')
  print()
  print('    cipd auth-login')
  print()
  print('and follow the instructions.')
  print()
  print('NOTE: Use your google.com credentials, not chromium.org.')
  print()
  print('-----------------------------------------------------------------')
  print()
  sys.stdout.flush()

def main():
    parser = argparse.ArgumentParser(
       description='configure reclient cfgs',
       formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument(
        '--rewrapper_cfg_project', '--rbe_project',
        help='RBE instance project id for rewrapper configs. '
             'Only specify if different from --rbe_instance\n'
             'TODO(b/270201776) --rbe_project is deprecated, '
             'remove once no more usages exist')
    parser.add_argument(
        '--reproxy_cfg_template',
        help='If set REPROXY_CFG_TEMPLATE will be used to generate '
             'reproxy.cfg. --rbe_instance must be specified.')
    parser.add_argument('--rbe_instance',
                        help='RBE instance for rewrapper and reproxy configs',
                        default=os.environ.get('RBE_instance'))
    parser.add_argument('--cipd_prefix',
                        help='cipd package name prefix',
                        default='infra_internal/rbe/reclient_cfgs')
    parser.add_argument('--skip_remoteexec_cfg_fetch',
                        help='skip downloading reclient cfgs from CIPD server',
                        action='store_true')
    parser.add_argument(
        '--quiet',
        help='Suppresses info logs',
        action='store_true')

    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO,
                        format="%(message)s")

    if not args.rewrapper_cfg_project and not args.rbe_instance:
        logging.error(
           'At least one of --rbe_instance and --rewrapper_cfg_project '
           'must be provided')
        return 1

    rbe_project = args.rewrapper_cfg_project
    if not args.rewrapper_cfg_project:
        rbe_project = RbeProjectFromInstance(args.rbe_instance)

    if args.reproxy_cfg_template:
        if not args.rbe_instance:
            logging.error(
              '--rbe_instance is required if --reproxy_cfg_template is set')
            return 1
        if not GenerateReproxyCfg(
          args.reproxy_cfg_template, args.rbe_instance, rbe_project):
           return 1

    if args.skip_remoteexec_cfg_fetch:
        return 0

    logging.info('fetch reclient_cfgs for RBE project %s...' % rbe_project)

    cipd_prefix = posixpath.join(args.cipd_prefix, rbe_project)

    tool_revisions = {
        'chromium-browser-clang': ClangRevision(),
        'nacl': NaclRevision(),
        'python': '3.8.0',
    }
    for toolchain in tool_revisions:
      revision = tool_revisions[toolchain]
      if not revision:
        logging.info('failed to detect %s revision' % toolchain)
        continue

      toolchain_root = os.path.join(THIS_DIR, toolchain)
      cipd_ref = 'revision/' + revision
      # 'cipd ensure' initializes the directory.
      try:
        CipdEnsure(posixpath.join(cipd_prefix, toolchain),
                    ref=cipd_ref,
                    directory=toolchain_root,
                    quiet=args.quiet)
      except CipdAuthError as e:
        RequestCipdAuthentication()
        return 1
      except CipdError as e:
        logging.error(e)
        return 1
      win_cross_cfg_dir = 'win-cross'
      wcedir = os.path.join(THIS_DIR, win_cross_cfg_dir, toolchain)
      if not os.path.exists(wcedir):
          os.makedirs(wcedir, mode=0o755)
      if os.path.exists(os.path.join(toolchain_root, win_cross_cfg_dir)):
          # copy in win-cross/toolchain
          # as windows may not use symlinks.
          for cfg in glob.glob(os.path.join(toolchain_root,
                                            win_cross_cfg_dir,
                                            '*.cfg')):
              fname = os.path.join(wcedir, os.path.basename(cfg))
              if os.path.exists(fname):
                  os.chmod(fname, 0o777)
                  os.remove(fname)
              logging.info('Copy from %s to %s...' % (cfg, fname))
              shutil.copy(cfg, fname)

    return 0

if __name__ == '__main__':
    sys.exit(main())
