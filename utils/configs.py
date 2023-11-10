import os
import time
import yaml
import copy
import configparser
from logging.handlers import TimedRotatingFileHandler
from . import os_lib, converter


def load_config_from_yml(path) -> dict:
    return yaml.load(open(path, 'rb'), Loader=yaml.Loader)


def load_config_from_ini(path) -> configparser.ConfigParser:
    config = configparser.ConfigParser()
    config.read(path, encoding="utf-8")
    return config


class ArgDict(dict):
    """Convenience class that behaves like a dict but allows access with the attribute syntax.
    so that it can be treated as `argparse.ArgumentParser().parse_args()`"""

    def __getattr__(self, name: str):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

    def __setattr__(self, name: str, value) -> None:
        self[name] = value

    def __delattr__(self, name: str) -> None:
        del self[name]


def collapse_dict(d: dict):
    """

    Example:
        >>> d = {'a': {'b': 1, 'c': 2, 'e': {'f': 4}}, 'd': 3}
        >>> collapse_dict(d)
        >>> {'a.b': 1, 'a.c': 2, 'a.e.f': 4, 'd': 3}

    """

    def cur(cur_dic, cur_k, new_dic):
        for k, v in cur_dic.items():
            if isinstance(v, dict):
                k = f'{cur_k}.{k}'
                cur(v, k, new_dic)
            else:
                new_dic[f'{cur_k}.{k}'] = v

        return new_dic

    new_dic = cur(d, '', {})
    new_dic = {k[1:]: v for k, v in new_dic.items()}
    return new_dic


def expand_dict(d: dict):
    """expand dict while '.' in key or '=' in value

    Example:
        >>> d = {'a.b': 1}
        >>> expand_dict(d)
        {'a': {'b': 1}}

        >>> d = {'a': 'b=1'}
        >>> expand_dict(d)
        {'a': {'b': 1}}

        >>> d = {'a.b.c.d': 1, 'a.b': 'c.e=2', 'a.b.e': 3}
        >>> expand_dict(d)
        {'a': {'b': {'c': {'d': 1, 'e': '2'}, 'e': 3}}}
    """

    def cur_str(k, v, cur_dic):
        if '.' in k:
            a, b = k.split('.', 1)
            v = cur_str(b, v, cur_dic.get(a, {}))
            return {a: v}
        elif isinstance(v, dict):
            cur_dic[k] = cur_dict(v, cur_dic.get(k, {}))
            return cur_dic
        else:
            if isinstance(v, str) and '=' in v:
                kk, vv = v.split('=', 1)
                v = cur_dict({kk.strip(): vv.strip()}, cur_dic.get(k, {}))
            cur_dic[k] = v
            return cur_dic

    def cur_dict(cur_dic, new_dic):
        for k, v in cur_dic.items():
            new_dic = merge_dict(new_dic, cur_str(k, v, new_dic))

        return new_dic

    return cur_dict(d, {})


def merge_dict(d1: dict, d2: dict) -> dict:
    """merge values from d1 and d2
    if had same key, d2 will cover d1

    Example:
        >>> d1 = {'a': {'b': {'c': 1}}}
        >>> d2 = {'a': {'b': {'d': 2}}}
        >>> merge_dict(d1, d2)
        {'a': {'b': {'c': 1, 'd': 2}}}

    """

    def cur(cur_dic, new_dic):
        for k, v in new_dic.items():
            if k in cur_dic and isinstance(v, dict) and isinstance(cur_dic[k], dict):
                v = cur(cur_dic[k], v)

            cur_dic[k] = v

        return cur_dic

    return cur(copy.deepcopy(d1), copy.deepcopy(d2))


def permute_obj(obj: dict or list):
    """

    Example:
        
        >>> kwargs = [{'a': [1], 'b': [2, 3]}, {'c': [4, 5, 6]}]
        >>> permute_obj(kwargs)
        [{'a': 1, 'b': 2}, {'a': 1, 'b': 3}, {'c': 4}, {'c': 5}, {'c': 6}]

    """

    def cur(cur_obj: dict):
        r = [{}]
        for k, v in cur_obj.items():
            r = [{**rr, k: vv} for rr in r for vv in v]

        return r

    ret = []
    if isinstance(obj, dict):
        ret += cur(obj)
    else:
        for o in obj:
            ret += cur(o)

    return ret


def parse_params_example(path, parser) -> dict:
    """an example for parse parameters"""

    def params_params_from_file(path) -> dict:
        """user params, low priority"""

        return expand_dict(load_config_from_yml(path))

    def params_params_from_env(flag='Global.') -> dict:
        """global params, middle priority"""
        import os

        args = {}
        for k, v in os.environ.items():
            if k.startswith(flag):
                k = k.replace(flag, '')
                args[k] = v

        config = expand_dict(args)
        config = converter.DataConvert.str_value_to_constant(config)

        return config

    def params_params_from_arg(parser) -> dict:
        """local params, high priority
        # parser will be created like that
        import argparse

        parser = argparse.ArgumentParser()
        ...
        parser.add_argument('-c', '--config', nargs='+', default=[], help='global config')
        """

        args = parser.parse_args()
        _config = args.config
        if _config:
            _config = dict(s.split('=') for s in _config)
            _config = expand_dict(_config)
            _config = converter.DataConvert.str_value_to_constant(_config)
        else:
            _config = {}

        return _config

    config = params_params_from_file(path)
    config = merge_dict(config, params_params_from_env())
    config = merge_dict(config, params_params_from_arg(parser))

    return config
