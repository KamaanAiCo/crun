#!/bin/env python3
# crun - OCI runtime written in C
#
# Copyright (C) 2017, 2018, 2019 Giuseppe Scrivano <giuseppe@scrivano.org>
# crun is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# crun is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with crun.  If not, see <http://www.gnu.org/licenses/>.

# A null in the configuration where a string is expected is parsed into a
# NULL pointer, since a NULL is how a field that is not present is
# represented.  Every place walking such a value has to cope with it, so
# put a null in each string of a configuration, one at a time, and check
# that crun says what is wrong instead of dying.
#
# Built with the sanitizers, this also catches the undefined behavior of
# handing a NULL to a function declared not to take one.

import copy
import json
import os
import shutil
import subprocess
import tempfile
from tests_utils import *


def full_config():
    """A configuration touching as many string-valued fields as possible."""
    conf = base_config()
    add_all_namespaces(conf)

    conf['process']['args'] = ['/init', 'true']
    conf['process']['env'] = ['PATH=/bin', 'TERM=xterm']
    conf['process']['scheduler'] = {'policy': 'SCHED_OTHER',
                                    'flags': ['SCHED_FLAG_RESET_ON_FORK']}
    conf['process']['rlimits'] = [{'type': 'RLIMIT_NOFILE', 'hard': 1024, 'soft': 1024}]
    conf['annotations'] = {'run.oci.a': '1', 'org.systemd.property.CPUWeight': '100'}
    conf['hooks'] = {
        'createRuntime': [{'path': '/bin/true', 'args': ['true', 'x'], 'env': ['A=1']}],
        'poststop': [{'path': '/bin/true', 'args': ['true', 'y'], 'env': ['B=2']}],
    }
    conf['linux']['sysctl'] = {'net.ipv4.ip_forward': '0'}
    conf['linux']['resources'] = {'unified': {'memory.high': 'max'}}
    conf['linux']['seccomp'] = {
        'defaultAction': 'SCMP_ACT_ALLOW',
        'architectures': ['SCMP_ARCH_X86_64'],
        'flags': ['SECCOMP_FILTER_FLAG_LOG'],
        'syscalls': [{'names': ['mkdir', 'rmdir'], 'action': 'SCMP_ACT_ERRNO'}],
    }
    return conf


def walk_strings(node, path=''):
    """Yield (path, container, key) for every string in the structure."""
    if isinstance(node, dict):
        for k, v in node.items():
            p = '%s.%s' % (path, k) if path else k
            if isinstance(v, str):
                yield p, node, k
            else:
                yield from walk_strings(v, p)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            p = '%s[%d]' % (path, i)
            if isinstance(v, str):
                yield p, node, i
            else:
                yield from walk_strings(v, p)


def run_config(conf, cid, root, bundle):
    """Run a container to completion, return (returncode, output)."""
    with open(os.path.join(bundle, 'config.json'), 'w') as f:
        json.dump(conf, f)

    # The output goes to a file rather than a pipe: the container process
    # inherits stderr, so reading a pipe to the end of file would wait for
    # the container itself.
    log = os.path.join(bundle, 'out.log')
    with open(log, 'w+b') as lf:
        p = subprocess.run([get_crun_path(), '--root', root, 'run', '--bundle', bundle, cid],
                           stdout=lf, stderr=lf, stdin=subprocess.DEVNULL,
                           timeout=60, close_fds=False)

    with open(log, 'rb') as lf:
        out = lf.read().decode(errors='replace')

    subprocess.run([get_crun_path(), '--root', root, 'delete', '-f', cid],
                   capture_output=True, close_fds=False)
    return p.returncode, out


def test_null_config_values():
    with tempfile.TemporaryDirectory() as bundle:
        root = os.path.join(bundle, 'state')
        os.makedirs(os.path.join(bundle, 'rootfs'))
        shutil.copy(get_init_path(), os.path.join(bundle, 'rootfs', 'init'))

        conf = full_config()

        # A configuration that does not run makes the whole test vacuous.
        rc, out = run_config(copy.deepcopy(conf), 'null-base', root, bundle)
        if rc != 0:
            logger.info("the unmodified configuration does not run (rc %d): %s", rc, out)
            return -1

        paths = [p for p, _, _ in walk_strings(conf)]
        logger.info("checking a null in each of %d strings", len(paths))

        failed = []
        for n, (path, _, _) in enumerate(walk_strings(conf)):
            variant = copy.deepcopy(conf)
            for p, holder, key in walk_strings(variant):
                if p == path:
                    holder[key] = None
                    break

            rc, out = run_config(variant, 'null-%d' % n, root, bundle)

            # A negative code is death by a signal, and the sanitizers say so
            # in the output.  Without them, a container process that died on a
            # NULL leaves the runtime with a broken channel and no error of
            # its own, which is what the remaining markers are about.
            crashed = (rc < 0
                       or 'runtime error:' in out
                       or 'Sanitizer' in out
                       or 'read from the init process' in out
                       or 'read FD: connection closed' in out
                       or 'sync socket: Broken pipe' in out)
            if crashed:
                logger.info("null in %s: rc %d, output: %s", path, rc, out[:400])
                failed.append(path)

        if failed:
            logger.info("crashed on a null in: %s", ', '.join(failed))
            return -1

    return 0


all_tests = {
    "null-config-values": test_null_config_values,
}

if __name__ == "__main__":
    tests_main(all_tests)
