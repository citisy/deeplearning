import cv2
import numpy as np
import torch
from torch import nn, optim
from torch.utils.data import DataLoader
from tqdm import tqdm
from models.layers import SimpleInModule
from utils.os_lib import MemoryInfo
from metrics import classifier
from data_parse.cv_data_parse.data_augmentation import crop, scale, geometry, RandomApply
from data_parse import DataRegister
from .base import Process, BaseDataset


class ClsDataset(BaseDataset):
    def __getitem__(self, idx):
        ret = super().__getitem__(idx)
        image, _class = ret['image'], ret['_class']

        return torch.Tensor(image), _class


class ClsProcess(Process):
    dataset = ClsDataset

    def fit(self, dataset, max_epoch, batch_size, save_period=None, dataloader_kwargs=dict()):
        dataloader = DataLoader(dataset, shuffle=True, batch_size=batch_size, **dataloader_kwargs)

        self.model.to(self.device)

        optimizer = optim.Adam(self.model.parameters())
        loss_func = nn.CrossEntropyLoss()
        score = -1

        self.model.train()

        for i in range(max_epoch):
            pbar = tqdm(dataloader, desc=f'train {i}/{max_epoch}')

            for data, label in pbar:
                data = data.to(self.device, non_blocking=True)
                label = label.to(self.device)

                optimizer.zero_grad()

                pred = self.model(data)
                loss = loss_func(pred, label)

                loss.backward()
                optimizer.step()

                pbar.set_postfix({
                    'loss': f'{loss.item():.06}',
                    'cpu_info': MemoryInfo.get_process_mem_info(),
                    'gpu_info': MemoryInfo.get_gpu_mem_info()
                })

            if save_period and i % save_period == save_period - 1:
                self.save(f'{self.model_dir}/{self.dataset_version}_last.pth')

                val_data = self.get_val_data()
                val_dataset = self.dataset(val_data, augment_func=self.val_data_augment)
                result = self.metric(val_dataset, batch_size)
                if result['score'] > score:
                    self.save(f'{self.model_dir}/{self.dataset_version}_best.pth')
                    score = result['score']

    def predict(self, dataset, batch_size=128, dataloader_kwargs=dict()):
        dataloader = DataLoader(dataset, batch_size=batch_size, **dataloader_kwargs)  # 单卡shuffle=True，多卡shuffle=False

        pred = []
        true = []
        with torch.no_grad():
            self.model.eval()
            for data, label in tqdm(dataloader):
                data = data.to(self.device)
                label = label.cpu().detach().numpy()

                p = self.model(data).cpu().detach().numpy()
                p = np.argmax(p, axis=1)

                pred.extend(p.tolist())
                true.extend(label.tolist())

        pred = np.array(pred)
        true = np.array(true)

        return true, pred

    def metric(self, dataset, batch_size=128, **kwargs):
        true, pred = self.predict(dataset, batch_size, **kwargs)

        result = classifier.top_metric.f_measure(true, pred)

        result.update(
            score=result['f']
        )

        return result

    def data_augment(self, ret):
        ret.update(dst=self.input_size)
        ret.update(crop.Random()(**ret))
        ret.update(RandomApply([
            geometry.HFlip(),
            geometry.VFlip(),
        ])(**ret))
        return super().data_augment(ret)

    def get_train_data(self):
        """example"""
        from data_parse.cv_data_parse.ImageNet import Loader

        loader = Loader(f'data/ImageNet2012')
        convert_class = {7: 0, 40: 1}

        data = loader(set_type=DataRegister.TRAIN, image_type=DataRegister.ARRAY, generator=False,
                      wnid=[
                          'n02124075',  # Egyptian cat,
                          'n02110341'  # dalmatian, coach dog, carriage dog
                      ]
                      )[0]

        for tmp in data:
            tmp['_class'] = convert_class[tmp['_class']]

        return data

    def get_val_data(self):
        """example"""
        from data_parse.cv_data_parse.ImageNet import Loader
        loader = Loader(f'data/ImageNet2012')
        convert_class = {7: 0, 40: 1}
        cache_data = loader(set_type=DataRegister.VAL, image_type=DataRegister.PATH, generator=False)[0]
        data = []

        for tmp in cache_data:
            if tmp['_class'] not in [7, 40]:
                continue

            tmp['_class'] = convert_class[tmp['_class']]
            x = cv2.imread(tmp['image'])
            x = scale.Proportion()(x, 256)['image']
            x = crop.Center()(x, self.input_size)['image']
            tmp['image'] = x

            data.append(tmp)

        return data


class LeNet_mnist(ClsProcess):
    """
    Usage:
        .. code-block:: python

            from examples.image_cls import LeNet_mnist as Process

            Process().run(max_epoch=10, train_batch_size=256, predict_batch_size=256)
            {'score': 0.9899}
    """

    def __init__(self):
        from models.image_classifier.LeNet import Model

        in_ch = 1
        input_size = 28
        output_size = 10

        super().__init__(
            model=Model(in_ch, input_size, output_size),
            model_version='LeNet',
            dataset_version='mnist'
        )

    def metric(self, dataset, batch_size=128):
        true, pred = self.predict(dataset, batch_size)

        acc = np.sum(true == pred) / len(true)

        return dict(score=acc)

    def get_train_data(self):
        from data_parse.cv_data_parse.Mnist import Loader

        loader = Loader('data/mnist')

        return loader(set_type=DataRegister.TRAIN, image_type=DataRegister.ARRAY, generator=False)[0]

    def get_val_data(self):
        from data_parse.cv_data_parse.Mnist import Loader

        loader = Loader('data/mnist')

        return loader(set_type=DataRegister.TEST, image_type=DataRegister.ARRAY, generator=False)[0]

    def data_augment(self, ret):
        return ret


class LeNet_cifar(ClsProcess):
    """
    Usage:
        .. code-block:: python

            from examples.image_cls import LeNet_cifar as Process

            Process().run(max_epoch=150, train_batch_size=128, predict_batch_size=256)
            {'score': 0.6082}
    """

    def __init__(self):
        from models.image_classifier.LeNet import Model

        in_ch = 3
        input_size = 32
        output_size = 10

        super().__init__(
            model=Model(in_ch, input_size, output_size),
            model_version='LeNet',
            dataset_version='cifar-10-batches-py',
            input_size=input_size
        )

    def metric(self, dataset, batch_size=128):
        true, pred = self.predict(dataset, batch_size)

        acc = np.sum(true == pred) / len(true)

        return dict(score=acc)

    def get_train_data(self):
        from data_parse.cv_data_parse.Cifar import Loader

        loader = Loader('data/cifar-10-batches-py')

        return loader(set_type=DataRegister.TRAIN, image_type=DataRegister.ARRAY, generator=False)

    def get_val_data(self):
        from data_parse.cv_data_parse.Cifar import Loader

        loader = Loader('data/cifar-10-batches-py')

        return loader(set_type=DataRegister.TEST, image_type=DataRegister.ARRAY, generator=False)


class AlexNet_ImageNet(ClsProcess):
    """
    Usage:
        .. code-block:: python

            from examples.image_cls import AlexNet_ImageNet as Process

            Process().run()
            {'p': 0.8461538461538461, 'r': 0.88, 'f': 0.8627450980392156}
    """

    def __init__(self):
        from models.image_classifier.AlexNet import Model

        in_ch = 3
        input_size = 224
        output_size = 2

        super().__init__(
            model=Model(in_ch, input_size, output_size),
            model_version='AlexNet',
            input_size=input_size
        )

    def get_train_data(self):
        data = super().get_train_data()
        for tmp in data:
            tmp['image'] = scale.Proportion()(tmp['image'], 256)['image']

        return data


class Vgg_ImageNet(ClsProcess):
    """
    Usage:
        .. code-block:: python

            from examples.image_cls import Vgg_ImageNet as Process

            Process().run()
            {'p': 0.9230769230769231, 'r': 0.96, 'f': 0.9411764705882353, 'score': 0.9411764705882353}
    """

    def __init__(self):
        from models.image_classifier.VGG import Model

        in_ch = 3
        input_size = 224
        output_size = 2

        super().__init__(
            model=Model(in_ch, input_size, output_size),
            model_version='Vgg',
            input_size=input_size
        )

    def data_augment(self, ret):
        ret.update(dst=self.input_size)
        ret.update(scale.Jitter((256, 384))(**ret))
        ret.update(RandomApply([geometry.HFlip()])(**ret))
        return ret


class Inception_ImageNet(ClsProcess):
    """
    Usage:
        .. code-block:: python

            from examples.image_cls import Inception_ImageNet as Process

            Process().run()
            {'p': 0.8363636363636363, 'r': 0.92, 'f': 0.8761904761904761, 'score': 0.8761904761904761}

            Process(model_version='InceptionV3').run()
            {'p': 0.98, 'r': 0.98, 'f': 0.98, 'score': 0.98}
    """

    def __init__(self, model_version='InceptionV1'):
        if model_version == 'InceptionV1':
            from models.image_classifier.InceptionV1 import Model
            input_size = 224

        elif model_version == 'InceptionV3':
            from models.image_classifier.InceptionV3 import InceptionV3 as Model
            input_size = 299

        else:
            raise ValueError(f'dont support {model_version = }')

        in_ch = 3
        output_size = 2

        super().__init__(
            model=Model(in_ch, input_size, output_size),
            model_version=model_version,
            input_size=input_size
        )

    def get_train_data(self):
        data = super().get_train_data()
        for tmp in data:
            tmp['image'] = scale.Proportion()(tmp['image'], 256)['image']

        return data


class ResNet_ImageNet(ClsProcess):
    """
    Usage:
        .. code-block:: python

            from examples.image_cls import ResNet_ImageNet as Process

            Process().run()
            {'p': 0.9230769230769231, 'r': 0.96, 'f': 0.9411764705882353, 'score': 0.9411764705882353}
    """

    def __init__(self):
        from models.image_classifier.ResNet import Model

        in_ch = 3
        input_size = 224
        output_size = 2

        super().__init__(
            model=Model(in_ch, input_size, output_size),
            model_version='ResNet',
            input_size=input_size
        )

    def data_augment(self, ret):
        ret.update(dst=self.input_size)
        ret.update(scale.Jitter((256, 384))(**ret))
        ret.update(RandomApply([geometry.HFlip()])(**ret))
        return ret


class DenseNet_ImageNet(ClsProcess):
    """
    Usage:
        .. code-block:: python

            from examples.image_cls import DenseNet_ImageNet as Process

            Process().run()
            {'p': 0.819672131147541, 'r': 1.0, 'f': 0.9009009009009009, 'score': 0.9009009009009009}
    """

    def __init__(self):
        from models.image_classifier.DenseNet import Model

        in_ch = 3
        input_size = 224
        output_size = 2

        super().__init__(
            model=Model(in_ch, input_size, output_size),
            model_version='DenseNet',
            input_size=input_size
        )

    def get_train_data(self):
        data = super().get_train_data()
        for tmp in data:
            tmp['image'] = scale.Proportion()(tmp['image'], 256)['image']

        return data


class SENet_ImageNet(ClsProcess):
    """
    Usage:
        .. code-block:: python

            from examples.image_cls import SENet_ImageNet as Process

            Process().run()
            {'p': 0.847457627118644, 'r': 1.0, 'f': 0.9174311926605504, 'score': 0.9174311926605504}
    """

    def __init__(self):
        from models.image_classifier.SENet import Model

        in_ch = 3
        input_size = 224
        output_size = 2

        super().__init__(
            model=Model(in_ch, input_size, output_size),
            model_version='SENet',
            input_size=input_size
        )

    def get_train_data(self):
        data = super().get_train_data()
        for tmp in data:
            tmp['image'] = scale.Proportion()(tmp['image'], 256)['image']

        return data


class SqueezeNet_ImageNet(ClsProcess):
    """
    Usage:
        .. code-block:: python

            from examples.image_cls import SqueezeNet_ImageNet as Process

            Process().run(train_batch_size=32, predict_batch_size=32)
            {'p': 0.7538461538461538, 'r': 0.98, 'f': 0.8521739130434782, 'score': 0.8521739130434782}
    """

    def __init__(self):
        from models.image_classifier.SqueezeNet import Model

        in_ch = 3
        input_size = 224
        output_size = 2

        super().__init__(
            model=Model(in_ch, input_size, output_size),
            model_version='SqueezeNet',
            input_size=input_size
        )

    def get_train_data(self):
        data = super().get_train_data()
        for tmp in data:
            tmp['image'] = scale.Proportion()(tmp['image'], 256)['image']

        return data


class MobileNet_ImageNet(ClsProcess):
    """
    Usage:
        .. code-block:: python

            from examples.image_cls import MobileNet_ImageNet as Process

            Process().run(train_batch_size=32, predict_batch_size=32)
            {'p': 0.9795918367346939, 'r': 0.96, 'f': 0.9696969696969697, 'score': 0.9696969696969697}
    """

    def __init__(self):
        from models.image_classifier.MobileNetV1 import Model

        in_ch = 3
        input_size = 224
        output_size = 2

        super().__init__(
            model=Model(in_ch, input_size, output_size),
            model_version='MobileNet',
            input_size=input_size
        )

    def get_train_data(self):
        data = super().get_train_data()
        for tmp in data:
            tmp['image'] = scale.Proportion()(tmp['image'], 256)['image']

        return data


class ShuffleNet_ImageNet(ClsProcess):
    """
    Usage:
        .. code-block:: python

            from examples.image_cls import ShuffleNet_ImageNet as Process

            Process().run(train_batch_size=64, predict_batch_size=64)
            {'p': 0.8679245283018868, 'r': 0.92, 'f': 0.8932038834951457, 'score': 0.8932038834951457}
    """

    def __init__(self):
        from models.image_classifier.ShuffleNetV1 import Model

        in_ch = 3
        input_size = 224
        output_size = 2

        super().__init__(
            model=Model(in_ch, input_size, output_size),
            model_version='ShuffleNet',
            input_size=input_size
        )

    def get_train_data(self):
        data = super().get_train_data()
        for tmp in data:
            tmp['image'] = scale.Proportion()(tmp['image'], 256)['image']

        return data


class IGC_cifar(ClsProcess):
    """
    Usage:
        .. code-block:: python

            from examples.image_cls import IGC_cifar as Process

            Process().run(train_batch_size=64, predict_batch_size=64)
            {'score': 0.8058}
    """

    def __init__(self):
        from models.image_classifier.IGCV1 import Model

        in_ch = 3
        input_size = 32
        output_size = 10

        super().__init__(
            model=Model(in_module=SimpleInModule(out_channels=in_ch), output_size=output_size),
            model_version='IGC',
            dataset_version='cifar-10-batches-py',
            input_size=input_size
        )

        self.input_size = input_size

    def metric(self, dataset, batch_size=128):
        true, pred = self.predict(dataset, batch_size)

        acc = np.sum(true == pred) / len(true)

        return dict(score=acc)

    def get_train_data(self):
        from data_parse.cv_data_parse.Cifar import Loader

        loader = Loader('data/cifar-10-batches-py')

        return loader(set_type=DataRegister.TRAIN, image_type=DataRegister.ARRAY, generator=False)[0]

    def get_val_data(self):
        from data_parse.cv_data_parse.Cifar import Loader

        loader = Loader('data/cifar-10-batches-py')

        return loader(set_type=DataRegister.TEST, image_type=DataRegister.ARRAY, generator=False)[0]


class CondenseNet_ImageNet(ClsProcess):
    """
    Usage:
        .. code-block:: python

            from examples.image_cls import CondenseNet_ImageNet as Process

            Process().run(train_batch_size=64, predict_batch_size=64)
            {'p': 0.9333333333333333, 'r': 0.84, 'f': 0.8842105263157894, 'score': 0.8842105263157894}
    """

    def __init__(self):
        from models.image_classifier.CondenseNet import Model

        in_ch = 3
        input_size = 224
        output_size = 2

        super().__init__(
            model=Model(in_ch, input_size, output_size),
            model_version='CondenseNet',
            input_size=input_size
        )

    def get_train_data(self):
        data = super().get_train_data()
        for tmp in data:
            tmp['image'] = scale.Proportion()(tmp['image'], 256)['image']

        return data

    def fit(self, dataset, max_epoch, batch_size):
        from models.image_classifier import CondenseNet

        group_lasso_lambda = 0.1

        dataloader = DataLoader(dataset, shuffle=True, batch_size=batch_size)

        self.model.to(self.device)

        optimizer = optim.Adam(self.model.parameters())
        loss_func = nn.CrossEntropyLoss()

        learned_module_list = [m for m in self.model.modules() if isinstance(m, CondenseNet.LGConv)]

        # 训练
        self.model.train()  # 训练模式

        for i in range(max_epoch):
            pbar = tqdm(dataloader, desc=f'train {i}/{max_epoch}')
            CondenseNet.progress = i / max_epoch

            for data, label in pbar:  # Dataset的__getitem__返回的参数
                # 生成数据
                data = data.to(self.device, non_blocking=True)
                label = label.to(self.device)

                optimizer.zero_grad()

                # 前向传递
                pred = self.model(data)
                loss = loss_func(pred, label)

                lasso_loss = 0
                for m in learned_module_list:
                    lasso_loss = lasso_loss + m.lasso_loss

                loss = loss + group_lasso_lambda * lasso_loss

                loss.backward()
                optimizer.step()

                pbar.set_postfix({'loss': f'{loss.item(): .06}'})
