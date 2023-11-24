import cv2
import torch
from torch import optim, nn
from metrics import text_generation
from data_parse.cv_data_parse.data_augmentation import crop, scale, geometry, channel, RandomApply, Apply, complex, pixel_perturbation
from data_parse import DataRegister
from pathlib import Path
from data_parse.cv_data_parse.base import DataVisualizer
from processor import Process, DataHooks, bundled, BaseDataset, MixDataset, IterDataset
from utils import configs, cv_utils, os_lib


class TrProcess(Process):
    def on_train_step(self, rets, container, **kwargs) -> dict:
        images = [torch.from_numpy(ret.pop('image')).to(self.device, non_blocking=True, dtype=torch.float) for ret in rets]
        transcription = [ret['transcription'] for ret in rets]
        images = torch.stack(images)
        output = self.model(images, transcription)
        return output

    def metric(self, **kwargs):
        container = self.predict(**kwargs)
        result = text_generation.top_metric.f_measure(container['trues'], container['preds'])

        result.update(
            score=result['f']
        )

        return result

    def on_val_step(self, rets, container, **kwargs) -> tuple:
        images = [torch.from_numpy(ret.pop('image')).to(self.device, non_blocking=True, dtype=torch.float) for ret in rets]
        images = torch.stack(images)
        transcription = [ret['transcription'] for ret in rets]

        outputs = container['model'](images)
        container['preds'].extend(outputs['pred'])
        container['trues'].extend(transcription)
        return rets, outputs

    def on_val_step_end(self, rets, outputs, container, is_visualize=False, batch_size=16, max_vis_num=None, **kwargs):
        if is_visualize:
            max_vis_num = max_vis_num or float('inf')
            counters = container['counters']
            n = min(batch_size, max_vis_num - counters['vis_num'])
            if n > 0:
                for ret, _p in zip(rets, outputs['pred']):
                    _id = Path(ret['_id'])
                    ret['_id'] = f'{_id.stem}({_p}){_id.suffix}'
                    ret['image'] = ret['ori_image']
                DataVisualizer(f'{self.cache_dir}/{counters["epoch"]}', verbose=False, pbar=False)(rets[:n])
                self.get_log_trace(bundled.WANDB).setdefault('val_image', []).extend(
                    [self.wandb.Image(cv2.cvtColor(ret['image'], cv2.COLOR_BGR2RGB), caption=ret['_id']) for ret in rets[:n]]
                )
                counters['vis_num'] += n


class DataProcess(DataHooks):
    train_data_num = int(5e5)
    val_data_num = int(5e4)

    aug = Apply([
        scale.LetterBox(pad_type=(crop.RIGHT, crop.CENTER)),
        channel.Keep3Dims(),
        # pixel_perturbation.MinMax(),
        # pixel_perturbation.Normalize(0.5, 0.5),
        pixel_perturbation.Normalize(127.5, 127.5),
        channel.HWC2CHW()
    ])

    def train_data_augment(self, ret) -> dict:
        ret.update(
            RandomApply([
                pixel_perturbation.GaussNoise(),
            ], probs=[0.2])(**ret)
        )
        ret.update(dst=self.input_size)
        ret.update(self.aug(**ret))

        return ret

    def val_data_augment(self, ret) -> dict:
        ret.update(dst=self.input_size)
        ret.update(self.aug(**ret))

        return ret

    def save_char_dict(self, char_dict):
        saver = os_lib.Saver(stdout_method=self.log)
        saver.save_json(char_dict, f'{self.work_dir}/char_dict.json')

    def load_char_dict(self):
        loader = os_lib.Loader(stdout_method=self.log)
        return loader.load_json(f'{self.work_dir}/char_dict.json')


class MJSynth(DataProcess):
    dataset_version = 'MJSynth'
    data_dir = 'data/MJSynth'

    input_size = (100, 32)  # make sure that image_w / 4 - 1 > max_len
    in_ch = 1
    out_features = 36  # 26 for a-z + 10 for 0-9
    max_seq_len = 25

    def get_train_data(self, *args, **kwargs):
        from data_parse.cv_data_parse.MJSynth import Loader

        loader = Loader(self.data_dir)
        iter_data = loader.load(
            set_type=DataRegister.TRAIN, image_type=DataRegister.GRAY_ARRAY, generator=False,
            return_lower=True,
            max_size=self.train_data_num,
        )[0]

        try:
            char_dict = self.load_char_dict()
        except:
            char_dict = {c: i + 1 for i, c in enumerate(loader.lower_char_list)}
        self.model.char2id = char_dict
        self.model.id2char = {v: k for k, v in char_dict.items()}
        self.save_char_dict(char_dict)

        return iter_data

    def get_val_data(self, *args, **kwargs):
        from data_parse.cv_data_parse.MJSynth import Loader

        loader = Loader(self.data_dir)
        iter_data = loader.load(
            set_type=DataRegister.VAL, image_type=DataRegister.GRAY_ARRAY, generator=False,
            return_lower=True,
            max_size=self.val_data_num,
        )[0]

        char_dict = self.load_char_dict()
        self.model.char2id = char_dict
        self.model.id2char = {v: k for k, v in char_dict.items()}

        return iter_data


class SynthText(DataProcess):
    # so slow...
    dataset_version = 'SynthText'
    data_dir = 'data/SynthText'
    train_dataset_ins = IterDataset
    train_dataset_ins.length = DataProcess.train_data_num

    input_size = (100, 32)  # make sure that image_w / 4 - 1 > max_len
    in_ch = 1
    out_features = 62  # 26 * 2 for a-z + 10 for 0-9
    max_seq_len = 25

    def get_train_data(self, *args, **kwargs):
        from data_parse.cv_data_parse.SynthOcrText import Loader

        loader = Loader(self.data_dir, verbose=False)
        iter_data = loader.load(
            image_type=DataRegister.GRAY_ARRAY, generator=True,
            max_size=self.train_data_num,
        )[0]

        try:
            char_dict = self.load_char_dict()
        except:
            char_list = loader.get_char_list()
            char_list.remove(' ')
            char_dict = {c: i + 1 for i, c in enumerate(char_list)}

        self.model.char2id = char_dict
        self.model.id2char = {v: k for k, v in char_dict.items()}
        self.save_char_dict(char_dict)

        return iter_data

    def get_val_data(self, *args, **kwargs):
        from data_parse.cv_data_parse.SynthOcrText import Loader

        loader = Loader(self.data_dir)
        iter_data = loader.load(
            image_type=DataRegister.GRAY_ARRAY, generator=False,
            max_size=self.val_data_num,
        )[0]

        char_dict = self.load_char_dict()
        self.model.char2id = char_dict
        self.model.id2char = {v: k for k, v in char_dict.items()}

        return iter_data


class MixMJSynthSynthText(DataProcess):
    dataset_version = 'MixMJSynthSynthText'
    data_dir1 = 'data/MJSynth'
    data_dir2 = 'data/SynthText'
    train_dataset_ins = MixDataset
    dataset_ratio = [0.5, 0.5]

    input_size = (100, 32)  # make sure that image_w / 4 - 1 > max_len
    in_ch = 1
    out_features = 62  # 26 * 2 for a-z + 10 for 0-9
    max_seq_len = 25

    def get_train_data(self, *args, **kwargs):
        from data_parse.cv_data_parse.MJSynth import Loader

        loader1 = Loader(self.data_dir1)
        num = int(self.train_data_num * self.dataset_ratio[0])
        iter_data1 = loader1.load(
            set_type=DataRegister.TRAIN, image_type=DataRegister.GRAY_ARRAY, generator=False,
            max_size=num,
        )[0]

        from data_parse.cv_data_parse.SynthOcrText import Loader

        loader2 = Loader(self.data_dir2, verbose=False)
        num = int(self.train_data_num * self.dataset_ratio[1])
        iter_data2 = loader2.load(
            image_type=DataRegister.GRAY_ARRAY, generator=True,
            max_size=num,
        )[0]
        IterDataset.length = num

        try:
            char_dict = self.load_char_dict()
        except:
            char_set = set(loader1.char_list)
            char_list = loader2.get_char_list()
            char_list.remove(' ')
            char_set |= set(char_list)
            char_dict = {c: i + 1 for i, c in enumerate(char_set)}

        self.model.char2id = char_dict
        self.model.id2char = {v: k for k, v in char_dict.items()}
        self.save_char_dict(char_dict)

        return (iter_data1, BaseDataset), (iter_data2, IterDataset)

    def get_val_data(self, *args, **kwargs):
        from data_parse.cv_data_parse.MJSynth import Loader

        loader = Loader(self.data_dir1)
        iter_data = loader.load(
            set_type=DataRegister.VAL, image_type=DataRegister.GRAY_ARRAY, generator=False,
            return_lower=True,
            max_size=self.val_data_num,
        )[0]

        char_dict = self.load_char_dict()
        self.model.char2id = char_dict
        self.model.id2char = {v: k for k, v in char_dict.items()}

        return iter_data


class CRNN(TrProcess):
    model_version = 'CRNN'

    def set_model(self):
        from models.text_recognition.crnn import Model

        self.model = Model(
            in_ch=self.in_ch,
            input_size=self.input_size,
            out_features=self.out_features,
            max_seq_len=self.max_seq_len
        )

    def set_optimizer(self):
        self.optimizer = optim.Adam(self.model.parameters(), lr=0.0005)


class CRNN_MJSynth(CRNN, MJSynth):
    """
    Usage:
        .. code-block:: python

            from examples.text_recognition import CRNN_MJSynth as Process

            Process().run(max_epoch=500, train_batch_size=256, predict_batch_size=256)
            {'score': 0.7878}
    """


class CRNN_SynthText(CRNN, SynthText):
    """
    Usage:
        .. code-block:: python

            from examples.text_recognition import CRNN_SynthText as Process

            Process().run(max_epoch=500, train_batch_size=256, predict_batch_size=256)
    """


class CRNN_MixMJSynthSynthText(CRNN, MixMJSynthSynthText):
    """
    Usage:
        .. code-block:: python

            from examples.text_recognition import CRNN_MixMJSynthSynthText as Process

            Process().run(max_epoch=500, train_batch_size=256, predict_batch_size=256)
    """


class Svtr(TrProcess):
    model_version = 'Svtr'

    def set_model(self):
        from models.text_recognition.svtr import Model
        self.model = Model(
            in_ch=self.in_ch,
            input_size=self.input_size,
            out_features=self.out_features,
            max_seq_len=self.max_seq_len
        )

    def set_optimizer(self):
        self.optimizer = optim.Adam(self.model.parameters(), lr=0.0005)


class Svtr_MJSynth(Svtr, MJSynth):
    """
    Usage:
        .. code-block:: python

            from examples.text_recognition import Svtr_MJSynth as Process

            Process().run(max_epoch=500, train_batch_size=256, predict_batch_size=256)
            {'score': 0.7962}
    """