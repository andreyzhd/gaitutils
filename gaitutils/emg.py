# -*- coding: utf-8 -*-
"""
Created on Tue Mar 17 14:41:31 2015

Class for reading EMG

@author: Jussi (jnu@iki.fi)
"""


from __future__ import division
from past.builtins import basestring
from builtins import object
import numpy as np
import logging

from . import read_data, cfg, numutils

logger = logging.getLogger(__name__)


class EMG(object):
    """Class for processing and storing EMG data.

    Parameters
    ----------
    source : ViconNexus | str
        The data source. Can be a c3d filename or a ViconNexus instance.
    correction_factor : int, optional
        After read, the EMG data is multiplied by this factor.
     """

    def __init__(self, source, correction_factor=1):
        logger.debug('new EMG instance from %s' % source)
        self.source = source
        self.passband = cfg.emg.passband
        self._data = None
        self.t = None
        self.sfrate = None
        self.correction_factor = correction_factor

    @property
    def data(self):
        """Get the EMG data.

        The data is cached in an instance variable, so it's read only once
        during the lifetime of the instance.

        Returns
        -------
        dict
            The EMG data, keyed by channel name. Values are shape (N,) ndarrays
            of sample values.
        """
        if self._data is None:
            self._read_data()
        return self._data

    def _read_data(self):
        """Actually read the EMG data from source"""
        meta = read_data.get_metadata(self.source)
        logger.debug('reading EMG from %s' % meta['trialname'])
        self.sfrate = meta['analograte']
        emgdi = read_data.get_emg_data(self.source)
        self._data = emgdi['data']
        self.t = emgdi['t']

    def _match_name(self, chname):
        """Fuzzily match channel name"""
        if not (isinstance(chname, basestring) and len(chname)) >= 2:
            raise ValueError('invalid channel name: %s' % chname)
        matches = [x for x in self.data if x.find(chname) >= 0]
        if len(matches) == 0:
            raise KeyError('No matching channel for %s' % chname)
        else:
            ch = min(matches, key=len)  # choose shortest matching name
        if len(matches) > 1:
            logger.warning(
                'multiple channel matches for %s: %s -> %s' % (chname, matches, ch)
            )
        return ch

    def get_channel_data(self, chname, rms=False):
        """Return EMG data for a given channel.

        Uses name matching. The given chname is matched (case sensitive) against
        the channel names in the data source and the shortest match is returned.
        For example, with chname=='LGas', 'Voltage.LGas8' and
        'Voltage.LGas8_filtered' would be matches, and the former would be
        returned.

        Data is returned filtered if self.passband is set.

        Parameters
        ----------
        ch : string
            The EMG channel name. Name matching is used (see above).
        rms : bool
            Return moving-window RMS instead of raw data.
        """
        ch = self._match_name(chname)
        data = self.data[ch]
        if rms:
            data = numutils.rms(data, cfg.emg.rms_win)
        elif self.passband:  # no filtering for RMS data
            # filtered data is currently not cached; _filtfilt() call typically
            # takes less than 1 ms, so this should not be a problem, unless a
            # huge number of calls are made
            data = numutils._filtfilt(data, self.passband, self.sfrate)
        data *= self.correction_factor
        return data

    def has_channel(self, chname):
        """Check whether a channel exists in the data.

        Parameters
        ----------
        chname : str
            The desired channel name. Name matching is used (see docstring for
            get_channel_data)

        Returns
        -------
        bool
            True if the channel exists, False otherwise.
        """
        try:
            self._match_name(chname)
        except KeyError:
            return False
        return True

    def status_ok(self, chname):
        """Check whether a channel exists and has valid EMG signal.

        Parameters
        ----------
        chname : str
            The desired channel name. Name matching is used (see docstring for
            get_channel_data)

        Returns
        -------
        bool
            True if the channel exists and has valid data, False otherwise.
        """
        if not self.has_channel(chname):
            return False
        elif cfg.emg.chs_disabled and chname in cfg.emg.chs_disabled:
            return False
        data = self.get_channel_data(chname)
        return self._is_valid_emg(data) if cfg.emg.autodetect_bads else True

    @staticmethod
    def context_ok(chname, context):
        """Check if the channel context matches given context.

        Parameters
        ----------
        chname : str
            A configured channel name. (Name matching is not applied here.)
        context : str
            Context ('L' or 'R').

        Returns
        -------
        bool
            False if context does not match. True if the context matches or is unknown.
        """
        if (
            chname in cfg.emg.channel_context
            and context.upper() != cfg.emg.channel_context[chname].upper()
        ):
            return False
        return True

    def _is_valid_emg(self, data):
        """Check whether channel contains a valid EMG signal."""
        return cfg.emg.variance_ok[0] < np.var(data) < cfg.emg.variance_ok[1]


class AvgEMG(EMG):
    """Class for storing averaged RMS EMG. This tries to match EMG class API but differs
    in following ways:
    -precomputed RMS data is stored in self._data
    -only the RMS data can be returned
    -no filtering is done
    """

    def __init__(self, data):
        self._data = data

    def get_channel_data(self, chname, rms=None):
        if not rms:
            raise RuntimeError('AvgEMG can only return averaged RMS data')
        chname = self._match_name(chname)
        return self._data[chname]

    def status_ok(self, chname):
        return self.has_channel(chname)

    def _is_valid_emg(self, data):
        raise RuntimeError('signal check not implemented for averaged EMG')

    def filt(self, y, passband):
        raise RuntimeError('filtering not implemented for averaged EMG')

    def read(self):
        raise RuntimeError('read not implemented for averaged EMG')
