from .bundled import *
from .data_process import *
from .model_process import *
from utils import converter


def setup_seed(seed=42):
    """42 is a lucky number"""
    import torch.backends.cudnn as cudnn

    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.cuda.manual_seed(seed)
    np.random.seed(seed)
    cudnn.benchmark = False
    cudnn.deterministic = True


class Process(
    LogHooks,
    DataHooks,
    CheckpointHooks,
    ModelHooks,
):
    """
    all the global vars of the class can be set when creating a new instance,
    e.g.
        there is a class defined with global vars like that:
            class MyProcess(Process):
                input_size = 640
                ...

        and then, you can set special value of `input_size` like that:
            MyProcess(input_size=512, ...)

    """

    def __init__(self, **kwargs):
        super().__init__()
        self.__dict__.update(kwargs)

    log_dir = None
    date = datetime.now().isoformat()
    _model_cache_dir = 'model_data'
    _result_cache_dir = 'cache_data'
    default_model_path: str
    cache_dir: str

    def init(self):
        setup_seed()
        self.init_log_base(self.log_dir)
        self.init_wandb()

        self.work_dir = f'{self._model_cache_dir}/{self.model_version}/{self.dataset_version}'
        self.cache_dir = f'{self._result_cache_dir}/{self.model_version}/{self.dataset_version}'
        self.default_model_path = f'{self.work_dir}/weight.pth'
        os_lib.mk_dir(self.work_dir)

        self.log(f'{torch.__version__ = }')
        self.log(f'{self.model_version = }')
        self.log(f'{self.dataset_version = }')
        self.log(f'{self.work_dir = }')
        self.log(f'{self.cache_dir = }')

        self.device = torch.device(f"cuda:{self.device}" if torch.cuda.is_available() else "cpu") if self.device is not None else 'cpu'

        if not hasattr(self, 'model') or self.model is None:
            self.set_model()

        # note that, it must be set device before load_state_dict()
        self.model.to(self.device)

        if not hasattr(self, 'optimizer') or self.optimizer is None:
            self.set_optimizer()

        if not hasattr(self, 'stopper') or self.stopper is None:
            self.set_stopper()

    def run(self, max_epoch=100, train_batch_size=16, predict_batch_size=None, check_period=None, fit_kwargs=dict(), metric_kwargs=dict()):
        self.init()
        self.model_info()

        fit_kwargs.setdefault('metric_kwargs', metric_kwargs)
        self.fit(
            max_epoch=max_epoch,
            batch_size=train_batch_size,
            check_period=check_period,
            dataloader_kwargs=dict(num_workers=min(train_batch_size, 16)),
            **fit_kwargs
        )

        self.save(self.default_model_path, save_type=WEIGHT)

        # self.load(self.model_path, save_type=WEIGHT)
        # self.load(f'{self.model_dir}/{self.dataset_version}/last.pth', save_type=WEIGHT)

        r = self.metric(
            batch_size=predict_batch_size or train_batch_size,
            dataloader_kwargs=dict(num_workers=min(predict_batch_size or train_batch_size, 16)),
            **metric_kwargs
        )
        for k, v in r.items():
            self.logger.info({k: v})


class ParamsSearch:
    """
    Usage:
        .. code-block:: python

            ######## example 1 ########
            from examples.image_classifier import ClsProcess, ImageNet
            from models.image_classifier.ResNet import Model
            from torch import optim

            class ResNet_ImageNet(ClsProcess, ImageNet):
                '''define your own process'''

            params_search = ParamsSearch(
                process=ResNet_ImageNet,
                params=dict(
                    model=dict(
                        instance=Model,
                        const=dict(in_ch=3, out_features=2),
                        var=dict(input_size=(224, 256, 320))
                    ),
                    callable_optimizer=dict(
                        instance=lambda **kwargs: lambda params: optim.Adam(params, **kwargs),
                        var=dict(lr=(0.001, 0.01))
                    ),
                    sys=dict(
                        var=dict(lf=[
                            lambda x, max_epoch, lrf: (1 - x / max_epoch) * (1.0 - lrf) + lrf,
                            lambda x, max_epoch, lrf: ((1 - math.cos(x * math.pi / max_epoch)) / 2) * (lrf - 1) + 1,
                        ])
                    )
                ),
                run_kwargs=dict(max_epoch=100, check_period=4),
                process_kwargs=dict(use_wandb=True),
                model_version='ResNet',
                dataset_version='ImageNet2012.ps',
            )
            # there is 3*2*2 test group
            params_search.run()

            ######## example 2 ########
            from models.object_detection.YoloV5 import Model, head_config, make_config, default_model_multiple
            params_search = ParamsSearch(
                process=Process,
                params=dict(
                    model=dict(
                        instance=Model,
                        const=dict(
                            n_classes=20,
                            in_module_config=dict(in_ch=3, input_size=640),
                            head_config=head_config
                        ),
                        var=[
                            {k: [v] for k, v in make_config(**default_model_multiple['yolov5n']).items()},
                            {k: [v] for k, v in make_config(**default_model_multiple['yolov5s']).items()},
                            {'head_config.anchor_t': [3, 4, 5]}
                        ]
                    ),
                    sys=dict(
                        const=dict(
                            input_size=None,
                            device=0,
                            cls_alias=classes
                        ),
                    )
                ),
                run_kwargs=dict(max_epoch=100, check_period=4, metric_kwargs=dict(visualize=True, max_vis_num=8)),
                process_kwargs=dict(use_wandb=True),
                model_version='yolov5-test',
                dataset_version='Voc.ps',
            )
            # there are 5 test groups
            params_search.run()

    """

    def __init__(
            self,
            process, params=dict(),
            process_kwargs=dict(), run_kwargs=dict(),
            model_version='', dataset_version='',
    ):
        self.process = process
        self.process_kwargs = process_kwargs
        self.run_kwargs = run_kwargs
        self.model_version = model_version
        self.dataset_version = dataset_version

        var_params = {k: v['var'] for k, v in params.items() if 'var' in v}
        self.var_params = {k: configs.permute_obj(var_p) for k, var_p in var_params.items()}
        self.const_params = {k: v['const'] for k, v in params.items() if 'const' in v}
        self.var_instance = {k: v['instance'] for k, v in params.items() if 'instance' in v}
        self.total_params = [[]]
        keys = set(self.var_params.keys()) | set(self.const_params.keys())
        for k in keys:
            var_ps = self.var_params.get(k, [{}])
            const_p = self.const_params.get(k, {})
            self.total_params = [_ + [(var_p, const_p)] for _ in self.total_params for var_p in var_ps]

        self.keys = keys

    def run(self):
        kwargs = copy.deepcopy(self.process_kwargs)
        for _ in self.total_params:
            sub_version = ''
            info_msg = ''
            for key, (var_p, const_p) in zip(self.keys, _):
                tmp_var_p = configs.collapse_dict(var_p)
                for k, v in tmp_var_p.items():
                    if len(str(v)) > 8:
                        s = converter.DataConvert.str_to_md5(str(v))
                        sub_version += f'{k}={s[:6]};'
                    else:
                        sub_version += f'{k}={v};'
                    info_msg += f'{k}={v};'

                var_p = configs.expand_dict(var_p)
                params = configs.merge_dict(var_p, const_p)
                if key in self.var_instance:
                    ins = self.var_instance[key]
                    kwargs[key] = ins(**params)
                else:
                    kwargs.update(params)

            sub_version = sub_version[:-1]
            info_msg = info_msg[:-1]

            kwargs['model_version'] = self.model_version
            kwargs['dataset_version'] = f'{self.dataset_version}/{sub_version}'
            kwargs['log_dir'] = f'model_data/{self.model_version}/{self.dataset_version}/{sub_version}/logs'
            process = self.process(**kwargs)
            process.logger.info(info_msg)
            process.run(**self.run_kwargs)
            torch.cuda.empty_cache()