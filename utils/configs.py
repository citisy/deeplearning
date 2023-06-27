import os
import time
import yaml
import copy
import configparser
import logging
import logging.config
from logging.handlers import TimedRotatingFileHandler
from . import os_lib, converter


def load_config_from_yml(path) -> dict:
    return yaml.load(open(path, 'rb'), Loader=yaml.Loader)


def load_config_from_ini(path) -> configparser.ConfigParser:
    config = configparser.ConfigParser()
    config.read(path, encoding="utf-8")
    return config


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
            new_dic.update(cur_str(k, v, new_dic))

        return new_dic

    return cur_dict(d, {})


def merge_dict(d1: dict, d2: dict):
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


class MultiProcessTimedRotatingFileHandler(TimedRotatingFileHandler):
    @property
    def dfn(self):
        current_time = int(time.time())
        # get the time that this sequence started at and make it a TimeTuple
        dst_now = time.localtime(current_time)[-1]
        t = self.rolloverAt - self.interval
        if self.utc:
            time_tuple = time.gmtime(t)
        else:
            time_tuple = time.localtime(t)
            dst_then = time_tuple[-1]
            if dst_now != dst_then:
                if dst_now:
                    addend = 3600
                else:
                    addend = -3600
                time_tuple = time.localtime(t + addend)
        dfn = self.rotation_filename(self.baseFilename + "." + time.strftime(self.suffix, time_tuple))

        return dfn

    def shouldRollover(self, record):
        """
        是否应该执行日志滚动操作：
        1、存档文件已存在时，执行滚动操作
        2、当前时间 >= 滚动时间点时，执行滚动操作
        """
        dfn = self.dfn
        t = int(time.time())
        if t >= self.rolloverAt or os.path.exists(dfn):
            return 1
        return 0

    def doRollover(self):
        """
        执行滚动操作
        1、文件句柄更新
        2、存在文件处理
        3、备份数处理
        4、下次滚动时间点更新
        """
        if self.stream:
            self.stream.close()
            self.stream = None
        # get the time that this sequence started at and make it a TimeTuple

        dfn = self.dfn

        # 存档log 已存在处理
        if not os.path.exists(dfn):
            self.rotate(self.baseFilename, dfn)

        # 备份数控制
        if self.backupCount > 0:
            for s in self.getFilesToDelete():
                os.remove(s)

        # 延迟处理
        if not self.delay:
            self.stream = self._open()

        # 更新滚动时间点
        current_time = int(time.time())
        new_rollover_at = self.computeRollover(current_time)
        while new_rollover_at <= current_time:
            new_rollover_at = new_rollover_at + self.interval

        # If DST changes and midnight or weekly rollover, adjust for this.
        if (self.when == 'MIDNIGHT' or self.when.startswith('W')) and not self.utc:
            dst_at_rollover = time.localtime(new_rollover_at)[-1]
            dst_now = time.localtime(current_time)[-1]
            if dst_now != dst_at_rollover:
                if not dst_now:  # DST kicks in before next rollover, so we need to deduct an hour
                    addend = -3600
                else:  # DST bows out before next rollover, so we need to add an hour
                    addend = 3600
                new_rollover_at += addend
        self.rolloverAt = new_rollover_at


def logger_init(config={}, log_dir=None):
    """logging配置
    默认loggers：['', 'basic', 'service_standard', 'service', '__main__']

    Examples
        .. code-block:: python
            import logging
            from utils.configs import logger_init

            logger_init()
            logger = logging.getLogger('service')
            logger.info('')
    """

    default_logging_config = {
        'version': 1,
        'disable_existing_loggers': True,
        'formatters': {
            'standard': {
                'format': '[ %(asctime)s ] [%(levelname)s] [%(name)s]: %(message)s',
                'datefmt': '%Y-%m-%d %H:%M:%S'
            },
            'precise': {
                'format': '[ %(asctime)s ] [%(levelname)s] [%(name)s:%(filename)s:%(funcName)s:%(lineno)d]: %(message)s',
                'datefmt': '%Y-%m-%d %H:%M:%S'
            }
        },
        'handlers': {
            # 屏幕输出流
            'default': {
                'level': 'DEBUG',
                'formatter': 'standard',
                'class': 'logging.StreamHandler',
                'stream': 'ext://sys.stdout',
            },

            # 简单的无格式屏幕输出流
            'print': {
                'level': 'DEBUG',
                'class': 'logging.StreamHandler',
                'stream': 'ext://sys.stdout',
            },
        },
        'loggers': {
            # root logger
            '': {
                'handlers': ['default'],
                'level': 'INFO',
                'propagate': False
            },

            # 简单的无格式屏幕输出流
            'print': {
                'handlers': ['print'],
                'level': 'INFO',
                'propagate': False
            }
        }
    }

    if log_dir is not None:     # add file handles
        os_lib.mk_dir(log_dir)
        default_logging_config = merge_dict(default_logging_config, {
            'handlers': {
                # 简略信息info
                'info_standard': {
                    'level': 'INFO',
                    'formatter': 'standard',
                    'class': 'utils.configs.MultiProcessTimedRotatingFileHandler',
                    'filename': f'{log_dir}/info_standard.log',
                    'when': 'W0',
                    'backupCount': 5,
                },

                # 详细信息info
                'info': {
                    'level': 'INFO',
                    'formatter': 'precise',
                    'class': 'utils.configs.MultiProcessTimedRotatingFileHandler',
                    'filename': f'{log_dir}/info.log',
                    'when': 'D',
                    'backupCount': 15,
                },

                # 详细信息error
                'error': {
                    'level': 'ERROR',
                    'formatter': 'precise',
                    'class': 'utils.configs.MultiProcessTimedRotatingFileHandler',
                    'filename': f'{log_dir}/error.log',
                    'when': 'W0',
                    'backupCount': 5,
                },
            },

            'loggers': {
                'basic': {
                    'handlers': ['default', 'info_standard', 'error', 'critical'],
                    'level': 'INFO',
                    'propagate': False
                },
                'service_standard': {
                    'handlers': ['default', 'info_standard', 'error', 'critical'],
                    'level': 'INFO',
                    'propagate': False
                },
                'service': {
                    'handlers': ['default', 'info', 'error', 'critical'],
                    'level': 'INFO',
                    'propagate': False
                },
            }

        })

    default_logging_config = merge_dict(default_logging_config, config)
    logging.config.dictConfig(default_logging_config)


def parse_params_example() -> dict:
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

    def params_params_from_arg() -> dict:
        """local params, high priority"""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument('key1', type=str, default='value1', help='note of key1')
        parser.add_argument('key2', action='store_true', help='note of key2')
        parser.add_argument('key3', nargs='+', default=[], help='note of key3')  # return a list
        ...

        args = parser.parse_args()
        return expand_dict(vars(args))

    config = params_params_from_file('your config path')
    config = merge_dict(config, params_params_from_env())
    config = merge_dict(config, params_params_from_arg())

    return config
