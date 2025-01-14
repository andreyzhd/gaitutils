# -*- coding: utf-8 -*-
"""
Create web-based gait report using dash.

@author: Jussi (jnu@iki.fi)
"""

import plotly.graph_objs as go
import dash

from dash.dependencies import Input, Output, State
import flask
from flask import request
import logging
import base64
import pickle
import os
from pathlib import Path

from ulstools.num import age_from_hetu

from .. import (
    normaldata,
    models,
    sessionutils,
    numutils,
    videos,
)
from ..config import cfg
from ..envutils import GaitDataError
from ..sessionutils import enf_to_trialfile
from ..trial import Trial
from ..viz.plot_plotly import plot_trials, plot_extracted_box
from ..viz import timedist, layouts
from ..stats import AvgTrial, _trials_extract_values
from ..gui.qt_widgets import ProgressSignals

try:
    from dash import dcc, html  # new style
except ImportError:
    import dash_core_components as dcc  # old style
    import dash_html_components as html


logger = logging.getLogger(__name__)


def _make_dropdown_lists(options):
    """Helper for dcc.Dropdown.

    Take a list of label/value dicts (with arbitrary type values) and returns
    (list, dict). Needed since dcc.Dropdown can only take str values. identity
    is fed to dcc.Dropdown() and mapper is used for getting the actual values at
    the callback."""
    identity = list()
    mapper = dict()
    for option in options:
        di = {'label': option['label'], 'value': option['label']}
        if 'disabled' in option and option['disabled']:
            di['disabled'] = True
        identity.append(di)
        mapper[option['label']] = option['value']
    return identity, mapper


def _shutdown_server():
    """Shutdown flask server, see http://flask.pocoo.org/snippets/67/"""
    func = request.environ.get('werkzeug.server.shutdown')
    if func is None:
        raise RuntimeError('Not running with the Werkzeug Server')
    func()


def _report_name(sessions, long_name=True):
    """Create a title for the dash report"""
    sessions_str = ' / '.join(s.name for s in sessions)
    if long_name:
        report_type = (
            'Single session report' if len(sessions) == 1 else 'Comparison report'
        )
    else:
        report_type = 'Single' if len(sessions) == 1 else 'Comparison'
    return f'{report_type}: {sessions_str}'


def dash_report(
    sessions,
    info=None,
    max_cycles=None,
    tags=None,
    signals=None,
    recreate_plots=None,
    video_only=None,
):
    """Create a gait report dash app.

    Parameters
    ----------
    sessions : list
        List of session directories. For more than one session dirs, a
        comparison report will be created. Up to three sessions can be compared
        in the report.
    info : dict | None
        The patient info. If not None, some info will be shown in the report.
    max_cycles : dict | None
        Maximum number of gait cycles to plot for each variable type. If None,
        taken from config.
    tags : list | None
        Eclipse tags for finding dynamic gait trials. If None, will be taken from config.
    signals : ProgressSignals | None
        Instance of ProgressSignals, used to send progress updates across
        threads and track the cancel flag which aborts the creation of the
        report. If None, a dummy one will be created.
    recreate_plots : bool
        If True, force recreation of the report figures. Otherwise, cached
        figure data will be used, unless the report c3d files have changed (a
        checksum mechanism is used to verify this).
    video_only : bool
        If True, create a video-only report (no gait curves).

    Returns
    -------
    dash.Dash | None
        The dash (flask) app, or None if report creation failed.
    """

    # best to check early
    if not os.access(cfg.general.browser_path, os.X_OK):
        raise RuntimeError(f'Invalid configured web browser: {cfg.general.browser_path}')

    sessions = [Path(s) for s in sessions]

    if recreate_plots is None:
        recreate_plots = False

    if video_only is None:
        video_only = False

    # relative width of left panel (1-12)
    # uncomment to use narrower video panel for 3-session comparison
    # LEFT_WIDTH = 8 if len(sessions) == 3 else 7
    LEFT_WIDTH = 8
    VIDS_TOTAL_HEIGHT = 88  # % of browser window height

    if len(sessions) < 1 or len(sessions) > 3:
        raise ValueError('Need a list of one to three sessions')
    is_comparison = len(sessions) > 1
    report_name = _report_name(sessions)
    info = info or sessionutils.default_info()

    # tags for dynamic trials
    if tags is None:
        dyn_tags = cfg.eclipse.tags
    else:
        dyn_tags = tags

    # signals is used to track progress across threads; if not given, create a dummy one
    if signals is None:
        signals = ProgressSignals()

    # this tag will be shown in the menu for static trials
    static_tag = 'Static'

    # get the camera labels
    # reduce to a set, since there may be several labels for given id
    camera_labels = set(cfg.general.camera_labels.values())
    # add camera labels for overlay videos
    # XXX: may cause trouble if camera labels already contain the string 'overlay'
    camera_labels_overlay = [lbl + ' overlay' for lbl in camera_labels]
    camera_labels.update(camera_labels_overlay)
    # build dict of videos for given tag / camera label
    # videos will be listed in session order
    vid_urls = dict()
    all_tags = dyn_tags + [static_tag] + cfg.eclipse.video_tags
    for tag in all_tags:
        vid_urls[tag] = dict()
        for camera_label in camera_labels:
            vid_urls[tag][camera_label] = list()

    # collect all session enfs into dict
    enfs = {session: dict() for session in sessions}
    data_enfs = list()  # enfs that are used for data
    signals.progress.emit('Collecting trials...', 0)
    for session in sessions:
        if signals.canceled:
            return None
        enfs[session] = dict(dynamic=dict(), static=dict(), vid_only=dict())
        # collect dynamic trials for each tag
        for tag in dyn_tags:
            dyns = sessionutils.get_enfs(session, tags=tag, trial_type='dynamic')
            if len(dyns) > 1:
                logger.warning(f'multiple tagged trials ({tag}) for {session}')
            dyn_trial = dyns[-1:]
            enfs[session]['dynamic'][tag] = dyn_trial  # may be empty list
            if dyn_trial:
                data_enfs.extend(dyn_trial)
        # require at least one dynamic trial for each session
        if not any(enfs[session]['dynamic'][tag] for tag in dyn_tags):
            raise GaitDataError(f'No tagged dynamic trials found for {session}')
        # collect static trial (at most 1 per session)
        # rules:
        # -prefer enfs that have a corresponding c3d file, even for a video-only report
        # (so that the same static gets used for both video-only and full reports)
        # -prefer the newest static trial
        sts = sessionutils.get_enfs(session, trial_type='static')
        for st in reversed(sts):  # newest first
            st_c3d = sessionutils.enf_to_trialfile(st, '.c3d')
            if st_c3d.is_file():
                static_trial = [st]
                break
        else:
            # no c3ds were found - just pick the latest static trial
            static_trial = sts[-1:]
        enfs[session]['static'][static_tag] = static_trial
        if static_trial:
            data_enfs.extend(static_trial)
        # collect video-only dynamic trials
        for tag in cfg.eclipse.video_tags:
            dyn_vids = sessionutils.get_enfs(session, tags=tag)
            if len(dyn_vids) > 1:
                logger.warning(
                    f'multiple tagged video-only trials ({tag}) for {session}'
                )
            enfs[session]['vid_only'][tag] = dyn_vids[-1:]

    # collect all videos for given tag and camera, listed in session order
    signals.progress.emit('Finding videos...', 0)
    for session in sessions:
        for trial_type in enfs[session]:
            for tag, enfs_this in enfs[session][trial_type].items():
                if enfs_this:
                    enf = enfs_this[0]  # only one enf per tag and session
                    for camera_label in camera_labels:
                        overlay = 'overlay' in camera_label
                        real_camera_label = (
                            camera_label[: camera_label.find(' overlay')]
                            if overlay
                            else camera_label
                        )
                        # need to convert filename, since get_trial_videos cannot
                        # deal with enf names
                        c3d = enf_to_trialfile(enf, 'c3d')
                        vids_this = videos.get_trial_videos(
                            c3d,
                            camera_label=real_camera_label,
                            vid_ext=cfg.general.video_converted_ext,
                            overlay=overlay,
                        )
                        if vids_this:
                            vid = vids_this[0]
                            url = f'/static/{vid.name}'
                            vid_urls[tag][camera_label].append(url)

    # build dcc.Dropdown options list for cameras and tags
    # list cameras which have videos for any tag
    opts_cameras = list()
    for camera_label in sorted(camera_labels):
        if any(vid_urls[tag][camera_label] for tag in all_tags):
            opts_cameras.append({'label': camera_label, 'value': camera_label})
    # list tags which have videos for any camera
    opts_tags = list()
    for tag in all_tags:
        if any(vid_urls[tag][camera_label] for camera_label in camera_labels):
            opts_tags.append({'label': f'{tag}', 'value': tag})
    # add null entry in case we got no videos at all
    if not opts_tags:
        opts_tags.append({'label': 'No videos', 'value': 'no videos', 'disabled': True})

    # create (or load) the figures
    # this section is only run if we have c3d data
    if not video_only:
        data_c3ds = [enf_to_trialfile(enffile, 'c3d') for enffile in data_enfs]
        # at this point, all the c3ds need to exist
        missing = [fn for fn in data_c3ds if not fn.is_file()]
        if missing:
            missing_trials = ', '.join([fn.stem for fn in missing])
            raise GaitDataError(
                f'c3d files missing for following trials: {missing_trials}'
            )
        # see whether we can load report figures from disk
        digest = numutils._files_digest(data_c3ds)
        logger.debug(f'report data digest: {digest}')
        # the cached data is always saved into alphabetically first session
        data_dir = sorted(sessions)[0]
        data_fn = data_dir / f'web_report_{digest}.dat'
        if data_fn.is_file() and not recreate_plots:
            logger.info(f'loading saved report data from {data_fn}')
            signals.progress.emit('Loading saved report...', 0)
            try:
                with open(data_fn, 'rb') as f:
                    saved_report_data = pickle.load(f)
            except UnicodeDecodeError:
                logger.warning('cannot open report (probably made with legacy version)')
                logger.warning('recreating...')
                saved_report_data = dict()
        else:
            saved_report_data = dict()
            logger.info('no saved data found or recreate forced')

        # make Trial instances for all dynamic and static trials
        # this is currently necessary even if saved figures are used
        trials_dyn = list()
        trials_dyn_dict = dict()  # also organize dynamic trials by session
        trials_static = list()
        for session in sessions:
            trials_dyn_dict[session] = list()
            for tag in dyn_tags:
                if enfs[session]['dynamic'][tag]:
                    if signals.canceled:
                        return None
                    c3dfile = enf_to_trialfile(enfs[session]['dynamic'][tag][0], 'c3d')
                    tri = Trial(c3dfile)
                    trials_dyn.append(tri)
                    trials_dyn_dict[session].append(tri)
            if enfs[session]['static'][static_tag]:
                c3dfile = enf_to_trialfile(enfs[session]['static']['Static'][0], 'c3d')
                tri = Trial(c3dfile)
                trials_static.append(tri)

        emg_auto_layout = None

        # stuff that's needed to (re)create the figures
        if not saved_report_data:
            age = None
            if info['hetu'] is not None:
                # compute subject age at session time
                session_dates = [
                    sessionutils.get_session_date(session) for session in sessions
                ]
                ages = [age_from_hetu(info['hetu'], d) for d in session_dates]
                try:
                    age = max(ages)
                except TypeError:
                    age = None

            # create Markdown text for patient info
            patient_info_text = '##### %s ' % (
                info['fullname'] if info['fullname'] else 'Name unknown'
            )
            if info['hetu']:
                patient_info_text += f"({info['hetu']})"
            patient_info_text += '\n\n'
            # if age:
            #     patient_info_text += 'Age at measurement time: %d\n\n' % age

            # load normal data for gait models; we have to do it here instead of
            # leaving it up to plot_trials, since it's session (age) specific
            signals.progress.emit('Loading normal data...', 0)
            model_normaldata = normaldata._read_configured_model_normaldata(age)

            # make average trials for each session
            avg_trials = [
                AvgTrial.from_trials(trials_dyn_dict[session], sessionpath=session)
                for session in sessions
            ]

            # prepare for the curve-extracted value plots
            logger.debug('extracting values for curve-extracted plots...')
            vardefs_dict = dict(cfg.report.vardefs)
            allvars = [
                vardef[0] for vardefs in vardefs_dict.values() for vardef in vardefs
            ]
            from_models = set(models.model_from_var(var) for var in allvars)
            if None in from_models:
                raise GaitDataError(f'unknown variables in extract list: {allvars}')
            curve_vals = {
                session.name: _trials_extract_values(trials, from_models=from_models)
                for session, trials in trials_dyn_dict.items()
            }

            # in EMG layout, keep chs that are active in any of the trials
            signals.progress.emit('Reading EMG data', 0)
            try:
                emgs = [tr.emg for tr in trials_dyn]
                emg_auto_layout = layouts._rm_dead_channels(emgs, cfg.layouts.std_emg)
                if not emg_auto_layout:
                    emg_auto_layout = None
            except GaitDataError:
                emg_auto_layout = None

        # the layouts are specified as lists of tuples: (title, layout_spec)
        # where title is the page title, and layout_spec is either string or tuple.
        # if string, it denotes a special layout (e.g. 'patient_info')
        # if tuple, the first element should be the string 'layout_name' and the second
        # a gaitutils configured layout name;
        # alternatively the first element can be 'layout' and the second element a
        # valid gaitutils layout
        page_layouts = dict(cfg.web_report.page_layouts)

        # pick desired single variables from model and append
        pigvars = (
            models.pig_lowerbody.varlabels_nocontext
            | models.pig_lowerbody_kinetics.varlabels_nocontext
        )
        pigvars = sorted(pigvars.items(), key=lambda item: item[1])
        pigvars_louts = {varlabel: ('layout', [[var]]) for var, varlabel in pigvars}
        page_layouts.update(pigvars_louts)

        # add supplementary data for normal layouts
        supplementary_default = dict()

        dd_opts_multi_upper = list()
        dd_opts_multi_lower = list()

        # loop through the layouts, create or load figures
        report_data_new = dict()
        for k, (page_label, layout_spec) in enumerate(page_layouts.items()):
            signals.progress.emit(
                f'Creating plot: {page_label}', 100 * k / len(page_layouts)
            )
            if signals.canceled:
                return None
            # for comparison report, include session info in plot legends and
            # use session specific line style
            emg_mode = None
            if is_comparison:
                legend_type = cfg.report.comparison_legend_type
                style_by = cfg.report.comparison_style_by
                color_by = cfg.report.comparison_color_by
                if cfg.report.comparison_emg_as_envelope:
                    emg_mode = 'envelope'
            else:
                legend_type = cfg.report.legend_type
                style_by = cfg.report.style_by
                color_by = cfg.report.color_by

            try:
                if saved_report_data:
                    logger.debug(f'loading {page_label} from saved report data')
                    if page_label not in saved_report_data:
                        # will be caught, resulting in empty menu item
                        raise RuntimeError
                    else:
                        figdata = saved_report_data[page_label]
                else:
                    logger.debug(f'creating figure data for {page_label}')
                    # the 'special' layouts are indicated by a string
                    if isinstance(layout_spec, str):
                        if layout_spec == 'time_dist':
                            figdata = timedist.plot_comparison(
                                sessions, big_fonts=False, backend='plotly'
                            )
                        elif layout_spec == 'patient_info':
                            figdata = patient_info_text
                        elif layout_spec == 'static_kinematics':
                            layout_ = cfg.layouts.lb_kinematics
                            figdata = plot_trials(
                                trials_static,
                                layout_,
                                model_normaldata=False,
                                cycles='unnormalized',
                                legend_type='short_name_with_cyclename',
                                style_by=style_by,
                                color_by=color_by,
                                big_fonts=True,
                            )
                        elif layout_spec == 'static_emg':
                            layout_ = cfg.layouts.std_emg
                            figdata = plot_trials(
                                trials_static,
                                layout_,
                                model_normaldata=False,
                                cycles='unnormalized',
                                legend_type='short_name_with_cyclename',
                                style_by=style_by,
                                color_by=color_by,
                                big_fonts=True,
                            )
                        elif layout_spec == 'emg_auto':
                            if emg_auto_layout is None:  # no valid EMG channels
                                raise RuntimeError
                            else:
                                figdata = plot_trials(
                                    trials_dyn,
                                    emg_auto_layout,
                                    emg_mode=emg_mode,
                                    legend_type=legend_type,
                                    style_by=style_by,
                                    color_by=color_by,
                                    supplementary_data=supplementary_default,
                                    big_fonts=True,
                                )
                        elif layout_spec == 'kinematics_average':
                            layout_ = cfg.layouts.lb_kinematics
                            figdata = plot_trials(
                                avg_trials,
                                layout_,
                                style_by=style_by,
                                color_by=color_by,
                                model_normaldata=model_normaldata,
                                big_fonts=True,
                            )
                        elif layout_spec == 'disabled':
                            # exception will be caught in this loop, resulting in empty menu item
                            raise RuntimeError
                        else:  # unrecognized layout; this will cause an exception
                            raise Exception(f'Invalid page layout: {str(layout_spec)}')

                    # regular layouts and curve-extracted layouts are indicated by tuple
                    elif isinstance(layout_spec, tuple):
                        if layout_spec[0] in ['layout_name', 'layout']:
                            if layout_spec[0] == 'layout_name':
                                # get a configured layout by name
                                layout = layouts.get_layout(layout_spec[1])
                            else:
                                # it's already a valid layout
                                layout = layout_spec[1]
                            # plot according to layout
                            figdata = plot_trials(
                                trials_dyn,
                                layout,
                                model_normaldata=model_normaldata,
                                max_cycles=max_cycles,
                                emg_mode=emg_mode,
                                legend_type=legend_type,
                                style_by=style_by,
                                color_by=color_by,
                                supplementary_data=supplementary_default,
                                big_fonts=True,
                            )
                        elif layout_spec[0] == 'curve_extracted':
                            the_vardefs = vardefs_dict[layout_spec[1]]
                            figdata = plot_extracted_box(curve_vals, the_vardefs)
                        else:
                            raise Exception(f'Invalid page layout: {str(layout_spec)}')
                    else:
                        raise Exception(f'Invalid page layout: {str(layout_spec)}')

                # save the newly created data
                if not saved_report_data:
                    if isinstance(figdata, go.Figure):
                        # serialize go.Figures before saving
                        # this makes them much faster for pickle to handle
                        # apparently dcc.Graph can eat the serialized json directly,
                        # so no need to do anything on load
                        figdata_ = figdata.to_plotly_json()
                    else:
                        figdata_ = figdata
                    report_data_new[page_label] = figdata_

                # make the upper and lower panel graphs from figdata, depending
                # on data type
                def _is_base64(s):
                    """Test for valid base64 encoding"""
                    try:
                        return base64.b64encode(base64.b64decode(s)) == s
                    except Exception:
                        return False

                # this is for old style timedist figures that were in base64
                # encoded svg
                if layout_spec == 'time_dist' and _is_base64(figdata):
                    graph_upper = html.Img(
                        src=f'data:image/svg+xml;base64,{figdata}',
                        id='gaitgraph%d' % k,
                        style={'height': '100%'},
                    )
                    graph_lower = html.Img(
                        src=f'data:image/svg+xml;base64,{figdata}',
                        id='gaitgraph%d' % (len(page_layouts) + k),
                        style={'height': '100%'},
                    )
                elif layout_spec == 'patient_info':
                    graph_upper = dcc.Markdown(figdata)
                    graph_lower = graph_upper
                else:
                    # plotly fig -> dcc.Graph
                    graph_upper = dcc.Graph(
                        figure=figdata, id='gaitgraph%d' % k, style={'height': '100%'}
                    )
                    graph_lower = dcc.Graph(
                        figure=figdata,
                        id='gaitgraph%d' % (len(page_layouts) + k),
                        style={'height': '100%'},
                    )
                dd_opts_multi_upper.append({'label': page_label, 'value': graph_upper})
                dd_opts_multi_lower.append({'label': page_label, 'value': graph_lower})

            except (RuntimeError, GaitDataError) as e:  # could not create a figure
                logger.warning(f'failed to create figure for {page_label}: {e}')
                # insert the menu options but make them disabled
                dd_opts_multi_upper.append(
                    {'label': page_label, 'value': page_label, 'disabled': True}
                )
                dd_opts_multi_lower.append(
                    {'label': page_label, 'value': page_label, 'disabled': True}
                )
                continue

        opts_multi, mapper_multi_upper = _make_dropdown_lists(dd_opts_multi_upper)
        opts_multi, mapper_multi_lower = _make_dropdown_lists(dd_opts_multi_lower)

        # if plots were newly created, save them to disk
        if not saved_report_data:
            logger.debug(f'saving report data into {data_fn}')
            signals.progress.emit('Saving report data to disk...', 99)
            with open(data_fn, 'wb') as f:
                pickle.dump(report_data_new, f, protocol=-1)

    def make_left_panel(split=True, upper_value='Kinematics', lower_value='Kinematics'):
        """Helper to make the left graph panels. If split=True, make two stacked panels"""

        # the upper graph & dropdown
        items = [
            dcc.Dropdown(
                id='dd-vars-upper-multi',
                clearable=False,
                options=opts_multi,
                value=upper_value,
            ),
            html.Div(
                id='div-upper', style={'height': '50%'} if split else {'height': '100%'}
            ),
        ]

        if split:
            # add the lower one
            items.extend(
                [
                    dcc.Dropdown(
                        id='dd-vars-lower-multi',
                        clearable=False,
                        options=opts_multi,
                        value=lower_value,
                    ),
                    html.Div(id='div-lower', style={'height': '50%'}),
                ]
            )

        return html.Div(items, style={'height': '80vh'})

    # create the app
    app = dash.Dash('gaitutils')
    # use local packaged versions of JavaScript libs etc. (no internet needed)
    app.css.config.serve_locally = True
    app.scripts.config.serve_locally = True
    app.title = _report_name(sessions, long_name=False)

    # this is for generating the classnames in the CSS
    num2words = {
        1: 'one',
        2: 'two',
        3: 'three',
        4: 'four',
        5: 'five',
        6: 'six',
        7: 'seven',
        8: 'eight',
        9: 'nine',
        10: 'ten',
        11: 'eleven',
        12: 'twelve',
    }
    classname_left = f'{num2words[LEFT_WIDTH]} columns'
    classname_right = f'{num2words[12 - LEFT_WIDTH]} columns'

    if video_only:
        app.layout = html.Div(
            [  # row
                html.Div(
                    [  # single main div
                        dcc.Dropdown(
                            id='dd-camera',
                            clearable=False,
                            options=opts_cameras,
                            value='Front camera',
                        ),
                        dcc.Dropdown(
                            id='dd-video-tag',
                            clearable=False,
                            options=opts_tags,
                            value=opts_tags[0]['value'],
                        ),
                        html.Div(id='videos'),
                    ],
                    className='12 columns',
                ),
            ],
            className='row',
        )
    else:  # the two-panel layout with graphs and video
        app.layout = html.Div(
            [  # row
                html.Div(
                    [  # left main div
                        html.H6(report_name),
                        dcc.Checklist(
                            id='split-left',
                            options=[{'label': 'Two panels', 'value': 'split'}],
                            value=[],
                        ),
                        # need split=True so that both panels are in initial layout
                        html.Div(make_left_panel(split=True), id='div-left-main'),
                    ],
                    className=classname_left,
                ),
                html.Div(
                    [  # right main div
                        dcc.Dropdown(
                            id='dd-camera',
                            clearable=False,
                            options=opts_cameras,
                            value='Front camera',
                        ),
                        dcc.Dropdown(
                            id='dd-video-tag',
                            clearable=False,
                            options=opts_tags,
                            value=opts_tags[0]['value'],
                        ),
                        html.Div(id='videos'),
                    ],
                    className=classname_right,
                ),
            ],
            className='row',
        )

        @app.callback(
            Output('div-left-main', 'children'),
            [Input('split-left', 'value')],
            [State('dd-vars-upper-multi', 'value')],
        )
        def update_panel_layout(split_panels, upper_value):
            split = 'split' in split_panels
            return make_left_panel(split, upper_value=upper_value)

        @app.callback(
            Output('div-upper', 'children'), [Input('dd-vars-upper-multi', 'value')]
        )
        def update_contents_upper_multi(sel_var):
            return mapper_multi_upper[sel_var]

        @app.callback(
            Output('div-lower', 'children'), [Input('dd-vars-lower-multi', 'value')]
        )
        def update_contents_lower_multi(sel_var):
            return mapper_multi_lower[sel_var]

    def _video_elem(title, url, max_height):
        """Create a video element with title"""
        if not url:
            return 'No video found'
        vid_el = html.Video(
            src=url,
            controls=True,
            loop=True,
            preload='auto',
            title=title,
            style={'max-height': max_height, 'max-width': '100%'},
        )
        # return html.Div([title, vid_el])  # titles above videos
        return vid_el

    @app.callback(
        Output('videos', 'children'),
        [Input('dd-camera', 'value'), Input('dd-video-tag', 'value')],
    )
    def update_videos(camera_label, tag):
        """Create a list of video divs according to camera and tag selection"""
        if tag == 'no videos':
            return 'No videos found'
        vid_urls_ = vid_urls[tag][camera_label]
        if not vid_urls_:
            return 'No videos found'
        nvids = len(vid_urls_)
        max_height = str(int(VIDS_TOTAL_HEIGHT / nvids)) + 'vh'
        return [_video_elem('video', url, max_height) for url in vid_urls_]

    # add a static route to serve session data. be careful outside firewalls
    @app.server.route('/static/<resource>')
    def serve_file(resource):
        for session in sessions:
            filepath = session / resource
            if filepath.is_file():
                return flask.send_from_directory(str(session), resource)
        return None

    # add shutdown method - see http://flask.pocoo.org/snippets/67/
    @app.server.route('/shutdown')
    def shutdown():
        logger.debug('Received shutdown request...')
        _shutdown_server()
        return 'Server shutting down...'

    # inject some info of our own
    app._gaitutils_report_name = report_name

    # XXX: the Flask app ends up with a logger by the name of 'gaitutils', which has a default
    # stderr handler. since logger hierarchy corresponds to package hierarchy,
    # this creates a bug where all gaitutils package loggers propagate their messages into
    # the app logger and they get shown multiple times. as a dirty fix, we disable the
    # handlers for the app logger (they still get shown since they propagate to the root logger)
    app.logger.handlers = []

    return app
