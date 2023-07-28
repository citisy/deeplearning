from torch import nn
from ..layers import Conv, Linear, ConvInModule, OutModule, BaseImgClsModel

# refer to table 1
# (n_res, out_ch, n_conv)
Res18_config = ((2, 64, 2), (2, 128, 2), (2, 256, 2), (2, 512, 2))  # 18 = 1 conv + 16 conv + 1 fcn
Res34_config = ((3, 64, 2), (4, 128, 2), (6, 256, 2), (3, 512, 2))
Res50_config = ((3, 256, 3), (4, 512, 3), (6, 1024, 3), (3, 2048, 3))
Res101_config = ((3, 256, 3), (4, 512, 3), (23, 1024, 3), (3, 2048, 3))
Res152_config = ((3, 256, 3), (8, 512, 3), (36, 1024, 3), (3, 2048, 3))


class Model(BaseImgClsModel):
    """[Deep Residual Learning for Image Recognition](https://arxiv.org/pdf/1512.03385.pdf)
    See Also `torchvision.models.resnet`
    """

    def __init__(
            self,
            in_ch=None, input_size=None, out_features=None,
            backbone_config=Res18_config, add_block: nn.Module = None, block_config=dict(), **kwargs
    ):
        backbone = Backbone(backbone_config=backbone_config, add_block=add_block, **block_config)

        super().__init__(
            in_ch=in_ch,
            input_size=input_size,
            out_features=out_features,
            backbone=backbone,
            **kwargs
        )


class Backbone(nn.Sequential):
    def __init__(self, backbone_config=Res18_config, add_block: nn.Module = None, **block_config):
        super().__init__()

        layers = [
            Conv(3, 64, 7, s=2),
            nn.MaxPool2d(3, stride=2, padding=1)
        ]

        in_ch = 64

        for i, (n_res, out_ch, n_conv) in enumerate(backbone_config):
            for j in range(n_res):
                if i != 0 and j == 0:
                    layers.append(ResBlock(in_ch, out_ch, n_conv, s=2, add_block=add_block, **block_config))
                else:
                    layers.append(ResBlock(in_ch, out_ch, n_conv, add_block=add_block, **block_config))

                in_ch = out_ch

        self.conv_seq = nn.Sequential(*layers)
        self.out_channels = in_ch


class ResBlock(nn.Module):
    def __init__(self, in_ch, out_ch, n_conv=2, s=1,
                 add_block: nn.Module = None, **block_config):
        super().__init__()
        if n_conv == 2:
            self.conv_seq = nn.Sequential(
                Conv(in_ch, out_ch, k=3, s=s),
                Conv(out_ch, out_ch, k=3, s=1, p=1, is_act=False),
            )
        elif n_conv == 3:  # use bottleneck
            hidden_ch = out_ch // 4
            self.conv_seq = nn.Sequential(
                Conv(in_ch, hidden_ch, k=1, s=s),
                Conv(hidden_ch, hidden_ch, k=3),
                Conv(hidden_ch, out_ch, k=3, s=1, p=1, is_act=False),
            )

        else:
            raise ValueError(f'Not supported {n_conv = }')

        if add_block:
            self.conv_seq.append(add_block(out_ch, **block_config))

        self.is_projection_x = in_ch != out_ch
        if self.is_projection_x:
            self.conv_x = Conv(in_ch, out_ch, k=1, s=s)
        else:
            self.conv_x = nn.Sequential()

        self.act = nn.ReLU()

    def forward(self, x):
        x1 = self.conv_seq(x)
        x2 = self.conv_x(x)
        x = self.act(x1 + x2)
        return x
