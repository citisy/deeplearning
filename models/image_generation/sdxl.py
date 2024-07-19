import numpy as np
import torch
from torch import nn, einsum
from einops import rearrange, repeat, reduce
from utils import torch_utils
from . import ldm, ddpm, ddim, sdv1, sdv2, k_diffusion
from .ldm import WeightLoader
from ..text_image_pretrain import CLIP as CLIPModel


class Config(sdv2.Config):
    """only for inference"""

    CLIP = 'clip'
    TIMESTEP = 'timestep'

    EULER = 'Euler'

    # for EmbedderWarp input_key
    TXT = 'txt'
    ORIGINAL_SIZE_AS_TUPLE = 'original_size_as_tuple'
    CROP_COORDS_TOP_LEFT = 'crop_coords_top_left'
    TARGET_SIZE_AS_TUPLE = 'target_size_as_tuple'
    AESTHETIC_SCORE = 'aesthetic_score'
    FPS = 'fps'
    FPS_ID = 'fps_id'
    MOTION_BUCKET_ID = 'motion_bucket_id'
    POOL_IMAGE = 'pool_image'
    COND_AUG = 'cond_aug'
    COND_FRAMES = 'cond_frames'
    COND_FRAMES_WITHOUT_NOISE = 'cond_frames_without_noise'

    xl_model = dict(
        scale=5,
        scale_factor=0.13025,
    )

    xl_sampler = dict(
        name='Euler',
        **k_diffusion.Config.get(),
    )

    # for vanilla v2 model
    legacy_v2_embedder = dict(
        name=CLIP,
        input_key=TXT,
        params=sdv2.Config.v2_cond
    )

    embedder_clip = dict(
        name=CLIP,
        input_key=TXT,
        params=dict(
            is_proj=False,
            **CLIPModel.Config.openai_text_large,
            layer=sdv1.Config.RAW_HIDDEN,
            layer_idx=CLIPModel.Config.openai_text_large['num_hidden_layers'] - 2,  # second to last state
        )
    )

    embedder_open_clip = dict(
        name=CLIP,
        input_key=TXT,
        params=dict(
            **CLIPModel.Config.laion_text_bigG_14,
            layer=sdv1.Config.RAW_HIDDEN,
            layer_idx=CLIPModel.Config.laion_text_bigG_14['num_hidden_layers'] - 2,  # second to last state
            return_pooled=True
        )
    )

    embedder_original_size = dict(
        name=TIMESTEP,
        input_key=ORIGINAL_SIZE_AS_TUPLE,
        params=dict()
    )

    embedder_crop_coords = dict(
        name=TIMESTEP,
        input_key=CROP_COORDS_TOP_LEFT,
        params=dict()
    )

    embedder_target_size = dict(
        name=TIMESTEP,
        input_key=TARGET_SIZE_AS_TUPLE,
        params=dict()
    )

    embedder_aesthetic_score = dict(
        name=TIMESTEP,
        input_key=AESTHETIC_SCORE,
        params=dict()
    )

    xl_base_cond = [
        embedder_clip,
        embedder_open_clip,
        embedder_original_size,
        embedder_crop_coords,
        embedder_target_size,
    ]

    xl_refiner_cond = [
        embedder_open_clip,
        embedder_original_size,
        embedder_crop_coords,
        embedder_aesthetic_score,
    ]

    xl_base_backbone = dict(
        num_classes=ldm.Config.SEQUENTIAL,
        adm_in_channels=2816,  # 1028 * 2 + 256 * 3
        ch_mult=(1, 2, 4),
        # note, for reduce computation, the first layer do not use attention,
        # but use more attention in the middle block
        attend_layers=(1, 2),
        transformer_depth=(1, 2, 10),
        head_dim=64,
        use_linear_in_transformer=True
    )

    xl_refiner_backbone = dict(
        num_classes=ldm.Config.SEQUENTIAL,
        adm_in_channels=2560,  # 1028 * 2 + 256 * 2
        unit_dim=384,
        ch_mult=(1, 2, 4, 4),
        attend_layers=(1, 2),
        transformer_depth=4,
    )

    default_model = 'xl_base'

    @classmethod
    def make_full_config(cls):
        config_dict = dict(
            # todo: legacy_v2 supported

            # support sdxl-base-*
            xl_base=dict(
                model_config=cls.xl_model,
                sampler_config=cls.xl_sampler,
                cond_config=cls.xl_base_cond,
                vae_config=cls.vae,
                backbone_config=cls.xl_base_backbone
            ),

            # support sdxl-refiner-*
            xl_refiner=dict(
                model_config=cls.xl_model,
                sampler_config=cls.xl_sampler,
                cond_config=cls.xl_refiner_cond,
                vae_config=cls.vae,
                backbone_config=cls.xl_refiner_backbone
            ),

            # support sdxl-Turbo-*
            xl_turbo=dict(
                model_config=cls.xl_model,
                sampler_config=cls.xl_sampler,
                cond_config=cls.xl_base_cond,
                vae_config=cls.vae,
                backbone_config=cls.xl_base_backbone
            )
        )
        return config_dict


class WeightConverter(ldm.WeightConverter):
    cond0 = {
        'conditioner.embedders.{5}.transformer.' + k: 'cond.embedders.{5}.transformer.' + v
        for k, v in CLIPModel.WeightConverter.openai_convert_dict.items()
    }

    cond1 = {
        'conditioner.embedders.{5}.model.' + k: 'cond.embedders.{5}.transformer.' + v
        for k, v in CLIPModel.WeightConverter.laion_convert_dict.items()
    }

    cond_convert_dict = {
        **cond0,
        **cond1
    }

    transpose_keys = ('cond.embedders.1.transformer.text_model.proj.weight',)

    @classmethod
    def from_official_lora(cls, state_dict):
        cond_convert_dict = {}
        for k, v in cls.cond0.items():
            k = '.'.join(k.split('.')[4:])
            k = ('lora_te1.' + k).replace('.', '_')
            cond_convert_dict[k] = v.replace('{5}', '0')

        for k, v in cls.cond0.items():
            k = '.'.join(k.split('.')[4:])
            k = ('lora_te2.' + k).replace('.', '_')
            cond_convert_dict[k] = v.replace('{5}', '1')

        backbone_convert_dict = {}
        for k, v in cls.backbone_convert_dict.items():
            k = '.'.join(k.split('.')[2:])
            k = ('lora_unet.' + k).replace('.', '_')
            cond_convert_dict[k] = v

        convert_dict = {
            **cond_convert_dict,
            **backbone_convert_dict,
        }

        state_dict = torch_utils.Converter.convert_keys(state_dict, convert_dict)

        convert_dict = {
            '{0}.lora_down': '{0}.down',
            '{0}.lora_up': '{0}.up',
        }
        state_dict = torch_utils.Converter.convert_keys(state_dict, convert_dict)

        convert_dict = {
            '{0}.input_blocks_{1}.': '{0}.input_blocks.{1}.',
            '{0}.output_blocks_{1}.': '{0}.output_blocks.{1}.',
        }
        state_dict = torch_utils.Converter.convert_keys(state_dict, convert_dict)

        return state_dict


class Model(ldm.Model):
    """https://github.com/Stability-AI/generative-models"""

    # for video
    num_frames = 14

    sampler_mapping = {
        Config.EULER: k_diffusion.EulerSampler
    }

    def make_sampler(self, sampler_config=Config.sampler_config, **kwargs):
        self.sampler = self.sampler_mapping.get(sampler_config['name'], sampler_config['name'])(**sampler_config)

    def make_cond(self, cond_config=[], **kwargs):
        return EmbedderWarp(cond_config)

    def make_txt_cond(self, text, neg_text=None, **kwargs) -> dict:
        default_value = {
            Config.ORIGINAL_SIZE_AS_TUPLE: self.image_size,
            Config.CROP_COORDS_TOP_LEFT: (0, 0),
            Config.TARGET_SIZE_AS_TUPLE: self.image_size,
        }

        value_dicts = []
        _neg_text = neg_text if neg_text is not None else [''] * len(text)
        for prompt, negative_prompt in zip(text, _neg_text):
            value_dict = {}
            for k in self.cond.input_keys:
                if k == Config.TXT:
                    value_dict.update(
                        prompt=prompt,
                        negative_prompt=negative_prompt
                    )
                else:
                    value_dict[k] = kwargs.get(k, default_value[k])

            value_dicts.append(value_dict)

        c_values, uc_values = self.cond(value_dicts, return_uc=self.scale > 1 and neg_text is not None)

        return dict(
            c_values=c_values,
            uc_values=uc_values
        )

    def diffuse(self, x, time, c_values=None, uc_values=None, **kwargs):
        if uc_values is not None:
            x = torch.cat([x] * 2)
            time = torch.cat([time] * 2)
            cond = torch.cat([uc_values[self.cond.COND], c_values[self.cond.COND]])
            y = torch.cat([uc_values[self.cond.VECTOR], c_values[self.cond.VECTOR]])
        else:
            cond = c_values[self.cond.COND]
            y = c_values[self.cond.VECTOR]

        z = self.backbone(x, timesteps=time, context=cond, y=y)
        if uc_values is not None:
            e_t_uncond, e_t = z.chunk(2)

            if isinstance(self.scale, list):
                scale = torch.tensor(self.scale).to(e_t_uncond.device)
                scale = repeat(scale, "1 t -> b t", b=e_t_uncond.shape[0])
                scale = scale.view((*scale.shape, *e_t.shape[2:]))
                e_t_uncond = rearrange(e_t_uncond, "(b t) ... -> b t ...", t=self.num_frames)
                e_t = rearrange(e_t, "(b t) ... -> b t ...", t=self.num_frames)
                e_t = rearrange(e_t_uncond + scale * (e_t - e_t_uncond), "b t ... -> (b t) ...")
            else:
                e_t = e_t_uncond + self.scale * (e_t - e_t_uncond)

        else:
            e_t = z

        return e_t


class ConcatTimestepEmbedderND(nn.Module):
    """embeds each dimension independently and concatenates them"""

    def __init__(self, output_size=256):
        super().__init__()
        self.timestep = ldm.SinusoidalPosEmb(output_size)
        self.output_size = output_size

    def forward(self, x):
        if x.ndim == 1:
            x = x[:, None]
        assert len(x.shape) == 2
        b, dims = x.shape
        x = rearrange(x, "b d -> (b d)")
        emb = self.timestep(x)
        emb = rearrange(emb, "(b d) d2 -> b (d d2)", b=b, d=dims, d2=self.output_size)
        return emb


class EmbedderWarp(nn.Module):
    VECTOR = 'vector'
    COND = 'cond'
    CONCAT = 'concat'

    OUTPUT_DIM2KEYS = {2: VECTOR, 3: COND, 4: CONCAT, 5: CONCAT}
    KEY2CATDIM = {VECTOR: 1, COND: 2, CONCAT: 1}

    embedder_mapping = {
        Config.CLIP: sdv1.CLIPEmbedder,
        Config.TIMESTEP: ConcatTimestepEmbedderND
    }

    def __init__(self, cond_configs: list):
        super().__init__()
        embedders = []
        input_keys = []
        output_size = 0
        for cond_config in cond_configs:
            layer = self.embedder_mapping.get(cond_config['name'], cond_config['name'])(**cond_config['params'])
            input_key = cond_config['input_key']
            embedders.append(layer)
            input_keys.append(input_key)
            if input_key == Config.TXT:
                output_size += layer.output_size

        self.embedders = nn.ModuleList(embedders)
        self.input_keys = input_keys
        self.output_size = output_size  # 2048

    @property
    def device(self):
        return torch_utils.ModuleInfo.possible_device(self)

    def forward(self, value_dicts, return_uc=True):
        batch, batch_uc = self.get_batch(value_dicts)
        c_values = self.get_cond(batch)
        uc_values = self.get_cond(batch_uc) if return_uc else None
        return c_values, uc_values

    def get_cond(self, batch, force_zero_embeddings=()):
        output = {}
        for input_key, embedder in zip(self.input_keys, self.embedders):
            emb_out = embedder(batch[input_key])
            if not isinstance(emb_out, (list, tuple)):
                emb_out = [emb_out]

            for emb in emb_out:
                if input_key in force_zero_embeddings:
                    emb = torch.zeros_like(emb)

                out_key = self.OUTPUT_DIM2KEYS[emb.dim()]
                output.setdefault(out_key, []).append(emb)

        for out_key, v in output.items():
            output[out_key] = torch.cat(v, self.KEY2CATDIM[out_key])

        return output

    def get_batch(self, value_dicts):
        device = self.device
        batch = {}
        batch_uc = {}

        for value_dict in value_dicts:
            for key in self.input_keys:
                if key == Config.TXT:
                    pass

                elif key == Config.POOL_IMAGE:
                    batch.setdefault(key, []).append(value_dict[key].to(dtype=torch.half))

                else:
                    batch.setdefault(key, []).append(torch.tensor(value_dict[key]))

        for k, v in batch_uc.items():
            batch_uc[k] = torch.stack(v).to(device)

        for k, v in batch.items():
            batch[k] = torch.stack(v).to(device)

            if k not in batch_uc:
                batch_uc[k] = torch.clone(batch[k])

        batch[Config.TXT] = torch.stack([value_dict["prompt"] for value_dict in value_dicts])
        batch_uc[Config.TXT] = torch.stack([value_dict["negative_prompt"] for value_dict in value_dicts])

        return batch, batch_uc
