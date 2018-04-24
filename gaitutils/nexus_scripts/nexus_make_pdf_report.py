# -*- coding: utf-8 -*-
"""

Script to create the full pdf gait report.
Note: specific to the Helsinki gait lab.


@author: Jussi (jnu@iki.fi)
"""

import time
import datetime
import logging
import os.path as op
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from collections import defaultdict

from gaitutils import (Plotter, cfg, register_gui_exception_handler, layouts,
                       numutils, normaldata)
from gaitutils.nexus import get_sessionpath, find_tagged
import nexus_kin_consistency
import nexus_emg_consistency
import nexus_musclelen_consistency
import nexus_kin_average
import nexus_trials_velocity
import nexus_time_distance_vars


logger = logging.getLogger(__name__)

sort_field = 'NOTES'  # sort trials by the given Eclipse key
page_size = (11.69, 8.27)  # report page size


def _add_footer(fig, txt):
    fig.text(0, 0, txt, fontsize=8, color='black', ha='left', va='bottom')


def _add_header(fig, txt):
    fig.text(0, 1, txt, fontsize=8, color='black', ha='left', va='top')


def _savefig(pdf, fig, header=None, footer=None):
    """add header/footer into page and save as A4"""
    if fig is None:
        return
    if header is not None:
        _add_header(fig, header)
    if footer is not None:
        _add_footer(fig, footer)
    fig.set_size_inches(page_size[0], page_size[1])
    pdf.savefig(fig)


def do_plot(fullname=None, hetu=None, pages=None, description=None):

    if fullname is None:
        fullname = ''
    if hetu is None:
        hetu = ''
    if pages is None:
        # if no pages specified, do everything
        pages = defaultdict(lambda: True)
    else:
        if not any(pages.values()):
            raise Exception('No pages to print')

    tagged_figs = []
    repr_figs = []
    eclipse_tags = dict()
    do_emg_consistency = False

    tagged_trials = find_tagged()
    if not tagged_trials:
        raise ValueError('No marked trials found in session directory')
    # use creation date of 1st tagged trial as session timestamp
    tagged1 = op.splitext(tagged_trials[0])[0] + '.x1d'
    session_t = datetime.datetime.fromtimestamp(op.getctime(tagged1))
    logger.debug('session timestamp: %s', session_t)
    # compute subject age at time of session
    age = numutils.age_from_hetu(hetu, session_t) if hetu else None

    sessionpath = get_sessionpath()
    session = op.split(sessionpath)[-1]
    session_root = op.split(sessionpath)[0]
    patient_code = op.split(session_root)[1]
    pdfname = session + '.pdf'
    pdf_all = op.join(sessionpath, pdfname)

    # make header page
    # timestr = time.strftime('%d.%m.%Y')  # current time, not currently used
    fig_hdr = plt.figure()
    ax = plt.subplot(111)
    plt.axis('off')
    title_txt = 'HUS Liikelaboratorio\n'
    title_txt += u'Kävelyanalyysin tulokset\n'
    title_txt += '\n'
    title_txt += u'Nimi: %s\n' % fullname
    title_txt += u'Henkilötunnus: %s\n' % (hetu if hetu else 'ei tiedossa')
    title_txt += u'Ikä mittaushetkellä: %s\n' % ('%d vuotta' % age if age
                                                   else 'ei tiedossa')
    title_txt += u'Mittaus: %s\n' % session
    if description:
        title_txt += u'Kuvaus: %s\n' % description
    title_txt += u'Mittauksen pvm: %s\n' % session_t.strftime('%d.%m.%Y')
    title_txt += u'Liikelaboratorion potilaskoodi: %s\n' % patient_code
    ax.text(.5, .8, title_txt, ha='center', va='center', weight='bold',
            fontsize=14)

    header = u'Nimi: %s Henkilötunnus: %s' % (fullname, hetu)
    musclelen_ndata = normaldata.normaldata_age(age)
    footer_musclelen = (u' Normaalidata: %s' % musclelen_ndata if
                        musclelen_ndata else u'')

    pl = Plotter()

    for c3d in tagged_trials:

        pl.open_trial(c3d)
        representative = (pl.trial.eclipse_data[sort_field].upper()
                          in ['R1', 'L1'])

        # FIXME: this would choose R when valid for both
        if 'R' in pl.trial.fp_events['valid']:
            side = 'R'
        elif 'L' in pl.trial.fp_events['valid']:
            side = 'L'
        else:
            # raise Exception('No kinetics for %s' % c3d)
            # in some cases, kinetics are not available, but we do not want
            # to die on it
            logger.warning('No kinetics for %s' % c3d)
            side = 'R'

        side_str = 'right' if side == 'R' else 'left'

        # representative single trial plots
        if representative:
            if pages['TimeDistRepresentative']:
                fig = nexus_time_distance_vars.do_single_trial_plot(c3d,
                                                                    show=False)
                repr_figs.append(fig)

        # try to figure out whether we have any valid EMG signals
        emg_active = any([pl.trial.emg.status_ok(ch) for ch in
                          cfg.emg.channel_labels])

        if emg_active:

            if pages['EMGCons']:
                do_emg_consistency = True

            if pages['KinEMGMarked']:
                # kinetics-EMG
                pl.layout = (cfg.layouts.lb_kinetics_emg_r if side == 'R' else
                             cfg.layouts.lb_kinetics_emg_l)

                maintitle = 'Kinetics-EMG (%s) for %s' % (side_str,
                                                          pl.title_with_eclipse_info())
                fig = pl.plot_trial(maintitle=maintitle, show=False)
                tagged_figs.append(fig)
                eclipse_tags[fig] = (pl.trial.eclipse_data[sort_field])

                # save individual pdfs
                if representative:
                    pdf_name = 'kinetics_EMG_%s_%s.pdf' % (pl.trial.trialname,
                                                           side_str)
                    logger.debug('creating %s' % pdf_name)
                    pl.create_pdf(pdf_name=pdf_name)

            if pages['EMGMarked']:
                # EMG
                maintitle = pl.title_with_eclipse_info('EMG plot for')
                layout = cfg.layouts.std_emg
                pl.layout = layouts.rm_dead_channels(c3d, pl.trial.emg, layout)
                fig = pl.plot_trial(maintitle=maintitle, show=False)
                tagged_figs.append(fig)
                eclipse_tags[fig] = (pl.trial.eclipse_data[sort_field])

                # save individual pdfs
                if representative:
                    pdf_prefix = 'EMG_'
                    pl.create_pdf(pdf_prefix=pdf_prefix)

    tagged_figs.sort(key=lambda fig: eclipse_tags[fig])

    # trial velocity plot
    if pages['TrialVelocity']:
        fig_vel = nexus_trials_velocity.do_plot(show=False, make_pdf=False)
    else:
        fig_vel = None

    # time-distance average
    if pages['TimeDistAverage']:
        fig_timedist_avg = nexus_time_distance_vars.do_session_average_plot(show=False, make_pdf=False)
    else:
        fig_timedist_avg = None

    # consistency plots
    # write these out separately for inclusion in Polygon report
    if pages['KinCons']:
        fig_kin_cons = nexus_kin_consistency.do_plot(show=False, make_pdf=True)
    else:
        fig_kin_cons = None

    if pages['MuscleLenCons']:
        fig_musclelen_cons = nexus_musclelen_consistency.do_plot(show=False,
                                                                 age=age,
                                                                 make_pdf=True)
    else:
        fig_musclelen_cons = None

    if do_emg_consistency:
        fig_emg_cons = nexus_emg_consistency.do_plot(show=False, make_pdf=True)
    else:
        fig_emg_cons = None

    # average plots
    if pages['KinAverage']:
        figs_kin_avg = nexus_kin_average.do_plot(show=False, make_pdf=False)
    else:
        figs_kin_avg = list()

    logger.debug('creating multipage pdf %s' % pdf_all)
    with PdfPages(pdf_all) as pdf:
        _savefig(pdf, fig_hdr)
        _savefig(pdf, fig_vel, header)
        _savefig(pdf, fig_timedist_avg, header)
        _savefig(pdf, fig_kin_cons, header)
        _savefig(pdf, fig_musclelen_cons, header, footer_musclelen)
        _savefig(pdf, fig_emg_cons, header)
        for fig in figs_kin_avg:
            _savefig(pdf, fig, header)
        for fig in repr_figs:
            _savefig(pdf, fig, header)
        for fig in tagged_figs:
            _savefig(pdf, fig, header)

    # close all created figures, otherwise they'll pop up on next show() call
    plt.close('all')


if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG)
    register_gui_exception_handler()
    do_plot()
