from __future__ import annotations

import re
from collections.abc import Iterable
from contextlib import suppress
from copy import deepcopy
from dataclasses import fields
from typing import Any, Dict, overload

import pandas as pd

from ..iapi import labels_view
from .evaluation import after_stat, stage

__all__ = ("aes",)

X_AESTHETICS = {"x", "xmin", "xmax", "xend", "xintercept"}
Y_AESTHETICS = {"y", "ymin", "ymax", "yend", "yintercept"}

ALL_AESTHETICS = {
    "alpha",
    "angle",
    "color",
    "colour",
    "fill",
    "group",
    "intercept",
    "label",
    "lineheight",
    "linetype",
    "lower",
    "middle",
    "radius",
    "sample",
    "shape",
    "size",
    "slope",
    "stroke",
    "upper",
    "weight",
    *X_AESTHETICS,
    *Y_AESTHETICS,
}

POSITION_AESTHETICS = X_AESTHETICS | Y_AESTHETICS

SCALED_AESTHETICS = {
    "x",
    "y",
    "alpha",
    "color",
    "colour",
    "fill",
    "linetype",
    "shape",
    "size",
    "stroke",
}

NO_GROUP = -1

# Aesthetics modifying searchers, DEPRECATED
STAT_RE = re.compile(r"\bstat\(")
DOTS_RE = re.compile(r"\.\.([a-zA-Z0-9_]+)\.\.")


class aes(Dict[str, Any]):
    """
    Create aesthetic mappings

    Parameters
    ----------
    x : expression | array_like | scalar
        x aesthetic mapping
    y : expression | array_like | scalar
        y aesthetic mapping
    **kwargs : dict
        Other aesthetic mappings

    Notes
    -----
    Only the **x** and **y** aesthetic mappings can be specified as
    positional arguments. All the rest must be keyword arguments.

    The value of each mapping must be one of:

    - **string**::

            import pandas as pd
            import numpy as np

            arr = [11, 12, 13]
            df = pd.DataFrame({'alpha': [1, 2, 3],
                               'beta': [1, 2, 3],
                               'gam ma': [1, 2, 3]})

            # Refer to a column in a dataframe
            ggplot(df, aes(x='alpha', y='beta'))

    - **array_like**::

            # A variable
            ggplot(df, aes(x='alpha', y=arr))

            # or an inplace list
            ggplot(df, aes(x='alpha', y=[4, 5, 6]))

    - **scalar**::

            # A scalar value/variable
            ggplot(df, aes(x='alpha', y=4))

            # The above statement is equivalent to
            ggplot(df, aes(x='alpha', y=[4, 4, 4]))

    - **String expression**::

            ggplot(df, aes(x='alpha', y='2*beta'))
            ggplot(df, aes(x='alpha', y='np.sin(beta)'))
            ggplot(df, aes(x='df.index', y='beta'))

            # If `count` is an aesthetic calculated by a stat
            ggplot(df, aes(x='alpha', y=after_stat('count')))
            ggplot(df, aes(x='alpha', y=after_stat('count/np.max(count)')))

      The strings in the expression can refer to;

        1. columns in the dataframe
        2. variables in the namespace
        3. aesthetic values (columns) calculated by the ``stat``

      with the column names having precedence over the variables.
      For expressions, columns in the dataframe that are mapped to
      must have names that would be valid python variable names.

      This is okay::

        # 'gam ma' is a column in the dataframe
        ggplot(df, aes(x='df.index', y='gam ma'))

      While this is not::

        # 'gam ma' is a column in the dataframe, but not
        # valid python variable name
        ggplot(df, aes(x='df.index', y='np.sin(gam ma)'))

    ``aes`` has 2 internal methods you can use to transform variables being
    mapped.

        1. ``factor`` - This function turns the variable into a factor.
            It is just an alias to ``pd.Categorical``::

                ggplot(mtcars, aes(x='factor(cyl)')) + geom_bar()

        2. ``reorder`` - This function changes the order of first variable
            based on values of the second variable::

                df = pd.DataFrame({
                    'x': ['b', 'd', 'c', 'a'],
                    'y': [1, 2, 3, 4]
                })

                ggplot(df, aes('reorder(x, y)', 'y')) + geom_col()

    .. rubric:: The group aesthetic

    ``group`` is a special aesthetic that the user can *map* to.
    It is used to group the plotted items. If not specified, it
    is automatically computed and in most cases the computed
    groups are sufficient. However, there may be cases were it is
    handy to map to it.

    See Also
    --------
    :func:`after_stat` : For how to map aesthetics to variable calculated
        by the stat
    :func:`after_scale` : For how to alter aesthetics after the data has been
        mapped by the scale.
    :class:`stage` : For how to map to evaluate the mapping to aesthetics at
        more than one stage of the plot building pipeline.
    """

    def __init__(self, *args, **kwargs):
        kwargs = rename_aesthetics(kwargs)
        kwargs.update(zip(("x", "y"), args))
        kwargs = self._convert_deprecated_expr(kwargs)
        self.update(kwargs)

    def _convert_deprecated_expr(self, kwargs):
        """
        Handle old-style calculated aesthetic expression mappings

        Just converts them to use `stage` e.g.
        'stat(count)' to after_stat(count)
        '..count..' to after_stat(count)
        """
        for name, value in kwargs.items():
            if not isinstance(value, stage):
                if is_calculated_aes(value):
                    _after_stat = strip_calculated_markers(value)
                    kwargs[name] = after_stat(_after_stat)
        return kwargs

    @property
    def _starting(self):
        """
        Return the subset of aesthetics mapped from the layer data

        The mapping is a dict of the form ``{name: expr}``, i.e the
        stage class has been peeled off.
        """
        d = {}
        for name, value in self.items():
            if not isinstance(value, stage):
                d[name] = value
            elif isinstance(value, stage) and value.start is not None:
                d[name] = value.start

        return d

    @property
    def _calculated(self):
        """
        Return only the aesthetics mapped to calculated statistics

        The mapping is a dict of the form ``{name: expr}``, i.e the
        stage class has been peeled off.
        """
        d = {}
        for name, value in self.items():
            if isinstance(value, stage) and value.after_stat is not None:
                d[name] = value.after_stat

        return d

    @property
    def _scaled(self):
        """
        Return only the aesthetics mapped to after scaling

        The mapping is a dict of the form ``{name: expr}``, i.e the
        stage class has been peeled off.
        """
        d = {}
        for name, value in self.items():
            if isinstance(value, stage) and value.after_scale is not None:
                d[name] = value.after_scale

        return d

    def __deepcopy__(self, memo):
        """
        Deep copy without copying the environment
        """
        cls = self.__class__
        result = cls.__new__(cls)
        memo[id(self)] = result

        # Just copy the keys and point to the env
        for key, item in self.items():
            result[key] = deepcopy(self[key], memo)

        return result

    def __radd__(self, gg):
        """
        Add aesthetic mappings to ggplot
        """
        self = deepcopy(self)
        gg.mapping.update(self)
        gg.labels.update(make_labels(self))
        return gg

    def copy(self):
        return aes(**self)

    def inherit(self, other: dict[str, Any] | aes) -> aes:
        """
        Create a  mapping that inherits aesthetics in other

        Parameters
        ----------
        other: aes | dict[str, Any]
            Default aesthetics

        Returns
        -------
        new : aes
            Aesthetic mapping
        """
        new = self.copy()
        for k in other:
            if k not in self:
                new[k] = other[k]
        return new


@overload
def rename_aesthetics(obj: list[str]) -> list[str]:
    ...


@overload
def rename_aesthetics(obj: dict[str, Any]) -> dict[str, Any]:
    ...


def rename_aesthetics(
    obj: list[str] | dict[str, Any]
) -> list[str] | dict[str, Any]:
    """
    Rename aesthetics in obj

    Parameters
    ----------
    obj : dict or list
        Object that contains aesthetics names

    Returns
    -------
    obj : dict or list
        Object that contains aesthetics names
    """
    if isinstance(obj, dict):
        for name in tuple(obj.keys()):
            new_name = name.replace("colour", "color")
            if name != new_name:
                obj[new_name] = obj.pop(name)
    else:
        obj = [name.replace("colour", "color") for name in obj]

    return obj


def is_calculated_aes(ae):
    """
    Return True if Aesthetic expression maps to calculated statistic

    This function is now only used to identify the deprecated versions
    e.g. "..var.." or "stat(var)".

    Parameters
    ----------
    ae : object
        Single aesthetic mapping

    >>> is_calculated_aes('density')
    False

    >>> is_calculated_aes(4)
    False

    >>> is_calculated_aes('..density..')
    True

    >>> is_calculated_aes('stat(density)')
    True

    >>> is_calculated_aes('stat(100*density)')
    True

    >>> is_calculated_aes('100*stat(density)')
    True
    """
    if not isinstance(ae, str):
        return False

    for pattern in (STAT_RE, DOTS_RE):
        if pattern.search(ae):
            return True

    return False


def strip_stat(value):
    """
    Remove stat function that mark calculated aesthetics

    Parameters
    ----------
    value : object
        Aesthetic value. In most cases this will be a string
        but other types will pass through unmodified.

    Return
    ------
    out : object
        Aesthetic value with the dots removed.

    >>> strip_stat('stat(density + stat(count))')
    density + count

    >>> strip_stat('stat(density) + 5')
    density + 5

    >>> strip_stat('5 + stat(func(density))')
    5 + func(density)

    >>> strip_stat('stat(func(density) + var1)')
    func(density) + var1

    >>> strip_stat('stat + var1')
    stat + var1

    >>> strip_stat(4)
    4
    """

    def strip_hanging_closing_parens(s):
        """
        Remove leftover  parens
        """
        # Use and integer stack to track parens
        # and ignore leftover closing parens
        stack = 0
        idx = []
        for i, c in enumerate(s):
            if c == "(":
                stack += 1
            elif c == ")":
                stack -= 1
                if stack < 0:
                    idx.append(i)
                    stack = 0
                    continue
            yield c

    with suppress(TypeError):
        if STAT_RE.search(value):
            value = re.sub(r"\bstat\(", "", value)
            value = "".join(strip_hanging_closing_parens(value))

    return value


def strip_dots(value):
    """
    Remove dots(if any) that mark calculated aesthetics

    Parameters
    ----------
    value : object
        Aesthetic value. In most cases this will be a string
        but other types will pass through unmodified.

    Return
    ------
    out : object
        Aesthetic value with the dots removed.
    """
    with suppress(TypeError):
        value = DOTS_RE.sub(r"\1", value)
    return value


def strip_calculated_markers(value):
    """
    Remove markers for calculated aesthetics

    Parameters
    ----------
    value : object
        Aesthetic value. In most cases this will be a string
        but other types will pass through unmodified.

    Return
    ------
    out : object
        Aesthetic value with the dots removed.
    """
    return strip_stat(strip_dots(value))


def aes_to_scale(var):
    """
    Look up the scale that should be used for a given aesthetic
    """
    if var in {"x", "xmin", "xmax", "xend", "xintercept"}:
        var = "x"
    elif var in {"y", "ymin", "ymax", "yend", "yintercept"}:
        var = "y"
    return var


def is_position_aes(vars_):
    """
    Figure out if an aesthetic is a position aesthetic or not
    """
    try:
        return all(aes_to_scale(v) in {"x", "y"} for v in vars_)
    except TypeError:
        return aes_to_scale(vars_) in {"x", "y"}


def make_labels(mapping: dict[str, Any] | aes) -> labels_view:
    """
    Convert aesthetic mapping into text labels
    """

    def _nice_label(value: Any) -> str | None:
        if isinstance(value, pd.Series):
            return value.name  # pyright: ignore
        elif not isinstance(value, Iterable) or isinstance(value, str):
            return str(value)
        else:
            return None

    def _make_label(ae: str, value: Any) -> str | None:
        if not isinstance(value, stage):
            return _nice_label(value)
        elif value.start is None:
            if value.after_stat is not None:
                return value.after_stat
            elif value.after_scale is not None:
                return value.after_scale
            else:
                # return ''
                raise ValueError("Unknown mapping")
        else:
            if value.after_stat is not None:
                return value.after_stat
            else:
                return _nice_label(value)

    valid_names = {f.name for f in fields(labels_view)}
    return labels_view(
        **{
            str(ae): _make_label(ae, label)
            for ae, label in mapping.items()
            if ae in valid_names
        }
    )


def is_valid_aesthetic(value, ae):
    """
    Return True if `value` looks valid.

    Parameters
    ----------
    value : object
        Value to check
    ae : str
        Aesthetic name

    Returns
    -------
    out : bool
        Whether the value is of a valid looking form.

    Notes
    -----
    There are no guarantees that he value is spot on
    valid.
    """
    if ae == "linetype":
        named = {
            "solid",
            "dashed",
            "dashdot",
            "dotted",
            "_",
            "--",
            "-.",
            ":",
            "None",
            " ",
            "",
        }
        if value in named:
            return True

        # tuple of the form (offset, (on, off, on, off, ...))
        # e.g (0, (1, 2))
        conditions = [
            isinstance(value, tuple),
            isinstance(value[0], int),
            isinstance(value[1], tuple),
            len(value[1]) % 2 == 0,
            all(isinstance(x, int) for x in value[1]),
        ]
        if all(conditions):
            return True
        return False

    elif ae == "shape":
        if isinstance(value, str):
            return True

        # tuple of the form (numsides, style, angle)
        # where style is in the range [0, 3]
        # e.g (4, 1, 45)
        conditions = [
            isinstance(value, tuple),
            all(isinstance(x, int) for x in value),
            0 <= value[1] < 3,
        ]
        if all(conditions):
            return True
        return False

    elif ae in {"color", "fill"}:
        if isinstance(value, str):
            return True
        with suppress(TypeError):
            if isinstance(value, (tuple, list)) and all(
                0 <= x <= 1 for x in value
            ):
                return True
        return False

    # For any other aesthetics we return False to allow
    # for special cases to be discovered and then coded
    # for appropriately.
    return False


def has_groups(data):
    """
    Check if data is grouped

    Parameters
    ----------
    data : dataframe
        Data

    Returns
    -------
    out : bool
        If True, the data has groups.
    """
    # If any row in the group column is equal to NO_GROUP, then
    # the data all of them are and the data has no groups
    return data.loc[0, "group"] != NO_GROUP
