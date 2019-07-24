"""Tools to convert click commands to flask WTForms
"""

import tempfile
import re
import logging
import pathlib
import os

from flask import render_template, make_response
from flask_wtf import FlaskForm

from jinja2 import Environment, BaseLoader

from click import types
import click_datetime

from wtforms import BooleanField, StringField, IntegerField, DateTimeField

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
        logging.debug('Tweak %s ignores previous result of %s', self.name())
        response = None
        with open(self.file_name) as my_fd:
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


class ClickToWTF:

    def __init__(self, clickCmd, skip_opt_re=None, tweaks: list = None):
        self.clickCmd = clickCmd
        self.rawTemplate = None
        self.skip_opt_re = skip_opt_re if not skip_opt_re else re.compile(
            skip_opt_re)
        self.tweaks = tweaks if tweaks else []
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

        return ClickForm

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
        elif isinstance(opt.type, click_datetime.Datetime):
            field = DateTimeField(opt.name, validators=[], description=str(
                opt.help), default=opt.default)
        else:
            raise TypeError('Cannot represent click type %s in WTF' % (
                opt.type))
        return field

    def process(self, form):
        kwargs = {opt.name: getattr(form, opt.name).data
                  for opt in self.clickCmd.params
                  if opt.name not in self.gobbled_opts}
        self.pad_kwargs(kwargs)
        raw_result = self.clickCmd.callback(**kwargs)
        result = raw_result
        for tweak in self.tweaks:
            result = tweak.post_process_result(self, result)
        
        return result

    def post_process_result(self, raw_result):
        return raw_result

    def show_form(self, form=None, template=None):
        if form is None:
            form = self.form()
        data = {'intro': self.clickCmd.help}
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

