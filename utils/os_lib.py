import cv2
import json
import os
import pickle
import time
import psutil
import uuid
import random
import numpy as np
import pandas as pd
from functools import wraps
from pathlib import Path
from typing import List, Any


def mk_dir(dir_path):
    dir_path = Path(dir_path)

    if not dir_path.is_dir():
        dir_path.mkdir(parents=True, exist_ok=True)


def mk_parent_dir(file_path):
    file_path = Path(file_path)
    dir_path = file_path.parent
    mk_dir(dir_path)


def auto_suffix(obj):
    if isinstance(obj, (list, tuple, set)):
        s = '.txt'
    elif isinstance(obj, dict):
        s = '.json'
    elif isinstance(obj, np.ndarray) and obj.dtype == np.uint8:
        s = '.png'
    elif isinstance(obj, pd.DataFrame):
        s = '.csv'
    else:
        s = '.pkl'
    return s


class Saver:
    def __init__(self, verbose=True, stdout_method=print, stdout_fmt='Save to %s successful!', stderr_method=print, stderr_fmt='Save to %s failed!'):
        self.verbose = verbose
        self.stdout_method = stdout_method if verbose else FakeIo()
        self.stdout_fmt = stdout_fmt
        self.stderr_method = stderr_method if verbose else FakeIo()
        self.stderr_fmt = stderr_fmt

    def stdout(self, path):
        self.stdout_method(self.stdout_fmt % path)

    def stderr(self, path):
        self.stderr_method(self.stderr_fmt % path)

    def auto_save(self, obj, path: str):
        suffix = Path(path).suffix.lower()
        mk_parent_dir(path)

        if suffix in ('.js', '.json'):
            self.save_json(obj, path)
        elif suffix in ('.txt',):
            self.save_txt(obj, path)
        elif suffix in ('.pkl',):
            self.save_pkl(obj, path)
        elif suffix in ('.png', '.jpg', '.jpeg', 'tiff'):
            self.save_img(obj, path)
        elif suffix in ('.csv',):
            self.save_csv(obj, path)
        else:
            self.save_bytes(obj, path)

    def save_json(self, obj: dict, path):
        with open(path, 'w', encoding='utf8') as f:
            json.dump(obj, f, ensure_ascii=False, indent=4)

        self.stdout(path)

    def save_txt(self, obj: iter, path, sep='\n'):
        with open(path, 'w', encoding='utf8') as f:
            f.write(sep.join(obj))

        self.stdout(path)

    def save_pkl(self, obj, path):
        with open(path, 'wb') as f:
            pickle.dump(obj, f)

        self.stdout(path)

    def save_bytes(self, obj: bytes, path):
        with open(path, 'wb') as f:
            f.write(obj)

        self.stdout(path)

    def save_img(self, obj: np.ndarray, path):
        # it will error with chinese path in low version of cv2
        # it has fixed in high version already
        # cv2.imencode('.png', obj)[1].tofile(path)
        flag = cv2.imwrite(path, obj)
        if flag:
            self.stdout(path)
        else:
            self.stderr(path)

    def save_csv(self, obj: pd.DataFrame, path):
        obj.to_csv(path)
        self.stdout(path)

    def save_pdf_to_img(self, path, page=None, image_dir=None, scale_ratio=1.33):
        """select pages from pdf, and save with image type"""
        images = loader.load_pdf_to_images2(
            path,
            scale_ratio=scale_ratio
        )

        if isinstance(page, int):
            images = [images[page]]

        elif isinstance(page, list):
            images = [images[_] for _ in page]

        image_dir = image_dir or path.replace('pdfs', 'images').replace(Path(path).suffix, '')
        mk_dir(image_dir)

        for i, img in enumerate(images):
            self.save_img(img, f'{image_dir}/{i}.png')

    def save_pdf_to_pdf(self, path, page=None, save_path=None):
        """select pages from pdf, and save with pdf type"""
        from PyPDF2 import PdfFileReader, PdfFileWriter

        pdf_reader = PdfFileReader(path)
        pdf_writer = PdfFileWriter()

        if isinstance(page, int):
            pdf_writer.addPage(pdf_reader.getPage(page))

        elif isinstance(page, list):
            for i in page:
                pdf_writer.addPage(pdf_reader.getPage(i))

        suffix = Path(path).suffix
        save_path = save_path or path.replace(suffix, '_' + suffix)

        with open(save_path, 'wb') as out:
            pdf_writer.write(out)

        self.stdout(save_path)


class Loader:
    def __init__(self, verbose=True, stdout_method=print, stdout_fmt='Read %s successful!'):
        self.verbose = verbose
        self.stdout_method = stdout_method if verbose else FakeIo()
        self.stdout_fmt = stdout_fmt

    def stdout(self, path):
        self.stdout_method(self.stdout_fmt % path)

    def auto_load(self, path: str):
        suffix = Path(path).suffix

        if suffix in ('.js', '.json'):
            obj = self.load_json(path)
        elif suffix in ('.txt',):
            obj = self.load_txt(path)
        elif suffix in ('.pkl',):
            obj = self.load_pkl(path)
        elif suffix in ('.png', '.jpg', '.jpeg',):
            obj = self.load_img(path)
        else:
            obj = self.load_bytes(path)

        return obj

    def load_json(self, path) -> dict:
        with open(path, 'r', encoding='utf8') as f:
            obj = json.load(f)
        self.stdout(path)

        return obj

    def load_txt(self, path) -> iter:
        with open(path, 'r', encoding='utf8') as f:
            obj = f.read().strip().split('\n')

        self.stdout(path)
        return obj

    def load_pkl(self, path):
        with open(path, 'rb') as f:
            obj = pickle.load(f)

        self.stdout(path)
        return obj

    def load_bytes(self, path) -> bytes:
        with open(path, 'rb') as f:
            obj = f.read()

        self.stdout(path)
        return obj

    def load_img(self, path, channel_fixed_3=False) -> np.ndarray:
        # it will error with chinese path in low version of cv2
        # it has fixed in high version already
        # img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), -1)
        img = cv2.imread(path)
        assert img is not None

        if channel_fixed_3:
            if img.shape[2] == 3:
                return img
            elif img.shape[2] == 4:  # bgra
                img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
                return img
            elif img.shape[2] == 1:  # gray
                img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
                return img
            else:
                raise ValueError("image has weird channel number: %d" % img.shape[2])

        self.stdout(path)
        return img

    def load_pdf_to_images(
            self,
            obj: str or bytes, scale_ratio: float = 1.33,
            rotate: int = 0, alpha=False, bgr=False
    ) -> List[np.ndarray]:
        import fitz

        if isinstance(obj, str):
            doc = fitz.open(obj)
        else:
            doc = fitz.open(stream=obj, filetype='pdf')

        images = []

        for page in doc:
            trans = fitz.Matrix(scale_ratio, scale_ratio).prerotate(rotate)  # rotate means clockwise
            pm = page.get_pixmap(matrix=trans, alpha=alpha)
            data = pm.tobytes()  # bytes
            img_np = np.frombuffer(data, dtype=np.uint8)

            flag = 1 if bgr else -1
            img = cv2.imdecode(img_np, flags=flag)

            images.append(img)

        if isinstance(obj, str):
            self.stdout(obj)

        return images

    def load_pdf_to_images2(
            self,
            obj: str or bytes, scale_ratio: float = 1.33,
    ) -> List[np.ndarray]:
        from pdf2image import convert_from_path, convert_from_bytes

        dpi = 72 * scale_ratio
        if isinstance(obj, (str, Path)):
            images = convert_from_path(obj, dpi=dpi)
        else:
            images = convert_from_bytes(obj, dpi=dpi)

        images = [cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR) for image in images]

        if isinstance(obj, str):
            self.stdout(obj)

        return images


class MemoryCacher:
    def __init__(self, max_size=None,
                 verbose=True, stdout_method=print, stdout_fmt='Save to %s successful!',
                 **saver_kwargs
                 ):
        self.max_size = max_size
        self.verbose = verbose
        self.stdout_method = stdout_method if verbose else FakeIo()
        self.cache = {}

    def cache_one(self, obj):
        time_str = time.time()
        uid = str(uuid.uuid1())
        _id = (uid, time_str)
        self.delete_over_range()
        self.cache[_id] = obj
        return _id

    def cache_batch(self, objs):
        _ids = []
        for obj in objs:
            _id = self.cache_one(obj)
            _ids.append(_id)
        return _ids

    def delete_over_range(self):
        if not self.max_size:
            return

        if len(self.cache) >= self.max_size:
            key = min(self.cache.keys(), key=lambda x: x[1])
            self.cache.pop(key)

    def get_one(self, _id=None):
        if _id is None:
            _id = random.choice(list(self.cache.keys()))
        return self.cache[_id]

    def get_batch(self, _ids=None, size=None):
        if _ids is None:
            keys = list(self.cache.keys())
            _ids = np.random.choice(range(len(keys)), size, replace=False)
            _ids = [keys[_] for _ in _ids]

        return [self.cache[_id] for _id in _ids]


class FileCacher:
    def __init__(self, cache_dir=None, max_size=None,
                 verbose=True, stdout_method=print, stdout_fmt='Have deleted %s successful!',
                 saver_kwargs=dict(), loader_kwargs=dict()):
        mk_dir(cache_dir)
        self.cache_dir = Path(cache_dir)
        self.max_size = max_size
        self.verbose = verbose
        self.stdout_method = stdout_method if verbose else FakeIo()
        self.stdout_fmt = stdout_fmt
        self.saver = Saver(verbose, stdout_method, **saver_kwargs)
        self.loader = Loader(verbose, stdout_method, **loader_kwargs)

    def cache_one(self, obj, file_name=None):
        file_name = file_name or str(uuid.uuid1()) + auto_suffix(obj)
        path = f'{self.cache_dir}/{file_name}'
        self.delete_over_range(suffix=Path(path).suffix)
        self.saver.auto_save(obj, path)
        return file_name

    def cache_batch(self, objs, file_names=None):
        file_names = file_names or [str(uuid.uuid1()) + auto_suffix(obj) for obj in objs]
        for obj, file_name in zip(objs, file_names):
            path = f'{self.cache_dir}/{file_name}'
            self.delete_over_range(suffix=Path(path).suffix)
            self.saver.auto_save(obj, path)
        return file_names

    def delete_over_range(self, suffix=''):
        if not self.max_size:
            return

        caches = [str(_) for _ in self.cache_dir.glob(f'*{suffix}')]

        if len(caches) > self.max_size:
            ctime = [os.path.getctime(fp) for fp in caches]
            min_ctime = min(ctime)
            old_path = caches[ctime.index(min_ctime)]
            try:
                os.remove(old_path)
            except FileNotFoundError:
                # todo: if it occur, number of file would be greater than max_size
                self.stdout_method('Two process thread were crashed while deleting file possibly')
                return

            self.stdout_method(self.stdout_fmt % old_path)
            return old_path

    def get_one(self, file_name=None):
        if file_name is None:
            caches = [str(_) for _ in self.cache_dir.glob(f'*')]
            path = random.choice(caches)
        else:
            path = f'{self.cache_dir}/{file_name}'

        return self.loader.auto_load(path)

    def get_batch(self, file_names=None, size=None):
        if file_names is None:
            caches = [str(_) for _ in self.cache_dir.glob(f'*')]
            paths = np.random.choice(caches, size, replace=False)
        else:
            paths = [f'{self.cache_dir}/{file_name}' for file_name in file_names]

        return [self.loader.auto_load(path) for path in paths]


class MongoDBCacher:
    def __init__(self, host='127.0.0.1', port=27017, user=None, password=None, database=None, collection=None,
                 max_size=None, verbose=True, stdout_method=print, stdout_fmt='Save _id[%s] successful!',
                 **mongo_kwargs):
        from pymongo import MongoClient

        self.client = MongoClient(host, port, **mongo_kwargs)
        self.db = self.client[database]
        self.db.authenticate(user, password)
        self.collection = self.db[collection]
        self.max_size = max_size
        self.verbose = verbose
        self.stdout_method = stdout_method if verbose else FakeIo()
        self.stdout_fmt = stdout_fmt

    def cache_one(self, obj: dict, _id=None):
        self.delete_over_range()

        obj['update_time'] = int(time.time())
        if _id is None:
            x = self.collection.insert_one(obj)
            _id = x.inserted_id

        else:
            self.collection.update_one({'_id': _id}, {'$set': obj}, upsert=True)

        self.stdout_method(self.stdout_fmt % _id)
        return _id

    def cache_batch(self, objs, _ids=None):
        self.delete_over_range(len(objs))
        if _ids is None:
            x = self.collection.insert_many(objs)
            _ids = x.inserted_ids

        else:
            for _id, obj in zip(_ids, objs):
                self.collection.update_one({'_id': _id}, {'$set': obj}, upsert=True)

        return _ids

    def delete_over_range(self, batch_size=1):
        if not self.max_size:
            return

        query = self.collection.find()
        if query.count() > self.max_size - batch_size:
            x = query.sort({'update_time': 1}).limit(1)
            self.collection.delete_one(x)

    def get_one(self, _id=None):
        if _id is None:
            return self.collection.find_one()
        else:
            return self.collection.find_one({'_id': _id})

    def get_batch(self, _ids=None, size=None):
        if _ids is None:
            return [self.collection.find_one() for _ in range(size)]
        else:
            return [self.collection.find_one({'_id': _id}) for _id in _ids]


class FakeIo:
    """a placeholder, empty io method to cheat some functions which must use an io method,
    it means that the method do nothing in fact,
    it is useful to reduce the amounts of code changes

    Examples
    .. code-block:: python

        # save obj
        io_method = open

        # do not save obj
        io_method = FakeIo

        with io_method(fp, 'w', encoding='utf8') as f:
            f.write(obj)
    """

    def __init__(self, *args, **kwargs):
        pass

    def write(self, *args, **kwargs):
        pass

    def close(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    def __call__(self, *args, **kwargs):
        pass


class FakeWandb:
    def __init__(self, *args, **kwargs):
        pass

    def init(self, *args, **kwargs):
        return self

    def Table(self, *args, **kwargs):
        return self

    def Image(self, *args, **kwargs):
        return self

    def log(self, *args, **kwargs):
        pass

    def finish(self, *args, **kwargs):
        pass


class Retry:
    def __init__(self, verbose=True, stdout_method=print, count=3, wait=15):
        self.verbose = verbose
        self.stdout_method = stdout_method if verbose else FakeIo()
        self.count = count
        self.wait = wait

    def add_try(
            self,
            error_message='',
            err_type=(ConnectionError, TimeoutError)
    ):
        def wrap2(func):
            @wraps(func)
            def wrap(*args, **kwargs):
                for i in range(self.count):
                    try:
                        return func(*args, **kwargs)

                    except err_type as e:
                        if i >= self.count - 1:
                            raise e

                        msg = error_message or f'Something error occur, sleep {self.wait} seconds, and then retry'
                        self.stdout_method(msg)
                        time.sleep(self.wait)
                        self.stdout_method(f'{i + 2}th try!')

            return wrap

        return wrap2


class IgnoreException:
    def __init__(self, verbose=True, stdout_method=print):
        self.stdout_method = stdout_method if verbose else FakeIo()

    def add_ignore(
            self,
            error_message='',
            err_type=(ConnectionError, TimeoutError)
    ):
        def wrap2(func):
            @wraps(func)
            def wrap(*args, **kwargs):
                try:
                    return func(*args, **kwargs)

                except err_type as e:
                    msg = error_message or f'Something error occur: {e}'
                    self.stdout_method(msg)

            return wrap

        return wrap2


class AutoLog:
    """
    Examples
        .. code-block:: python

            from utils.os_lib import AutoLog
            auto_log = AutoLog()

            class SimpleClass:
                @auto_log.memory_log('success')
                @auto_log.memory_log()
                @auto_log.time_log()
                def func(self):
                    ...
    """

    def __init__(self, verbose=True, stdout_method=print, is_simple_log=True, is_time_log=True, is_memory_log=True):
        self.stdout_method = stdout_method if verbose else FakeIo()
        self.is_simple_log = is_simple_log
        self.is_time_log = is_time_log
        self.is_memory_log = is_memory_log

    def simple_log(self, string):
        def wrap2(func):
            @wraps(func)
            def wrap(*args, **kwargs):
                r = func(*args, **kwargs)
                if self.is_simple_log:
                    self.stdout_method(string)
                return r

            return wrap

        return wrap2

    def time_log(self, prefix_string=''):
        def wrap2(func):
            @wraps(func)
            def wrap(*args, **kwargs):
                if self.is_time_log:
                    st = time.time()
                    r = func(*args, **kwargs)
                    et = time.time()
                    self.stdout_method(f'{prefix_string} - elapse[{et - st:.3f}s]!')
                else:
                    r = func(*args, **kwargs)
                return r

            return wrap

        return wrap2

    def memory_log(self, prefix_string=''):
        def wrap2(func):
            @wraps(func)
            def wrap(*args, **kwargs):
                if self.is_memory_log:
                    a = MemoryInfo.get_process_mem_info()
                    r = func(*args, **kwargs)
                    b = MemoryInfo.get_process_mem_info()
                    self.stdout_method(f'{prefix_string}\nbefore: {a}\nafter: {b}')
                else:
                    r = func(*args, **kwargs)
                return r

            return wrap

        return wrap2


class MemoryInfo:
    from .visualize import TextVisualize

    @classmethod
    def get_process_mem_info(cls, pretty_output=True):
        """
        uss, 进程独立占用的物理内存（不包含共享库占用的内存）
        rss, 该进程实际使用物理内存（包含共享库占用的全部内存）
        vms, 虚拟内存总量
        """
        pid = os.getpid()
        p = psutil.Process(pid)
        info = p.memory_full_info()
        info = {
            'pid': str(pid),
            'uss': info.uss,
            'rss': info.rss,
            'vms': info.vms,
        }

        if pretty_output:
            for k, v in info.items():
                if k != 'pid':
                    info[k] = cls.TextVisualize.num_to_human_readable_str(v)

        return info

    @classmethod
    def get_cpu_mem_info(cls, pretty_output=True):
        """
        percent, 实际已经使用的内存占比
        total, 内存总的大小
        available, 还可以使用的内存
        free, 剩余的内存
        used, 已经使用的内存
        """
        info = dict(psutil.virtual_memory()._asdict())
        if pretty_output:
            for k, v in info.items():
                if k != 'percent':
                    info[k] = cls.TextVisualize.num_to_human_readable_str(v)

        return info

    @classmethod
    def get_gpu_mem_info(cls, device=0, pretty_output=True):
        import pynvml
        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(device)
        info = pynvml.nvmlDeviceGetMemoryInfo(handle)
        info = dict(
            total=info.free,
            used=info.used,
            free=info.free,
        )
        if pretty_output:
            for k, v in info.items():
                info[k] = cls.TextVisualize.num_to_human_readable_str(v)

        return info

    @classmethod
    def get_mem_info(cls, pretty_output=True):
        info = dict(
            process_mem=cls.get_process_mem_info(pretty_output),
            env_mem=cls.get_cpu_mem_info(pretty_output),
            gpu_mem=cls.get_gpu_mem_info(pretty_output),
        )

        return info


saver = Saver()
loader = Loader()
retry = Retry()
auto_log = AutoLog()
ignore_exception = IgnoreException()
