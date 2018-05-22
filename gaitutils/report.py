# -*- coding: utf-8 -*-
"""
Reporting functions, WIP

@author: Jussi (jnu@iki.fi)
"""


import dash
import dash_core_components as dcc
import dash_html_components as html
from dash.dependencies import Input, Output, State
import plotly.tools
import flask
import plotly
import plotly.graph_objs as go
import numpy as np
from itertools import cycle
from collections import OrderedDict
import logging
import os.path as op
import os
import subprocess
import ctypes
import base64
import datetime
import io

import gaitutils
from gaitutils import cfg, normaldata, models, layouts, GaitDataError
from gaitutils.nexus import find_tagged


logger = logging.getLogger(__name__)


def convert_videos(vidfiles, check_only=False, prog_callback=None):
    """Convert video files using command and options defined in cfg.
    If check_only, return whether files were already converted.
    During conversion, prog_callback will be called with % of task done
    and name of current video file"""
    CONV_EXT = '.ogv'  # extension for converted files
    if not isinstance(vidfiles, list):
        vidfiles = [vidfiles]
    convfiles = {vidfile: op.splitext(vidfile)[0] + CONV_EXT for vidfile
                 in vidfiles}
    converted = [op.isfile(fn) for fn in convfiles.values()]  # already done
    if check_only:
        return all(converted)

    # XXX: this disables Windows protection fault dialogs
    # needed since ffmpeg2theora may crash after conversion is complete (?)
    SEM_NOGPFAULTERRORBOX = 0x0002  # From MSDN
    ctypes.windll.kernel32.SetErrorMode(SEM_NOGPFAULTERRORBOX)

    vidconv_bin = cfg.general.videoconv_path
    vidconv_opts = cfg.general.videoconv_opts
    if not (op.isfile(vidconv_bin) and os.access(vidconv_bin, os.X_OK)):
        raise ValueError('Invalid video converter executable: %s'
                         % vidconv_bin)

    n_to_conv = len(vidfiles) - converted.count(True)
    k = 0
    for vidfile, convfile in convfiles.items():
        if not op.isfile(convfile):
            if prog_callback is not None:
                prog_callback(100*k/n_to_conv, vidfile)
            # XXX could parallelize with non-blocking Popen() calls?
            subprocess.call([vidconv_bin]+vidconv_opts.split()+[vidfile],
                            stdout=None, creationflags=0x08000000)  # NO_WINDOW flag
            k += 1
    return convfiles.values()


def _make_dropdown_lists(options):
    """This takes a list of label/value dicts (with arbitrary type values)
    and returns list and dict. Needed since dcc.Dropdown can only take str
    values. identity is fed to dcc.Dropdown() and mapper is used for getting
    the actual values at the callback."""
    identity = list()
    mapper = dict()
    for option in options:
        di = {'label': option['label'], 'value': option['label']}
        if 'disabled' in option and option['disabled']:
            di['disabled'] = True
        identity.append(di)
        mapper[option['label']] = option['value']
    return identity, mapper


def _plotly_fill_between(x, ylow, yhigh, **kwargs):
    """Fill area between ylow and yhigh"""
    x_ = np.concatenate([x, x[::-1]])  # construct a closed curve
    y_ = np.concatenate([yhigh, ylow[::-1]])
    return go.Scatter(x=x_, y=y_, fill='toself', **kwargs)


def _var_title(var):
    """Get proper title for variable"""
    mod = models.model_from_var(var)
    if mod:
        if var in mod.varlabels_noside:
            return mod.varlabels_noside[var]
        elif var in mod.varlabels:
            return mod.varlabels[var]
    elif var in cfg.emg.channel_labels:
        return cfg.emg.channel_labels[var]
    else:
        return ''


def _truncate_trialname(trialname):
    """Remove leading date string from trial names, e.g. 2018_5_12"""
    tn_split = trialname.split('_')
    datetxt = '-'.join(tn_split[:3])
    try:
        datetime.datetime.strptime(datetxt, '%Y-%m-%d')
        return '%s' % '_'.join(tn_split[3:])
    except ValueError:
        return trialname


def _plot_trials(trials, layout, model_normaldata, legend_type='tag_only',
                 trial_linestyles='same'):
    """Make a plotly plot of layout, including given trials.

    trials: list of gaitutils.Trial instances
    layout: list of lists defining plot layout (see plot.py)
    model_normaldata: dict of normal data for model variables
    legend_type: 'tag_only' for Eclipse tag, 'name_with_tag' or 'full'
    trial_linestyles: 'same' for all identical, 'trial' for trial specific
                      style, 'session' for session specific style
    """

    # configurabe opts (here for now)
    label_fontsize = 12  # x, y labels
    subtitle_fontsize = 12  # subplot titles

    nrows = len(layout)
    ncols = len(layout[0])

    if len(trials) > len(plotly.colors.DEFAULT_PLOTLY_COLORS):
        logger.warning('Not enough colors for plot')
    colors = cycle(plotly.colors.DEFAULT_PLOTLY_COLORS)

    allvars = [item for row in layout for item in row]
    titles = [_var_title(var) for var in allvars]
    fig = plotly.tools.make_subplots(rows=nrows, cols=ncols,
                                     subplot_titles=titles)
    tracegroups = set()
    model_normaldata_legend = True
    emg_normaldata_legend = True

    session_linestyles = dict()
    dash_styles = cycle(['solid', 'dash', 'dot', 'dashdot'])

    for trial in trials:
        trial_color = colors.next()

        for context in ['R', 'L']:
            # FIXME: hardcoded to 1st cycle
            cycle_ind = 1
            cyc = trial.get_cycle(context, cycle_ind)
            trial.set_norm_cycle(cyc)

            for i, row in enumerate(layout):
                for j, var in enumerate(row):
                    plot_ind = i * ncols + j + 1  # plotly subplot index
                    xaxis = 'xaxis%d' % plot_ind  # name of plotly xaxis
                    yaxis = 'yaxis%d' % plot_ind  # name of plotly yaxis

                    # in legend, traces will be grouped according to tracegroup (which is also the label)
                    if legend_type == 'name_with_tag':
                        tracegroup = '%s / %s' % (trial.trialname,
                                                  trial.eclipse_tag)
                    elif legend_type == 'short_name_with_tag':
                        tracegroup = '%s / %s' % (_truncate_trialname(trial.trialname),
                                                  trial.eclipse_tag)
                    elif legend_type == 'tag_only':
                        tracegroup = trial.eclipse_tag
                    elif legend_type == 'full':  # inc cycle 
                        raise Exception('not implemented yet')
                        #tracegroup = '%s / %s' % (trial.name_with_description,
                        #                          cycle_desc[context])
                    else:
                        raise ValueError('Invalid legend type')

                    # only show the legend for the first trace in the tracegroup, so we do not repeat legends
                    show_legend = tracegroup not in tracegroups

                    mod = models.model_from_var(var)
                    if mod:  # plot model variable
                        do_plot = True
                        
                        if var in mod.varnames_noside:
                            var = context + var

                        if mod.is_kinetic_var(var) and not cyc.on_forceplate:
                            do_plot = False

                        if do_plot:
                            t, y = trial[var]

                            if trial_linestyles == 'trial':
                                # trial specific color, left side dashed
                                line = {'color': trial_color}
                                if context == 'L':
                                    line['dash'] = 'dash'
                            elif trial_linestyles == 'same':
                                # identical color for all trials
                                line = {'color':
                                        cfg.plot.model_tracecolors[context]}
                            elif trial_linestyles == 'session':
                                # session specific line style
                                line = {'color':
                                        cfg.plot.model_tracecolors[context]}
                                if trial.sessiondir in session_linestyles:
                                    dash_style = session_linestyles[trial.sessiondir]
                                else:
                                    dash_style = dash_styles.next()
                                    session_linestyles[trial.sessiondir] = dash_style
                                line['dash'] = dash_style

                            trace = go.Scatter(x=t, y=y, name=tracegroup,
                                               legendgroup=tracegroup,
                                               showlegend=show_legend,
                                               line=line)

                            tracegroups.add(tracegroup)
                            fig.append_trace(trace, i+1, j+1)

                        # last model trace was plotted
                        # FIXME: is this logic also working for EMG?
                        if trial == trials[-1] and context == 'L':
                            # plot model normal data
                            if var[0].upper() in ['L', 'R']:
                                nvar = var[1:]
                            if model_normaldata and nvar in model_normaldata:
                                key = nvar
                            else:
                                key = None
                            ndata = (model_normaldata[key] if key in
                                     model_normaldata else None)
                            if ndata is not None:
                                # FIXME: hardcoded color
                                normalx = np.linspace(0, 100, ndata.shape[0])
                                ntrace = _plotly_fill_between(normalx,
                                                              ndata[:, 0],
                                                              ndata[:, 1],
                                                              fillcolor='rgba(100, 100, 100, 0.3)',
                                                              name='Norm.',
                                                              legendgroup='Norm.',
                                                              showlegend=model_normaldata_legend,
                                                              line=go.Line(color='transparent'))
                                fig.append_trace(ntrace, i+1, j+1)
                                model_normaldata_legend = False  # add to legend only once

                            # rm x tick labels, plot too crowded
                            fig['layout'][xaxis].update(showticklabels=False)
                            # LaTeX does not render, so rm units from ylabel
                            ylabel = ' '.join(mod.ylabels[var].split(' ')[k]
                                              for k in [0, -1])
                            fig['layout'][yaxis].update(title=ylabel, titlefont={'size': label_fontsize})


                    # plot EMG variable
                    elif (trial.emg.is_channel(var) or var in
                          cfg.emg.channel_labels):
                        do_plot = True
                        # plot only if EMG channel context matches cycle ctxt
                        if var[0] != context:
                            do_plot = False
                        t, y = trial[var]
                        if not trial.emg.status_ok(var):
                            do_plot = False
                            # FIXME: maybe annotate disconnected chans
                            # _no_ticks_or_labels(ax)
                            # _axis_annotate(ax, 'disconnected')
                        if do_plot:
                            line = {'width': 1, 'color': trial_color}
                            y *= 1e3  # plot mV
                            trace = go.Scatter(x=t, y=y, name=tracegroup,
                                               legendgroup=tracegroup,
                                               showlegend=show_legend,
                                               line=line)
                            tracegroups.add(tracegroup)
                            fig.append_trace(trace, i+1, j+1)

                        # last trace was plotted
                        if trial == trials[-1] and context == 'L':
                            # plot EMG normal bars
                            if var in cfg.emg.channel_normaldata:
                                emgbar_ind = cfg.emg.channel_normaldata[var]
                                for inds in emgbar_ind:
                                    # FIXME: hardcoded color
                                    # NOTE: using big values (>~1e3) for the normal bar height triggers a plotly bug
                                    # and screws up the normal bars (https://github.com/plotly/plotly.py/issues/1008)
                                    ntrace = _plotly_fill_between(inds, [-1e1]*2, [1e1]*2,  # simulate x range fill by high y values
                                                                  name='EMG norm.',
                                                                  legendgroup='EMG norm.',
                                                                  showlegend=emg_normaldata_legend,
                                                                  fillcolor='rgba(255, 0, 0, 0.3)',
                                                                  line=go.Line(color='transparent'))                                                                  
                                    fig.append_trace(ntrace, i+1, j+1)
                                    emg_normaldata_legend = False  # add to legend only once
                        
                            emg_yrange = np.array([-cfg.plot.emg_yscale, cfg.plot.emg_yscale]) * cfg.plot.emg_multiplier
                            fig['layout'][yaxis].update(title=cfg.plot.emg_ylabel, titlefont={'size': label_fontsize},
                                                        range=emg_yrange)  # FIXME: cfg
                            # prevent changes due to legend clicks etc.
                            fig['layout'][xaxis].update(range=[0, 100])
                            # rm x tick labels, plot too crowded
                            fig['layout'][xaxis].update(showticklabels=False)

                    elif var is None:
                        continue

                    elif 'legend' in var:  # 'legend' is for mpl plotter only
                        continue

                    else:
                        raise Exception('Unknown variable %s' % var)

    # reduce subplot title font size
    for anno in fig['layout']['annotations']:
        anno['font']['size'] = subtitle_fontsize

    # put x labels on last row only, re-enable tick labels for last row
    inds_last = range((nrows-1)*ncols, nrows*ncols)
    axes_last = ['xaxis%d' % (ind+1) for ind in inds_last]
    for ax in axes_last:
        fig['layout'][ax].update(title='% of gait cycle',
                                 titlefont={'size': label_fontsize},
                                 showticklabels=True)

    margin = go.Margin(l=50, r=0, b=50, t=50, pad=4)  # NOQA: 741
    layout = go.Layout(legend=dict(x=100, y=.5), margin=margin,
                       font={'size': label_fontsize},
                       hovermode='closest')

    fig['layout'].update(layout)
    return fig


def _time_dist_plot(c3ds, sessions):
    cond_labels = [op.split(session)[-1] for session in sessions]
    fig = gaitutils.nexus_time_distance_vars._plot_trials(c3ds, cond_labels)
    buf = io.BytesIO()
    fig.savefig(buf, format='svg', bbox_inches='tight')
    buf.seek(0)
    return buf


def dash_report(sessions=None, tags=None):
    """Multisession dash app"""

    # relative width of left panel (1-12)
    # 3-session comparison uses narrower video panel
    # LEFT_WIDTH = 8 if len(sessions) == 3 else 7
    LEFT_WIDTH = 8
    VIDS_TOTAL_HEIGHT = 88  # % of browser window height
    camera_labels = cfg.general.camera_labels.values()

    if not sessions:
        return None

    if len(sessions) < 1 or len(sessions) > 3:
        raise ValueError('Need a list of one to three sessions')

    is_comparison = len(sessions) > 1

    sessions_str = ' / '.join([op.split(s)[-1] for s in sessions])
    report_type = ('Single session report:' if len(sessions) == 1
                   else 'Comparison report:')
    report_name = '%s %s' % (report_type, sessions_str)

    if tags is None:
        # if doing a comparison, pick representative trials only
        tags = (cfg.plot.eclipse_repr_tags if is_comparison else
                cfg.plot.eclipse_tags)

    # load the trials
    trials = list()
    c3ds_all = list()
    for session in sessions:
        c3ds = find_tagged(sessionpath=session, tags=tags)
        c3ds_all.append(c3ds)
        # for comparison, require that correct number of trials is found
        if is_comparison and len(c3ds) != len(tags):
            raise ValueError('Expected %d tagged trials for session %s'
                             % (len(tags), session))
        trials_this = [gaitutils.Trial(c3d) for c3d in c3ds]
        trials.extend(trials_this)
    trials = sorted(trials, key=lambda tr: tr.eclipse_tag)
    if not any(c3ds_all):
        return None

    # load normal data for gait models
    model_normaldata = dict()
    for fn in cfg.general.normaldata_files:
        ndata = normaldata.read_normaldata(fn)
        model_normaldata.update(ndata)

    # create directory of trial videos for each tag and camera selection
    vid_urls = dict()
    for tag in tags:
        vid_urls[tag] = dict()
        for camera_label in camera_labels:

            tagged = [tr for tr in trials if tag == tr.eclipse_tag]
            vid_files = [tr.get_video_by_label(camera_label, ext='ogv')
                         for tr in tagged]
            vid_urls[tag][camera_label] = dict()
            vid_urls[tag][camera_label] = ['/static/%s' % op.split(fn)[1] if fn
                                           else None for fn in vid_files]

    # add videos from static trials; works a bit differently since st. trials
    # are not loaded
    vid_urls['Static'] = dict()
    static_c3ds = list()
    for session in sessions:
        static_this = gaitutils.nexus.find_tagged(['Static'], ['TYPE'],
                                                  session)
        if static_this:
            static_c3ds.append(static_this[-1])  # pick the last static trial

    for camera_id, camera_label in cfg.general.camera_labels.items():
        vid_urls['Static'][camera_label] = list()
        for static_c3d in static_c3ds:
            vid_files = gaitutils.nexus.find_trial_videos(static_c3d,
                                                          'ogv', camera_id)
            vid_urls['Static'][camera_label].extend(['/static/%s' % op.split(fn)[1] for fn in vid_files])

    # build dcc.Dropdown options list for the cameras and tags
    opts_cameras = list()
    for label in set(camera_labels):
        opts_cameras.append({'label': label, 'value': label})
    opts_tags = list()
    for tag in tags:
        if any([vid_urls[tag][camera_label] for camera_label in camera_labels]):
            opts_tags.append({'label': '%s' % tag, 'value': tag})
    if any([vid_urls['Static'][camera_label] for camera_label in camera_labels]):
        opts_tags.append({'label': 'Static', 'value': 'Static'})

    # build dcc.Dropdown options list for the trials
    trials_dd = list()
    for tr in trials:
        trials_dd.append({'label': tr.name_with_description,
                          'value': tr.trialname})
    # precreate graphs
    emgs = [tr.emg for tr in trials]
    emg_layout = layouts.rm_dead_channels_multitrial(emgs, cfg.layouts.std_emg)
    _layouts = OrderedDict([
            ('Kinematics', cfg.layouts.lb_kinematics),
            ('Kinematics + kinetics', cfg.layouts.lb_kin_web),
            ('Kinetics', cfg.layouts.lb_kinetics_web),
            ('EMG', emg_layout),
            ('Kinetics-EMG left', cfg.layouts.lb_kinetics_emg_l),
            ('Kinetics-EMG right', cfg.layouts.lb_kinetics_emg_r),
            ('Muscle length', cfg.layouts.musclelen),
            ('Time-distance variables', 'time_dist'),
            ])

    # pick desired single variables from model and append
    pig_singlevars = sorted(models.pig_lowerbody.varlabels_noside.items(),
                            key=lambda item: item[1])
    singlevars = OrderedDict([(varlabel, [[var]]) for var, varlabel in
                              pig_singlevars])
    _layouts.update(singlevars)

    dd_opts_multi_upper = list()
    dd_opts_multi_lower = list()

    for k, (label, layout) in enumerate(_layouts.items()):
        logger.debug('creating plot for %s' % label)
        # for comparison report, include session info in plot legends and
        # use session specific line style
        trial_linestyles = 'session' if is_comparison else 'same'
        legend_type = 'short_name_with_tag' if is_comparison else 'tag_only'
        try:

            # special layout
            if isinstance(layout, basestring):
                if layout == 'time_dist':
                    buf = _time_dist_plot(c3ds_all, sessions)
                    encoded_image = base64.b64encode(buf.read())
                    graph_upper = html.Img(src='data:image/svg+xml;base64,{}'.
                                           format(encoded_image),
                                           id='gaitgraph%d' % k,
                                           style={'height': '100%'})
                    graph_lower = html.Img(src='data:image/svg+xml;base64,{}'.
                                           format(encoded_image),
                                           id='gaitgraph%d'
                                           % (len(_layouts)+k),
                                           style={'height': '100%'})

                elif layout == 'patient_info':
                    pass
                else:
                    raise ValueError('Invalid plot type')

            # regular gaitutils layout
            else:
                fig_ = _plot_trials(trials, layout, model_normaldata,
                                    legend_type=legend_type,
                                    trial_linestyles=trial_linestyles)
                graph_upper = dcc.Graph(figure=fig_, id='gaitgraph%d' % k,
                                        style={'height': '100%'})
                graph_lower = dcc.Graph(figure=fig_, id='gaitgraph%d'
                                        % (len(_layouts)+k),
                                        style={'height': '100%'})

            dd_opts_multi_upper.append({'label': label, 'value': graph_upper})
            dd_opts_multi_lower.append({'label': label, 'value': graph_lower})

        except GaitDataError:
            logger.warning('Failed to create plot for %s' % label)
            # insert the menu options but make them disabled
            dd_opts_multi_upper.append({'label': label, 'value': label,
                                        'disabled': True})
            dd_opts_multi_lower.append({'label': label, 'value': label,
                                        'disabled': True})
            continue

    opts_multi, mapper_multi_upper = _make_dropdown_lists(dd_opts_multi_upper)
    opts_multi, mapper_multi_lower = _make_dropdown_lists(dd_opts_multi_lower)

    def make_left_panel(split=True, upper_value='Kinematics',
                        lower_value='Kinematics'):
        """Make the left graph panels. If split, make two stacked panels"""

        # the upper graph & dropdown
        items = [
                    dcc.Dropdown(id='dd-vars-upper-multi', clearable=False,
                                 options=opts_multi,
                                 value=upper_value),

                    html.Div(id='div-upper', style={'height': '50%'}
                             if split else {'height': '100%'})
                ]

        if split:
            # add the lower one
            items.extend([
                            dcc.Dropdown(id='dd-vars-lower-multi',
                                         clearable=False,
                                         options=opts_multi,
                                         value=lower_value),

                            html.Div(id='div-lower', style={'height': '50%'})
                        ])

        return html.Div(items, style={'height': '80vh'})

    # create the app
    app = dash.Dash()

    # this is for generating the classnames in the CSS
    num2words = {1: 'one', 2: 'two', 3: 'three', 4: 'four', 5: 'five',
                 6: 'six', 7: 'seven', 8: 'eight', 9: 'nine', 10: 'ten',
                 11: 'eleven', 12: 'twelve'}
    classname_left = '%s columns' % num2words[LEFT_WIDTH]
    classname_right = '%s columns' % num2words[12-LEFT_WIDTH]

    app.layout = html.Div([  # row

            html.Div([  # left main div

                    html.H6(report_name),

                    dcc.Checklist(id='split-left',
                                  options=[{'label': 'Two panels',
                                            'value': 'split'}], values=[]),

                    # need split=True so that both panels are in initial layout
                    html.Div(make_left_panel(split=True), id='div-left-main')

                    ], className=classname_left),

            html.Div([  # right main div

                    dcc.Dropdown(id='dd-camera', clearable=False,
                                 options=opts_cameras,
                                 value='Front camera'),

                    dcc.Dropdown(id='dd-video-tag', clearable=False,
                                 options=opts_tags,
                                 value=opts_tags[0]['value']),

                    html.Div(id='videos'),

                    ], className=classname_right),

                     ], className='row')

    @app.callback(
            Output('div-left-main', 'children'),
            [Input('split-left', 'values')],
            [State('dd-vars-upper-multi', 'value')]
        )
    def update_panel_layout(split_panels, upper_value):
        split = 'split' in split_panels
        return make_left_panel(split, upper_value=upper_value)

    @app.callback(
            Output('div-upper', 'children'),
            [Input('dd-vars-upper-multi', 'value')]
        )
    def update_contents_upper_multi(sel_var):
        return mapper_multi_upper[sel_var]

    @app.callback(
            Output('div-lower', 'children'),
            [Input('dd-vars-lower-multi', 'value')]
        )
    def update_contents_lower_multi(sel_var):
        return mapper_multi_lower[sel_var]

    def _video_elem(title, url, max_height):
        """Create a video element with title"""
        if not url:
            return 'No video found for %s' % title
        vid_el = html.Video(src=url, controls=True, loop=True, preload='auto',
                            title=title, style={'max-height': max_height,
                                                'max-width': '100%'})
        # return html.Div([title, vid_el])  # titles above videos
        return vid_el

    @app.callback(
            Output('videos', 'children'),
            [Input('dd-camera', 'value'),
             Input('dd-video-tag', 'value')]
        )
    def update_videos(camera_label, tag):
        """Create a list of video divs according to camera and tag selection"""
        vid_urls_ = vid_urls[tag][camera_label]
        if not vid_urls_:
            return 'No videos found'
        nvids = len(vid_urls_)
        max_height = str(VIDS_TOTAL_HEIGHT / nvids) + 'vh'
        return [_video_elem('video', url, max_height) for url in vid_urls_]

    # add a static route to serve session data. be careful outside firewalls
    @app.server.route('/static/<resource>')
    def serve_file(resource):
        for session in sessions:
            filepath = op.join(session, resource)
            if op.isfile(filepath):
                return flask.send_from_directory(session, resource)
        return None

    # the 12-column external css
    # FIXME: local copy?
    app.css.append_css({
        'external_url': 'https://codepen.io/chriddyp/pen/bWLwgP.css'
    })

    return app