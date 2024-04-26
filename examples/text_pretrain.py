import os
import re
import torch
import numpy as np
import pandas as pd
from typing import List, Optional
from utils import math_utils, os_lib, torch_utils
from processor import Process, DataHooks, BaseDataset, ModelHooks, CheckpointHooks, IterIterDataset
from data_parse.nlp_data_parse.pre_process import spliter, bundled, dict_maker, cleaner, snack


class RandomChoiceTextPairsDataset(BaseDataset):
    """for Next Sentence Prediction"""

    def __getitem__(self, idx):
        """all text pair in iter_data is the true text pair"""
        ret = self.iter_data[idx]
        texts = ret['texts']
        segment_pair = ret['segment_pair']
        segment_pair_tags_pair = ret['segment_pair_tags_pair']

        # 50% to select another text as the false sample
        if np.random.random() < 0.5:
            next_ret = np.random.choice(self.iter_data)
            next_text = next_ret['texts'][1]
            next_segment = next_ret['segment_pair'][1]
            next_segment_pair_tags = next_ret['segment_pair_tags_pair'][1]

            texts = (texts[0], next_text)
            segment_pair = (segment_pair[0], next_segment)
            segment_pair_tags_pair = (segment_pair_tags_pair[0], next_segment_pair_tags)
            _class = 0

        else:
            _class = 1

        ret = dict(
            texts=texts,
            segment_pair=segment_pair,
            segment_pair_tags_pair=segment_pair_tags_pair,
            _class=_class
        )

        return self.augment_func(ret)


class RandomReverseTextPairsDataset(BaseDataset):
    """for Sentence Order Prediction"""

    def __getitem__(self, idx):
        ret = self.iter_data[idx]
        text = ret['text']
        segment = ret['segment']
        segment_pair_tags = ret['segment_pair_tags']

        # 50% to reverse the text
        if np.random.random() < 0.5:
            next_segment = segment[::-1]
            _class = 0
        else:
            next_segment = segment
            _class = 1

        ret = dict(
            texts=(text, text),
            segment_pair=(segment, next_segment),
            segment_pair_tags_pair=(segment_pair_tags, [1] * len(segment_pair_tags)),
            _class=_class
        )

        return self.augment_func(ret)


class IterIterBatchDataset(IterIterDataset):
    """for loading large file"""

    def __iter__(self):
        from torch.utils.data import get_worker_info

        worker_info = get_worker_info()
        n = 1 if worker_info is None else worker_info.num_workers

        i = 0
        while i < self.length:
            rets = self.iter_data.get()
            for ret in self.process_batch(rets):
                yield ret

                i += n

    def process_batch(self, rets):
        if self.augment_func:
            rets = self.augment_func(rets)

        return rets


class DataProcessForBert(DataHooks):
    data_dir: str
    max_seq_len: int = 512

    val_dataset_ins = BaseDataset
    train_dataset_ins = BaseDataset

    train_data_num = None
    val_data_num = None

    is_mlm: bool
    is_nsp: bool

    tokenizer: bundled.BertTokenizer

    def _filter_func(self, x):
        if re.search('[0-9]', x):
            return False

        if re.search('[^a-z]', x):
            return False

        return True


class TextProcessForBert(DataProcessForBert):
    def make_vocab(self):
        # todo: make word piece
        sp_token_dict = self.tokenizer.sp_token_dict
        iter_data = self.get_train_data()
        paragraphs = [ret['text'] for ret in iter_data]
        paragraphs = cleaner.Lower().from_paragraphs(paragraphs)
        segments = self.tokenizer.spliter.from_paragraphs(paragraphs)
        word_dict = dict_maker.word_id_dict(segments, start_id=len(sp_token_dict), filter_func=self._filter_func)
        vocab = list(sp_token_dict.values()) + list(word_dict.keys())
        self.save_vocab(vocab)
        return vocab

    is_chunk = False

    def data_preprocess(self, iter_data, train=True):
        paragraphs = [ret['text'] for ret in iter_data]
        # ['bert-base-uncased'] -> [['bert', '-', 'base', '-', 'un', '##cased']]
        segments = self.tokenizer.spliter.from_paragraphs(paragraphs)
        if not self.is_nsp and self.is_chunk and train:
            segments = self.tokenizer.chunker_spliter.from_segments(segments)

            iter_data = []
            for segment in segments:
                iter_data.append(dict(
                    segment=segment,
                    segment_pair_tags=[0] * len(segment),
                    text=' '.join(segment)
                ))
        else:
            for ret, segment in zip(iter_data, segments):
                # _class need
                ret.update(
                    segment=segment,
                    segment_pair_tags=[0] * len(segment),
                    text=' '.join(segment)
                )
        return iter_data

    def count_seq_len(self):
        iter_data = self.get_train_data()
        iter_data = self.train_data_preprocess(iter_data)
        s = [len(ret['segment']) for ret in iter_data]
        self.log(f'mean seq len is {np.mean(s)}, max seq len is {np.max(s)}, min seq len is {np.min(s)}')

    def data_augment(self, ret, train=True) -> dict:
        _ret = ret
        ret = dict(ori_text=_ret['text'])
        segments = [_ret['segment']]
        segment_pair_tags = [_ret['segment_pair_tags']]
        if train and self.is_mlm:
            segments, mask_tags = self.tokenizer.perturbation.from_segments(segments)
            # while all([j == self.tokenizer.sp_id_dict['skip'] for i in mask_tags for j in i]):
            #     # to avoid nan loss
            #     segments, mask_tags = self.tokenizer.perturbation.from_segments(segments)
            ret.update(mask_tag=mask_tags[0])

        ret.update(
            segment=segments[0],
            segment_pair_tags=segment_pair_tags[0],
        )

        if self.is_nsp:
            ret.update(_class=_ret['_class'])

        return ret


class TextPairProcessForBert(DataProcessForBert):
    def make_vocab(self):
        # todo: make word piece
        sp_token_dict = self.tokenizer.sp_token_dict
        iter_data = self.get_train_data()
        paragraphs = [' '.join(ret['texts']) for ret in iter_data]
        paragraphs = cleaner.Lower().from_paragraphs(paragraphs)
        segments = self.tokenizer.spliter.from_paragraphs(paragraphs)
        word_dict = dict_maker.word_id_dict(segments, start_id=len(sp_token_dict), filter_func=self._filter_func)
        vocab = list(sp_token_dict.values()) + list(word_dict.keys())
        self.save_vocab(vocab)
        return vocab

    def data_preprocess(self, iter_data, train=True):
        text_pairs = [ret['texts'] for ret in iter_data]
        text_pairs = math_utils.transpose(text_pairs)
        tmp = []
        for paragraphs in text_pairs:
            segments = self.tokenizer.spliter.from_paragraphs(paragraphs)
            tmp.append(segments)

        segment_pairs = math_utils.transpose(tmp)
        for ret, segment_pair in zip(iter_data, segment_pairs):
            # todo, replace unused token by unknown word here, and then save the vocab
            ret.update(
                segment_pair=segment_pair,
                segment_pair_tags_pair=([0] * len(segment_pair[0]), [1] * len(segment_pair[1]))
            )

        return iter_data

    def count_seq_len(self):
        iter_data = self.get_train_data()
        iter_data = self.train_data_preprocess(iter_data)
        s = [len(ret['segment_pair'][0]) + len(ret['segment_pair'][1]) for ret in iter_data]
        self.log(f'mean seq len is {np.mean(s)}, max seq len is {np.max(s)}, min seq len is {np.min(s)}')

    def data_augment(self, ret, train=True) -> dict:
        """
        - dynamic mask(todo: add whole word mask)
        - add special token
        - encode(token id + segment id)
        """
        _ret = ret
        ret = dict(ori_text=_ret['texts'])
        segment_pairs = [_ret['segment_pair']]
        segment_pair_tags_pairs = [_ret['segment_pair_tags_pair']]

        if train and self.is_mlm:
            segment_pairs = math_utils.transpose(segment_pairs)
            tmp = []
            tmp2 = []
            for segments in segment_pairs:
                segments, mask_tags = self.tokenizer.perturbation.from_segments(segments)
                tmp.append(segments)
                tmp2.append(mask_tags)

            segment_pairs = math_utils.transpose(tmp)

            mask_tags_pairs = math_utils.transpose(tmp2)
            mask_tags = snack.joint(mask_tags_pairs, sep_obj=self.tokenizer.skip_id, keep_end=False)
            ret.update(mask_tag=mask_tags[0])

        segments = snack.joint(segment_pairs, sep_obj=self.tokenizer.sep_token, keep_end=False)
        segment_pair_tags = snack.joint(segment_pair_tags_pairs, sep_obj=0, keep_end=False)

        ret.update(
            segment=segments[0],
            segment_pair_tags=segment_pair_tags[0]
        )

        if self.is_nsp:
            ret.update(_class=_ret['_class'])

        return ret


class SimpleTextForBert(TextProcessForBert):
    dataset_version = 'simple_text'
    data_dir: str

    def get_data(self, *args, train=True, **kwargs):
        from data_parse.nlp_data_parse.SimpleText import Loader, DataRegister
        loader = Loader(self.data_dir)

        if train:
            return loader.load(set_type=DataRegister.TRAIN, max_size=self.train_data_num, return_label=self.is_nsp, generator=False)[0]

        else:
            return loader.load(set_type=DataRegister.TEST, max_size=self.val_data_num, return_label=self.is_nsp, generator=False)[0]


class SimpleTextPairForBert(TextPairProcessForBert):
    dataset_version = 'simple_text_pair'
    data_dir: str

    def get_data(self, *args, train=True, **kwargs):
        from data_parse.nlp_data_parse.SimpleTextPair import Loader, DataRegister
        loader = Loader(self.data_dir)

        if train:
            return loader.load(set_type=DataRegister.TRAIN, max_size=self.train_data_num, generator=False)[0]
        else:
            return loader.load(set_type=DataRegister.TEST, max_size=self.val_data_num, generator=False)[0]


class LargeSimpleTextForBert(DataProcessForBert):
    """for loading large file"""
    dataset_version = 'simple_text'
    one_step_data_num = int(1e6)
    is_chunk = False

    def get_data(self, *args, train=True, batch_size=None, **kwargs):
        from data_parse.nlp_data_parse.SimpleText import Loader, DataRegister
        import multiprocessing

        def gen_func():
            loader = Loader(self.data_dir)

            if train:
                iter_data = loader.load(set_type=DataRegister.TRAIN, max_size=self.train_data_num, return_label=self.is_nsp, generator=True)[0]
            else:
                iter_data = loader.load(set_type=DataRegister.TEST, max_size=self.val_data_num, return_label=self.is_nsp, generator=True)[0]

            rets = []
            for i, ret in enumerate(iter_data):
                rets.append(ret)
                if i % batch_size == batch_size - 1:
                    yield rets
                    rets = []

                if rets:
                    yield rets

        def producer(q):
            iter_data = gen_func()
            while True:
                if not q.full():
                    try:
                        q.put(next(iter_data))
                    except StopIteration:
                        iter_data = gen_func()
                        q.put(next(iter_data))

        q = multiprocessing.Queue(8)
        p = multiprocessing.Process(target=producer, args=(q,))
        p.daemon = True
        p.start()

        if train:
            return IterIterBatchDataset(q, length=self.one_step_data_num, augment_func=self.train_data_augment)
        else:
            return IterIterBatchDataset(q, length=self.one_step_data_num, augment_func=self.val_data_augment)

    def data_augment(self, rets, train=True) -> List[dict]:
        """preprocess + data_augment"""
        rets = TextProcessForBert.data_preprocess(self, rets, train)
        rets = [TextProcessForBert.data_augment(self, ret, train) for ret in rets]
        return rets


class SOP(DataProcessForBert):
    train_dataset_ins = RandomReverseTextPairsDataset
    val_dataset_ins = RandomReverseTextPairsDataset

    dataset_version = 'simple_text'
    is_chunk = False

    def get_data(self, *args, train=True, **kwargs):
        from data_parse.nlp_data_parse.SimpleText import Loader, DataRegister
        loader = Loader(self.data_dir)

        if train:
            return loader.load(set_type=DataRegister.TRAIN, max_size=self.train_data_num, return_label=False, generator=False)[0]
        else:
            return loader.load(set_type=DataRegister.TEST, max_size=self.val_data_num, return_label=False, generator=False)[0]

    def make_vocab(self):
        return TextProcessForBert.make_vocab(self)

    def count_seq_len(self):
        return TextProcessForBert.count_seq_len(self)

    def data_preprocess(self, iter_data, train=True):
        return TextProcessForBert.data_preprocess(self, iter_data, train)

    def data_augment(self, ret, train=True) -> dict:
        return TextPairProcessForBert.data_augment(self, ret, train)


class Bert(Process):
    model_version = 'bert'
    is_mlm = True
    is_nsp = True
    use_scaler = True
    scheduler_strategy = 'step'  # step
    tokenizer: bundled.BertTokenizer
    max_seq_len: int

    def set_model(self):
        from models.text_pretrain.bert import Model
        self.get_vocab()
        self.model = Model(
            self.tokenizer.vocab_size,
            pad_id=self.tokenizer.pad_id,
            skip_id=self.tokenizer.skip_id,
            is_nsp=self.is_nsp, is_mlm=self.is_mlm
        )

    def get_vocab(self):
        vocab = super().get_vocab()
        self.tokenizer = bundled.BertTokenizer(vocab, max_seq_len=self.max_seq_len)

    def set_optimizer(self):
        # todo, use the optimizer config from paper(lr=1e-4, betas=(0.9, 0.999), weight_decay=0.1), the training is failed
        # in RoBERTa, beta_2=0.98
        self.optimizer = torch.optim.Adam(self.model.parameters(), lr=1e-4, betas=(0.5, 0.999))

    def get_model_inputs(self, rets, train=True):
        segments = [ret['segment'] for ret in rets]
        segment_pair_tags = [ret['segment_pair_tags'] for ret in rets]
        lens = [len(seg) for seg in segments]
        r = self.tokenizer.encode_segments(segments, segment_pair_tags)
        r = torch_utils.Converter.arrays_to_tensors(r, self.device)
        inputs = dict(
            x=r['segments_ids'],
            segment_label=r['segment_pair_tags'],
            attention_mask=r['valid_segment_tags'],
            lens=lens
        )

        if train:
            next_tags = torch.tensor([ret['_class'] for ret in rets]).to(self.device) if self.is_nsp else None
            mask_tags = None
            if self.is_mlm:
                mask_tags = [ret['mask_tag'] for ret in rets]
                mask_tags = snack.align(
                    mask_tags, max_seq_len=self.max_seq_len,
                    start_obj=self.tokenizer.skip_id, end_obj=self.tokenizer.skip_id, pad_obj=self.tokenizer.skip_id
                )
                mask_tags = torch.tensor(mask_tags).to(self.device)

            inputs.update(
                next_true=next_tags,
                mask_true=mask_tags
            )

        return inputs

    def on_train_step(self, rets, **kwargs) -> dict:
        inputs = self.get_model_inputs(rets)
        with torch.cuda.amp.autocast(True):
            output = self.model(**inputs)

        return output

    def metric(self, *args, return_score='full', **kwargs) -> dict:
        """

        Args:
            *args:
            return_score: 'full', 'next' or 'mask'
            **kwargs:

        """
        from metrics import classification
        container = self.predict(**kwargs)

        metric_results = {}
        for name, results in container['model_results'].items():
            result = {}
            if self.is_nsp:
                next_trues = np.array(results['next_trues'])
                next_preds = np.array(results['next_preds'])
                acc = np.sum(next_trues == next_preds) / next_trues.size
                next_result = classification.top_metric.f1(next_trues, next_preds)
                result.update({
                    'score.acc': acc,
                    'score.f1': next_result['f'],
                    **next_result})

            if self.is_mlm:
                mask_trues = results['mask_trues']
                mask_preds = results['mask_preds']

                skip_id = self.tokenizer.skip_id
                mask_trues = snack.align(mask_trues, max_seq_len=self.max_seq_len, pad_obj=skip_id, auto_pad=False)
                mask_preds = snack.align(mask_preds, max_seq_len=self.max_seq_len, pad_obj=skip_id, auto_pad=False)

                mask_trues = np.array(mask_trues)
                mask_preds = np.array(mask_preds)
                n_quit = np.sum((mask_trues == skip_id) & (mask_preds == skip_id))
                n_true = np.sum(mask_trues == mask_preds)
                mask_score = (n_true - n_quit) / (mask_trues.size - n_quit)
                result.update({'score.mask': mask_score})

            if return_score == 'next':
                result.update(score=result['score.acc'])
            elif return_score == 'mask':
                result.update(score=result['score.mask'])
            elif return_score == 'full':
                if not self.is_nsp:
                    result.update(score=result['score.mask'])
                elif not self.is_mlm:
                    result.update(score=result['score.acc'])
                else:
                    result.update(score=(result['score.acc'] + result['score.mask']) / 2)
            else:
                raise

            metric_results[name] = result

        return metric_results

    def on_val_step(self, rets, **kwargs) -> dict:
        model_inputs = self.get_model_inputs(rets, train=False)

        model_results = {}
        for name, model in self.models.items():
            outputs = model(**model_inputs)

            ret = dict()

            if self.is_nsp:
                ret.update(
                    next_outputs=outputs['next_pred'],
                    next_preds=outputs['next_pred'].argmax(1).cpu().numpy().tolist(),
                )

            if self.is_mlm:
                lens = model_inputs['lens']
                mask_preds = outputs['mask_pred'].argmax(-1).cpu().numpy().tolist()
                mask_preds = [preds[1: l + 1] for preds, l in zip(mask_preds, lens)]
                mask_trues = model_inputs['x'].cpu().numpy().tolist()
                mask_trues = [t[1: l + 1] for t, l in zip(mask_trues, lens)]
                ret.update(
                    mask_outputs=outputs['mask_pred'],
                    mask_preds=mask_preds,
                    mask_trues=mask_trues,
                    pred_segment=self.tokenizer.numeralizer.decode(mask_preds),
                    true_segment=[ret['segment'] for ret in rets]
                )

            model_results[name] = ret

        return model_results

    def on_val_reprocess(self, rets, model_results, **kwargs):
        for name, results in model_results.items():
            r = self.val_container['model_results'].setdefault(name, dict())
            r.setdefault('texts', []).extend([ret['ori_text'] for ret in rets])

            if self.is_nsp:
                r.setdefault('next_trues', []).extend([ret['_class'] for ret in rets])
                r.setdefault('next_preds', []).extend(results['next_preds'])

            if self.is_mlm:
                r.setdefault('mask_trues', []).extend(results['mask_trues'])
                r.setdefault('mask_preds', []).extend(results['mask_preds'])

    def on_val_step_end(self, *args, **kwargs):
        """do not visualize"""

    def on_val_end(self, is_visualize=False, max_vis_num=None, **kwargs):
        # todo: make a file to be submitted to https://gluebenchmark.com directly
        if is_visualize:
            for name, results in self.val_container['model_results'].items():
                data = []
                vis_num = max_vis_num or len(results['texts'])
                for i in range(vis_num):
                    text = results['texts'][i]
                    d = dict()
                    if isinstance(text, str):
                        d['text'] = text
                    else:
                        d['text1'] = text[0]
                        d['text2'] = text[1]

                    if self.is_nsp:
                        d['next_true'] = results['next_trues'][i]
                        d['next_pred'] = results['next_preds'][i]

                    if self.is_mlm:
                        d['mask_true'] = results['mask_trues'][i]
                        d['mask_pred'] = results['mask_preds'][i]

                    data.append(d)
                df = pd.DataFrame(data)
                os_lib.Saver(stdout_method=self.log).auto_save(df, f'{self.cache_dir}/{self.counters["epoch"]}/{name}.csv', index=False)

    def gen_predict_inputs(self, *objs, start_idx=None, end_idx=None, **kwargs):
        rets = []
        for text in objs[0][start_idx: end_idx]:
            ret = dict(text=text)
            rets.append(ret)
        rets = self.val_data_preprocess(rets)
        return rets

    def on_predict_step_end(self, model_results, **kwargs):
        for name, results in model_results.items():
            self.predict_container['model_results'].setdefault(name, []).extend(results['pred_segment'])


class LoadBertFromHFPretrain(CheckpointHooks):
    """load pretrain model from hugging face"""

    def load_pretrain(self):
        if hasattr(self, 'pretrain_model'):
            from models.text_pretrain.bert import WeightLoader, WeightConverter
            state_dict = WeightLoader.from_hf(self.pretrain_model)
            state_dict = WeightConverter.from_hf(state_dict)
            self.model.load_state_dict(state_dict, strict=False)


class BertMLMFromHFPretrain(Bert, LoadBertFromHFPretrain, TextProcessForBert):
    """
    Usage:
        .. code-block:: python

            from examples.text_pretrain import BertMLMFromHFPretrain as Process

            process = Process(pretrain_model='...', vocab_fn='...')
            process.init()

            # if using `bert-base-uncased` pretrain model
            process.single_predict('The goal of life is [MASK].')
            # ['the', 'goal', 'of', 'life', 'is', 'life', '.']

            process.batch_predict([
                'The goal of life is [MASK].',
                'Paris is the [MASK] of France.'
            ])
            # ['the', 'goal', 'of', 'life', 'is', 'life', '.']
            # ['.', 'is', 'the', 'capital', 'of', 'france', '.']
    """
    dataset_version = ''
    is_nsp = False
    is_chunk = False


class BertMLM_SimpleText(Bert, SimpleTextForBert):
    """
    Usage:
        .. code-block:: python

            from examples.text_pretrain import BertMLM_SimpleText as Process

            Process(vocab_fn='...').run(max_epoch=20, train_batch_size=16, fit_kwargs=dict(check_period=1, accumulate=192))
    """
    is_nsp = False
    is_chunk = True


class Bert_SOP(Bert, SOP):
    """
    Usage:
        .. code-block:: python

            from examples.text_pretrain import BertMLM_SimpleText as Process

            # about 200M data
            Process(vocab_fn='...').run(max_epoch=20, train_batch_size=16, fit_kwargs=dict(check_period=1, accumulate=192))
            {'score': 0.931447}
    """


class TextProcessForGpt(DataHooks):
    data_dir: str
    max_seq_len: int = 512

    val_dataset_ins = BaseDataset
    train_dataset_ins = BaseDataset

    train_data_num = None
    val_data_num = None

    tokenizer: bundled.GPT2Tokenizer

    def data_preprocess(self, iter_data, train=True):
        paragraphs = [ret['text'] for ret in iter_data]
        # ['hello world!'] -> [['hello', ' world', '!']]
        segments = self.tokenizer.spliter.from_paragraphs(paragraphs)

        for ret, segment in zip(iter_data, segments):
            ret.update(
                segment=segment,
                segment_pair_tags=[0] * len(segment),
                text=''.join(segment)
            )

        return iter_data


class SimpleTextForGpt(TextProcessForGpt):
    dataset_version = 'simple_text'
    data_dir: str

    def get_data(self, *args, train=True, **kwargs):
        from data_parse.nlp_data_parse.SimpleText import Loader, DataRegister
        loader = Loader(self.data_dir)

        if train:
            return loader.load(set_type=DataRegister.TRAIN, max_size=self.train_data_num, return_label=True, generator=False)[0]

        else:
            return loader.load(set_type=DataRegister.TEST, max_size=self.val_data_num, return_label=True, generator=False)[0]


class GPT2(Process):
    model_version = 'GPT2'
    use_scaler = True
    scheduler_strategy = 'step'  # step
    tokenizer: bundled.GPT2Tokenizer
    max_seq_len: int
    max_gen_len = 20

    def set_model(self):
        from models.text_pretrain.gpt2 import Model, Config
        self.get_vocab()
        self.model = Model(
            self.tokenizer.vocab_size,
            pad_id=self.tokenizer.pad_id,
            **Config.get('117M')
        )

    encoder_fn: str

    def get_vocab(self):
        self.tokenizer = bundled.GPT2Tokenizer.from_pretrain(self.encoder_fn, self.vocab_fn)

    def set_optimizer(self):
        self.optimizer = torch.optim.Adam(self.model.parameters(), lr=1e-4, betas=(0.5, 0.999))

    def get_model_inputs(self, rets, train=True):
        segments = [ret['segment'] for ret in rets]
        r = self.tokenizer.encode_segments(segments)
        return dict(
            x=torch.tensor(r['segments_ids'], device=self.device, dtype=torch.long),
            seq_lens=r['seq_lens'],
            max_gen_len=self.max_gen_len
        )

    def on_train_step(self, rets, **kwargs) -> dict:
        inputs = self.get_model_inputs(rets)
        with torch.cuda.amp.autocast(True):
            output = self.model(**inputs)

        return output

    def on_val_step(self, rets, **kwargs) -> dict:
        inputs = self.get_model_inputs(rets, train=False)
        seq_lens = inputs['seq_lens']
        max_gen_len = inputs['max_gen_len']

        model_results = {}
        for name, model in self.models.items():
            outputs = model(**inputs)

            ret = dict()

            preds = outputs['preds'].cpu().numpy().tolist()
            preds = [pred[:seq_lens[i] + max_gen_len] for i, pred in enumerate(preds)]
            ret.update(
                preds=preds,
                pred_segment=self.tokenizer.numerizer.decode(preds)
            )

            model_results[name] = ret

        return model_results

    def on_val_step_end(self, *args, **kwargs):
        """do not visualize"""

    def gen_predict_inputs(self, *objs, start_idx=None, end_idx=None, **kwargs):
        rets = []
        for text in objs[0][start_idx: end_idx]:
            ret = dict(text=text)
            rets.append(ret)
        rets = self.val_data_preprocess(rets)
        return rets

    def on_predict_step_end(self, model_results, **kwargs):
        for name, results in model_results.items():
            self.predict_container['model_results'].setdefault(name, []).extend(results['pred_segment'])


class LoadGPT2FromOpenaiPretrain(CheckpointHooks):
    """load pretrain model from openai"""

    def load_pretrain(self):
        if hasattr(self, 'pretrain_model'):
            from models.text_pretrain.gpt2 import WeightConverter, WeightLoader

            state_dict = WeightLoader.from_openai_tf(self.pretrain_model, n_layer=self.model.n_layer)
            state_dict = WeightConverter.from_openai(state_dict)
            self.model.load_state_dict(state_dict, strict=False)


class LoadGPT2FromHFPretrain(CheckpointHooks):
    """load pretrain model from huggingface"""

    def load_pretrain(self):
        if hasattr(self, 'pretrain_model'):
            from models.text_pretrain.gpt2 import WeightLoader, WeightConverter
            state_dict = WeightLoader.from_hf(self.pretrain_model)
            self.model.load_state_dict(WeightConverter.from_huggingface(state_dict), strict=False)


class GPT2FromOpenaiPretrain(GPT2, LoadGPT2FromOpenaiPretrain, TextProcessForGpt):
    """
    Usage:
        .. code-block:: python

            from examples.text_pretrain import GPT2FromOpenaiPretrain as Process

            process = Process(pretrain_model='...', vocab_fn='...', encoder_fn='...')
            process.init()

            # if using `117M` pretrain model
            process.single_predict('My name is Julien and I like to')
            # My name is Julien and I like to play with my friends. I'm a big fan of the game and I'm looking forward to playing

            process.batch_predict([
                'My name is Julien and I like to',
                'My name is Thomas and my main'
            ])
            # My name is Julien and I like to play with my friends. I'm a big fan of the game and I'm looking forward to playing
            # My name is Thomas and my main goal is to make sure that I'm not just a guy who's going to be a part of
    """
    dataset_version = 'openai_pretrain'


class SimpleTextForT5(DataHooks):
    dataset_version = 'simple_text'
    data_dir: str

    max_seq_len: int = 512

    val_dataset_ins = BaseDataset
    train_dataset_ins = BaseDataset

    train_data_num = None
    val_data_num = None

    tokenizer: bundled.T5Tokenizer

    def get_data(self, *args, train=True, **kwargs):
        from data_parse.nlp_data_parse.SimpleText import Loader, DataRegister
        loader = Loader(self.data_dir)

        if train:
            return loader.load(set_type=DataRegister.TRAIN, max_size=self.train_data_num, return_label=True, generator=False)[0]

        else:
            return loader.load(set_type=DataRegister.TEST, max_size=self.val_data_num, return_label=True, generator=False)[0]


class T5(Process):
    model_version = 'T5'
    use_scaler = True
    scheduler_strategy = 'step'  # step
    tokenizer: bundled.T5Tokenizer
    max_seq_len: int
    max_gen_len = 20

    def set_model(self):
        from models.text_pretrain.T5 import Model, Config
        self.get_vocab()
        self.model = Model(
            self.tokenizer.vocab_size,
            eos_id=self.tokenizer.eos_id,
            **Config.get('small')
        )

    encoder_fn: str

    def get_vocab(self):
        self.tokenizer = bundled.T5Tokenizer.from_pretrain(self.vocab_fn)

    def set_optimizer(self):
        self.optimizer = torch.optim.Adam(self.model.parameters(), lr=1e-4, betas=(0.5, 0.999))

    def get_model_inputs(self, rets, train=True):
        paragraphs = [ret['text'] for ret in rets]
        inputs = self.tokenizer.encode(paragraphs)
        inputs = torch_utils.Converter.arrays_to_tensors(inputs, self.device)
        return dict(
            x=inputs['segments_ids'],
            seq_lens=inputs['seq_lens'],
            attention_mask=inputs['valid_segment_tags'],
            max_gen_len=self.max_gen_len
        )

    def on_train_step(self, rets, **kwargs) -> dict:
        inputs = self.get_model_inputs(rets)
        with torch.cuda.amp.autocast(True):
            output = self.model(**inputs)

        return output

    def on_val_step(self, rets, **kwargs) -> dict:
        inputs = self.get_model_inputs(rets, train=False)
        inputs.pop('seq_lens')
        inputs.pop('max_gen_len')

        model_results = {}
        for name, model in self.models.items():
            outputs = model(**inputs)

            ret = dict()

            preds = outputs['preds'].cpu().numpy().tolist()
            ret.update(
                preds=preds,
                pred_segment=self.tokenizer.decode(preds)
            )

            model_results[name] = ret

        return model_results

    def on_val_step_end(self, *args, **kwargs):
        """do not visualize"""

    def gen_predict_inputs(self, *objs, start_idx=None, end_idx=None, **kwargs):
        rets = []
        for text in objs[0][start_idx: end_idx]:
            ret = dict(text=text)
            rets.append(ret)
        rets = self.val_data_preprocess(rets)
        return rets

    def on_predict_step_end(self, model_results, **kwargs):
        for name, results in model_results.items():
            self.predict_container['model_results'].setdefault(name, []).extend(results['pred_segment'])


class LoadT5FromHFPretrain(CheckpointHooks):
    """load pretrain model from huggingface"""

    def load_pretrain(self):
        if hasattr(self, 'pretrain_model'):
            from models.text_pretrain.T5 import WeightLoader, WeightConverter
            state_dict = WeightLoader.from_hf(self.pretrain_model)
            state_dict = WeightConverter.from_hf(state_dict)
            self.model.load_state_dict(state_dict, strict=False)


class T5FromHFPretrain(T5, LoadT5FromHFPretrain, SimpleTextForT5):
    """
    Usage:
        .. code-block:: python

            from examples.text_pretrain import T5FromHFPretrain as Process

            process = Process(pretrain_model='...', vocab_fn='...', encoder_fn='...')
            process.init()

            # if using `117M` pretrain model
            process.single_predict('translate English to German: The house is wonderful.')
            # Das Haus ist wunderbar.

            process.batch_predict([
                'translate English to German: The house is wonderful.'
                'summarize: studies have shown that owning a dog is good for you',
            ])
            # Das Haus ist wunderbar.
            # studies have shown that owning a dog is good for you .
    """
    dataset_version = 'huggingface_pretrain'
