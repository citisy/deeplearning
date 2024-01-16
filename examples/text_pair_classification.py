import torch
from .text_pretrain import TextPairProcess, Bert as Bert_


class MNLI(TextPairProcess):
    dataset_version = 'MNLI'
    data_dir = 'data/MNLI'

    # mean seq len is 36.91200452251326, max seq len is 441, min seq len is 2
    max_seq_len = 64
    n_classes = 3

    task = 'matched'
    # task = 'mismatched'

    def get_data(self, *args, train=True, **kwargs):
        from data_parse.nlp_data_parse.MNLI import Loader, DataRegister
        loader = Loader(self.data_dir)

        if train:
            return loader.load(set_type=DataRegister.TRAIN, max_size=self.train_data_num, generator=False)[0]
        else:
            return loader.load(set_type=DataRegister.DEV, max_size=self.val_data_num, generator=False, task=self.task)[0]


class QQP(TextPairProcess):
    dataset_version = 'QQP'
    data_dir = 'data/QQP'

    # mean seq len is 27.579311027192823, max seq len is 327, min seq len is 3
    max_seq_len = 64
    n_classes = 2

    def get_data(self, *args, train=True, **kwargs):
        from data_parse.nlp_data_parse.QQP import Loader, DataRegister
        loader = Loader(self.data_dir)

        if train:
            return loader.load(set_type=DataRegister.TRAIN, max_size=self.train_data_num, generator=False)[0]
        else:
            return loader.load(set_type=DataRegister.DEV, max_size=self.val_data_num, generator=False)[0]


class QNLI(TextPairProcess):
    dataset_version = 'QNLI'
    data_dir = 'data/QNLI'

    # mean seq len is 46.41800406709756, max seq len is 545, min seq len is 9
    max_seq_len = 64
    n_classes = 2

    def get_data(self, *args, train=True, **kwargs):
        from data_parse.nlp_data_parse.QNLI import Loader, DataRegister
        loader = Loader(self.data_dir)
        if train:
            return loader.load(set_type=DataRegister.TRAIN, max_size=self.train_data_num, generator=False)[0]
        else:
            return loader.load(set_type=DataRegister.DEV, max_size=self.val_data_num, generator=False)[0]


class MRPC(TextPairProcess):
    dataset_version = 'MRPC'
    data_dir = 'data/MRPC'

    # mean seq len is 50.24165848871443, max seq len is 100, min seq len is 16
    max_seq_len = 128
    n_classes = 2

    def get_data(self, *args, train=True, **kwargs):
        from data_parse.nlp_data_parse.MRPC import Loader, DataRegister
        loader = Loader(self.data_dir)
        if train:
            return loader.load(set_type=DataRegister.TRAIN, max_size=self.train_data_num, generator=False)[0]
        else:
            return loader.load(set_type=DataRegister.TEST, max_size=self.val_data_num, generator=False)[0]


class RTE(TextPairProcess):
    dataset_version = 'RTE'
    data_dir = 'data/RTE'

    # mean seq len is 67.19799196787149, max seq len is 286, min seq len is 10
    max_seq_len = 128
    n_classes = 2

    def get_data(self, *args, train=True, **kwargs):
        from data_parse.nlp_data_parse.RTE import Loader, DataRegister
        loader = Loader(self.data_dir)
        if train:
            return loader.load(set_type=DataRegister.TRAIN, max_size=self.train_data_num, generator=False)[0]
        else:
            return loader.load(set_type=DataRegister.DEV, max_size=self.val_data_num, generator=False)[0]


class Bert(Bert_):
    is_mlm = False  # only nsp strategy

    def set_model(self):
        from models.text_pair_classification.bert import Model

        self.get_vocab()
        self.model = Model(self.vocab_size, sp_tag_dict=self.sp_tag_dict, out_features=self.n_classes)

    def set_optimizer(self):
        self.optimizer = torch.optim.AdamW(self.model.parameters(), lr=5e-5)


class Bert_MNLI(Bert, MNLI):
    """
    Usage:
        .. code-block:: python

            from examples.text_pair_classification import Bert_MNLI as Process

            # about 200M data pretrain
            Process(pretrain_model='...', task='matched').run(max_epoch=5, train_batch_size=128, fit_kwargs=dict(check_period=1))
            {'score': 0.6782}  # match acc

            Process(pretrain_model='...', task='mismatched').run(max_epoch=5, train_batch_size=128, fit_kwargs=dict(check_period=1))
            {'score': 0.6891}  # mismatch acc
    """


class Bert_QQP(Bert, QQP):
    """
    Usage:
        .. code-block:: python

            from examples.text_pair_classification import Bert_QQP as Process

            # about 200M data pretrain
            Process(pretrain_model='...').run(max_epoch=5, train_batch_size=128, predict_batch_size=128, check_period=1)
            {'score': 0.86450/0.82584}    # acc/f1
    """


class Bert_QNLI(Bert, QNLI):
    """
    Usage:
        .. code-block:: python

            from examples.text_pair_classification import Bert_QNLI as Process

            # no pretrain data, use QNLI data to train directly
            Process().run(max_epoch=50, train_batch_size=128, fit_kwargs=dict(check_period=1))
            {'score': 0.5712}   # acc

            # about 200M data pretrain
            Process(pretrain_model='...').run(max_epoch=5, train_batch_size=128, fit_kwargs=dict(check_period=1))
            {'score': 0.8002}   # acc

            # use weights from huggingface
            from transformers import BertForSequenceClassification
            from models.text_pair_classification.bert import convert_hf_weights

            process = Process()
            model = BertForSequenceClassification.from_pretrained('...', num_labels=2)
            process.model.load_state_dict(convert_hf_weights(model.state_dict()))
            process.run(max_epoch=5, train_batch_size=128, fit_kwargs=dict(check_period=1))
            {'score': 0.89254}   # acc
    """


class BertFull_QNLI(Bert_, QNLI):
    """
    Usage:
        .. code-block:: python

            from examples.text_pair_classification import BertFull_QNLI as Process

            # no pretrain data, use QNLI data to train with nsp and mlm directly
            Process().run(max_epoch=50, train_batch_size=128, fit_kwargs=dict(check_period=1))
            {'score': 0.80395}   # acc
    """


class Bert_MRPC(Bert, MRPC):
    """
    Usage:
        .. code-block:: python

            from examples.text_pair_classification import Bert_QNLI as Process

            # about 200M data pretrain
            Process(pretrain_model='...').run(max_epoch=5, train_batch_size=128, fit_kwargs=dict(check_period=1))
            {'score': 0.71420/0.80032}   # acc/f1
    """


class Bert_RTE(Bert, RTE):
    """
    Usage:
        .. code-block:: python

            from examples.text_pair_classification import Bert_RTE as Process

            # about 200M data pretrain
            Process(pretrain_model='...').run(max_epoch=5, train_batch_size=128, fit_kwargs=dict(check_period=1))
            {'score': 0.57761}   # acc
    """
