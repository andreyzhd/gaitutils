# -*- coding: utf-8 -*-
"""
Vicon Nexus utils & data readers

@author: Jussi (jnu@iki.fi)
"""

from __future__ import print_function, division
from collections import defaultdict
import sys
import numpy as np
import os.path as op
import psutil
import glob
import logging
import time
import multiprocessing

from .numutils import _change_coords, _isfloat
from .utils import TrialEvents
from .envutils import GaitDataError
from .config import cfg


logger = logging.getLogger(__name__)


def _find_nexus_path(vicon_path=None):
    """Return path to most recent Nexus version.

    vicon_path is the Vicon root directory.
    """
    if vicon_path is None:
        vicon_path = r'C:\Program Files (x86)\Vicon'  # educated guess
    if not op.isdir(vicon_path):
        return None
    nexus_glob = op.join(vicon_path, 'Nexus?.*')
    nexus_dirs = glob.glob(nexus_glob)
    if not nexus_dirs:
        return None
    nexus_vers = [op.split(dir_)[1][5:] for dir_ in nexus_dirs]
    # convert into major,minor lists: [[2,1], [2,10]] etc.
    try:
        nexus_vers = [[int(s) for s in v.split('.')] for v in nexus_vers]
    except ValueError:
        return None
    # 2-key sort using first major and then minor version number
    idx = nexus_vers.index(max(nexus_vers, key=lambda l: (l[0], l[1])))
    return nexus_dirs[idx]


def _add_nexus_path(vicon_path):
    """Add Nexus SDK dir to sys.path"""

    nexus_path = _find_nexus_path(vicon_path)
    if nexus_path is None:
        logger.warning(
            'cannot locate Nexus installation directory under %s' % vicon_path
        )
        return

    sdk_path = op.join(nexus_path, 'SDK', 'Python')
    if sdk_path not in sys.path:
        sys.path.append(sdk_path)
    else:
        logger.debug('%s already in sys.path' % sdk_path)

    # import from Win32 or Win64 according to bitness of Python interpreter
    bitness = '64' if sys.maxsize > 2 ** 32 else '32'
    win = 'Win' + bitness
    _win_sdk_path = op.join(nexus_path, 'SDK', win)

    # check that the path for the wrong architecture has not already been
    # added to path (this may happen when running inside Nexus)
    win_other = 'Win32' if win == 'Win64' else 'Win64'
    _win_sdk_other = op.join(nexus_path, 'SDK', win_other)
    if _win_sdk_other in sys.path:
        logger.debug('%s already in sys.path, removing' % _win_sdk_other)
        sys.path.remove(_win_sdk_other)

    if _win_sdk_path not in sys.path:
        logger.debug('using Nexus SDK from %s' % _win_sdk_path)
        sys.path.append(_win_sdk_path)
    else:
        logger.debug('%s already in sys.path' % _win_sdk_path)
    return _win_sdk_path


# try to add Nexus SDK to sys.path and import ViconNexus
if sys.version_info.major >= 3:
    logger.debug('running on Python 3 or newer, cannot import Nexus API (yet)')
    nexus_path = ''
else:
    vicon_path = op.normpath(cfg.general.vicon_path)
    nexus_path = _add_nexus_path(vicon_path)
    try:
        import ViconNexus
    except ImportError:
        logger.debug('cannot import Vicon Nexus SDK')
        nexus_path = ''
sys.stdout.flush()  # make sure import warnings get printed


def _nexus_pid():
    """Try to return the PID of the currently running Nexus process"""
    PROCNAME = "Nexus.exe"
    for proc in psutil.process_iter():
        try:
            if proc.name() == PROCNAME:
                return proc.pid
        # catch NoSuchProcess for procs that disappear inside loop
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass
    return None


def _nexus_version():
    """Try to return the actual version of the running Nexus process
    (API does not do that). Hackish and probably unreliable. Returns dict of
    major and minor version number if successful, otherwise (None, None)"""
    PROCNAME = "Nexus.exe"
    for proc in psutil.process_iter():
        try:
            if proc.name() == PROCNAME:
                exname = proc.exe()
                vstart = exname.find('2.')  # assumes ver >2.
                vend = exname.find('\\Nexus.exe')
                if vstart == -1 or vend == -1:
                    return None, None
                try:
                    ver_str = exname[vstart:vend]
                    vmaj, vmin = ver_str.split('.')
                    return int(vmaj), int(vmin)
                except ValueError:  # cannot interpret version string
                    return None, None
        except psutil.AccessDenied:
            pass
    return None, None


def _nexus_ver_greater(major, minor):
    """Checks if running Nexus version is at least the given version"""
    vmaj, vmin = _nexus_version()
    if vmaj is None:
        return False
    else:
        return vmaj >= major and vmin >= minor


def viconnexus():
    """Return a ViconNexus() (SDK control object) instance.

    Raises an exception if Nexus is not running.

    Returns
    -------
    ViconNexus
        The instance.
    """
    _check_nexus()
    return ViconNexus.ViconNexus()


def _close_trial():
    """Try to close currently opened Nexus trial"""
    vicon = viconnexus()
    # this op was not supported before Nexus 2.8
    if _nexus_ver_greater(2, 8):
        logger.info('force closing open trial')
        vicon.CloseTrial(5000)
    else:
        logger.info('current Nexus API version does not support closing trials')


def _open_trial(trialpath, close_first=True):
    """Open trial in Nexus"""
    vicon = viconnexus()
    if close_first:
        _close_trial()
    # Nexus wants the path without filename extension (e.g. .c3d)
    trialpath_ = op.splitext(trialpath)[0]
    vicon.OpenTrial(trialpath_, 60)


def get_subjectnames(single_only=True):
    """Get current subject name(s) from Nexus.

    Parameters
    ----------
    single_only : bool, optional
        Accept and return a single subject only. If True, an exception will be
        raised if Nexus has multiple subjects defined.

    Returns
    -------
    str | list
        The subject name, or a list of names.
    """
    vicon = viconnexus()
    get_sessionpath()  # check whether we can get data
    names_ = vicon.GetSubjectNames()
    if not names_:
        raise GaitDataError('No subject defined in Nexus')
    if single_only:
        if len(names_) > 1:
            raise GaitDataError('Nexus returns multiple subjects')
    # workaround a Nexus 2.6 bug (?) that creates extra names with weird unicode
    # strings
    names_ = [name for name in names_ if u'\ufffd1' not in name]
    return names_[0] if single_only else names_


def _check_nexus():
    """Check whether Nexus is currently running"""
    if not _nexus_pid():
        raise GaitDataError('Vicon Nexus does not seem to be running')


def get_sessionpath():
    """Get path to current Nexus session.

    Returns
    -------
    str
        The path.
    """
    try:
        vicon = viconnexus()
        sessionpath = vicon.GetTrialName()[0]
    except IOError:  # may be raised if Nexus was just terminated
        sessionpath = None
    if not sessionpath:
        raise GaitDataError(
            'Cannot get Nexus session path, no session or maybe in Live mode?'
        )
    return op.normpath(sessionpath)


def _run_pipeline(pipeline, foo, timeout):
    """Wrapper needed for multiprocessing module due to pickle limitations"""
    vicon = viconnexus()
    return vicon.Client.RunPipeline(pipeline, foo, timeout)


def _run_pipelines(pipelines):
    """Run given Nexus pipeline(s).

    Note: this version will stall the calling Python interpreter until the
    pipeline is finished.
    """
    if type(pipelines) != list:
        pipelines = [pipelines]
    for pipeline in pipelines:
        logger.debug('running pipeline: %s' % pipeline)
        result = _run_pipeline(pipeline.encode('utf-8'), '', cfg.autoproc.nexus_timeout)
        if result.Error():
            logger.warning('error while trying to run Nexus pipeline: %s' % pipeline)


def _run_pipelines_multiprocessing(pipelines):
    """Run given Nexus pipeline(s) via the multiprocessing module.

    The idea is to work around the Python global interpreter lock, since the
    Nexus SDK does not release it. By starting a new interpreter process for the
    pipeline, this version causes the invoking thread to sleep and release the
    GIL while the pipeline is running.
    """
    if type(pipelines) != list:
        pipelines = [pipelines]
    for pipeline in pipelines:
        logger.debug('running pipeline via multiprocessing module: %s' % pipeline)
        args = (pipeline.encode('utf-8'), '', cfg.autoproc.nexus_timeout)
        p = multiprocessing.Process(target=_run_pipeline, args=args)
        p.start()
        while p.exitcode is None:
            time.sleep(0.1)


def _get_trialname():
    """Get current Nexus trialname without the session path"""
    vicon = viconnexus()
    trialname_ = vicon.GetTrialName()
    return trialname_[1]


def _is_vicon_instance(obj):
    """Check if obj is an instance of ViconNexus"""
    return obj.__class__.__name__ == 'ViconNexus'


def _get_nexus_subject_param(vicon, name, param):
    """Wrapper to get subject parameter from Nexus."""
    value = vicon.GetSubjectParam(name, param)
    # for unknown reasons, above method may return tuple or float
    # depending on whether script is run from Nexus or outside
    if type(value) == tuple:
        value = value[0] if value[1] else None
    return value


def _get_marker_names(vicon, trajs_only=True):
    """Return marker names from Nexus.

    If trajs_only, only return markers with trajectories.
    """
    subjname = get_subjectnames()
    markers = vicon.GetMarkerNames(subjname)
    # only get markers with trajectories - excludes calibration markers
    if trajs_only:
        markers = [mkr for mkr in markers if vicon.HasTrajectory(subjname, mkr)]
    return markers


def _get_metadata(vicon):
    """Read trial and subject metadata from Nexus.

    See read.data.get_metadata for details."""
    _check_nexus()
    logger.debug('reading metadata from Vicon Nexus')
    subjname = get_subjectnames()
    params_available = vicon.GetSubjectParamNames(subjname)
    subj_params = defaultdict(lambda: None)
    subj_params.update(
        {
            par: _get_nexus_subject_param(vicon, subjname, par)
            for par in params_available
        }
    )
    trialname = _get_trialname()
    if not trialname:
        raise GaitDataError('No trial loaded in Nexus')
    sessionpath = get_sessionpath()
    markers = _get_marker_names(vicon)
    # get foot strike and toeoffevents. GetEvents() indices seem to often be 1
    # frame less than on Nexus display - only happens with ROI?
    lstrikes = vicon.GetEvents(subjname, "Left", "Foot Strike")[0]
    rstrikes = vicon.GetEvents(subjname, "Right", "Foot Strike")[0]
    ltoeoffs = vicon.GetEvents(subjname, "Left", "Foot Off")[0]
    rtoeoffs = vicon.GetEvents(subjname, "Right", "Foot Off")[0]
    events = TrialEvents(
        rstrikes=rstrikes, lstrikes=lstrikes, rtoeoffs=rtoeoffs, ltoeoffs=ltoeoffs
    )

    # offset will be subtracted from event frame numbers to get correct
    # 0-based index for frame data. for Nexus, it is always 1 (Nexus uses
    # 1-based frame numbering)
    offset = 1
    length = vicon.GetFrameCount()
    framerate = vicon.GetFrameRate()
    # get analog rate. this may not be mandatory if analog devices
    # are not used, but currently it needs to succeed.
    devids = vicon.GetDeviceIDs()
    if not devids:
        raise GaitDataError('Cannot determine analog rate')
    else:
        # rates may be 0 for some devices, we just pick the maximum as "the rate"
        analogrates = [vicon.GetDeviceDetails(id)[2] for id in devids]
        analograte = max(rate for rate in analogrates if _isfloat(rate))
    if analograte == 0.0:
        raise GaitDataError('Cannot determine analog rate')
    samplesperframe = analograte / framerate
    logger.debug(
        'offset @ %d, %d frames, framerate %d Hz, %d samples per '
        'frame' % (offset, length, framerate, samplesperframe)
    )
    # get n of forceplates
    fp_devids = [
        id_ for id_ in devids if vicon.GetDeviceDetails(id_)[1].lower() == 'forceplate'
    ]

    return {
        'trialname': trialname,
        'sessionpath': sessionpath,
        'offset': offset,
        'framerate': framerate,
        'analograte': analograte,
        'name': subjname,
        'subj_params': subj_params,
        'events': events,
        'length': length,
        'samplesperframe': samplesperframe,
        'n_forceplates': len(fp_devids),
        'markers': markers,
    }


def _get_emg_data(vicon):
    """Read EMG data from Nexus. Uses the configured EMG device name."""
    return _get_analog_data(vicon, cfg.emg.devname)


def _get_accelerometer_data(vicon):
    """Read accelerometer data from Nexus. Uses the configured acc device name."""
    return _get_analog_data(vicon, cfg.analog.accelerometer_devname)


def _get_analog_data(vicon, devname):
    """Read analog data from Vicon Nexus.

    Parameters
    ----------
    vicon : ViconNexus
        The SDK object.
    devname : str
        The analog device name, set in Nexus configuration. E.g. 'Myon EMG'.

    Returns
    -------
    dict
        Dict with keys 't' (time points corresponding to data samples) and
        'data' (the analog data as shape (N,) ndarray, for each output channel).
    """
    # match devname exactly (not case-sensitive though)
    ids = [
        id_
        for id_ in vicon.GetDeviceIDs()
        if vicon.GetDeviceDetails(id_)[0].lower() == devname.lower()
    ]
    if len(ids) > 1:
        raise GaitDataError('Multiple matching analog devices for %s' % devname)
    elif len(ids) == 0:
        raise GaitDataError('No matching analog devices for %s' % devname)
    dev_id = ids[0]
    dname, dtype, drate, outputids, _, _ = vicon.GetDeviceDetails(dev_id)
    # gather device outputs; there does not seem to be any reliable way to
    # identify output IDs that have actual EMG signal, so we use the heuristic
    # of units being volts. this may lead to inclusion of some channels (e.g.
    # Foot Switch on Noraxon) that are not actually EMG
    emg_outputids = [
        outputid
        for outputid in outputids
        if vicon.GetDeviceOutputDetails(dev_id, outputid)[2] == 'volt'
    ]

    data = dict()
    for outputid in emg_outputids:
        # get list of channel names and IDs
        outputname, _, _, _, chnames, chids = vicon.GetDeviceOutputDetails(dev_id, outputid)
        for chid in chids:
            chdata, _, chrate = vicon.GetDeviceChannel(dev_id, outputid, chid)
            chname = chnames[chid - 1]  # chids start from 1
            # in case of multiple output ids (e.g. Noraxon), the channel
            # names may not be unique, so try to generate unique names by
            # merging output name and channel name
            if len(emg_outputids) > 1:
                logger.warning('merging output %s and channel name %s for a unique name' % (outputname, chname))
                chname = '%s_%s' % (outputname, chname)
            if chname in data:
                raise RuntimeError('duplicate EMG channel; check Nexus device settings')
            data[chname] = np.array(chdata)
    # WIP: sanity checks for data (channel lengths equal, etc.)    
    t = np.arange(len(chdata)) / drate  # time axis
    return {'t': t, 'data': data}


def _get_1_forceplate_data(vicon, devid):
    """Read data of single forceplate from Nexus.
    Data is returned in global (laboratory) coordinate frame."""
    # get available forceplate ids
    logger.debug('reading forceplate data from devid %d' % devid)
    dname, dtype, drate, outputids, nfp, _ = vicon.GetDeviceDetails(devid)
    # outputs should be force, moment, cop. read them one by one
    outputid = outputids[0]
    chid = vicon.GetDeviceChannelIDFromName(devid, outputid, 'Fx')
    fx, chready, chrate = vicon.GetDeviceChannelGlobal(devid, outputid, chid)
    chid = vicon.GetDeviceChannelIDFromName(devid, outputid, 'Fy')
    fy, chready, chrate = vicon.GetDeviceChannelGlobal(devid, outputid, chid)
    chid = vicon.GetDeviceChannelIDFromName(devid, outputid, 'Fz')
    fz, chready, chrate = vicon.GetDeviceChannelGlobal(devid, outputid, chid)
    # moments
    outputid = outputids[1]
    chid = vicon.GetDeviceChannelIDFromName(devid, outputid, 'Mx')
    mx, chready, chrate = vicon.GetDeviceChannelGlobal(devid, outputid, chid)
    chid = vicon.GetDeviceChannelIDFromName(devid, outputid, 'My')
    my, chready, chrate = vicon.GetDeviceChannelGlobal(devid, outputid, chid)
    chid = vicon.GetDeviceChannelIDFromName(devid, outputid, 'Mz')
    mz, chready, chrate = vicon.GetDeviceChannelGlobal(devid, outputid, chid)
    # center of pressure
    outputid = outputids[2]
    chid = vicon.GetDeviceChannelIDFromName(devid, outputid, 'Cx')
    copx, chready, chrate = vicon.GetDeviceChannelGlobal(devid, outputid, chid)
    chid = vicon.GetDeviceChannelIDFromName(devid, outputid, 'Cy')
    copy, chready, chrate = vicon.GetDeviceChannelGlobal(devid, outputid, chid)
    chid = vicon.GetDeviceChannelIDFromName(devid, outputid, 'Cz')
    copz, chready, chrate = vicon.GetDeviceChannelGlobal(devid, outputid, chid)
    cop_w = np.array([copx, copy, copz]).transpose()
    F = np.array([fx, fy, fz]).transpose()
    M = np.array([mx, my, mz]).transpose()
    Ftot = np.linalg.norm(F, axis=1)
    # translation and rotation matrices -> world coords
    # suspect that Nexus wR is wrong (does not match displayed plate axes)?
    wR = np.array(nfp.WorldR).reshape(3, 3)
    wT = np.array(nfp.WorldT)
    # plate corners -> world coords
    cor = np.stack([nfp.LowerBounds, nfp.UpperBounds])
    cor_w = _change_coords(cor, wR, wT)
    cor_full = np.array(
        [
            cor_w[0, :],
            [cor_w[0, 0], cor_w[1, 1], cor_w[0, 2]],
            cor_w[1, :],
            [cor_w[1, 0], cor_w[0, 1], cor_w[0, 2]],
        ]
    )

    lb = np.min(cor_w, axis=0)
    ub = np.max(cor_w, axis=0)
    # check that CoP stays inside plate boundaries
    cop_ok = np.logical_and(cop_w[:, 0] >= lb[0], cop_w[:, 0] <= ub[0]).all()
    cop_ok &= np.logical_and(cop_w[:, 1] >= lb[1], cop_w[:, 1] <= ub[1]).all()
    if not cop_ok:
        logger.warning('center of pressure outside plate boundaries, clipping to plate')
        cop_w[:, 0] = np.clip(cop_w[:, 0], lb[0], ub[0])
        cop_w[:, 1] = np.clip(cop_w[:, 1], lb[1], ub[1])
    return {
        'F': F,
        'M': M,
        'Ftot': Ftot,
        'CoP': cop_w,
        'wR': wR,
        'wT': wT,
        'lowerbounds': lb,
        'upperbounds': ub,
        'cor_w': cor_w,
        'cor_full': cor_full,
    }


def _get_forceplate_data(vicon):
    """Read data of all forceplates from Nexus.

    See read_data.get_forceplate_data() for details.
    """
    # get forceplate ids
    logger.debug('reading forceplate data from Vicon Nexus')
    devids = [
        id_
        for id_ in vicon.GetDeviceIDs()
        if vicon.GetDeviceDetails(id_)[1].lower() == 'forceplate'
    ]
    if len(devids) == 0:
        logger.debug('no forceplates detected')
        return None
    logger.debug('detected %d forceplate(s)' % len(devids))
    # filter by device name
    if cfg.autoproc.nexus_forceplate_devnames:
        devids = [
            id
            for id in devids
            if vicon.GetDeviceDetails(id)[0] in cfg.autoproc.nexus_forceplate_devnames
        ]
    return [_get_1_forceplate_data(vicon, devid) for devid in devids]


def _swap_markers(vicon, marker1, marker2):
    """Swap trajectories of given two markers in the current trial"""
    subj = get_subjectnames()
    m1 = vicon.GetTrajectory(subj, marker1)
    m2 = vicon.GetTrajectory(subj, marker2)
    vicon.SetTrajectory(subj, marker2, m1[0], m1[1], m1[2], m1[3])
    vicon.SetTrajectory(subj, marker1, m2[0], m2[1], m2[2], m2[3])


def _get_marker_data(vicon, markers, ignore_missing=False):
    """Get position data for specified markers.

    See read_data.get_marker_data for details.
    """
    if not isinstance(markers, list):
        markers = [markers]
    subj = get_subjectnames()
    mkrdata = dict()
    for marker in markers:
        x, y, z, _ = vicon.GetTrajectory(subj, marker)
        if len(x) == 0:
            if ignore_missing:
                logger.warning('Cannot read trajectory %s from Nexus' % marker)
                continue
            else:
                raise GaitDataError(
                    'Cannot read marker trajectory from Nexus: %s' % marker
                )
        mkrdata[marker] = np.array([x, y, z]).transpose()
    return mkrdata


def _get_model_data(vicon, model):
    """Read model output variables (e.g. Plug-in Gait).

    See read_data.get_model_data for details.
    """
    modeldata = dict()
    var_dims = (3, vicon.GetFrameCount())
    subj = get_subjectnames()
    for var in model.read_vars:
        nums, bools = vicon.GetModelOutput(subj, var)
        if nums:
            data = np.squeeze(np.array(nums))
        else:
            logger.info('cannot read variable %s, returning nans' % var)
            data = np.empty(var_dims)
            data[:] = np.nan
        modeldata[var] = data
    return modeldata


def _create_events(vicon, context, strikes, toeoffs):
    """Create foot strike and toeoff events in Nexus"""
    logger.debug('marking events in Nexus')
    side_str = 'Right' if context == 'R' else 'Left'
    subjectname = get_subjectnames()
    for fr in strikes:
        vicon.CreateAnEvent(subjectname, side_str, 'Foot Strike', int(fr + 1), 0)
    for fr in toeoffs:
        vicon.CreateAnEvent(subjectname, side_str, 'Foot Off', int(fr + 1), 0)
