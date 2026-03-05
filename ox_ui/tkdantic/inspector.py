"""Tools to inspect things to figure out the interface.
"""

import functools
import inspect

from pydantic import BaseModel

from ox_ui.tkdantic.command import Command


def commands_for_cls(klass):
    """Inspect a class and produce a list of Command objects for the class.

    See pydantic_method docs for more details.
    """

    result = []
    for name, method in inspect.getmembers(
            klass, predicate=inspect.isfunction):
        pydantic_params = getattr(method, '_pydantic_params', None)
        if pydantic_params is not None:
            assert len(pydantic_params) <= 1, 'Cannot handle multi-params'
            result.append(Command(title=name, parameters=[
                v for _, v in pydantic_params.items()], description=(
                          method.__doc__ or 'No doc provided.')))

    return result


def pydantic_method(func):
    """Decorator used to mark a method as callable with pydantic model.

You can mark methods with `@pydantic_method` to make them easy to find
with the `commands_for_cls` function. This is useful since you can use
the extracted Command list to auto-populate a GUI to know both which
commands are available and how to call them.

Methods marked with the `@pydantic_method` decorator must have a single
argument which is a pydantic model. You must also provide a type-hint
indicating the model type so the GUI knows how to construct it.
    """
    hints = inspect.get_annotations(func)
    params = list(inspect.signature(func).parameters.values())
    has_self = params and params[0].name in ('self', 'cls')

    non_self_params = params[1:] if has_self else params
    model_params = {
        p.name: hints[p.name]
        for p in non_self_params
        if p.name in hints and isinstance(
                hints[p.name], type) and issubclass(hints[p.name], BaseModel)
    }

    @functools.wraps(func)
    def wrapper(*args):
        if has_self:
            self_arg, rest = args[0:1], args[1:]
        else:
            self_arg, rest = (), args

        new_args = []
        param_names = [p.name for p in non_self_params]
        for arg, name in zip(rest, param_names):
            if name in model_params and isinstance(arg, str):
                new_args.append(model_params[name].model_validate_json(arg))
            else:
                new_args.append(arg)
        return func(*self_arg, *new_args)

    wrapper._pydantic_params = model_params  # pylint:disable=protected-access
    return wrapper
