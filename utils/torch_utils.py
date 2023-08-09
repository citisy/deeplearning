import copy
import math
import torch
from torch import nn


class ModuleInfo:
    @classmethod
    def profile_per_layer(cls, module: nn.Module, depth=None):
        profiles = []

        def cur(current_m, dep):
            for name, m in current_m._modules.items():
                if dep <= 1 or len(m._modules) == 0:
                    profiles.append((name, str(type(m))[8:-2], cls.profile(m)))
                else:
                    cur(m, dep - 1)

        depth = depth or float('inf')
        cur(module, depth)
        return profiles

    @classmethod
    def profile(cls, module):
        return dict(
            params=cls.profile_params(module),
            grads=cls.profile_grads(module),
            args=cls.profile_args(module)
        )

    @staticmethod
    def profile_params(module):
        return sum(x.numel() for x in module.parameters())

    @staticmethod
    def profile_grads(module):
        return sum(x.numel() for x in module.parameters() if x.requires_grad)

    @staticmethod
    def profile_args(module):
        args = {}
        if hasattr(module, 'in_channels'):
            args['i_ch'] = module.in_channels
        if hasattr(module, 'out_channels'):
            args['o_ch'] = module.out_channels
        if hasattr(module, 'in_features'):
            args['i_f'] = module.in_features
        if hasattr(module, 'out_features'):
            args['o_f'] = module.out_features
        if hasattr(module, 'input_size'):
            args['i_size'] = module.input_size
        if hasattr(module, 'output_size'):
            args['o_size'] = module.output_size
        if hasattr(module, 'kernel_size'):
            k = module.kernel_size
            if isinstance(k, (list, tuple)):
                k = k[0]
            args['k'] = k
        if hasattr(module, 'stride'):
            s = module.stride
            if isinstance(s, (list, tuple)):
                s = s[0]
            args['s'] = s
        if hasattr(module, 'padding'):
            p = module.padding
            if isinstance(p, (list, tuple)):
                p = p[0]
            args['p'] = p

        return args

    @staticmethod
    def profile_flops(module, input_size, *test_args):
        import thop

        p = next(module.parameters())
        test_data = torch.empty((1, *input_size), device=p.device)
        flops = thop.profile(copy.deepcopy(module), inputs=(test_data, *test_args), verbose=False)[0] / 1E9 * 2  # stride GFLOPs

        return flops


class EarlyStopping:
    def __init__(self, thres=0.005, patience=30, min_epoch=None, verbose=True, stdout_method=print):
        self.thres = thres
        self.best_fitness = 0.0
        self.best_epoch = 0
        self.acc_epoch = 0
        self.min_epoch = min_epoch or 0
        self.last_epoch = self.min_epoch
        self.patience = patience or float('inf')
        self.verbose = verbose
        self.stdout_method = stdout_method

    def __call__(self, epoch, fitness):
        if epoch < self.min_epoch:
            return False

        if fitness - self.best_fitness > self.thres:
            self.best_epoch = epoch
            self.best_fitness = fitness
            self.acc_epoch = 0

        elif abs(self.best_fitness - fitness) <= self.thres:
            self.acc_epoch += epoch - self.last_epoch

        self.last_epoch = epoch
        stop = self.acc_epoch >= self.patience
        if stop and self.verbose:
            self.stdout_method(f'Early Stopping training. Best results observed at epoch {self.best_epoch}, and best fitness is {self.best_fitness}')
        return stop


def initialize_layers(module, init_gain=0.02, init_type='normal'):
    for m in module.modules():
        t = type(m)

        if t is nn.BatchNorm2d:
            m.eps = 1e-3
            m.momentum = 0.03
            m.weight.data.normal_(1.0, init_gain)
            m.bias.data.fill_(0.)

        elif t in [nn.Hardswish, nn.LeakyReLU, nn.ReLU, nn.ReLU6, nn.SiLU]:
            m.inplace = True

        elif t in [nn.Conv2d, nn.Linear]:
            if init_type == 'normal':
                nn.init.normal_(m.weight.data, 0.0, init_gain)
            elif init_type == 'xavier':
                nn.init.xavier_normal_(m.weight.data, gain=init_gain)
            elif init_type == 'kaiming':
                nn.init.kaiming_normal_(m.weight.data, a=0, mode='fan_in')
            elif init_type == 'orthogonal':
                nn.init.orthogonal_(m.weight.data, gain=init_gain)

            if hasattr(m, 'bias') and m.bias is not None:
                nn.init.constant_(m.bias.data, 0.0)

        elif t in [nn.ConvTranspose2d]:
            m.weight.data.copy_(bilinear_kernel(m.in_channels, m.out_channels, m.kernel_size[0]))


def bilinear_kernel(in_channels, out_channels, kernel_size):
    factor = (kernel_size + 1) // 2
    if kernel_size % 2 == 1:
        center = factor - 1
    else:
        center = factor - 0.5
    og = (torch.arange(kernel_size).reshape(-1, 1), torch.arange(kernel_size).reshape(1, -1))
    f = (1 - torch.abs(og[0] - center) / factor) * (1 - torch.abs(og[1] - center) / factor)
    weight = torch.zeros((in_channels, out_channels, kernel_size, kernel_size))
    ch = min(in_channels, out_channels)
    weight[range(ch), range(ch), :, :] = f
    return weight


class Export:
    @staticmethod
    def to_pickle(model):
        pass

    @staticmethod
    def to_jit(model, trace_input):
        with torch.no_grad():
            model.eval()
            # warmup, make sure that the model is initialized right
            model(trace_input)
            jit_model = torch.jit.trace(model, trace_input)

        return jit_model


def is_parallel(model):
    # Returns True if model is of type DP or DDP
    return type(model) in (nn.parallel.DataParallel, nn.parallel.DistributedDataParallel)


def de_parallel(model):
    # De-parallelize a model: returns single-GPU model if model is of type DP or DDP
    return model.module if is_parallel(model) else model


def copy_attr(a, b, include=(), exclude=()):
    # Copy attributes from b to a, options to only include [...] and to exclude [...]
    for k, v in b.__dict__.items():
        if (len(include) and k not in include) or k.startswith('_') or k in exclude:
            continue
        else:
            setattr(a, k, v)


class ModelEMA:
    """ Updated Exponential Moving Average (EMA) from https://github.com/rwightman/pytorch-image-models
    Keeps a moving average of everything in the model state_dict (parameters and buffers)
    For EMA details see https://www.tensorflow.org/api_docs/python/tf/train/ExponentialMovingAverage
    """

    def __init__(self, model, decay=0.9999, tau=2000, updates=0):
        # Create EMA
        self.ema = copy.deepcopy(de_parallel(model)).eval()  # FP32 EMA
        # if next(model.parameters()).device.type != 'cpu':
        #     self.ema.half()  # FP16 EMA
        self.updates = updates  # number of EMA updates
        self.decay = lambda x: decay * (1 - math.exp(-x / tau))  # decay exponential ramp (to help early epochs)
        for p in self.ema.parameters():
            p.requires_grad_(False)

    def update(self, model):
        # Update EMA parameters
        with torch.no_grad():
            self.updates += 1
            d = self.decay(self.updates)

            msd = de_parallel(model).state_dict()  # model state_dict
            for k, v in self.ema.state_dict().items():
                if v.dtype.is_floating_point:
                    v *= d
                    v += (1 - d) * msd[k].detach()

    def update_attr(self, model, include=(), exclude=('process_group', 'reducer')):
        # Update EMA attributes
        copy_attr(self.ema, model, include, exclude)
