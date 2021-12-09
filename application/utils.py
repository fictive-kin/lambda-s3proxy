# -*- coding: utf-8 -*-

import random
import string


def str2bool(s):
    if s == 'False' or s == 'false' or s == 'FALSE' or s == '0':
        return False
    return bool(s)


def random_string(length=5):  # pylint: disable=no-self-use
    return ''.join(
        random.SystemRandom().choice(string.ascii_lowercase +
                                     string.ascii_uppercase +
                                     string.digits) for _ in range(length))
