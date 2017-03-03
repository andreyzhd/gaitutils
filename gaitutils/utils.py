# -*- coding: utf-8 -*-
"""

Utility functions for processing gait data.

@author: Jussi (jnu@iki.fi)
"""

from read_data import get_marker_data, get_forceplate_data, get_metadata
from numutils import rising_zerocross, falling_zerocross, _baseline
from scipy import signal
from scipy.signal import medfilt
import numpy as np
import logging
import matplotlib.pyplot as plt

logger = logging.getLogger(__name__)


def get_crossing_frame(source, marker, dim=1, p0=0):
    """ Return frame(s) where marker position (dimension dim) crosses r0
    (units are as returned by Nexus, usually mm).
    Dims are x=0, y=1, z=2. """
    mrkdata = get_marker_data(source, marker)
    P = mrkdata[marker + '_P']
    y = P[:, dim]
    nzind = np.where(y != 0)  # nonzero elements == valid data (not nice)
    y[nzind] -= p0
    zx = np.append(rising_zerocross(y), falling_zerocross(y))
    ycross = list()
    # sanity checks
    for p in zx:
        # y must be nonzero on either side of crossing (valid data)
        if p-10 > 0 and p+10 < len(y):
            if y[p-10] != 0 and y[p+10] != 0:
                # y must change sign also around p
                if np.sign(y[p-10]) != np.sign(y[p+10]):
                        ycross.append(p)
    return ycross


def get_movement_direction(source, marker, dir):
    """ Return average direction of movement for given marker """
    dir = dir.lower()
    dir = {'x': 0, 'y': 1, 'z': 2}[dir]
    mrkdata = get_marker_data(source, marker)
    P = mrkdata[marker+'_P']
    ddiff = np.median(np.diff(P[:, dir]))  # median of derivative
    return 1 if ddiff > 0 else -1


def butter_filt(data, passband, sfreq, bord=5):
    """ Design a filter and forward-backward filter given data to
    passband, e.g. [1, 40].
    Passband is given in Hz. None for no filtering.
    Implemented as pure lowpass/highpass, if highpass/lowpass freq == 0
    """
    if passband is None:
        return data
    elif len(passband) != 2:
        raise Exception('Passband must be a vector of length 2')
    passbandn = 2 * np.array(passband) / sfreq
    if passbandn[0] == 0:  # lowpass
        b, a = signal.butter(bord, passbandn[1], btype='lowpass')
    elif passbandn[1] == 0:  # highpass
        b, a = signal.butter(bord, passbandn[1], btype='highpass')
    else:  # bandpass
        b, a = signal.butter(bord, passbandn, btype='bandpass')
    return signal.filtfilt(b, a, data)


def check_forceplate_contact(source, check_weight=True, check_cop=True):
    """ See whether the trial has valid forceplate contact.
    Uses forceplate data and marker positions.

    Conditions:
    -check max total force, must correspond to subject weight
    (disable by check_weight=False)
    -center of pressure must not change too much during contact time
    (disable by check_cop=False)
    -foot markers must be inside plate edges at strike time

    Returns dict as:
    return {'strikes': strike_fr, 'toeoffs': toeoff_fr}
    
    """

    # autodetection parameters
    # TODO: move into config
    F_REL_THRESHOLD = .2  # force rise / fall threshold
    FMAX_REL_MIN = .8  # maximum force as % of bodyweight must exceed this
    MAX_COP_SHIFT = 300  # maximum CoP shift (in x or y dir) in mm
    # time specified in seconds -> analog frames
    # FRISE_WINDOW = .05 * fp0['sfrate']
    # FMAX_MAX_DELAY = .95 * fp0['sfrate']
    # right feet markers
    RIGHT_FOOT_MARKERS = ['RHEE', 'RTOE', 'RANK']
    # left foot markers
    LEFT_FOOT_MARKERS = ['LHEE', 'LTOE', 'LANK']
    # tolerance for toeoff in forward dir (mm)
    TOEOFF_TOL = 20
    # ankle marker tolerance in dir orthogonal to gait (mm)
    ANKLE_TOL = 20

    # get subject info
    info = get_metadata(source)
    fpdata = get_forceplate_data(source)

    results = dict()
    results['strikes'] = {}
    results['toeoffs'] = {}

    # get marker data and find "forward" direction
    mrkdata = get_marker_data(source, RIGHT_FOOT_MARKERS+LEFT_FOOT_MARKERS)
    pos = sum([mrkdata[name+'_P'] for name in
               LEFT_FOOT_MARKERS+RIGHT_FOOT_MARKERS])
    fwd_dir = np.argmax(np.var(pos, axis=0))
    orth_dir = 0 if fwd_dir == 1 else 1
    logger.debug('gait forward direction seems to be %s' %
                 {0: 'x', 1: 'y', 2: 'z'}[fwd_dir])

    for plate_ind, fp in enumerate(fpdata):
        logger.debug('analyzing plate %d' % plate_ind)
        # test the force data
        # FIXME: filter should maybe depend on sampling freq
        forcetot = signal.medfilt(fp['Ftot'])
        forcetot = _baseline(forcetot)
        fmax = max(forcetot)
        fmaxind = np.where(forcetot == fmax)[0][0]  # first maximum
        logger.debug('max force: %.2f N at %.2f' % (fmax, fmaxind))
        bodymass = info['bodymass']
        if bodymass is None:
            f_threshold = F_REL_THRESHOLD * fmax
            logger.warning('body mass unknown, thresholding force at %.2f N',
                           f_threshold)
        else:
            logger.debug('body mass %.2f kg' % bodymass)
            f_threshold = F_REL_THRESHOLD * bodymass
            if check_weight:
                if fmax < FMAX_REL_MIN * bodymass * 9.81:
                    logger.debug('insufficient max. force on plate')
                    continue
            else:
                logger.debug('ignoring subject weight')
        # find indices where force crosses threshold
        try:
            logger.debug('force threshold: %.2f N' % f_threshold)
            friseind = rising_zerocross(forcetot-f_threshold)[0]  # first rise
            ffallind = falling_zerocross(forcetot-f_threshold)[-1]  # last fall
            logger.debug('force rise: %d fall: %d' % (friseind, ffallind))
        except IndexError:
            logger.debug('cannot detect force rise/fall')
            continue
        # check shift of center of pressure during roi
        # cop is here in plate coordinates, but it does not matter as we're
        # only looking for the magnitude of the shift
        if check_cop:
            cop_roi = fp['CoP'][friseind:ffallind, :]
            cop_shift = cop_roi.max(axis=0) - cop_roi.min(axis=0)
            total_shift = np.sqrt(np.sum(cop_shift**2))
            logger.debug('CoP total shift %.2f mm' % total_shift)
            if total_shift > MAX_COP_SHIFT:
                logger.debug('center of pressure shifts too much '
                             '(double contact?)')
                continue
        else:
            logger.debug('ignoring center of pressure')

        # frame indices are 1-based so need to add 1 (what about c3d?)
        strike_fr = int(np.round(friseind / info['samplesperframe'])) + 1
        toeoff_fr = int(np.round(ffallind / info['samplesperframe'])) + 1
        logger.debug('strike @ frame %d, toeoff @ %d' % (strike_fr, toeoff_fr))

        # if we got here, force data looked ok; next, check marker data
        # first compute plate boundaries in world coords
        mins = fp['lowerbounds']
        maxes = fp['upperbounds']

        # check markers
        this_valid = None
        for markers in [RIGHT_FOOT_MARKERS, LEFT_FOOT_MARKERS]:
            ok = True
            for marker_ in markers:
                mins_s, maxes_s = mins.copy(), maxes.copy()
                mins_t, maxes_t = mins.copy(), maxes.copy()
                # extra tolerance for ankle marker in sideways direction
                if 'ANK' in marker_:
                    mins_t[orth_dir] -= ANKLE_TOL
                    maxes_t[orth_dir] += ANKLE_TOL
                    mins_s[orth_dir] -= ANKLE_TOL
                    maxes_s[orth_dir] += ANKLE_TOL
                # extra tolerance for all markers in gait direction @ toeoff
                maxes_t[fwd_dir] += TOEOFF_TOL
                mins_t[fwd_dir] -= TOEOFF_TOL
                marker = marker_ + '_P'
                ok &= mins_s[0] < mrkdata[marker][strike_fr, 0] < maxes_s[0]
                ok &= mins_s[1] < mrkdata[marker][strike_fr, 1] < maxes_s[1]
                if not ok:
                    logger.debug('marker %s failed on-plate check during foot '
                                 'strike' % marker_)
                    break
                ok &= mins_t[0] < mrkdata[marker][toeoff_fr, 0] < maxes_t[0]
                ok &= mins_t[1] < mrkdata[marker][toeoff_fr, 1] < maxes_t[1]
                if not ok:
                    logger.debug('marker %s failed on-plate check during '
                                 'toeoff ' % marker_)
                    break
            if ok:
                if this_valid:
                    raise Exception('both feet on plate, how come?')
                this_valid = 'R' if markers == RIGHT_FOOT_MARKERS else 'L'
                logger.debug('on-plate check ok for side %s' % this_valid)

        if not this_valid:
            logger.debug('plate %d: no valid foot strike' % plate_ind)
        else:
            logger.debug('plate %d: valid foot strike on %s at frame %d'
                         % (plate_ind, this_valid, strike_fr))

            if this_valid not in results['strikes']:
                results['strikes'][this_valid] = []
                results['toeoffs'][this_valid] = []

            results['strikes'][this_valid].append(strike_fr)
            results['toeoffs'][this_valid].append(toeoff_fr)

    logger.debug(results)
    return results



"""
    # kinetics ok, compute velocities at strike
    markers = RIGHT_FOOT_MARKERS if kinetics == 'R' else LEFT_FOOT_MARKERS



def _strike_toeoff_velocity(markerdata_r, markerdata_l):
     Return foot velocity at strike/toeoff 
    
    markers = mdata.keys()
    footctrV = np.zeros(mdata[markers[0]+'_V'].shape)
    for marker in mrkdata:
        footctrV += mrkdata[marker+'_V'] / len(markers)



    footctrv = np.sqrt(np.sum(footctrV[:, 1:3]**2, 1))
    
    
    strike_v = footctrv[int(strike_fr)]
    toeoff_v = footctrv[int(toeoff_fr)]

"""





