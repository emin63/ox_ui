"""Tools to convert click commands to generic command
"""

import datetime
import re
import logging

from click import types


class GenericField:
    """Class representing generic field similar to Flask WTForms
    """

    def __init__(self, name, validators=(), description=None,
                 type=str,  # pylint: disable=redefined-builtin
                 default=None):
        self.name = name
        self.validators = validators
        self.description = description
        self.data = default
        self.type = type


class ClickToGeneric:
    """Class to convert click command to generic command.
    """

    def __init__(self, click_cmd, skip_opt_re=None, tweaks: list = None):
        """Initializer.

        :param click_cmd:   A click command to turn into a form.

        :param skip_opt_re=None:  Regexp for options to skip in form.

        :param tweaks:  List of 'tweaks' to add (e.g., FileResponseTweak).

        """
        self.click_cmd = click_cmd
        self.skip_opt_re = skip_opt_re if not skip_opt_re else re.compile(
            skip_opt_re)
        self.tweaks = tweaks if tweaks else []
        self.gobbled_opts = {}

    def form_cls(self):
        """Create a class to represent the generic form for the command.
        """

        class GenericForm:
            """Generic form to run click command
            """

        for opt in self.click_cmd.params:
            if self.skip_opt_re and self.skip_opt_re.search(opt.name):
                logging.info('Option %s since matchs skip_opt_re', opt.name)
            elif self.gobble(opt.name):
                logging.info('Option %s gobbled', opt.name)
            else:
                field = self.click_opt_to_field(opt)
                setattr(GenericForm, opt.name, field)

        return GenericForm

    def gobble(self, name):
        """Remove options from standard processing.

This is needed so we can handle some special types of options differently.
        """
        for tweak in self.tweaks:
            reason = tweak.gobble(self, name)
            if reason:
                self.gobbled_opts[name] = reason
                return reason
        return None

    def pad_kwargs(self, kwargs):
        """Handle keyword args that need padding.
        """
        for tweak in self.tweaks:
            tweak.pad_kwargs(self, kwargs)

    def form(self, opts):
        """Create an instance of a generic form showing command arguments.
        """
        cls = self.form_cls()
        my_form = cls()
        for name, value in opts.items():
            if value is not None:
                field = getattr(my_form, name)
                field.data = value

        return my_form

    def make_int_field(self, opt):
        "Make an integer field."

        logging.debug('Making field for %s', self)
        return GenericField(
            opt.name, description=opt.help, default=opt.default,
            type=int)

    def make_bool_field(self, opt):
        "Make a bool field."

        logging.debug('Making field for %s', self)
        return GenericField(
            opt.name, description=opt.help, default=opt.default,
            type=bool)

    def make_str_field(self, opt):
        "Make a string field."

        logging.debug('Making field for %s', self)
        return GenericField(
            opt.name, description=opt.help, default=opt.default,
            type=int)

    def make_dt_field(self, opt):
        "Make a datetime field."

        logging.debug('Making field for %s', self)
        default = opt.default
        if callable(default):
            default = default()
        if isinstance(default, str):
            default = datetime.datetime.strptime(
                default, '%Y-%m-%d %H:%M:%S')

        return GenericField(
            opt.name, description=opt.help, default=default,
            type=datetime.datetime)

    def make_file_field(self, opt):
        "Make a file field."

        logging.debug('Making field for %s', self)
        return GenericField(opt.name, description=opt.help,
                            default=opt.default)

    def click_opt_to_field(self, opt):
        """Convert given click option to a GenericField.
        """
        if opt.type == types.INT:
            field = self.make_int_field(opt)
        elif opt.type == types.BOOL:
            field = self.make_bool_field(opt)
        elif opt.type == types.STRING:
            field = self.make_str_field(opt)
        elif isinstance(opt.type, types.DateTime) or (
                getattr(opt.type, 'name', '?') == 'datetime'):
            field = self.make_dt_field(opt)
        elif isinstance(opt.type, (types.File, types.Path)):
            field = self.make_file_field(opt)
        else:
            raise TypeError(f'Cannot represent click type {opt.type}')
        return field

    def process(self, form):
        """Take a generic form instance as input and process the command.

        :param form:    GenericForm instance with command options/args.

        ~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-

        :return:   Result of running the command.

        ~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-

        PURPOSE:   This handles figuring out the arguments, extracting
                   the callback from click, and executing it.
        """
        kwargs = {}
        for opt in self.click_cmd.params:
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
        wrapped_cmd = getattr(self.click_cmd.callback, '__wrapped__', None)
        if wrapped_cmd:
            raw_result = wrapped_cmd(**kwargs)
        else:
            raw_result = self.click_cmd.callback(**kwargs)

        return self.post_process(raw_result)

    def post_process(self, raw_result):
        """Take output of process method and do further postprocessing.

        Intended for sub-classes to override.
        """
        logging.debug('For %s, post-processing raw_result %s',
                      self, raw_result)
        result = raw_result
        for tweak in self.tweaks:
            result = tweak.post_process_result(self, result)
        return result

    def handle_request(self, opts=None):
        """Handle request to run command:

        :param opts=None:   Optional dictionary with string keys for names
                            of options to command and values to use.

        ~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-

        :return:  Result of running the command.

        ~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-

        PURPOSE:  Run the command in a generic way.

        """
        opts = opts or {}
        form = self.form(opts)
        result = self.process(form)
        return result
