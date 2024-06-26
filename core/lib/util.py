"""
Common/Misc utils
"""

import platform
import pprint
import typing
from typing import Any, Tuple, Optional

T = typing.TypeVar('T')

# global encoding
ENCODING = 'utf-8'


def as_int(o: Any) -> Tuple[int, Optional[Exception]]:
    """
    object to int
    :param o: object
    :return: int value, err
    """
    try:
        return int(o), None
    except Exception as e:
        return 0, e


def as_float(o: Any) -> Tuple[float, Optional[Exception]]:
    """
    object to float
    :param o: object
    :return: float value, err
    """
    try:
        return float(o), None
    except Exception as e:
        return 0.0, e


def is_win() -> bool:
    """
    is currently running on windows
    :return: if current system is of Windows family
    """
    return platform.system().lower().startswith('win')


def pfmt(o: Any, *args, **kwargs) -> str:
    """
    pretty format object
    :param o: object
    :return: object string
    """
    return pprint.pformat(o, *args, **kwargs)


def http_status_in_array(status: int, arr: []) -> bool:
    """
    check if http status is in array
    :param status: http status code
    :param arr: array of status codes
    :return: if status is in array
    """
    if not arr:
        return False
    status_str = str(status)[0:1] + "xx"
    return status in arr or status_str in arr or status_str.upper() in arr
