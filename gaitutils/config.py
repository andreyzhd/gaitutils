# -*- coding: utf-8 -*-
"""
Handles gaitutils config files.

@author: Jussi (jnu@iki.fi)
"""
from __future__ import print_function

from builtins import str
import os.path as op
import os
import sys
import logging
from pkg_resources import resource_filename

from . import envutils
from .parse_config import parse_config, update_config, dump_config

logger = logging.getLogger(__name__)


""" Work around stdout and stderr not being available, if we are run
using pythonw.exe on Windows. Without this, exception will be raised
e.g. on any print statement. """
if (sys.platform.find('win') != -1 and sys.executable.find('pythonw') != -1 and
   not envutils.run_from_ipython()):
    blackhole = open(os.devnull, 'w')
    sys.stdout = sys.stderr = blackhole

# default config
cfg_template = resource_filename(__name__, 'data/default.cfg')
# user specific config
# On Windows, this typically puts the config at C:\Users\Username, since the
# USERPROFILE environment variable points there. Putting the config in a
# networked home dir requires some tinkering with environment variables
# (e.g. setting HOME)
homedir = op.expanduser('~')
cfg_user = op.join(homedir, '.gaitutils.cfg')


# provide the global cfg instance
# read template config
cfg = parse_config(cfg_template)
if op.isfile(cfg_user):
    update_config(cfg, cfg_user)
else:
    print('no config file, trying to create %s' % cfg_user)
    cfg_txt = dump_config(cfg)
    with open(cfg_user, 'w', encoding='utf8') as f:
        f.writelines(cfg_txt)

# handle some deprecated/changed types for user convenience
if not isinstance(cfg.plot.emg_yscale, float):
    ysc = cfg.plot.emg_yscale[1]
    print('WARNING: emg_yscale was changed to float, using %g' % ysc)
    cfg.plot.emg_yscale = str(cfg.plot.emg_yscale[1])
if cfg.general.normaldata_files == 'default':
    fn = resource_filename('gaitutils', 'data/normal.gcd')
    cfg.general['normaldata_files'].update_value(fn)
if cfg.general.videoconv_path == 'default':
    fn = resource_filename('gaitutils', 'thirdparty/ffmpeg2theora.exe')
    cfg.general['videoconv_path'].update_value(fn)



sys.stdout.flush()  # make sure that warnings are printed out
