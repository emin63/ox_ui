"""Tools to convert click commands to flask WTForms
"""

import typing
import datetime
import tempfile
import re
import logging
import pathlib
import os

from flask import render_template, make_response, request
from flask_wtf import FlaskForm

from jinja2 import Environment, BaseLoader

from click import types


from wtforms import widgets
from wtforms import BooleanField, StringField, IntegerField, FileField, Field

from ox_ui import core as ox_ui_core
from ox_ui.assets import css


class FileResponseTweak:

    def __init__(self, arg_name, split_char='_', **mktemp_kwargs):
        self.arg_name = arg_name
        self.split_char = split_char
        self.mktemp_kwargs = dict(mktemp_kwargs)
        self.file_name = None

    def gobble(self, cmd, name):
        dummy = cmd
        return name == self.arg_name

    def pad_kwargs(self, cmd, kwargs):
        dummy = cmd
        self.file_name = tempfile.mktemp(**self.mktemp_kwargs)
        kwargs[self.arg_name] = self.file_name

    def name(self):
        return self.__class__.__name__

    def post_process_result(self, cmd, result):
        logging.debug('Tweak %s ignores previous result of %s', self.name(),
                      result)
        response = None
        # Use binary mode below to get consistent behaviour across platforms
        with open(self.file_name, 'rb') as my_fd:
            suffix = self.mktemp_kwargs.get('suffix', None)
            if suffix and self.split_char and self.split_char in suffix:
                fname = self.file_name.split(self.split_char)[-1]
            else:
                fname = self.file_name
            response = make_response(my_fd.read())
            response.headers.set('Content-Type', 'application/octest-stream')
            response.headers.set(
                'Content-Disposition', 'attachment', filename=fname)

        if os.path.exists(self.file_name):
            logging.info('Removing temporary file %s', self.file_name)
            os.remove(self.file_name)
        return response


class DateTimeFieldTweak(Field):
    """Ttext field which stores a `datetime.datetime` from multiple formats.
    """
    widget = widgets.TextInput()

    def __init__(self, label=None, validators=None,
                 formats=('%Y-%m-%d %H:%M:%S', '%Y-%m-%d'), **kwargs):
        super(DateTimeFieldTweak, self).__init__(label, validators, **kwargs)
        self.formats = formats

    def _value(self):
        if self.raw_data:
            return ' '.join(self.raw_data)
        return self.data and self.data.strftime(self.formats[0]) or ''

    def process_formdata(self, valuelist):
        if valuelist:
            date_str = ' '.join(valuelist)
            for fmt in self.formats:
                try:
                    self.data = datetime.datetime.strptime(date_str, fmt)
                    return
                except ValueError:
                    self.data = None
        raise ValueError(self.gettext('Not a valid datetime value'))


class ClickToWTF:

    def __init__(self, clickCmd, skip_opt_re=None, tweaks: list = None,
                 fill_get_from_url: bool = False):
        """Initializer.
        
        :param clickCmd:   A click command to turn into a form.
        
        :param skip_opt_re=None:  Regexp for options to skip in form.
        
        :param tweaks:  List of 'tweaks' to add (e.g., FileResponseTweak).
        
        :param fill_get_from_url:  If True, then when doing a GET request
                                   to fill out arguments we look in the
                                   URL params to override standard defaults.
        
        """
        self.clickCmd = clickCmd
        self.rawTemplate = None
        self.skip_opt_re = skip_opt_re if not skip_opt_re else re.compile(
            skip_opt_re)
        self.tweaks = tweaks if tweaks else []
        self.fill_get_from_url = fill_get_from_url
        self.gobbled_opts = {}

    def form_cls(self):

        class ClickForm(FlaskForm):
            """Form to run Click command.
            """

        for opt in self.clickCmd.params:
            if self.skip_opt_re and self.skip_opt_re.search(opt.name):
                logging.info('Option %s since matchs skip_opt_re', opt.name)
            elif self.gobble(opt.name):
                logging.info('Option %s gobbled', opt.name)
            else:
                field = self.click_opt_to_wtf_field(opt)
                setattr(ClickForm, opt.name, field)

        if request.method == 'GET' and self.fill_get_from_url:
            self.populate_form_from_url(ClickForm)
            
        return ClickForm

    def populate_form_from_url(self, ClickForm):
        """Populate a form from URL parameters.

        :param ClickForm:  Class form click form as produced by `form_cls`.

        ~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-

        PURPOSE:  Go through parameters for command and try to get default
                  values from URL parameters and put them into `ClickForm`.
                  This is useful for dynamically sending users to a command
                  with special default values.

                  This is mainly intended to be called from form_cls
                  only if self.fill_get_from_url is True because it is
                  a minor security risk. A malicious actor could send
                  the user to the form with malicious defaults so we only
                  do this if fill_get_from_url is set to True or if this is
                  explicitly called by a "safe" command.
        """
        logging.debug('Populating form %s from URL', ClickForm)
        for opt in self.clickCmd.params:
            url_value = request.args.get(opt.name, None)
            if url_value is not None:
                field = getattr(ClickForm, opt.name)
                field.kwargs['default'] = url_value

    def gobble(self, name):
        for tweak in self.tweaks:
            reason = tweak.gobble(self, name)
            if reason:
                self.gobbled_opts[name] = reason
                return reason
        return None

    def pad_kwargs(self, kwargs):
        for tweak in self.tweaks:
            tweak.pad_kwargs(self, kwargs)

    def form(self):
        cls = self.form_cls()
        return cls()

    def click_opt_to_wtf_field(self, opt):
        if opt.type == types.INT:
            field = IntegerField(opt.name, validators=[], description=str(
                opt.help), default=opt.default)
        elif opt.type == types.STRING:
            field = StringField(opt.name, validators=[], description=str(
                opt.help), default=opt.default)
        elif isinstance(opt.type, types.DateTime) or (
                getattr(opt.type, 'name', '?') == 'datetime'):
            kwargs = {}
            if hasattr(opt, 'foramts'):
                kwargs['formats'] = opt.formats
            default = opt.default
            if callable(default):
                default = default()
            if isinstance(default, str):
                default = datetime.datetime.strptime(
                    default, '%Y-%m-%d %H:%M:%S')

            field = DateTimeFieldTweak(
                opt.name, validators=[], description=str(opt.help),
                default=default, **kwargs)
        elif isinstance(opt.type, types.File):
            if opt.type.mode in ('r', 'rb'):  # file to read
                field = FileField(opt.name, validators=[], description=str(
                    opt.help))
        else:
            raise TypeError('Cannot represent click type %s in WTF' % (
                opt.type))
        return field

    def process(self, form):
        kwargs = {}
        for opt in self.clickCmd.params:
            if opt.name in self.gobbled_opts:
                continue
            value = getattr(form, opt.name).data
            if opt.multiple:
                cur = kwargs.get(opt.name, [])
                if not cur:
                    kwargs[opt.name] = cur
                cur.append(value)
            else:
                kwargs[opt.name] = value

        self.pad_kwargs(kwargs)
        wrapped_cmd = getattr(self.clickCmd.callback, '__wrapped__', None)
        if wrapped_cmd:
            raw_result = wrapped_cmd(**kwargs)
        else:
            raw_result = self.clickCmd.callback(**kwargs)
        result = raw_result
        for tweak in self.tweaks:
            result = tweak.post_process_result(self, result)

        return result

    def post_process_result(self, raw_result):
        return raw_result

    def get_form_data(self) -> typing.Dict[str, typing.Any]:
        """Return dictionary of form data for show_form method.

This method returns a dictionary with string keys that will be used by
the `show_form` method in rendering the form for the command.

The following are provided by default:

  - intro:  Text from the `help` variable of the command.

A standard pattern is to override this via something like

        def get_form_data(self):
            data = super().get_form_data()
            data['my_custom_data'] = SOMETHING
            return data

        """
        return {'intro': self.clickCmd.help}
        
    def show_form(self, form=None, template=None):
        if form is None:
            form = self.form()
        data = self.get_form_data()
        if template:
            result = render_template(template, form=form, **data)
        else:
            result = self.simple_render(form, **data)
        return result

    def simple_render(self, form, **data):
        if not self.rawTemplate:
            tpath = pathlib.Path(ox_ui_core.__file__).parent.joinpath(
                'sample_wtf.html')
            cpath = pathlib.Path(css.__file__).parent.joinpath('w3.css')
            self.rawTemplate = '<style>\n%s</style>\n%s\n' % (
                open(cpath).read(), open(tpath).read())
        rtemplate = Environment(loader=BaseLoader()).from_string(
            self.rawTemplate)
        result = rtemplate.render(form=form, **data)
        return result

    def handle_request(self):
        form = self.form()
        if form.validate_on_submit():
            result = self.process(form)
            return result
        logging.debug('Showing form either because invalid or first view')
        return self.show_form(form=form)
