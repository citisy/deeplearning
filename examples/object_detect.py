import copy
import time
import cv2
import math
import numpy as np
import torch
from torch import nn, optim
from tqdm import tqdm
from pathlib import Path
from data_parse.cv_data_parse.data_augmentation import crop, scale, geometry, pixel_perturbation, RandomApply, Apply, channel, complex
from data_parse.cv_data_parse.base import DataRegister, DataVisualizer
from .base import Process, BaseDataset
from utils import configs, os_lib, converter, cv_utils, log_utils
from utils.torch_utils import EMA
from metrics import object_detection
from typing import List


class OdDataset(BaseDataset):
    def process_one(self, idx):
        ret = copy.deepcopy(self.data[idx])
        if isinstance(ret['image'], str):
            ret['image_path'] = ret['image']
            ret['image'] = cv2.imread(ret['image'])

        ret['ori_image'] = ret['image']
        ret['ori_bboxes'] = ret['bboxes']
        ret['idx'] = idx

        if self.augment_func:
            ret = self.augment_func(ret)

        return ret


class OdProcess(Process):
    lrf = 0.01

    def lf(self, x, max_epoch, lrf):
        # return (1 - x / max_epoch) * (1.0 - lrf) + lrf
        return ((1 - math.cos(x * math.pi / max_epoch)) / 2) * (lrf - 1) + 1  # cos_lr

    def fit(self, max_epoch=100, batch_size=16, save_period=None, metric_kwargs=dict(), **dataloader_kwargs):
        train_dataloader, val_dataloader, metric_kwargs = self.on_train_start(batch_size, metric_kwargs, **dataloader_kwargs)
        # ema = EMA(self.model, step_start_ema=0, tau=2000)

        scheduler = optim.lr_scheduler.LambdaLR(self.optimizer, lr_lambda=lambda x: self.lf(x, max_epoch, self.lrf))
        scheduler.last_epoch = -1

        scaler = torch.cuda.amp.GradScaler(enabled=True)

        accumulate = 64 // batch_size
        j = 0

        for i in range(self.start_epoch, max_epoch):
            self.model.train()
            pbar = tqdm(train_dataloader, desc=f'train {i}/{max_epoch}')
            total_loss = 0
            total_batch = 0
            losses = None
            epoch_start_time = time.time()

            for rets in pbar:
                images = [torch.from_numpy(ret.pop('image')).to(self.device, non_blocking=True, dtype=torch.float) for ret in rets]
                gt_boxes = [torch.from_numpy(ret['bboxes']).to(self.device) for ret in rets]
                gt_cls = [torch.from_numpy(ret['classes']).to(self.device) for ret in rets]
                images = torch.stack(images)

                # note that, if the images have the same shape, minmax after stack if possible
                # it can reduce about 20 seconds per epoch to voc dataset
                images = images / 255

                # note that, amp method can make the model run in dtype of half
                # even though input has dtype of torch.half and weight has dtype of torch.float
                # so that, it would run in lower memory and cost less time
                with torch.cuda.amp.autocast(True):
                    output = self.model(images, gt_boxes, gt_cls)
                loss = output['loss']

                scaler.scale(loss).backward()
                if j % accumulate == 0:
                    scaler.unscale_(self.optimizer)  # unscale gradients
                    torch.nn.utils.clip_grad_norm_(self.model.parameters(), max_norm=10.0)  # clip gradients
                    scaler.step(self.optimizer)  # optimizer.step
                    scaler.update()
                    self.optimizer.zero_grad()
                    # ema.step(self.model)

                j += 1

                total_loss += loss.item()
                total_batch += len(rets)
                mean_loss = total_loss / total_batch

                losses = {'mean_loss': mean_loss}
                for k, v in output.items():
                    if k.startswith('loss'):
                        losses[k] = v.item()

                # mem_info = {
                #     'cpu_info': log_utils.MemoryInfo.get_process_mem_info(),
                #     'gpu_info': log_utils.MemoryInfo.get_gpu_mem_info()
                # }

                pbar.set_postfix({
                    **losses,
                    # **mem_info
                })

            scheduler.step()

            if self.on_train_epoch_end(i, save_period, val_dataloader,
                                       losses=losses,
                                       epoch_start_time=epoch_start_time,
                                       # model=ema.ema_model,
                                       **metric_kwargs):
                break

    def metric(self, *args, **kwargs):
        gt_rets, det_rets = self.predict(*args, **kwargs)
        df = object_detection.EasyMetric(verbose=False).quick_metric(gt_rets, det_rets, save_path=f'{self.model_dir}/{self.dataset_version}/result.csv')

        # self.log_info['table'] = self.wandb.Table(dataframe=df)
        for i in df.index:
            name = i
            if hasattr(self, 'cls_alias'):
                try:
                    name = self.cls_alias[i]
                except:
                    pass
            self.log_info[f'metrics/{name}'] = df['ap'][i]

        result = dict(
            per_class_result=df,
            score=df['ap']['mean']
        )

        return result

    def predict(self, val_dataloader=None, batch_size=16, cur_epoch=-1, model=None, visualize=False, max_vis_num=float('inf'), save_ret_func=None, **dataloader_kwargs):
        if val_dataloader is None:
            val_dataloader = self.on_val_start(batch_size, **dataloader_kwargs)

        model = model or self.model
        model.to(self.device)
        gt_rets, det_rets = [], []
        vis_num = 0

        with torch.no_grad():
            model.eval()
            for rets in tqdm(val_dataloader, desc='val'):
                images = [torch.from_numpy(ret.pop('image')).to(self.device) for ret in rets]
                images = torch.stack(images)
                images = images / 255

                outputs = model(images)
                outputs = [{k: v.to('cpu').numpy() for k, v in t.items()} for t in outputs]

                for i in range(len(images)):
                    output = outputs[i]
                    ret = rets[i]

                    output = configs.merge_dict(ret, output)
                    ret = self.val_data_restore(ret)
                    output = self.val_data_restore(output)

                    outputs[i] = output
                    rets[i] = ret

                    gt_rets.append(dict(
                        _id=ret['_id'],
                        bboxes=ret['bboxes'],
                        classes=ret['classes'],
                    ))

                    det_rets.append(dict(
                        _id=ret['_id'],
                        bboxes=output['bboxes'],
                        classes=output['classes'],
                        confs=output['confs']
                    ))

                vis_num = self.on_val_step_end(rets, outputs, cur_epoch, visualize, batch_size, max_vis_num, vis_num)

        if save_ret_func:
            save_ret_func(det_rets)

        return gt_rets, det_rets

    def on_val_step_end(self, rets, outputs, cur_epoch, visualize, batch_size, max_vis_num, vis_num):
        if visualize:
            n = min(batch_size, max_vis_num - vis_num)
            if n > 0:
                for ret, output in zip(rets, outputs):
                    ret['image'] = ret['ori_image']
                    output['image'] = ret['ori_image']

                cls_alias = self.cls_alias if hasattr(self, 'cls_alias') else None
                cache_image = DataVisualizer(f'{self.save_result_dir}/{cur_epoch}', verbose=False, pbar=False)(
                    rets[:n], outputs[:n], return_image=True, cls_alias=cls_alias
                )
                self.log_info.setdefault('val_image', []).extend([self.wandb.Image(img, caption=Path(r['_id']).stem) for img, r in zip(cache_image, rets)])
                vis_num += n

        return vis_num

    def single_predict(self, image: np.ndarray, *args, **kwargs):
        with torch.no_grad():
            self.model.eval()
            ret = self.val_data_augment({'image': image})
            images = torch.from_numpy(ret.pop('image')).to(self.device, non_blocking=True, dtype=torch.float).unsqueeze(0)
            images = images / 255
            outputs = self.model(images)
            outputs = [{k: v.to('cpu').numpy() for k, v in t.items()} for t in outputs]

            output = outputs[0]
            output = configs.merge_dict(ret, output)
            output = self.val_data_restore(output)

        return output

    def batch_predict(self, images: List[np.ndarray], batch_size=16, **kwargs):
        results = []
        with torch.no_grad():
            self.model.eval()
            for i in range(0, len(images), batch_size):
                rets = [self.val_data_augment({'image': image}) for image in images[i:i + batch_size]]
                batch_images = [torch.from_numpy(ret.pop('image')).to(self.device) for ret in rets]
                batch_images = torch.stack(batch_images)
                batch_images = batch_images / 255

                outputs = self.model(batch_images)
                outputs = [{k: v.to('cpu').numpy() for k, v in t.items()} for t in outputs]

                for ret, output in zip(rets, outputs):
                    output = configs.merge_dict(ret, output)
                    output = self.val_data_restore(output)
                    results.append(output)

        return results

    def fragment_predict(self, image: np.ndarray, **kwargs):
        images, coors = cv_utils.fragment_image(image, max_size=self.input_size, over_ratio=0.5, overlap_ratio=0.2)
        results = self.batch_predict(images)

        bboxes = []
        classes = []
        confs = []

        for (x1, y1, x2, y2), result in zip(coors, results):
            bbox = result['bboxes']
            cls = result['classes']
            conf = result['confs']

            bbox += (x1, y1, x1, y1)
            bboxes.append(bbox)
            classes.append(cls)
            confs.append(conf)

        bboxes = np.concatenate(bboxes)
        classes = np.concatenate(classes)
        confs = np.concatenate(confs)

        keep = cv_utils.non_max_suppression(bboxes, confs, object_detection.Iou.iou)
        bboxes = bboxes[keep]
        classes = classes[keep]
        confs = confs[keep]

        return dict(
            bboxes=bboxes,
            classes=classes,
            confs=confs
        )


class Voc(Process):
    data_dir = 'data/VOC2012'
    train_data_num = None
    val_data_num = None

    def get_train_data(self):
        from data_parse.cv_data_parse.Voc import Loader

        loader = Loader(self.data_dir)
        self.cls_alias = loader.classes

        return loader(set_type=DataRegister.TRAIN, image_type=DataRegister.PATH, generator=False,
                      task='',
                      max_size=self.train_data_num
                      )[0]

    def get_val_data(self):
        from data_parse.cv_data_parse.Voc import Loader

        loader = Loader(self.data_dir)
        self.cls_alias = loader.classes

        return loader(set_type=DataRegister.VAL, image_type=DataRegister.PATH, generator=False,
                      task='',
                      max_size=self.val_data_num,
                      )[0]

    aug = Apply([
        scale.LetterBox(),
        # pixel_perturbation.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
        channel.HWC2CHW()
    ])

    def data_augment(self, ret):
        ret.update(RandomApply([geometry.HFlip()])(**ret))
        ret.update(dst=self.input_size)
        ret.update(self.aug(**ret))
        return ret

    def complex_data_augment(self, idx, data, base_process):
        return base_process(idx)

    def val_data_augment(self, ret):
        ret.update(dst=self.input_size)
        ret.update(self.aug(**ret))
        return ret

    def val_data_restore(self, ret):
        ret = scale.LetterBox().restore(ret)
        return ret


class FastererRCNN_Voc(OdProcess, Voc):
    """
    Usage:
        .. code-block:: python

            from examples.object_detect import FastererRCNN_Voc as Process

            Process().run()
            {'score': 0.3910}
    """

    def __init__(self, device=1):
        from models.object_detection.FasterRCNN import Model

        in_ch = 3
        input_size = 800
        n_classes = 20

        super().__init__(
            model=Model(
                in_module_config=dict(in_ch=in_ch, input_size=input_size),
                n_classes=n_classes
            ),
            model_version='FastererRCNN',
            dataset_version='Voc2012',
            input_size=input_size,
            device=device
        )


class YoloV5(OdProcess):
    def __init__(self,
                 model_version='YoloV5',
                 device=0,
                 input_size=640,
                 in_ch=3,
                 n_classes=20,
                 model=None,
                 **kwargs
                 ):
        # auto anchors
        # from models.object_detection.YoloV5 import auto_anchors
        # head_config = auto_anchors(self.get_train_data(), input_size)

        from models.object_detection.YoloV5 import Model
        model = model or Model(
            n_classes,
            in_module_config=dict(in_ch=in_ch, input_size=input_size),
            # head_config=head_config
        )

        g = [], [], []  # optimizer parameter groups
        bn = tuple(v for k, v in nn.__dict__.items() if 'Norm' in k)  # normalization layers, i.e. BatchNorm2d()
        for v in model.modules():
            if hasattr(v, 'bias') and isinstance(v.bias, nn.Parameter):  # bias
                g[2].append(v.bias)
            if isinstance(v, bn):  # weight (no decay)
                g[1].append(v.weight)
            elif hasattr(v, 'weight') and isinstance(v.weight, nn.Parameter):  # weight (with decay)
                g[0].append(v.weight)

        weight_decay = 0.0005

        optimizer = optim.SGD(g[2], lr=0.01, momentum=0.937, nesterov=True)
        optimizer.add_param_group({'params': g[0], 'weight_decay': weight_decay})  # add g0 with weight_decay
        optimizer.add_param_group({'params': g[1]})  # add g1 (BatchNorm2d weights)

        del g

        super().__init__(
            model=model,
            optimizer=optimizer,
            model_version=model_version,
            input_size=input_size,
            device=device,
            **kwargs
        )


class VocWithYolov5Aug(Voc):
    def data_augment(self, ret):
        ret.update(RandomApply([geometry.HFlip()])(**ret))
        return ret

    mosaic_prob = 0.5

    def complex_data_augment(self, idx, data, base_process):
        if np.random.random() > self.mosaic_prob:
            idxes = [idx] + list(np.random.choice(range(len(data)), 3, replace=False))
            rets = []
            for idx in idxes:
                ret = base_process(idx)
                rets.append(ret)

            image_list = [ret['image'] for ret in rets]
            bboxes_list = [ret['bboxes'] for ret in rets]
            classes_list = [ret['classes'] for ret in rets]
            img_size = np.max([img.shape[:2] for img in image_list])
            ret = complex.Mosaic4(img_size=img_size)(
                image_list=image_list,
                bboxes_list=bboxes_list,
                classes_list=classes_list
            )
        else:
            ret = base_process(idx)

        ret.update(dst=self.input_size)
        ret.update(Apply([
            scale.LetterBox(),
            # pixel_perturbation.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
            channel.HWC2CHW()
        ])(**ret))

        return ret


class YoloV5_Voc(YoloV5, VocWithYolov5Aug):
    """
    Usage:
        .. code-block:: python

            from examples.object_detect import YoloV5_Voc as Process

            Process().run(max_epoch=500)
            {'score': 0.3529}
    """

    def __init__(self, dataset_version='Voc2012', **kwargs):
        super().__init__(dataset_version=dataset_version, **kwargs)


class Yolov5Dataset(Process):
    data_dir = 'yolov5/data_mapping'

    def get_train_data(self):
        from data_parse.cv_data_parse.YoloV5 import Loader, DataRegister

        convert_func = lambda ret: cv_utils.CoordinateConvert.mid_xywh2top_xyxy(
            ret['bboxes'],
            wh=(ret['image'].shape[1], ret['image'].shape[0]),
            blow_up=True
        )

        loader = Loader(self.data_dir)
        loader.on_end_convert = convert_func
        data = loader(set_type=DataRegister.TRAIN, image_type=DataRegister.PATH, generator=False, sub_dir='')[0]

        return data

    def get_val_data(self):
        from data_parse.cv_data_parse.YoloV5 import Loader, DataRegister

        convert_func = lambda ret: cv_utils.CoordinateConvert.mid_xywh2top_xyxy(
            ret['bboxes'],
            wh=(ret['image'].shape[1], ret['image'].shape[0]),
            blow_up=True
        )

        loader = Loader(self.data_dir)
        loader.on_end_convert = convert_func
        data = loader(set_type=DataRegister.VAL, image_type=DataRegister.PATH, generator=False, sub_dir='')[0]

        return data


class YoloV5_yolov5(YoloV5, Yolov5Dataset):
    def __init__(self, classes=None, dataset_version='yolov5', **kwargs):
        if 'n_classes' not in kwargs:
            kwargs['n_classes'] = len(classes)

        super().__init__(dataset_version=dataset_version, **kwargs)
