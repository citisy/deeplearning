from torch import nn
from ..layers import Linear
from .SEInception import SEBlock
from .ResNet import Model as ResNet, Res18_config


class Model(ResNet):
    """[Squeeze-and-Excitation Networks](https://arxiv.org/pdf/1709.01507.pdf)"""
    def __init__(
            self,
            in_ch=None, input_size=None, output_size=None,
            conv_config=Res18_config, **kwargs
    ):
        super().__init__(
            in_ch, input_size, output_size,
            conv_config=conv_config,
            add_block=SEBlock,
            **kwargs
        )
