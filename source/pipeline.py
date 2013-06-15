#!/usr/bin/env python
#
# $File: pipeline.py$
# $LastChangedDate: 2013-04-23 11:58:41 -0500 (Tue, 23 Apr 2013) $
# $Rev: 1855 $
#
# This file is part of variant_tools, a software application to annotate,
# summarize, and filter variants for next-gen sequencing ananlysis.
# Please visit http://varianttools.sourceforge.net for details.
#
# Copyright (C) 2013 Bo Peng (bpeng@mdanderson.org)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#

import os
import sys
import subprocess
import glob
import argparse
import logging
import shutil
import tarfile
import copy
import gzip
import bz2
import zipfile
import time
import re
import platform
import smtplib
from email.mime.text import MIMEText
from collections import namedtuple

try:
    import pysam
    hasPySam = True
except ImportError as e:
    hasPySam = False

class Pipeline:
    def __init__(self, name, fmt_args=[]):
        '''Input file format'''
        # locate a file format specification file
        self.description = None
        #
        if os.path.isfile(name + '.fmt'):
            self.name = os.path.split(name)[-1]
            args = self.parseArgs(name + '.fmt', fmt_args)
            self.parseFMT(name + '.fmt', defaults=args) 
        elif name.endswith('.fmt') and os.path.isfile(name):
            self.name = os.path.split(name)[-1][:-4]
            args = self.parseArgs(name, fmt_args)
            self.parseFMT(name, defaults=args) 
        else:
            url = 'format/{}.fmt'.format(name)
            try:
                fmt = downloadFile(url, quiet=True)
            except Exception as e:
                raise ValueError('Failed to download format specification file {}.fmt: {}'.format(name, e))
            self.name = name
            args = self.parseArgs(fmt, fmt_args)
            self.parseFMT(fmt, defaults=args)

    def parseArgs(self, filename, fmt_args):
        fmt_parser = ConfigParser.SafeConfigParser()
        fmt_parser.read(filename)
        parameters = fmt_parser.items('DEFAULT')
        parser = argparse.ArgumentParser(prog='vtools CMD --pipeline {}'.format(os.path.split(filename)[-1]), description='''Parameters to override fields of
            existing format.''')
        self.parameters = []
        for par in parameters:
            # $NAME_comment is used for documentation only
            if par[0].endswith('_comment'):
                continue
            par_help = [x[1] for x in parameters if x[0] == par[0] + '_comment']
            self.parameters.append((par[0], par[1], par_help[0] if par_help else ''))
            parser.add_argument('--{}'.format(par[0]), help=self.parameters[-1][2],
                nargs='*', default=par[1])
        args = vars(parser.parse_args(fmt_args))
        for key in args:
            if type(args[key]) == list:
                args[key] = ','.join(args[key])
        return args

    def parsePipeline(self, filename, defaults):
        parser = ConfigParser.SafeConfigParser()
        # this allows python3 to read .fmt file with non-ascii characters, but there is no
        # simple way to make it python2 compatible.
        #with open(filename, 'r', encoding='UTF-8') as inputfile:
        #    parser.readfp(inputfile)
        parser.read(filename)
        # sections?
        sections = parser.sections()
        if 'format description' not in sections:
            raise ValueError("Missing section 'format description'")
        #
        fields = []
        columns = []
        self.formatter = {}
        for section in sections:
            if section.lower() == 'format description':
                continue
            if section.lower() == 'field formatter':
                for item in parser.items(section, vars=defaults):
                    if item[0].startswith('fmt_'):
                        self.formatter[item[0][4:]] = item[1]
                continue
            if section.startswith('col_'):
                try:
                    items = [x[0] for x in parser.items(section, raw=True)]
                    for item in items:
                        if item.endswith('_comment'):
                            continue
                        if item not in ['field', 'adj', 'comment'] + defaults.keys():
                            raise ValueError('Incorrect key {} in section {}. Only field, adj and comment are allowed.'.format(item, section))
                    columns.append(
                        Column(index=int(section.split('_', 1)[1]),
                            field=parser.get(section, 'field', vars=defaults) if 'field' in items else '',
                            adj=parser.get(section, 'adj', vars=defaults) if 'adj' in items else None,
                            comment=parser.get(section, 'comment', raw=True) if 'comment' in items else '')
                        )
                except Exception as e:
                    raise ValueError('Invalid section {}: {}'.format(section, e))
            else:
                if not section.replace('_', '').isalnum():
                  raise ValueError('Illegal field name {}. Field names can only contain alphanumeric characters and underscores'.format(repr(section)))
                if section.upper() in SQL_KEYWORDS:
                  raise ValueError('Illegal field name. {} conflicts with SQL keywords'.format(repr(section)))
                try:
                    items = [x[0] for x in parser.items(section, raw=True)]
                    for item in items:
                        if item.endswith('_comment'):
                            continue
                        if item not in ['index', 'type', 'adj', 'comment'] + defaults.keys():
                            raise ValueError('Incorrect key {} in section {}. Only index, type, adj and comment are allowed.'.format(item, section))
                    fields.append(
                        Field(name=section,
                            index=parser.get(section, 'index', vars=defaults),
                            type=parser.get(section, 'type', vars=defaults),
                            adj=parser.get(section, 'adj', vars=defaults) if 'adj' in items else None,
                            comment=parser.get(section, 'comment', raw=True) if 'comment' in items else '')
                        )
                except Exception as e:
                    raise ValueError('Invalid section {}: {}'.format(section, e))
        #
        if len(fields) == 0:
            raise ValueError('No valid field is defined in format specification file {}'.format(self.name))
        #
        self.delimiter = '\t'
        #
        for item in parser.items('format description', vars=defaults):
            if item[0] == 'description':
                self.description = item[1]
            elif item[0] == 'delimiter':
                try:
                    # for None, '\t' etc
                    self.delimiter = eval(item[1])
                except:
                    # if failed, take things literally.
                    self.delimiter = item[1]
            elif item[0] == 'encoding':
                self.encoding = item[1]
            elif item[0] == 'preprocessor':
                self.preprocessor = item[1]
            elif item[0] == 'merge_by':
                self.merge_by_cols = [x-1 for x in eval(item[1])]
            elif item[0] == 'export_by':
                self.export_by_fields = item[1]
            elif item[0] == 'additional_exports':
                # additional files to export from the variant/sample tables
                self.additional_exports = item[1]
            elif item[0] == 'sort_output_by':
                self.order_by_fields = item[1]
            elif item[0] == 'header':
                if item[1] in ('none', 'None'):
                    self.header = None
                else:
                    try:
                        self.header = int(item[1])
                    except:
                        # in this case header is a pattern
                        self.header = re.compile(item[1])
            elif item[0] in ['variant', 'position', 'range', 'genotype', 'variant_info', 'genotype_info']:
                setattr(self, item[0] if item[0].endswith('_info') else item[0]+'_fields', [x.strip() for x in item[1].split(',') if x.strip()])
        #
        # Post process all fields
        if (not not self.variant_fields) + (not not self.position_fields) + (not not self.range_fields) != 1:
            raise ValueError('Please specify one and only one of "variant=?", "position=?" or "range=?"')
        #
        if self.variant_fields:
            self.input_type = 'variant'
            self.ranges = [0, 4]
            self.fields = self.variant_fields
            if len(self.fields) != 4:
                raise ValueError('"variant" fields should have four fields for chr, pos, ref, and alt alleles')
        elif self.position_fields:
            self.input_type = 'position'
            self.ranges = [0, 2]
            self.fields = self.position_fields
            if len(self.fields) != 2:
                raise ValueError('"position" fields should have two fields for chr and pos')
        elif self.range_fields:
            self.input_type = 'range'
            self.ranges = [0, 3]
            self.fields = self.range_fields
            if len(self.fields) != 3:
                raise ValueError('"range" fields should have three fields for chr and starting and ending position')
        #
        if self.input_type != 'variant' and not self.variant_info:
            raise ValueError('Input file with type position or range must specify variant_info')
        if self.input_type != 'variant' and self.genotype_info:
            raise ValueError('Input file with type position or range can not have any genotype information.')
        if self.genotype_fields and len(self.genotype_fields) != 1:
            raise ValueError('Only one genotype field is allowed to input genotype for one or more samples.')
        #
        if self.variant_info:
            self.fields.extend(self.variant_info)
        self.ranges.append(self.ranges[-1] + (len(self.variant_info) if self.variant_info else 0))
        if self.genotype_fields:
            self.fields.extend(self.genotype_fields)
        self.ranges.append(self.ranges[-1] + (len(self.genotype_fields) if self.genotype_fields else 0))
        if self.genotype_info:
            self.fields.extend(self.genotype_info)
        self.ranges.append(self.ranges[-1] + (len(self.genotype_info) if self.genotype_info else 0))
        #
        # now, change to real fields
        for i in range(len(self.fields)):
            fld = [x for x in fields if x.name == self.fields[i]]
            if len(fld) != 1:
                #
                # This is a special case that allows users to use expressions as field....
                #
                env.logger.warning('Undefined field {} in format {}.'.format(self.fields[i], filename))
                self.fields[i] = Field(name=self.fields[i], index=None, adj=None, type=None, comment='')
            else:
                self.fields[i] = fld[0]
        # other fields?
        self.other_fields = [x for x in fields if x not in self.fields]
        #
        # columns definition
        self.columns = []
        for idx in range(len(columns)):
            # find column
            try:
                col = [x for x in columns if x.index == idx + 1][0]
            except Exception as e:
                raise ValueError('Cannot find column {} from format specification: {}'.format(idx + 1, e))
            self.columns.append(col)

    def describe(self):
        print('Format:      {}'.format(self.name))
        if self.description is not None:
            print('Description: {}'.format('\n'.join(textwrap.wrap(self.description,
                initial_indent='', subsequent_indent=' '*2))))
        #
        if self.preprocessor is not None:
            print('Preprocessor: {}'.format(self.preprocessor))
        #
        print('\nColumns:')
        if self.columns:
            for col in self.columns:
                print('  {:12} {}'.format(str(col.index), '\n'.join(textwrap.wrap(col.comment,
                    subsequent_indent=' '*15))))
            if self.formatter:
                print('Formatters are provided for fields: {}'.format(', '.join(self.formatter.keys())))
        else:
            print('  None defined, cannot export to this format')
        #
        if self.input_type == 'variant':
            print('\n{0}:'.format(self.input_type))
        for fld in self.fields[self.ranges[0]:self.ranges[1]]:
            print('  {:12} {}'.format(fld.name, '\n'.join(textwrap.wrap(fld.comment,
                subsequent_indent=' '*15))))
        if self.ranges[1] != self.ranges[2]:
            print('\nVariant info:')
            for fld in self.fields[self.ranges[1]:self.ranges[2]]:
                print('  {:12} {}'.format(fld.name, '\n'.join(textwrap.wrap(fld.comment,
                    subsequent_indent=' '*15))))
        if self.ranges[2] != self.ranges[3]:
            print('\nGenotype:')
            for fld in self.fields[self.ranges[2]:self.ranges[3]]:
                print('  {:12} {}'.format(fld.name, '\n'.join(textwrap.wrap(fld.comment,
                    subsequent_indent=' '*15))))
        if self.ranges[3] != self.ranges[4]:
            print('\nGenotype info:')
            for fld in self.fields[self.ranges[3]:self.ranges[4]]:
                print('  {:12} {}'.format(fld.name, '\n'.join(textwrap.wrap(fld.comment,
                    subsequent_indent=' '*15))))
        if self.other_fields:
            print('\nOther fields (usable through parameters):')
            for fld in self.other_fields:
                print('  {:12} {}'.format(fld.name, '\n'.join(textwrap.wrap(fld.comment,
                    subsequent_indent=' '*15))))
        if self.parameters:
            print('\nFormat parameters:')
            for item in self.parameters:
                print('  {:12} {}'.format(item[0],  '\n'.join(textwrap.wrap(
                    '{} (default: {})'.format(item[2], item[1]),
                    subsequent_indent=' '*15))))
        else:
            print('\nNo configurable parameter is defined for this format.\n')




###############################################################################
#
# Runtime environment
#
# This class is used to create an singleton global object env that contains
# various runtime information, including
#
# 1. A logger object that writes information to standard output and a log file
# 2. A running_jobs list that keeps instances of running jobs
# 3. A options dictionary that hold all user-provided options. The options
#    can be accessed as attributed (e.g. env.OPT_JAVA).
#
###############################################################################
#
# All options, their default values, and a validator function
# if needed.
#
def physicalMemory():
    '''Get the amount of physical memory in the system'''
    # MacOSX?
    if platform.platform().startswith('Darwin'):
        # FIXME
        return None
    elif platform.platform().startswith('Linux'):
        try:
            res = subprocess.check_output('free').decode().split('\n')
            return int(res[1].split()[1])
        except Exception as e:
            return None

def javaXmxCheck(val):
    '''Check if the Xmx option is valid for OPT_JAVA'''
    ram = physicalMemory()
    # cannot check physical memory
    if ram is None:
        return
    # find option matching '-Xmx???'
    m = re.search('-Xmx(\d+)([^\s]*)(?:\s+|$)', val)
    if m is None:  # no -Xmx specified
        return
    try:
        size = int(m.group(1)) * {
            't': 10**9,
            'T': 10**9,
            'g': 10**6,
            'G': 10**6,
            'm': 10**3,
            'M': 10**3,
            '': 1
        }[m.group(2)]
    except:
        sys.exit('Invalid java option {}'.format(val))
    #
    if ram < size:
        sys.exit('Specified -Xms size {} is larger than available physical memory {}'
            .format(size, ram))

options = [
    ('RESOURCE_DIR', '~/.variant_tools/var_caller'),
    ('WORKING_DIR', ''),
    # notification emails
    ('NOTIFICATION_EMAIL', ''),
    ('SMTP_HOST', 'smtp.gmail.com'),  # for gmail
    ('SMTP_PORT', '465'),             # for gmail
    ('SMTP_SSL', 'True'),             # use SSL or not, default to True
    ('SMTP_USER', ''),                # email address
    ('SMTP_PASSWORD', ''),            # email password
    # option, default value
    ('PICARD_PATH', ''),
    ('GATK_PATH', ''),
    #
    #
    # for private use
    ('_WORKING_DIR', ''),
    ('_LOGFILE', ''),
]

class RuntimeEnvironment(object):
    '''Define the runtime environment of the pipeline'''
    # the following makes RuntimeEnvironment a singleton class
    _instance = None
    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(RuntimeEnvironment, cls).__new__(cls, *args, 
                **kwargs)
        return cls._instance

    def __init__(self):
        #
        # file and screen logging
        self._logger = None
        self._LOGFILE = None
        #
        # additional parameters for args
        self._validators = {}
        for x in options:
            if len(x) == 3:
                self._validators[x[0]] = x[2]
            if ('CALL_VARIANTS_' + x[0]) in os.environ:
                self.__setattr__(x[0], os.environ['CALL_VARIANTS_' + x[0]])
            else:
                self.__setattr__(x[0], x[1])
        #
        # running_jobs implements a simple multi-processing queue system. 
        # This variable holds a JOB tuple of the running jobs.
        #
        # Functions:
        #   call(cmd, upon_succ, wait=False)
        #      add an entry until there is less than self._max_jobs running jobs
        #   poll()
        #      check the number of running jobs
        #   wait()
        #      wait till all jobs are completed
        self._max_jobs = 1
        self.running_jobs = []

    def __setattr__(self, name, value):
        '''Set options '''
        if hasattr(self, '_validators') and name in self._validators:
            self._validators[name](value)
        object.__setattr__(self, name, value)

    #
    # max number of jobs
    #
    def _setMaxJobs(self, x):
        try:
            self._max_jobs = int(x)
        except Exception as e:
            sys.exit('Failed to set max jobs: {}'.format(e))

    jobs = property(lambda self:self._max_jobs, _setMaxJobs)

    class ColoredFormatter(logging.Formatter):
        ''' A logging format with colored output, which is copied from
        http://stackoverflow.com/questions/384076/how-can-i-make-the-python-logging-output-to-be-colored
        '''
        def __init__(self, msg):
            logging.Formatter.__init__(self, msg)
            self.LEVEL_COLOR = {
                'DEBUG': 'BLUE',
                'WARNING': 'PURPLE',
                'ERROR': 'RED',
                'CRITICAL': 'RED_BG',
                }
            self.COLOR_CODE={
                'ENDC':0,  # RESET COLOR
                'BOLD':1,
                'UNDERLINE':4,
                'BLINK':5,
                'INVERT':7,
                'CONCEALD':8,
                'STRIKE':9,
                'GREY30':90,
                'GREY40':2,
                'GREY65':37,
                'GREY70':97,
                'GREY20_BG':40,
                'GREY33_BG':100,
                'GREY80_BG':47,
                'GREY93_BG':107,
                'DARK_RED':31,
                'RED':91,
                'RED_BG':41,
                'LIGHT_RED_BG':101,
                'DARK_YELLOW':33,
                'YELLOW':93,
                'YELLOW_BG':43,
                'LIGHT_YELLOW_BG':103,
                'DARK_BLUE':34,
                'BLUE':94,
                'BLUE_BG':44,
                'LIGHT_BLUE_BG':104,
                'DARK_MAGENTA':35,
                'PURPLE':95,
                'MAGENTA_BG':45,
                'LIGHT_PURPLE_BG':105,
                'DARK_CYAN':36,
                'AUQA':96,
                'CYAN_BG':46,
                'LIGHT_AUQA_BG':106,
                'DARK_GREEN':32,
                'GREEN':92,
                'GREEN_BG':42,
                'LIGHT_GREEN_BG':102,
                'BLACK':30,
            }

        def colorstr(self, astr, color):
            return '\033[{}m{}\033[{}m'.format(self.COLOR_CODE[color], astr, 
                self.COLOR_CODE['ENDC'])

        def format(self, record):
            record = copy.copy(record)
            levelname = record.levelname
            if levelname in self.LEVEL_COLOR:
                record.levelname = self.colorstr(levelname, self.LEVEL_COLOR[levelname])
                record.name = self.colorstr(record.name, 'BOLD')
                record.msg = self.colorstr(record.msg, self.LEVEL_COLOR[levelname])
            return logging.Formatter.format(self, record)

    def _setLogger(self, logfile=None):
        '''Create a logger with colored console output, and a log file if a
        filename is provided.'''
        # create a logger
        if self._logger is not None:
            self._logger.handlers = []
        self._logger = logging.getLogger()
        self._logger.setLevel(logging.DEBUG)
        # output to standard output
        cout = logging.StreamHandler()
        cout.setLevel(logging.INFO)
        cout.setFormatter(RuntimeEnvironment.ColoredFormatter('%(levelname)s: %(message)s'))
        self._logger.addHandler(cout)
        if logfile is not None:
            # output to a log file
            ch = logging.FileHandler(logfile, 'a')
            ch.setLevel(logging.DEBUG)
            ch.setFormatter(logging.Formatter('%(asctime)s: %(levelname)s: %(message)s'))
            self._logger.addHandler(ch)
            self._LOGFILE = logfile
    #
    logger = property(lambda self: self._logger, _setLogger)

    

# create a runtime environment object
env = RuntimeEnvironment()


###############################################################################
#
# A simple job management scheme 
#
# NOTE:
#   subprocess.PIPE cannot be used for NGS commands because they tend to send
#   a lot of progress output to stderr, which might block PIPE and cause the
#   command itself to fail, or stall (which is even worse).
#
###############################################################################
JOB = namedtuple('JOB', 'proc cmd upon_succ start_time stdout stderr name')

def elapsed_time(start):
    '''Return the elapsed time in human readable format since start time'''
    second_elapsed = int(time.time() - start)
    days_elapsed = second_elapsed // 86400
    return ('{} days '.format(days_elapsed) if days_elapsed else '') + \
        time.strftime('%H:%M:%S', time.gmtime(second_elapsed % 86400))
 
def run_command(cmd, name=None, upon_succ=None, wait=True):
    '''Call an external command, raise an error if it fails.
    If upon_succ is specified, the specified function and parameters will be
    evalulated after the job has been completed successfully.
    If a name is given, stdout and stderr will be sent to name.out and 
    name.err under env.WORKING_DIR. Otherwise, stdout and stderr will be
    ignored (send to /dev/null).
    '''
    # merge mulit-line command into one line and remove extra white spaces
    cmd = ' '.join(cmd.split())
    if name is None:
        try:
            proc_out = subprocess.DEVNULL
            proc_err = subprocess.DEVNULL
        except:
            # subprocess.DEVNULL was introduced in Python 3.3
            proc_out = open(os.devnull, 'w')
            proc_err = open(os.devnull, 'w')
    else:
        name = name.replace('/', '_')
        proc_out = open(os.path.join(env.WORKING_DIR, name + '.out'), 'w')
        proc_err = open(os.path.join(env.WORKING_DIR, name + '.err'), 'w')
    if wait or env.jobs == 1:
        try:
            s = time.time()
            env.logger.info('Running {}'.format(cmd))
            proc = subprocess.Popen(cmd, shell=True, stdout=proc_out, stderr=proc_err)
            retcode = proc.wait()
            if name is not None:
                proc_out.close()
                proc_err.close()
            if retcode < 0:
                env.logger.error('Command {} was terminated by signal {} after executing {}'
                    .format(cmd, -retcode, elapsed_time(s)))
                sendmail('Command {} terminated.'.format(cmd[:40]))
                sys.exit(1)
            elif retcode > 0:
                if name is not None:
                    with open(os.path.join(env.WORKING_DIR, name + '.err')) as err:
                        for line in err.read().split('\n')[-20:]:
                            env.logger.error(line)
                env.logger.error("Command {} returned {} after executing {}"
                    .format(cmd, retcode, elapsed_time(s)))
                sendmail('Command {} failed.'.format(cmd[:40]))
                sys.exit(1)
            env.logger.info('Command {} completed successfully in {}'
                .format(cmd, elapsed_time(s)))
        except OSError as e:
            env.logger.error("Execution of command {} failed: {}".format(cmd, e))
            sendmail('Cannot execute command {}.'.format(cmd[:40]))
            sys.exit(1)
        # everything is OK
        if upon_succ:
            # call the function (upon_succ) using others as parameters.
            upon_succ[0](*(upon_succ[1:]))
    else:
        # wait for empty slot to run the job
        while True:
            if poll_jobs() >= env.jobs:
                time.sleep(5)
            else:
                break
        # there is a slot, start running
        proc = subprocess.Popen(cmd, shell=True, stdout=proc_out, stderr=proc_err)
        env.running_jobs.append(JOB(proc=proc, cmd=cmd, upon_succ=upon_succ,
            start_time=time.time(), stdout=proc_out, stderr=proc_err, name=name))
        env.logger.info('Running {}'.format(cmd))

def poll_jobs():
    '''check the number of running jobs.'''
    count = 0
    for idx, job in enumerate(env.running_jobs):
        if job is None:
            continue
        ret = job.proc.poll()
        if ret is None:  # still running
            count += 1
            continue
        #
        # job completed, close redirected stdout and stderr
        # for python 3.3. where job.stdout is DEVNULL, this will 
        # fail so try/except is needed.
        try:
            job.stdout.close()
            job.stderr.close()
        except:
            pass
        #
        if ret < 0:
            env.logger.error("Command {} was terminated by signal {} after executing {}"
                .format(job.cmd, -ret, elapsed_time(job.start_time)))
            sendmail('Command {} terminated.'.format(job.cmd[:40]))
            sys.exit(1)
        elif ret > 0:
            if job.name is not None:
                with open(os.path.join(env.WORKING_DIR, job.name + '.err')) as err:
                    for line in err.read().split('\n')[-50:]:
                        env.logger.error(line)
            env.logger.error('Execution of command {} failed after {} (return code {}).'
                .format(job.cmd, elapsed_time(job.start_time), ret))
            sendmail('Command {} failed.'.format(job.cmd[:40]))
            sys.exit(1)
        else:
            if job.name is not None:
                with open(os.path.join(env.WORKING_DIR, job.name + '.err')) as err:
                    for line in err.read().split('\n')[-10:]:
                        env.logger.info(line)
            # finish up
            if job.upon_succ:
                # call the upon_succ function
                job.upon_succ[0](*(job.upon_succ[1:]))
            env.logger.info('Command {} completed successfully in {}'
                .format(job.cmd, elapsed_time(job.start_time)))
            #
            env.running_jobs[idx] = None
    return count

def wait_all():
    '''Wait for all pending jobs to complete'''
    while poll_jobs() > 0:
        # sleep ten seconds before checking job status again.
        time.sleep(10)
    env.running_jobs = []



###############################################################################
#
# Utility functions
#
###############################################################################

def checkCmd(cmd):
    '''Check if a cmd exist'''
    if not hasattr(shutil, 'which'):
        # if shutil.which does not exist, use subprocess...
        try:
            subprocess.call(cmd, stdout=open(os.devnull, 'w'), stderr=open(os.devnull, 'w'))
            return
        except:
            env.logger.error('Command {} does not exist. Please install it and try again.'
                .format(cmd))
            sys.exit(1)
    if shutil.which(cmd) is None:
        env.logger.error('Command {} does not exist. Please install it and try again.'
            .format(cmd))
        sys.exit(1)


def sendmail(subject):
    '''Send an email to report status.'''
    if not env.NOTIFICATION_EMAIL:
        return
    try:
        if env._LOGFILE is not None:
            # email the last few lines of the log message
            with open(env._LOGFILE) as err:
                text = '\n'.join(err.read().split('\n')[-50:])
        else:
            text = 'JOB FAILED.'
        msg = MIMEText(text)
        msg['Subject'] = subject
        msg['From'] = env.SMTP_USER
        msg['To'] = env.NOTIFICATION_EMAIL
        if env.SMTP_SSL.lower() in ['true', '1', 'yes', 'y']:
            s = smtplib.SMTP_SSL(host=env.SMTP_HOST, port=env.SMTP_PORT)
        else:
            s = smtplib.SMTP(host=env.SMTP_HOST, port=env.SMTP_PORT)
        s.login(user=env.SMTP_USER, password=env.SMTP_PASSWORD)
        s.sendmail(env.SMTP_USER, [env.NOTIFICATION_EMAIL], msg.as_string())
        s.quit()
        env.logger.info('Email with subject "{}" is sent to {}'
            .format(subject[:40], env.NOTIFICATION_EMAIL))
    except Exception as e:
        env.logger.warning('Failed to send mail with subject {}: {}'
            .format(subject, e))


## def sendMail(header, message=''):
##     '''Send an email to report status.'''
##     if not env.NOTIFICATION_EMAIL:
##         return
##     sendmail_location = '/usr/sbin/sendmail' # sendmail location
##     p = os.popen('{} -t'.format(sendmail_location), 'w')
##     p.write('From: call_variants.py\n')
##     p.write('To: {}\n'.format(env.NOTIFICATION_EMAIL))
##     p.write('Subject: {}\n'.format(header))
##     p.write('\n') # blank line separating headers from body
##     p.write(message)
##     status = p.close()
##     if status != 0:
##        env.logger.warning('Send mail failed.')
## 

def downloadFile(URL, dest, quiet=False):
    '''Download a file from URL and save to dest'''
    # for some strange reason, passing wget without shell=True can fail silently.
    env.logger.info('Downloading {}'.format(URL))
    if os.path.isfile(dest):
        env.logger.warning('Using existing downloaded file {}.'.format(dest))
        return dest
    p = subprocess.Popen('wget {} -O {} {}'
        .format('-q' if quiet else '', TEMP(dest), URL), shell=True)
    ret = p.wait()
    if ret == 0 and os.path.isfile(TEMP(dest)):
        os.rename(TEMP(dest), dest)
        return dest
    else:
        try:
            os.remove(TEMP(dest))
        except OSError:
            pass
        raise RuntimeError('Failed to download {} using wget'.format(URL))


def fastqVersion(fastq_file):
    '''Detect the version of input fastq file. This can be very inaccurate'''
    #
    # This function assumes each read take 4 lines, and the last line contains
    # quality code. It collects about 1000 quality code and check their range,
    # and use it to determine if it is Illumina 1.3+
    #
    qual_scores = ''
    with open(fastq_file) as fastq:
        while len(qual_scores) < 1000:
            try:
                line = fastq.readline()
            except Exception as e:
                env.logger.error('Failed to read fastq file {}: {}'
                    .format(fastq_file, e))
                sys.exit(1)
            if not line.startswith('@'):
                raise ValueError('Wrong FASTA file {}'.format(fastq_file))
            line = fastq.readline()
            line = fastq.readline()
            if not line.startswith('+'):
                env.logger.warning(
                    'Suspiciout FASTA file {}: third line does not start with "+".'
                    .foramt(fastq_file))
                return 'Unknown'
            line = fastq.readline()
            qual_scores += line.strip()
    #
    min_qual = min([ord(x) for x in qual_scores])
    max_qual = max([ord(x) for x in qual_scores])
    env.logger.debug('FASTA file with quality score ranging {} to {}'
        .format(min_qual, max_qual))
    # Sanger qual score has range Phred+33, so 33, 73 with typical score range 0 - 40
    # Illumina qual scores has range Phred+64, which is 64 - 104 with typical score range 0 - 40
    if min_qual >= 64 or max_qual > 90:
        # option -I is needed for bwa if the input is Illumina 1.3+ read format (quliaty equals ASCII-64).
        return 'Illumina 1.3+'
    else:
        # no option is needed for bwa
        return 'Sanger'

def existAndNewerThan(ofiles, ifiles):
    '''Check if ofiles is newer than ifiles. The oldest timestamp
    of ofiles and newest timestam of ifiles will be used if 
    ofiles or ifiles is a list.'''
    if type(ofiles) == list:
        if not all([os.path.isfile(x) for x in ofiles]):
            return False
    else:
        if not os.path.isfile(ofiles):
            return False
    #
    if type(ofiles) == list:
        output_timestamp = min([os.path.getmtime(x) for x in ofiles])
    else:
        output_timestamp = os.path.getmtime(ofiles)
    #
    if type(ifiles) == list:
        input_timestamp = max([os.path.getmtime(x) for x in ifiles])
    else:
        input_timestamp = os.path.getmtime(ifiles)
    #
    if output_timestamp - input_timestamp < 2:
        env.logger.warning(
            'Existing output file {} is ignored because it is older than input file.'
            .format(', '.join(ofiles) if type(ofiles) == list else ofiles))
        return False
    else:
        # newer by at least 10 seconds.
        return True

def TEMP(filename):
    '''Temporary output of filename'''
    # turn path/filename.ext to path/filename_tmp???.ext, where ??? is
    # the process ID to avoid two processes writing to the same temp
    # files. That is to say, if two processes are working on the same step
    # they will produce different temp files, and the final results should 
    # still be valid.
    return '_tmp{}.'.format(os.getpid()).join(filename.rsplit('.', 1))

def decompress(filename, dest_dir=None):
    '''If the file ends in .tar.gz, .tar.bz2, .bz2, .gz, .tgz, .tbz2, decompress
    it to dest_dir (current directory if unspecified), and return a list of files.
    Uncompressed files will be returned untouched. If the destination files exist
    and newer, this function will return immediately.'''
    mode = None
    if filename.lower().endswith('.tar.gz') or filename.lower().endswith('.tar.bz2'):
        mode = 'r:gz'
    elif filename.lower().endswith('.tbz2') or filename.lower().endswith('.tgz'):
        mode = 'r:bz2'
    elif filename.lower().endswith('.tar'):
        mode = 'r'
    elif filename.lower().endswith('.gz'):
        dest_file = os.path.join('.' if dest_dir is None else dest_dir,
            os.path.basename(filename)[:-3])
        if existAndNewerThan(ofiles=dest_file, ifiles=filename):
            env.logger.warning('Using existing decompressed file {}'.format(dest_file))
        else:
            env.logger.info('Decompressing {} to {}'.format(filename, dest_file))
            with gzip.open(filename, 'rb') as gzinput, open(TEMP(dest_file), 'wb') as output:
                content = gzinput.read(10000000)
                while content:
                    output.write(content)
                    content = gzinput.read(10000000)
            # only rename the temporary file to the right one after finishing everything
            # this avoids corrupted files
            os.rename(TEMP(dest_file), dest_file)
        return [dest_file]
    elif filename.lower().endswith('.bz2'):
        dest_file = os.path.join('.' if dest_dir is None else dest_dir, os.path.basename(filename)[:-4])
        if existAndNewerThan(ofiles=dest_file, ifiles=filename):
            env.logger.warning('Using existing decompressed file {}'.format(dest_file))
        else:
            env.logger.info('Decompressing {} to {}'.format(filename, dest_file))
            with bz2.open(filename, 'rb') as bzinput, open(TEMP(dest_file), 'wb') as output:
                content = bzinput.read(10000000)
                while content:
                    output.write(content)
                    content = bzinput.read(10000000)
            # only rename the temporary file to the right one after finishing everything
            # this avoids corrupted files
            os.rename(TEMP(dest_file), dest_file)
        return [dest_file]
    elif filename.lower().endswith('.zip'):
        bundle = zipfile.ZipFile(filename)
        dest_dir = '.' if dest_dir is None else dest_dir
        bundle.extractall(dest_dir)
        env.logger.info('Decompressing {} to {}'.format(filename, dest_dir))
        return [os.path.join(dest_dir, name) for name in bundle.namelist()]
    #
    # if it is a tar file
    if mode is not None:
        env.logger.info('Extracting fastq sequences from tar file {}'
            .format(filename))
        #
        # MOTE: open a compressed tar file can take a long time because it needs to scan
        # the whole file to determine its content. I am therefore creating a manifest
        # file for the tar file in the dest_dir, and avoid re-opening when the tar file
        # is processed again.
        manifest = os.path.join( '.' if dest_dir is None else dest_dir,
            os.path.basename(filename) + '.manifest')
        all_extracted = False
        dest_files = []
        if existAndNewerThan(ofiles=manifest, ifiles=filename):
            all_extracted = True
            for f in [x.strip() for x in open(manifest).readlines()]:
                dest_file = os.path.join( '.' if dest_dir is None else dest_dir, os.path.basename(f))
                if existAndNewerThan(ofiles=dest_file, ifiles=filename):
                    dest_files.append(dest_file)
                    env.logger.warning('Using existing extracted file {}'.format(dest_file))
                else:
                    all_extracted = False
        #
        if all_extracted:
            return dest_files
        #
        # create a temporary directory to avoid corrupted file due to interrupted decompress
        try:
            os.mkdir('tmp' if dest_dir is None else os.path.join(dest_dir, 'tmp'))
        except:
            # directory might already exist
            pass
        #
        dest_files = []
        with tarfile.open(filename, mode) as tar:
            files = tar.getnames()
            # save content to a manifest
            with open(manifest, 'w') as manifest:
                for f in files:
                    manifest.write(f + '\n')
            for f in files:
                # if there is directory structure within tar file, decompress all to the current directory
                dest_file = os.path.join( '.' if dest_dir is None else dest_dir, os.path.basename(f))
                dest_files.append(dest_file)
                if existAndNewerThan(ofiles=dest_file, ifiles=filename):
                    env.logger.warning('Using existing extracted file {}'.format(dest_file))
                else:
                    env.logger.info('Extracting {} to {}'.format(f, dest_file))
                    tar.extract(f, 'tmp' if dest_dir is None else os.path.join(dest_dir, 'tmp'))
                    # move to the top directory with the right name only after the file has been properly extracted
                    shutil.move(os.path.join('tmp' if dest_dir is None else os.path.join(dest_dir, 'tmp'), f), dest_file)
            # set dest_files to the same modification time. This is used to
            # mark the right time when the files are created and avoid the use
            # of archieved but should-not-be-used files that might be generated later
            [os.utime(x, None) for x in dest_files]
        return dest_files
    # return source file if 
    return [filename]
   
   
def getReadGroup(fastq_filename, output_bam):
    '''Get read group information from names of fastq files.'''
    # Extract read group information from filename such as
    # GERALD_18-09-2011_p-illumina.8_s_8_1_sequence.txt. The files are named 
    # according to the lane that produced them and whether they
    # are paired or not: Single-end reads s_1_sequence.txt for lane 1;
    # s_2_sequence.txt for lane 2 Paired-end reads s_1_1_sequence.txt 
    # for lane 1, pair 1; s_1_2_sequence.txt for lane 1, pair 2
    #
    # This function return a read group string like '@RG\tID:foo\tSM:bar'
    #
    # ID* Read group identifier. Each @RG line must have a unique ID. The
    # value of ID is used in the RG
    #     tags of alignment records. Must be unique among all read groups
    #     in header section. Read group
    #     IDs may be modifid when merging SAM fies in order to handle collisions.
    # CN Name of sequencing center producing the read.
    # DS Description.
    # DT Date the run was produced (ISO8601 date or date/time).
    # FO Flow order. The array of nucleotide bases that correspond to the
    #     nucleotides used for each
    #     flow of each read. Multi-base flows are encoded in IUPAC format, 
    #     and non-nucleotide flows by
    #     various other characters. Format: /\*|[ACMGRSVTWYHKDBN]+/
    # KS The array of nucleotide bases that correspond to the key sequence
    #     of each read.
    # LB Library.
    # PG Programs used for processing the read group.
    # PI Predicted median insert size.
    # PL Platform/technology used to produce the reads. Valid values: 
    #     CAPILLARY, LS454, ILLUMINA,
    #     SOLID, HELICOS, IONTORRENT and PACBIO.
    # PU Platform unit (e.g. flowcell-barcode.lane for Illumina or slide for
    #     SOLiD). Unique identifier.
    # SM Sample. Use pool name where a pool is being sequenced.
    #
    filename = os.path.basename(fastq_filename)
    output = os.path.basename(output_bam)
    # sample name is obtained from output filename without file extension
    SM = output.split('.', 1)[0]
    # always assume ILLUMINA for this script and BWA for processing
    PL = 'ILLUMINA'  
    PG = 'BWA'
    #
    # PU is for flowcell and lane information, ID should be unique for each
    #     readgroup
    # ID is temporarily obtained from input filename without exteion
    ID = filename.split('.')[0]
    # try to get lan information from s_x_1/2 pattern
    try:
        PU = re.search('s_([^_]+)_', filename).group(1)
    except AttributeError:
        env.logger.warning('Failed to guess lane information from filename {}'
            .format(filename))
        PU = 'NA'
    # try to get some better ID
    try:
        # match GERALD_18-09-2011_p-illumina.8_s_8_1_sequence.txt
        m = re.match('([^_]+)_([^_]+)_([^_]+)_s_([^_]+)_([^_]+)_sequence.txt', filename)
        ID = '{}.{}'.format(m.group(1), m.group(4))
    except AttributeError as e:
        env.logger.warning('Input fasta filename {} does not match a known'
            ' pattern. ID is directly obtained from filename.'.format(filename))
    #
    rg = r'@RG\tID:{}\tPG:{}\tPL:{}\tPU:{}\tSM:{}'.format(ID, PG, PL, PU, SM)
    env.logger.info('Setting read group tag to {}'.format(rg))
    return rg


def countUnmappedReads(sam_files):
    #
    # count total reads and unmapped reads
    #
    # The previous implementation uses grep and wc -l, but
    # I cannot understand why these commands are so slow...
    #
    targets = ['{}.counts'.format(x) for x in sam_files]
    if not existAndNewerThan(ofiles=targets, ifiles=sam_files):
        for sam_file, target_file in zip(sam_files, targets):
            env.logger.info('Counting unmapped reads in {}'.format(sam_file))
            unmapped_count = 0
            total_count = 0
            with open(sam_file) as sam:
               for line in sam:
                   total_count += 1
                   if 'XT:A:N' in line:
                       unmapped_count += 1
            with open(target_file, 'w') as target:
                target.write('{}\n{}\n'.format(unmapped_count, total_count))
    #
    counts = []
    for count_file in targets:
        with open(count_file) as cnt:
            unmapped = int(cnt.readline())
            total = int(cnt.readline())
            counts.append((unmapped, total))
    return counts

def isBamPairedEnd(input_file):
    # we need pysam for this but pysam does not yet work for Python 3.3.
    if not hasPySam:
        env.logger.error('Cannot detect if input bam file has paired reads (missing pysam). Assuming paired.')
        return True
    bamfile = pysam.Samfile(input_file, 'rb')
    for read in bamfile.fetch():
        if read.is_paired:
            env.logger.info('{} is detected to have paired reads.'.format(input_file))
        else:
            env.logger.info('{} is detected to have single (non-paired) reads.'.format(input_file))
        return read.is_paired

#
# def hasReadGroup(bamfile):
#     '''Check if a bamfile has read group information, if not we will have to add 
#     @RG tag to the bam file. Note that technically speaking, read group contains
#     flowcell and lane information and should be lane-specific. That is to say a
#     bam file for the same sample might have several @RG with the same SM (sample
#     name) value. However, because GATK only uses SM to identify samples (treats 
#     different RG with the same SM as the same sample), it is OK to add a single 
#     RG tag to a SAM/BAM file is it contains reads for the same sample.'''
#     # the following code consulted a addReadGroups2BAMs.py file available online
#     sam_handle = pysam.Samfile(bamfile, 'r')
#     sam_line = sam_handle.next()
#     read_info = sam_line.qname
#     sam_handle.close()
#     instrument, run_id, flowcell_id, lane = read_info.split(":")[:4]
#     info = {#'sample_name': sample_name,
#             #'locality': locality,
#             #'inline_tag': inline_tag,
#             #'third_read_tag': third_read_tag,
#             'instrument':instrument,  
#             'run_id': run_id,
#             'flowcell_id': flowcell_id,
#             'lane': lane}
#     return info


###############################################################################
#
# Wrappers for commands used. Note that
# 
# 1. For each command, it should be initialized with basic information
#    for it to execute (e.g. reference genome) so that they do not have to
#    be supplied later.
#
# 2. The __init__ function should check the existence of the file, even
#    the version of the file, so the initialization of pipeline will check
#    the existence of the commands.
#
# 3. The member functions should wrap around functions of commands. These
#    functions should
#
#    a. accept a list of input files, and essential parameter
#    b. write to output file if specified from parameter (these are generally
#       the last step of the pipeline where the location of results are
#       specified by users.
#    c. these function should make use of existAndNewer and quit if the result
#       files exist and are newer.
#    d. these functions should try to use wait=True to allow parallelized
#       execution.
#    e. these functions should make use of env.WORKING_DIR as temporary output
#    f. extra options should be provided by users through env.options.
#
###############################################################################
class BWA:
    def __init__(self, ref_fasta, version=None):
        '''A BWA wrapper, if version is not None, a specific version is
        required.'''
        checkCmd('bwa')
        self.REF_fasta = ref_fasta
        
    def buildIndex(self):
        '''Create BWA index for reference genome file'''
        # bwa index  -a bwtsw wg.fa
        saved_dir = os.getcwd()
        os.chdir(env.RESOURCE_DIR)
        if os.path.isfile(self.REF_fasta + '.amb'):
            env.logger.warning('Using existing bwa indexed sequence {}.amb'
                .format(self.REF_fasta))
        else:
            run_command('bwa index {}  -a bwtsw {}'
                .format(env.OPT_BWA_INDEX, self.REF_fasta))
        os.chdir(saved_dir)

    def aln(self, fastq_files):
        '''Use bwa aln to process fastq files'''
        for input_file in fastq_files:
            dest_file = '{}/{}.sai'.format(env.WORKING_DIR, os.path.basename(input_file))
            if existAndNewerThan(ofiles=dest_file, ifiles=input_file):
                env.logger.warning('Using existing alignment index file {}'
                    .format(dest_file))
            else:
                # input file should be in fastq format (-t 4 means 4 threads)
                opt = ' -I ' if fastqVersion(input_file) == 'Illumina 1.3+' else ''
                if opt == ' -I ':
                    env.logger.warning('Using -I option for bwa aln command '
                        'because the sequences seem to be in Illumina 1.3+ format.')
                run_command('bwa aln {} {} -t 4 {}/{} {} > {}'
                    .format(opt, env.OPT_BWA_ALN, env.RESOURCE_DIR, 
                        self.REF_fasta, input_file, TEMP(dest_file)),
                    name=os.path.basename(dest_file),
                    upon_succ=(os.rename, TEMP(dest_file), dest_file),
                    wait=False)
        # wait for all bwa aln jobs to be completed
        wait_all()

    def sampe(self, fastq_files, sample_name=None):
        '''Use bwa sampe to generate aligned sam files for paird end reads'''
        sam_files = []
        for idx in range(len(fastq_files)//2):
            f1 = fastq_files[2*idx]
            f2 = fastq_files[2*idx + 1]
            rg = getReadGroup(f1, sample_name)
            sai1 = '{}/{}.sai'.format(env.WORKING_DIR, os.path.basename(f1))
            sai2 = '{}/{}.sai'.format(env.WORKING_DIR, os.path.basename(f2))
            sam_file = '{}/{}_bwa.sam'.format(env.WORKING_DIR, os.path.basename(f1))
            if existAndNewerThan(ofiles=sam_file, ifiles=[f1, f2, sai1, sai2]):
                env.logger.warning('Using existing sam file {}'.format(sam_file))
            else:
                run_command(
                    'bwa sampe {0} -r \'{1}\' {2}/{3} {4} {5} {6} {7} > {8}'
                    .format(
                        env.OPT_BWA_SAMPE, rg, env.RESOURCE_DIR, self.REF_fasta,
                        sai1, sai2, f1, f2, TEMP(sam_file)),
                    name=os.path.basename(sam_file),
                    upon_succ=(os.rename, TEMP(sam_file), sam_file),
                    wait=False)
            sam_files.append(sam_file)
        # wait for all jobs to be completed
        wait_all()
        return sam_files

    def samse(self, fastq_files, sample_name=None):
        '''Use bwa sampe to generate aligned sam files'''
        sam_files = []
        for f in fastq_files:
            sam_file = '{}/{}_bwa.sam'.format(env.WORKING_DIR, os.path.basename(f))
            rg = getReadGroup(f, sample_name)
            sai = '{}/{}.sai'.format(env.WORKING_DIR, os.path.basename(f))
            if existAndNewerThan(ofiles=sam_file, ifiles=[f, sai]):
                env.logger.warning('Using existing sam file {}'.format(sam_file))
            else:
                run_command(
                    'bwa samse {0} -r \'{1}\' {2}/{3} {4} {5} > {6}'
                    .format(
                        env.OPT_BWA_SAMSE, rg, env.RESOURCE_DIR,
                        self.REF_fasta, sai, f, TEMP(sam_file)),
                    name=os.path.basename(sam_file),
                    upon_succ=(os.rename, TEMP(sam_file), sam_file),
                    wait=False)
            sam_files.append(sam_file)
        # wait for all jobs to be completed
        wait_all()
        return sam_files        


class SAMTOOLS:
    def __init__(self, ref_fasta):
        checkCmd('samtools')
        self.REF_fasta = ref_fasta

    def buildIndex(self):
        '''Create index for reference genome used by samtools'''
        saved_dir = os.getcwd()
        os.chdir(env.RESOURCE_DIR)
        if os.path.isfile('{}.fai'.format(self.REF_fasta)):
            env.logger.warning('Using existing samtools sequence index {}.fai'
                .format(self.REF_fasta))
        else:
            run_command('samtools faidx {} {}'
                .format(env.OPT_SAMTOOLS_FAIDX, self.REF_fasta))
        os.chdir(saved_dir)

    def sortSam(self, sam_files):
        '''Convert sam file to sorted bam files.'''
        bam_files = []
        for sam_file in sam_files:
            bam_file = sam_file[:-4] + '.bam'
            if existAndNewerThan(ofiles=bam_file, ifiles=sam_file):
                env.logger.warning('Using existing bam file {}'.format(bam_file))
            else:
                run_command('samtools view {} -bt {}/{}.fai {} > {}'
                    .format(
                        env.OPT_SAMTOOLS_VIEW, env.RESOURCE_DIR,
                        self.REF_fasta, sam_file, TEMP(bam_file)),
                    name=os.path.basename(bam_file),
                    upon_succ=(os.rename, TEMP(bam_file), bam_file),
                    wait=False)
            bam_files.append(bam_file)
        # wait for all sam->bam jobs to be completed
        wait_all()
        #
        # sort bam files
        sorted_bam_files = []
        for bam_file in bam_files:
            sorted_bam_file = bam_file[:-4] + '_sorted.bam'
            if existAndNewerThan(ofiles=sorted_bam_file, ifiles=bam_file):
                env.logger.warning('Using existing sorted bam file {}'
                    .format(sorted_bam_file))
            else:
                run_command('samtools sort {} {} {}'
                    .format(
                        env.OPT_SAMTOOLS_SORT, bam_file,
                        TEMP(sorted_bam_file)),
                    name=os.path.basename(sorted_bam_file),
                    upon_succ=(os.rename, TEMP(sorted_bam_file), sorted_bam_file),
                    wait=False)
            sorted_bam_files.append(sorted_bam_file)
        wait_all()
        return sorted_bam_files

    def indexBAM(self, bam_file):
        '''Index the input bam file'''
        if existAndNewerThan(ofiles='{}.bai'.format(bam_file), ifiles=bam_file):
            env.logger.warning('Using existing bam index {}.bai'.format(bam_file))
        else:
            # samtools index input.bam [output.bam.bai]
            run_command('samtools index {} {} {}'.format(
                env.OPT_SAMTOOLS_INDEX, bam_file, TEMP(bam_file + '.bai')),
                upon_succ=(os.rename, TEMP(bam_file + '.bai'), bam_file + '.bai'))


class PICARD:
    '''Wrapper around command picard'''
    def __init__(self):
        if env.PICARD_PATH:
            if not os.path.isfile(os.path.join(os.path.expanduser(env.PICARD_PATH), 'SortSam.jar')):
                env.logger.error('Specified PICARD_PATH {} does not contain picard jar files.'
                    .format(env.PICARD_PATH))
                sys.exit(1)
        elif 'CLASSPATH' in os.environ:
            if not any([os.path.isfile(os.path.join(os.path.expanduser(x), 'SortSam.jar')) 
                    for x in os.environ['CLASSPATH'].split(':')]):
                env.logger.error('$CLASSPATH ({}) does not contain a path that contain picard jar files.'
                    .format(os.environ['CLASSPATH']))
                sys.exit(1)
            for x in os.environ['CLASSPATH'].split(':'):
                if os.path.isfile(os.path.join(os.path.expanduser(x), 'SortSam.jar')):
                    env.logger.info('Using picard under {}'.format(x))
                    env.PICARD_PATH = os.path.expanduser(x)
                    break
        else:
            env.logger.error('Please either specify path to picard using option '
                '--set PICARD_PATH=path, or set it in environment variable $CLASSPATH.')
            sys.exit(1)

    def sortSam(self, sam_files, output=None):
        '''Convert sam file to sorted bam files using Picard.'''
        # sort bam files
        sorted_bam_files = []
        for sam_file in sam_files:
            sorted_bam_file = sam_file[:-4] + '_sorted.bam'
            if existAndNewerThan(ofiles=sorted_bam_file, ifiles=sam_file):
                env.logger.warning('Using existing sorted bam file {}'
                    .format(sorted_bam_file))
            else:
                run_command(
                    'java {0} -jar {1}/SortSam.jar {2} I={3} O={4} SO=coordinate'
                    .format(
                        env.OPT_JAVA, env.PICARD_PATH, 
                        env.OPT_PICARD_SORTSAM, sam_file,
                        TEMP(sorted_bam_file)),
                    name=os.path.basename(sorted_bam_file),
                    upon_succ=(os.rename, TEMP(sorted_bam_file), sorted_bam_file),
                    wait=False)
            sorted_bam_files.append(sorted_bam_file)
        wait_all()
        return sorted_bam_files

    def mergeBAMs(self, bam_files):
        '''merge sam files'''
        # use Picard merge, not samtools merge: 
        # Picard keeps RG information from all Bam files, whereas samtools uses only 
        # inf from the first bam file
        merged_bam_file = bam_files[0][:-4] + '_merged.bam'
        if existAndNewerThan(ofiles=merged_bam_file, ifiles=bam_files):
            env.logger.warning('Using existing merged bam file {}'
                .format(merged_bam_file))
        else:
            run_command('''java {} -jar {}/MergeSamFiles.jar {} {}
                USE_THREADING=true
                VALIDATION_STRINGENCY=LENIENT
                ASSUME_SORTED=true
                OUTPUT={}'''.format(
                    env.OPT_JAVA, env.PICARD_PATH,
                    env.OPT_PICARD_MERGESAMFILES,
                    ' '.join(['INPUT={}'.format(x) for x in bam_files]),
                    TEMP(merged_bam_file)),
                name=os.path.basename(merged_bam_file),
                upon_succ=(os.rename, TEMP(merged_bam_file), merged_bam_file))
        return merged_bam_file

    def bam2fastq(self, input_file):
        '''This function extracts raw reads from an input BAM file to one or 
        more fastq files.'''
        #
        if isBamPairedEnd(input_file):
            output_files = [os.path.join(env.WORKING_DIR, '{}_{}.fastq'
                .format(os.path.basename(input_file)[:-4], x)) for x in [1,2]]
            if existAndNewerThan(ofiles=output_files, ifiles=input_file):
                env.logger.warning('Using existing sequence files {}'
                    .format(' and '.join(output_files)))
            else:
                run_command('''java {} -jar {}/SamToFastq.jar {} INPUT={}
                    FASTQ={} SECOND_END_FASTQ={}'''
                    .format(env.OPT_JAVA, env.PICARD_PATH,
                        env.OPT_PICARD_SAMTOFASTQ, input_file,
                        TEMP(output_files[0]), output_files[1]),
                    name=os.path.basename(output_files[0]),
                    upon_succ=(os.rename, TEMP(output_files[0]), output_files[0]))
            return output_files
        else:
            output_file = os.path.join(env.WORKING_DIR, '{}.fastq'
                .format(os.path.basename(input_file)[:-4]))
            if existAndNewerThan(ofiles=output_file, ifiles=input_file):
                env.logger.warning('Using existing sequence files {}'
                    .format(output_file))
            else:
                run_command('''java {} -jar {}/SamToFastq.jar {} INPUT={}
                    FASTQ={}'''
                    .format(env.OPT_JAVA, env.PICARD_PATH,
                        env.OPT_PICARD_SAMTOFASTQ, input_file,
                        TEMP(output_file)),
                    name=os.path.basename(output_file),
                    upon_succ=(os.rename, TEMP(output_file), output_file))
            return [output_file]

    def markDuplicates(self, bam_files):
        '''Mark duplicate using picard'''
        dedup_bam_files = []
        for bam_file in bam_files:
            dedup_bam_file = os.path.join(env.WORKING_DIR, os.path.basename(bam_file)[:-4] + '.dedup.bam')
            metrics_file = os.path.join(env.WORKING_DIR, os.path.basename(bam_file)[:-4] + '.metrics')
            if existAndNewerThan(ofiles=dedup_bam_file, ifiles=bam_file):
                env.logger.warning(
                    'Using existing bam files after marking duplicate {}'
                    .format(dedup_bam_file))
            else:
                run_command('''java {0} -jar {1}/MarkDuplicates.jar {2}
                    INPUT={3}
                    OUTPUT={4}
                    METRICS_FILE={5}
                    ASSUME_SORTED=true
                    VALIDATION_STRINGENCY=LENIENT
                    '''.format(
                        env.OPT_JAVA, env.PICARD_PATH,
                        env.OPT_PICARD_MARKDUPLICATES, bam_file,
                        TEMP(dedup_bam_file),
                        metrics_file), 
                    name=os.path.basename(dedup_bam_file),
                    upon_succ=(os.rename, TEMP(dedup_bam_file), dedup_bam_file),
                    wait=False)
            dedup_bam_files.append(dedup_bam_file)
        wait_all()
        return dedup_bam_files


class GATK:
    '''Wrapper around command gatk '''
    def __init__(self, ref_fasta, knownSites):
        '''Check if GATK is available, set GATK_PATH from CLASSPATH if the path
            is specified in CLASSPATH'''
        if env.GATK_PATH:
            if not os.path.isfile(os.path.join(os.path.expanduser(env.GATK_PATH),
                    'GenomeAnalysisTK.jar')):
                env.logger.error('Specified GATK_PATH {} does not contain GATK jar files.'
                    .format(env.GATK_PATH))
                sys.exit(1)
        elif 'CLASSPATH' in os.environ:
            if not any([os.path.isfile(os.path.join(os.path.expanduser(x), 'GenomeAnalysisTK.jar'))
                    for x in os.environ['CLASSPATH'].split(':')]):
                env.logger.error('$CLASSPATH ({}) does not contain a path that contain GATK jar files.'
                    .format(os.environ['CLASSPATH']))
                sys.exit(1)
            else:
                for x in os.environ['CLASSPATH'].split(':'):
                    if os.path.isfile(os.path.join(os.path.expanduser(x), 'GenomeAnalysisTK.jar')):
                        env.logger.info('Using GATK under {}'.format(x))
                        env.GATK_PATH = os.path.expanduser(x)
                        break
        else:
            env.logger.error('Please either specify path to GATK using option '
                '--set GATK_PATH=path, or set it in environment variable $CLASSPATH.')
            sys.exit(1) 
        #
        self.REF_fasta = ref_fasta
        self.knownSites = knownSites

    def downloadResourceBundle(self, URL, files):
        '''Utility function to download GATK resource bundle. If files specified by
        files already exist, use the existing downloaded files.'''
        #
        saved_dir = os.getcwd()
        os.chdir(env.RESOURCE_DIR)
        if all([os.path.isfile(x) for x in files]):
            env.logger.warning('Using existing GATK resource')
        else:
            run_command('wget -nc -r {}'.format(URL))
            # walk into the directory and get everything to the top directory
            # this is because wget -r saves files under URL/file
            for root, dirs, files in os.walk('.'):
                for name in files:
                    shutil.move(os.path.join(root, name), name)
        #
        # decompress all .gz files
        for gzipped_file in [x for x in os.listdir('.') if x.endswith('.gz') and 
            not x.endswith('tar.gz')]:
            if existAndNewerThan(ofiles=gzipped_file[:-3], ifiles=gzipped_file):
                env.logger.warning('Using existing decompressed file {}'
                    .format(gzipped_file[:-3]))
            else:
                decompress(gzipped_file, '.')
        os.chdir(saved_dir)


    def realignIndels(self, bam_file):
        '''Create realigner target and realign indels'''
        target = os.path.join(env.WORKING_DIR, os.path.basename(bam_file)[:-4] + '.IndelRealignerTarget.intervals')
        if existAndNewerThan(ofiles=target, ifiles=bam_file):
            env.logger.warning('Using existing realigner target {}'.format(target))
        else:
            run_command('''java {0} -jar {1}/GenomeAnalysisTK.jar {2} -I {3} 
                -R {4}/{5}
                -T RealignerTargetCreator
                --mismatchFraction 0.0
                -o {6} {7} '''.format(
                    env.OPT_JAVA, env.GATK_PATH,
                    env.OPT_GATK_REALIGNERTARGETCREATOR,
                    bam_file, env.RESOURCE_DIR,
                    self.REF_fasta, TEMP(target),
                    ' '.join(['-known {}/{}'.format(env.RESOURCE_DIR, x) for x in self.knownSites])),
                name=os.path.basename(target),
                upon_succ=(os.rename, TEMP(target), target))
        # 
        # realign around known indels
        cleaned_bam_file = os.path.join(env.WORKING_DIR, os.path.basename(bam_file)[:-4] + '.clean.bam')
        if existAndNewerThan(ofiles=cleaned_bam_file, ifiles=target):
            env.logger.warning('Using existing realigner bam file {}'.format(cleaned_bam_file))
        else:
            run_command('''java {0} -jar {1}/GenomeAnalysisTK.jar {2} -I {3} 
                -R {4}/{5}
                -T IndelRealigner 
                --targetIntervals {6}
                --consensusDeterminationModel USE_READS
                -compress 0 -o {7} {8}'''.format(
                    env.OPT_JAVA, env.GATK_PATH,
                    env.OPT_GATK_INDELREALIGNER,
                    bam_file, env.RESOURCE_DIR,
                    self.REF_fasta, target, TEMP(cleaned_bam_file),
                    ' '.join(['-known {}/{}'.format(env.RESOURCE_DIR, x) for x in self.knownSites])),
                name=os.path.basename(cleaned_bam_file),
                upon_succ=(os.rename, TEMP(cleaned_bam_file), cleaned_bam_file))
        # 
        return cleaned_bam_file


    def recalibrate(self, bam_file, recal_bam_file):
        '''Create realigner target and realign indels'''
        target = os.path.join(env.WORKING_DIR, os.path.basename(bam_file)[:-4] + '.grp')
        if existAndNewerThan(ofiles=target, ifiles=bam_file):
            env.logger.warning('Using existing base recalibrator target {}'.format(target))
        else:
            run_command('''java {0} -jar {1}/GenomeAnalysisTK.jar {2} 
                -T BaseRecalibrator
                -I {3} 
                -R {4}/{5}
                -cov ReadGroupCovariate
                -cov QualityScoreCovariate
                -cov CycleCovariate
                -cov ContextCovariate
                -o {6} {7}'''.format(
                    env.OPT_JAVA, env.GATK_PATH,
                    env.OPT_GATK_BASERECALIBRATOR, bam_file,
                    env.RESOURCE_DIR, self.REF_fasta, TEMP(target),
                    ' '.join(['-knownSites {}/{}'.format(env.RESOURCE_DIR, x) for x in self.knownSites])),
                name=os.path.basename(target),
                upon_succ=(os.rename, TEMP(target), target))
        #
        # recalibrate
        if existAndNewerThan(ofiles=recal_bam_file, ifiles=target):
            env.logger.warning('Using existing recalibrated bam file {}'.format(recal_bam_file))
        else:
            run_command('''java {0} -jar {1}/GenomeAnalysisTK.jar {2}
                -T PrintReads
                -I {3} 
                -R {4}/{5}
                -BQSR {6}
                -o {7}'''.format(
                    env.OPT_JAVA, env.GATK_PATH,
                    env.OPT_GATK_PRINTREADS, bam_file,
                    env.RESOURCE_DIR, self.REF_fasta, target, 
                    TEMP(recal_bam_file)),
                name=os.path.basename(recal_bam_file),
                upon_succ=(os.rename, TEMP(recal_bam_file), recal_bam_file))
        # 
        return recal_bam_file

    def reduceReads(self, input_file):
        target = input_file[:-4] + '_reduced.bam'
        if existAndNewerThan(ofiles=target, ifiles=input_file):
            env.logger.warning('Using existing reduced bam file {}'.format(target))
        else:
            run_command('''java {0} -jar {1}/GenomeAnalysisTK.jar {2}
                -T ReduceReads
                -I {3} 
                -R {4}/{5}
                -o {6}'''.format(
                    env.OPT_JAVA, env.GATK_PATH,
                    env.OPT_GATK_REDUCEREADS, input_file,
                    env.RESOURCE_DIR, self.REF_fasta,
                    TEMP(target)),
                name=os.path.basename(target),
                upon_succ=(os.rename, TEMP(target), target))
        # 
        return target

    def unifiedGenotyper(self, input_file):
        target = os.path.join(env.WORKING_DIR,
            os.path.basename(input_file)[:-4] + '_raw.vcf')
        if existAndNewerThan(ofiles=target, ifiles=input_file):
            env.logger.warning('Using existing called variants {}'.format(target))
        else:
            dbSNP_vcf = [x for x in self.knownSites if 'dbsnp' in x][0]
            run_command('''java {0} -jar {1}/GenomeAnalysisTK.jar
                -T UnifiedGenotyper
                {2}
                -I {3} 
                -R {4}/{5}
                --dbsnp {4}/{7}
                --genotype_likelihoods_model BOTH
                -o {6}'''.format(
                    env.OPT_JAVA, env.GATK_PATH,
                    env.OPT_GATK_UNIFIEDGENOTYPER, input_file,
                    env.RESOURCE_DIR, self.REF_fasta,
                    TEMP(target), dbSNP_vcf),
                name=os.path.basename(target),
                upon_succ=(os.rename, TEMP(target), target))
        # 
        return target

    def haplotypeCall(self, input_file):
        target = os.path.join(env.WORKING_DIR,
            os.path.basename(input_file)[:-4] + '.vcf')
        if existAndNewerThan(ofiles=target, ifiles=input_file):
            env.logger.warning('Using existing called variants {}'.format(target))
        else:
            run_command('''java {0} -jar {1}/GenomeAnalysisTK.jar {2} -I {3} 
                -R {4}/{5}
                -T HaplotypeCaller
                -o {6}'''.format(
                    env.OPT_JAVA, env.GATK_PATH,
                    env.OPT_GATK_HAPLOTYPECALLER, input_file,
                    env.RESOURCE_DIR, self.REF_fasta,
                    TEMP(target)),
                name=os.path.basename(target),
                upon_succ=(os.rename, TEMP(target), target))
        # 
        return target


    def variantRecalibration(self, input_file):
        #
        # SNPs error model
        #
        # target is log file
        recal_files = {} 
        target = os.path.join(env.WORKING_DIR,
            os.path.basename(input_file)[:-4] + '.SNPs')
        recal_files['SNP'] = target
        if existAndNewerThan(ofiles=target, ifiles=input_file):
            env.logger.warning('Using existing error model {}'.format(target))
        else:
            dbSNP_vcf = [x for x in self.knownSites if 'dbsnp' in x][0]
            hapmap_vcf = [x for x in self.knownSites if 'hapmap' in x][0]
            kg_vcf = [x for x in self.knownSites if '1000G_omni' in x][0]
            run_command('''java {0} -jar {1}/GenomeAnalysisTK.jar {2}
                -input {4}
                -T VariantRecalibrator
                -R {5}/{6}
                {3}
                -resource:hapmap,known=false,training=true,truth=true,prior=15.0 {5}/{7}
                -resource:omni,known=false,training=true,truth=true,prior=12.0	{5}/{8}
                -resource:dbsnp,known=true,training=false,truth=false,prior=2.0	{5}/{9}
                -mode SNP 
                -recalFile {10}.recal
                -tranchesFile {10}.tranches
                -rscriptFile {10}.R
                -log {11}'''.format(
                    env.OPT_JAVA, env.GATK_PATH,
                    env.OPT_GATK_VARIANTRECALIBRATOR,
                    env.OPT_GATK_VARIANTRECALIBRATOR_SNV,
                    input_file,
                    env.RESOURCE_DIR,
                    self.REF_fasta,
                    hapmap_vcf,
                    kg_vcf,
                    dbSNP_vcf,
                    target,
                    TEMP(target)),
                name=os.path.basename(target),
                upon_succ=(os.rename, TEMP(target), target))
        #
        # INDELs error model
        #
        # target is log file
        target = os.path.join(env.WORKING_DIR,
            os.path.basename(input_file)[:-4] + '.INDELs')
        recal_files['INDEL'] = target
        if existAndNewerThan(ofiles=target, ifiles=input_file):
            env.logger.warning('Using existing error model {}'.format(target))
        else:
            dbSNP_vcf = [x for x in self.knownSites if 'dbsnp' in x][0]
            indels_vcf = [x for x in self.knownSites if 'gold_standard.indels' in x][0]
            run_command('''java {0} -jar {1}/GenomeAnalysisTK.jar {2} 
                -input {4} 
                -T VariantRecalibrator
                -R {5}/{6}
                {3}
                -resource:mills,known=false,training=true,truth=true,prior=12.0 {5}/{7}
                -resource:dbsnp,known=true,training=false,truth=false,prior=2.0 {5}/{8}
                -mode INDEL 
                -recalFile {9}.recal
                -tranchesFile {9}.tranches
                -rscriptFile {9}.R
                -log {10}'''.format(
                    env.OPT_JAVA, env.GATK_PATH,
                    env.OPT_GATK_VARIANTRECALIBRATOR, 
                    env.OPT_GATK_VARIANTRECALIBRATOR_INDEL,
                    input_file,
                    env.RESOURCE_DIR, self.REF_fasta,
                    indels_vcf,
                    dbSNP_vcf,
                    target, TEMP(target)),
                name=os.path.basename(target),
                upon_succ=(os.rename, TEMP(target), target))
        #
        # SNP recalibration
        #
        target = os.path.join(env.WORKING_DIR,
                              os.path.basename(input_file)[:-4] + '.recal.SNPs.vcf')
        if existAndNewerThan(ofiles=target, ifiles=input_file):
            env.logger.warning('Using existing recalibrated variants {}'.format(target))
        else:
            run_command('''java {0} -jar {1}/GenomeAnalysisTK.jar {2} {3}
                --input {4} 
                -R {5}/{6}
                -T ApplyRecalibration
                -mode {7} 
                -recalFile {8}.recal
                -tranchesFile {8}.tranches
                -o {9}'''.format(
                    env.OPT_JAVA, env.GATK_PATH,
                    env.OPT_GATK_APPLYRECALIBRATION,
                    env.OPT_GATK_APPLYRECALIBRATION_SNV,
                    input_file,
                    env.RESOURCE_DIR, self.REF_fasta,
                    "SNP", recal_files["SNP"], TEMP(target)),
            name=os.path.basename(target),
            upon_succ=(os.rename, TEMP(target), target))
        #
        # INDEL recalibration
        # input file should be the output from the last step
        #
        input_file = target
        target = os.path.join(env.WORKING_DIR,
                              os.path.basename(input_file)[:-4] + '.INDELs.vcf')
        if existAndNewerThan(ofiles=target, ifiles=input_file):
            env.logger.warning('Using existing recalibrated variants {}'.format(target))
        else:
            run_command('''java {0} -jar {1}/GenomeAnalysisTK.jar {2} {3}
                --input {4} 
                -R {5}/{6}
                -T ApplyRecalibration
                -mode {7} 
                -recalFile {8}.recal
                -tranchesFile {8}.tranches
                -o {9}'''.format(
                    env.OPT_JAVA, env.GATK_PATH,
                    env.OPT_GATK_APPLYRECALIBRATION,
                    env.OPT_GATK_APPLYRECALIBRATION_INDEL,
                    input_file,
                    env.RESOURCE_DIR, self.REF_fasta,
                    "INDEL", recal_files["INDEL"], TEMP(target)),
            name=os.path.basename(target),
            upon_succ=(os.rename, TEMP(target), target))
        return target 


###############################################################################
#
#  Pipelines.
#
#  All pipelines should be subclasses of BasePipeline, and call related functions
#  in BasePipeline before specific actions are taken.
#
#  To add a pipeline, please 
#
#  1. derive a class from BasePipeline or one of its subclasses
#  2. define prepareResourceIfNotExist, align and call if they differ
#     from the function in the parent class.
#
###############################################################################
class BasePipeline:
    '''A vase variant caller that is supposed to provide most of the utility functions
    and common operations that are not specific to any pipeline.
    '''
    def __init__(self):
        pass

    #
    # PREPARE RESOURCE
    #
    # interface
    def prepareResourceIfNotExist(self):
        '''Prepare all resources for the pipeline. '''
        # download test data
        saved_dir = os.getcwd()
        os.chdir(env.RESOURCE_DIR)
        downloadFile('http://vtools.houstonbioinformatics.org/test_data/illumina_test_seq.tar.gz',
            dest='illumina_test_seq.tar.gz')
        if existAndNewerThan(ofiles=['illumina_test_seq_1.fastq', 'illumina_test_seq_2.fastq'],
                ifiles='illumina_test_seq.tar.gz'):
            env.logger.warning('Using existing test sequence files illumina_test_seq_1/2.fastq')
        else:
            decompress('illumina_test_seq.tar.gz', '.')
        os.chdir(saved_dir)

    #
    # align and create bam file
    #
    def align(self, input_files, output):
        '''Align to the reference genome'''
        if not output.endswith('.bam'):
            env.logger.error('Plase specify a .bam file in the --output parameter')
            sys.exit(1)

    def callVariants(self, input_files, ped_file, output):
        '''Call variants from a list of input files'''
        if not output.endswith('.vcf'):
           env.logger.error('Please specify a .vcf file in the --output parameter')
           sys.exit(1)
        for bam_file in input_files:
            if not os.path.isfile(bam_file):
                env.logger.error('Input file {} does not exist'.format(bam_file))
                sys.exit(1)
            if not os.path.isfile(bam_file + '.bai'):
                env.logger.error('Input bam file {} is not indexed.'.format(bam_file))
                sys.exit(1)

class b37_gatk_23(BasePipeline):
    '''A variant caller that uses gatk resource package 2.3 to call variants
    from build b37 of the human genome of the GATK resource bundle'''
    def __init__(self):
        BasePipeline.__init__(self)
        self.pipeline = 'b37_gatk_23'
        self.GATK_resource_url = 'ftp://gsapubftp-anonymous@ftp.broadinstitute.org/bundle/2.3/b37/*'
        self.REF_fasta = 'human_g1k_v37.fasta'
        self.knownSites = [
            'dbsnp_137.b37.vcf',
            'hapmap_3.3.b37.vcf',
            '1000G_omni2.5.b37.vcf',
            'Mills_and_1000G_gold_standard.indels.b37.vcf',
            '1000G_phase1.indels.b37.vcf',
        ]
        #
        # prepare the commands to be used
        self.bwa = BWA(self.REF_fasta)
        self.samtools = SAMTOOLS(self.REF_fasta)
        self.picard = PICARD()
        self.gatk = GATK(self.REF_fasta, self.knownSites)
        #
        env.RESOURCE_DIR = os.path.join(os.path.expanduser(env.RESOURCE_DIR), self.pipeline)
        if not os.path.isdir(env.RESOURCE_DIR):
            env.logger.info('Creating resource directory {}'.format(env.RESOURCE_DIR))
            os.makedirs(env.RESOURCE_DIR)

    def checkResource(self):
        '''Check if needed resource is available. This pipeline requires
        GATK resource bundle, commands wget, bwa, samtools, picard, and GATK. '''
        saved_dir = os.getcwd()
        os.chdir(env.RESOURCE_DIR)
        files = [self.REF_fasta, self.REF_fasta + '.amb', self.REF_fasta + '.fai']
        if not all([os.path.isfile(x) for x in files]):
            sys.exit('GATK resource bundle does not exist in directory {}. '
                'Please run "call_variants.py prepare_resource" befor you '
                'execute this command.'.format(env.RESOURCE_DIR))
        os.chdir(saved_dir)

    def prepareResourceIfNotExist(self):
        '''This function downloads the UCSC resource boundle for specified build and
        creates bwa and samtools indexed files from the whole genome sequence '''
        # download test data
        BasePipeline.prepareResourceIfNotExist(self)
        # these are pipeline specific
        GATK(self.REF_fasta, self.knownSites).downloadResourceBundle(
            self.GATK_resource_url, files=[self.REF_fasta + '.gz'])
        BWA(self.REF_fasta).buildIndex()
        SAMTOOLS(self.REF_fasta).buildIndex()

    def getFastqFiles(self, input_files):
        '''Decompress or extract input files to get a list of fastq files'''
        filenames = []
        for filename in input_files:
            if filename.lower().endswith('.bam') or filename.lower().endswith('.sam'):
                filenames.extend(self.picard.bam2fastq(filename))
                continue
            for fastq_file in decompress(filename, env.WORKING_DIR):
                try:
                    with open(fastq_file) as fastq:
                        line = fastq.readline()
                        if not line.startswith('@'):
                            raise ValueError('Wrong FASTA file {}'.foramt(fastq_file))
                    filenames.append(fastq_file)
                except Exception as e:
                    env.logger.error('Ignoring non-fastq file {}: {}'
                        .format(fastq_file, e))
        filenames.sort()
        return filenames

    #
    # This is the actual pipeline ....
    #
    def align(self, input_files, output):
        '''Align reads to hg19 reference genome and return a sorted, indexed bam file.'''
        BasePipeline.align(self, input_files, output)
        #
        # the working dir is a directory under output, the internediate files are saved to this
        # directory to avoid name conflict
        if not env._WORKING_DIR:
            env.WORKING_DIR = os.path.join(os.path.split(output)[0], os.path.basename(output) + '_align_cache')
        if not os.path.isdir(env.WORKING_DIR):
            os.makedirs(env.WORKING_DIR)
        #
        # step 1: decompress to get a list of fastq files
        fastq_files = self.getFastqFiles(input_files)
        #
        # step 2: call bwa aln to produce .sai files
        self.bwa.aln(fastq_files)
        #
        # step 3: generate .sam files for each pair of pairend reads, or reach file of unpaired reads
        paired = True
        if len(fastq_files) // 2 * 2 != len(fastq_files):
            env.logger.warning('Odd number of fastq files found, not handled as paired end reads.')
            paired = False
        for idx in range(len(fastq_files)//2):
            f1 = fastq_files[2*idx]
            f2 = fastq_files[2*idx + 1]
            if len(f1) != len(f2):
                env.logger.warning(
                    'Filenames {}, {} are not paired, not handled as paired end reads.'
                    .format(f1, f2))
                paired = False
                break
            diff = [ord(y)-ord(x) for x,y in zip(f1, f2) if x!=y]
            if diff != [1]:
                env.logger.warning(
                    'Filenames {}, {} are not paired, not handled as paired end reads.'
                    .format(f1, f2))
                paired = False
                break
        #
        # sam files?
        if paired:
            sam_files = self.bwa.sampe(fastq_files, output)
        else:
            sam_files = self.bwa.samse(fastq_files, output)
        #
        # check if mapping is successful
        counts = countUnmappedReads(sam_files)
        for f,c in zip(sam_files, counts):
            # more than 40% unmapped
            if c[1] == 0 or c[0]/c[1] > 0.4:
                env.logger.error('{}: {} out of {} reads are unmapped ({:.2f}% mapped)'
                    .format(f, c[0], c[1], 0 if c[1] == 0 else (100*(1 - c[0]/c[1]))))
                sys.exit(1)
            else:
                env.logger.info('{}: {} out of {} reads are unmapped ({:.2f}% mapped)'
                    .format(f, c[0], c[1], 100*(1 - c[0]/c[1])))
        # 
        # step 4: merge per-lane bam files to a single sample bam file
        if len(sam_files) > 1:
            merged_bam_file = self.picard.mergeBAMs(sam_files)
        else:
            merged_bam_file = sam_files[0]

        # step 4: convert sam to sorted bam files
        sorted_bam_file = self.picard.sortSam([merged_bam_file])[0]
        #
        # According to GATK best practice, dedup should be run at the
        # lane level (the documentation is confusing here though)
        #
        # step 5: remove duplicate
        dedup_bam_file = self.picard.markDuplicates([sorted_bam_file])[0]
        #
        #
        # step 7: index the output bam file
        self.samtools.indexBAM(dedup_bam_file)
        #
        # step 8: create indel realignment targets and recall
        cleaned_bam_file = self.gatk.realignIndels(dedup_bam_file)
        self.samtools.indexBAM(cleaned_bam_file)
        #
        # step 9: recalibration
        self.gatk.recalibrate(cleaned_bam_file, output)
        self.samtools.indexBAM(output)
        #
        # step 10: reduce reads
        reduced = self.gatk.reduceReads(output)
        self.samtools.indexBAM(reduced)

    def callVariants(self, input_files, pedfile, output):
        '''Call variants from a list of input files'''
        BasePipeline.callVariants(self, input_files, pedfile, output)
        #
        if not env._WORKING_DIR:
            env.WORKING_DIR = os.path.join(os.path.split(output)[0],
                os.path.basename(output) + '_call_cache')
        if not os.path.isdir(env.WORKING_DIR):
            os.makedirs(env.WORKING_DIR)
        #
        for input_file in input_files:
            #vcf_file = self.haplotypeCall(input_file)
            vcf_file = self.gatk.unifiedGenotyper(input_file)
            #   
            # variant calibrate
            recal_vcf_file = self.gatk.variantRecalibration(vcf_file)
        #
        # copy results to output
        shutil.copy(vcf_file, output)


class hg19_gatk_23(b37_gatk_23):
    '''A variant caller that uses gatk resource package 2.3 to call variants
    from build hg19 of the human genome'''
    def __init__(self):
        # do not call 
        BasePipeline.__init__(self)
        self.pipeline = 'hg19_gatk_23'
        # this piple just uses different resource bundle
        self.GATK_resource_url = 'ftp://gsapubftp-anonymous@ftp.broadinstitute.org/bundle/2.3/hg19/*'
        self.REF_fasta = 'ucsc.hg19.fasta'
        self.knownSites = [
            'dbsnp_137.hg19.vcf',
            'hapmap_3.3.hg19.vcf',
            '1000G_omni2.5.hg19.vcf',
            'Mills_and_1000G_gold_standard.indels.hg19.vcf',
            '1000G_phase1.indels.hg19.vcf',
        ]
        # prepare the commands to be used
        self.bwa = BWA(self.REF_fasta)
        self.picard = PICARD()
        self.samtools = SAMTOOLS(self.REF_fasta)
        self.gatk = GATK(self.REF_fasta, self.knownSites)
        #
        env.RESOURCE_DIR = os.path.join(os.path.expanduser(env.RESOURCE_DIR), self.pipeline)
        if not os.path.isdir(env.RESOURCE_DIR):
            env.logger.info('Creating resource directory {}'.format(env.RESOURCE_DIR))
            os.makedirs(env.RESOURCE_DIR)
        

def addCommonPipelineArguments(parser):
    parser.add_argument('--pipeline', nargs='?', default='b37_gatk_23',
        choices=['hg19_gatk_23', 'b37_gatk_23'],
        help='Name of the pipeline to be used to call variants.')
    parser.add_argument('--set', nargs='*', metavar='NAME+=VALUE',
        help='''Set runtime variables in the format of NAME=VALUE (set
            VALUE to NAME) or NAME+=VALUE (add VALUE to default value of
            NAME). NAME can be RESOURCE_DIR (directory for resources such as
            reference genome. Default to ~/.variant_tools/var_caller),
            WORKING_DIR (directory to hold all intermedate files, default to
            $output_$action_cache) PICARD_PATH (path to picard, should have
            a number of .jar files under it), GATK_PATH (path to gatk,
            should have GenomeAnalysisTK.jar under it) for path to JAR
            files (can be ignored if the paths are specified in environment
            variable $CLASSPATH), additional options to command java
            (OPT_JAVA, parameter to the java command, default to value
            "-Xmx4g"), and options to individual subcommands such as
            OPT_BWA_INDEX (additional option to bwa index) and OPT_SAMTOOLS_FAIDX.
            The following options are acceptable: {}. Note that the default
            values can be overridden by environment variables with name
            prefixed by CALL_VARIANTS_ (e.g. CALL_VARIANTS_RESOURCE_DIR).'''.format(
            ', '.join([x[0] + (' ({})'.format(x[1]) if x[1] else '') for x in options])))
    parser.add_argument('-j', '--jobs', default=1, type=int,
            help='''Maximum number of concurrent jobs.''')
 

def alignReadsArguments(parser):
    parser.add_argument('input_files', nargs='*',
        help='''One or more .txt, .fa, .fastq, .tar, .tar.gz, .tar.bz2, .tbz2, .tgz files
            that contain raw reads of a single sample. Files in sam/bam format are also
            acceptable, in which case raw reads will be extracted and aligned again to 
            generate a new bam file. ''')
    parser.add_argument('-o', '--output',
        help='''Output aligned reads to a sorted, indexed, dedupped, and recalibrated
            BAM file $output.bam.''')
    addCommonPipelineArguments(parser)

def alignReads(args):
    pass

def callVariantsArguments(parser):
    parser.add_argument('input_files', nargs='*',
        help='''One or more BAM files.''')
    parser.add_argument('-o', '--output',
        help='''Output parsered variants to the specified VCF file''')
    parser.add_argument('--pedfile',
        help='''A pedigree file that specifies the relationship between input
            samples, used for multi-sample parsering.''')
    addCommonPipelineArguments(parser)

def callVariants(args):
    pass

#     args = master_parser.parse_args()
#     if args.action is None:
#         master_parser.print_help()
#         sys.exit(0)
#     #
#     # temporary screen only logging
#     env.logger = None
#     #
#     # override using command line values --set
#     if hasattr(args, 'set') and args.set is not None:
#         for arg in args.set:
#             if '=' not in arg:
#                 sys.exit('Additional parameter should have form NAME=value')
#             name, value = arg.split('=', 1)
#             append = False
#             if name.endswith('+'):  # NAME+=VALUE
#                 name = name[:-1]
#                 append = True
#             if name not in [x[0] for x in options]:
#                 env.logger.error('Unrecognized environment variable {}: {} are allowed.'.format(
#                     name, ', '.join([x[0] for x in options])))
#                 sys.exit(1)
#             #
#             if append:
#                 env.__setattr__(name, env.__getattr__(name) + ' ' + value)
#             else:
#                 env.__setattr__(name, value)
#             env.logger.info('Environment variable {} is set to {}'.format(name, value))
#     #
#     # special handling of 'WORKING_DIR'
#     # if WORKING_DIR is not specified, WORKING_DIR will be output dependent.
#     env._WORKING_DIR = env.WORKING_DIR
#     #
#     if hasattr(args, 'output'):
#         if type(args.output) == list:
#             if not env._WORKING_DIR:
#                 env.WORKING_DIR = os.path.split(args.output[0])[0]
#             logname = os.path.basename(args.output[0]) + '.log'
#         elif args.output:
#             if not env._WORKING_DIR:
#                 env.WORKING_DIR = os.path.split(args.output)[0]
#             logname = os.path.basename(args.output) + '.log'
#         else:
#             if not env._WORKING_DIR:
#                 env.WORKING_DIR = '.'
#             logname = 'illumina_test.log'
#         if not os.path.isdir(env.WORKING_DIR):
#             os.makedirs(env.WORKING_DIR)
#         # screen + log file logging
#         env.logger = os.path.join(env.WORKING_DIR, logname)
#     #
#     # get a pipeline: args.pipeline is the name of the pipeline, also the name of the
#     # class (subclass of VariantCaller) that implements the pipeline
#     pipeline = eval(args.pipeline)()
#     if args.action == 'prepare_resource':
#         pipeline.prepareResourceIfNotExist()
#     elif args.action == 'align':
#         env.jobs = args.jobs
#         pipeline.checkResource()
#         if args.test_run:
#             ifiles = [os.path.join(env.RESOURCE_DIR, 'illumina_test_seq.tar.gz')]
#             ofiles = os.path.join(env.RESOURCE_DIR, 'illumina_test_seq.bam')
#             if not env.WORKING_DIR:
#                 env.logger.info('Using local cache directory illumina_test_seq_align_cache')
#                 env._WORKING_DIR = 'illumina_test_seq_align_cache'
#                 env.WORKING_DIR = env._WORKING_DIR
#             pipeline.align(ifiles, ofiles)
#             env.logger.info('Please remove working directory {} if you would like '
#                 'to rerun the test.'.format(env.WORKING_DIR))
#         else:
#             pipeline.align(args.input_files, args.output)
#     elif args.action == 'call':
#         env.jobs = args.jobs
#         pipeline.checkResource()
#         if args.test_run:
#             ifiles = [os.path.join(env.RESOURCE_DIR, 'illumina_test_seq.bam')]
#             ofiles = os.path.join(env.RESOURCE_DIR, 'illumina_test_seq.vcf')
#             if not env.WORKING_DIR:
#                 env.logger.info('Using local cache directory illumina_test_seq_call_cache')
#                 env._WORKING_DIR = 'illumina_test_seq_call_cache'
#                 env.WORKING_DIR = env._WORKING_DIR
#             pipeline.callVariants(ifiles, None, ofiles)
#             env.logger.info('Please remove working directory {} if you would like '
#                 'to rerun the test.'.format(env.WORKING_DIR))
#         else:
#             pipeline.callVariants(args.input_files, args.pedfile, args.output)
# 
