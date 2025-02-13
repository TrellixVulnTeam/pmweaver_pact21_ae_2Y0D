#
# Copyright 2019, Intel Corporation
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
#     * Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#
#     * Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in
#       the documentation and/or other materials provided with the
#       distribution.
#
#     * Neither the name of the copyright holder nor the names of its
#       contributors may be used to endorse or promote products derived
#       from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
"""Parser for user provided test configuration"""

import argparse
import os
import string
import sys
from datetime import timedelta

import context as ctx
import futils
import valgrind as vg

try:
    import testconfig
except ImportError:
    sys.exit('Please add valid testconfig.py file - see testconfig.py.example')


class _ConfigFromDict:
    """
    Class fields are created from provided dictionary. Used for creating
    a final config object
    """
    def __init__(self, dict_):
        for k, v in dict_.items():
            setattr(self, k, v)

    # special method triggered if class attribute was not found
    # https://docs.python.org/3.5/reference/datamodel.html#object.__getattr__
    def __getattr__(self, name):
        if name == 'pmem_fs_dir':
            raise futils.Skip('No PMEM test directory provided')
        if name == 'non_pmem_fs_dir':
            raise futils.Skip('No non-PMEM test directory provided')

        sys.exit('Provided test configuration may be invalid. '
                 'No "{}" field found in configuration.'.format(name))


def _str2list(config):
    """
    Convert the string with test sequence to equivalent list.
    example:
    _str2list("0-3,6") -->  [0, 1, 2, 3, 6]
    _str2list("1,3-5") -->  [1, 3, 4, 5]
    """
    arg = config['test_sequence']
    if not arg:
        # test_sequence not set, do nothing
        return

    seq = []
    try:
        if ',' in arg or '-' in arg:
            arg = arg.split(',')
            for number in arg:
                if '-' in number:
                    number = number.split('-')
                    begin = int(number[0])
                    end = int(number[1])
                    step = 1 if begin < end else -1
                    for x in range(begin, end + step, step):
                        seq.append(x)
                else:
                    seq.append(int(number))
        else:
            seq.append(int(arg))

    except (ValueError, IndexError):
        print('Provided test sequence "{}" is invalid'.format(arg))
        raise

    config['test_sequence'] = seq


def _str2time(config):
    """
    Convert the string with s, m, h, d suffixes to time format

    example:
    _str2time("5s")  -->  "0:00:05"
    _str2time("15m") -->  "0:15:00"
    """
    string_ = config['timeout']
    try:
        timeout = int(string_[:-1])
    except ValueError as e:
        raise ValueError("invalid timeout argument: {}".format(string_)) from e
    else:
        if "d" in string_:
            timeout = timedelta(days=timeout)
        elif "m" in string_:
            timeout = timedelta(minutes=timeout)
        elif "h" in string_:
            timeout = timedelta(hours=timeout)
        elif "s" in string_:
            timeout = timedelta(seconds=timeout)

        config['timeout'] = timeout.total_seconds()


def _str2ctx(config):
    """Convert context classes from strings to actual classes"""
    def class_from_string(name, base):
        if name == 'all':
            return base.__subclasses__()

        try:
            return next(b for b in base.__subclasses__()
                        if str(b) == name.lower())
        except StopIteration:
            print('Invalid config value: "{}".'.format(name))
            raise

    def convert_internal(key, base):
        if not isinstance(config[key], list):
            config[key] = ctx.expand(class_from_string(config[key], base))
        else:
            classes = [class_from_string(cl, base) for cl in config[key]]
            config[key] = ctx.expand(*classes)

    convert_internal('build', ctx._Build)
    convert_internal('test_type', ctx._TestType)
    convert_internal('fs', ctx._Fs)

    if config['force_enable'] is not None:
        config['force_enable'] = next(
            t for t in vg.TOOLS
            if t.name.lower() == config['force_enable'])


class Configurator():
    """Parser for user test configuration"""

    def __init__(self):
        self.argparser = self._init_argparser()

    def parse_config(self):
        """
        Parse and return test execution config object. Final config is
        composed from 2 config values - values from testconfig.py file
        and values provided by command line args.
        """
        try:
            args_config = self._get_args_config()

            # The order of configs addition in 'config' initialization
            # is relevant - values from each next added config overwrite
            # values of already existing keys.
            config = {**testconfig.config, **args_config}

            self._convert_to_usable_types(config)

            # Remake dict into class object for convenient fields acquisition
            return _ConfigFromDict(config)

        except KeyError as e:
            sys.exit("No config field '{}' found. "
                     "testconfig.py file may be invalid.".format(e.args[0]))

    def _convert_to_usable_types(self, config):
        """
        Converts config values types as parsed from user input into
        types usable by framework implementation
        """
        _str2ctx(config)
        _str2list(config)
        _str2time(config)

    def _get_args_config(self):
        """Return config values parsed from command line arguments"""

        # 'group' positional argument added only if RUNTESTS.py is the
        # execution entry point
        from_runtest = os.path.basename(sys.argv[0]) == 'RUNTESTS.py'
        if from_runtest:
            self.argparser.add_argument('group', nargs='*',
                                        help='Run only tests '
                                        'from selected groups')

        # remove possible whitespace and empty args
        sys.argv = [arg for arg in sys.argv if arg and not arg.isspace()]

        args = self.argparser.parse_args()

        if from_runtest:
            # test_sequence does not make sense if group is not set
            if args.test_sequence and not args.group:
                self.argparser.error('"--test_sequence" argument needs '
                                     'to have "group" arg set')

            # remove possible path characters added by shell hint
            args.group = [g.strip(string.punctuation) for g in args.group]

        # make into dict for type consistency
        return {k: v for k, v in vars(args).items() if v is not None}

    def _init_argparser(self):
        def ctx_choices(cls):
            return [str(c) for c in cls.__subclasses__()]

        parser = argparse.ArgumentParser()
        parser.add_argument('--fs_dir_force_pmem', type=int,
                            help='set PMEM_IS_PMEM_FORCE for tests run on'
                            ' pmem fs')
        parser.add_argument('-l', '--unittest_log_level', type=int,
                            help='set log level. 0 - silent, 1 - normal, '
                            '2 - verbose')
        parser.add_argument('--keep_going', type=bool,
                            help='continue execution despite test fails')
        parser.add_argument('-b', dest='build',
                            help='run only specified build type',
                            choices=ctx_choices(ctx._Build), nargs='*')
        parser.add_argument('-f', dest='fs',
                            choices=ctx_choices(ctx._Fs), nargs='*',
                            help='run tests on specified filesystem types.')
        parser.add_argument('-t', dest='test_type',
                            help='run only specified test type where'
                            'check = short + medium',
                            choices=ctx_choices(ctx._TestType), nargs='*')
        parser.add_argument('-o', dest='timeout',
                            help="set timeout for test execution timeout:"
                            "integer with an optional suffix:''s' for seconds,"
                            "'m' for minutes, 'h' for hours or 'd' for days.")
        parser.add_argument('-u', dest='test_sequence',
                            help='run only tests from specified test sequence '
                            'e.g.: 0-2,5 will execute TEST0, '
                            'TEST1, TEST2 and TEST5',
                            default='')

        tracers = parser.add_mutually_exclusive_group()
        tracers.add_argument('--tracer', dest='tracer', help='run C binary '
                             'with provided tracer command. With this option '
                             'stdout and stderr are not redirected, enabling '
                             'interactive sessions.',
                             default='')
        tracers.add_argument('--gdb', dest='tracer', action='store_const',
                             const='gdb --args', help='run gdb as a tracer')
        tracers.add_argument('--cgdb', dest='tracer', action='store_const',
                             const='cgdb --args', help='run cgdb as a tracer')

        if sys.platform != 'win32':
            fe_choices = [t.name.lower() for t in vg.TOOLS]
            parser.add_argument('--force-enable', choices=fe_choices,
                                default=None)

        return parser
