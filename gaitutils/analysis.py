# -*- coding: utf-8 -*-
"""
Created on Tue Mar 17 14:41:31 2015

Computations on gait trials

@author: Jussi (jnu@iki.fi)
"""


from __future__ import division
import numpy as np
import logging

from .trial import Trial
from . import c3d

logger = logging.getLogger(__name__)


def get_analysis(c3dfile, condition='unknown'):
    """ A wrapper that reads the c3d analysis values and inserts step width
    which is not computed by Nexus """
    di = c3d.get_analysis(c3dfile, condition=condition)
    # compute and insert step width - not done by Nexus
    sw = _step_width(c3dfile)
    di[condition]['Step Width'] = dict()
    # uses avg of all cycles from trial
    di[condition]['Step Width']['Right'] = np.array(sw['R']).mean()
    di[condition]['Step Width']['Left'] = np.array(sw['L']).mean()
    di[condition]['Step Width']['unit'] = 'mm'
    return di


def _step_width(source):
    """ Compute step width over trial cycles. See:
    https://www.vicon.com/faqs/software/how-does-nexus-plug-in-gait-and-polygon-calculate-gait-cycle-parameters-spatial-and-temporal
    Returns context keyed dict of lists.
    FIXME: marker name into params?
    FIXME: this (and similar) may also need to take Trial instance as argument
    to avoid creating new Trials
    """
    tr = Trial(source)
    sw = dict()
    mkr = 'TOE'  # marker name without context
    mdata = tr.marker_data
    for context, strikes in zip(['L', 'R'], [tr.lstrikes, tr.rstrikes]):
        sw[context] = list()
        nstrikes = len(strikes)
        if nstrikes < 2:
            continue
        # contralateral vars
        context_co = 'L' if context == 'R' else 'R'
        strikes_co = tr.lstrikes if context == 'R' else tr.rstrikes
        mname = context + mkr
        mname_co = context_co + mkr
        for j, strike in enumerate(strikes):
            if strike == strikes[-1]:  # last strike on this side
                break
            pos_this = mdata[mname+'_P'][strike]
            pos_next = mdata[mname+'_P'][strikes[j+1]]
            strikes_next_co = [k for k in strikes_co if k > strike]
            if len(strikes_next_co) == 0:  # no subsequent contralateral strike
                break
            pos_next_co = mdata[mname_co+'_P'][strikes_next_co[0]]
            # vector distance between 'step lines' (see url above)
            V1 = pos_next - pos_this
            V1 = V1 / np.linalg.norm(V1)
            VC = pos_next_co - pos_this
            VCP = V1 * np.dot(VC, V1)  # proj to ipsilateral line
            VSW = VCP - VC
            sw[context].append(np.linalg.norm(VSW))
    return sw