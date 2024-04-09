import math
import torch
import torch.nn as nn
import torch.nn.functional as F
from .transformers import make_causal_attention_mask, TransformerSequential
from .. import bundles, attentions, normalizations, embeddings, layers
from utils import torch_utils


class Config(bundles.Config):
    default_model = 'llama-2-7b'

    @classmethod
    def make_full_config(cls) -> dict:
        return {
            'llama-2-7b': dict(
                hidden_size=4096,
                multiple_of=256,
                num_attention_heads=32,
                n_layer=32,
            )
        }


class WeightLoader(bundles.WeightLoader):
    pass


class WeightConverter:
    @staticmethod
    def from_official(state_dict):
        convert_dict = {
            'tok_embeddings': 'embedding',

            'layers.{0}.attention.wq': 'encoder.{0}.attn_res.fn.to_qkv.0',
            'layers.{0}.attention.wk': 'encoder.{0}.attn_res.fn.to_qkv.1',
            'layers.{0}.attention.wv': 'encoder.{0}.attn_res.fn.to_qkv.2',
            'layers.{0}.attention.wo': 'encoder.{0}.attn_res.fn.to_out.linear',
            'layers.{0}.attention_norm': 'encoder.{0}.attn_res.norm',

            'layers.{0}.feed_forward.w1': 'encoder.{0}.ff_res.fn.f1.linear',
            'layers.{0}.feed_forward.w2': 'encoder.{0}.ff_res.fn.f2.linear',
            'layers.{0}.feed_forward.w3': 'encoder.{0}.ff_res.fn.f3.linear',
            'layers.{0}.ffn_norm': 'encoder.{0}.ff_res.norm',

            'output': 'head',

        }

        state_dict = torch_utils.Converter.convert_keys(state_dict, convert_dict)
        return state_dict


class Model(nn.Module):
    def __init__(self, vocab_size, hidden_size,
                 num_attention_heads, n_layer, multiple_of,
                 max_seq_len=512, max_batch_size=8,
                 drop_prob=0., norm_eps=1e-05):
        super().__init__()

        self.embedding = nn.Embedding(vocab_size, hidden_size)
        rotary_embedding = embeddings.RotaryEmbedding(max_seq_len * 2, hidden_size // num_attention_heads)
        ff_hidden_size = int(hidden_size * 4 * 2 / 3)
        ff_hidden_size = multiple_of * ((ff_hidden_size + multiple_of - 1) // multiple_of)
        self.encoder = TransformerSequential(
            hidden_size, num_attention_heads, ff_hidden_size,
            norm_first=True, drop_prob=drop_prob,
            attend_fn=attentions.MemoryRotaryAttend,
            attend_fn_kwargs=dict(
                n_heads=num_attention_heads,
                n_mem_size=max_seq_len,
                head_dim=hidden_size // num_attention_heads,
                max_batch_size=max_batch_size,
                embedding=rotary_embedding
            ),
            feed_forward_fn=FeedForward,
            norm_fn=normalizations.RMSNorm2D,
            norm_kwargs=dict(eps=1e-5),
            fn_kwargs=dict(bias=False),
            ff_kwargs=dict(bias=False),
            num_blocks=n_layer
        )
        self.norm = normalizations.RMSNorm2D(hidden_size, eps=norm_eps)
        self.head = nn.Linear(hidden_size, vocab_size, bias=False)

    def forward(self, x, **kwargs):
        if self.training:
            # note, shift one token to predict the future word
            trues = torch.cat([x[:, 1:], torch.full((len(x), 1), self.pad_id)], dim=1)
            preds = self.decode(x)
            loss = self.loss(preds, trues)
            return {'loss': loss}
        else:
            return {'preds': self.post_process(x, **kwargs)}

    def loss(self, preds, trues):
        preds = preds.transpose(1, 2)  # seq first -> class first
        return F.cross_entropy(preds, trues)

    def post_process(self, x, seq_lens=None, max_gen_len=100, top_k=1, **kwargs):
        assert seq_lens is not None
        batch_size = len(x)
        prev_pos = 0
        min_pos = min(seq_lens)
        for cur_pos in range(min_pos, max_gen_len):
            output_data = self.decode(x[:, prev_pos: cur_pos], start_pos=prev_pos, **kwargs)
            # add next preds
            x = torch.cat([x, torch.zeros((batch_size, 1)).to(x)], dim=-1)
            for index in range(batch_size):
                preds = output_data[index, -1]
                arg = torch.argsort(preds, descending=True)
                keep = arg[:top_k]
                preds = preds[keep]
                preds = preds / preds.sum()

                # random sampling
                next_id = keep[preds.multinomial(1)[0]]
                x[index][cur_pos] = next_id
            prev_pos = cur_pos
        return x

    def decode(self, sequence, start_pos=0, **kwargs):
        x = self.embedding(sequence)
        attention_mask = make_causal_attention_mask(x, start_pos=start_pos)
        x = self.encoder(x, attention_mask=attention_mask, start_pos=start_pos, **kwargs)
        x = self.norm(x)
        x = self.head(x)
        return x


class FeedForward(nn.Module):
    """y = F2(a(F1(x)) * F3(x))"""

    def __init__(self, hidden_size, ff_hidden_size, act=None, drop_prob=0.1, **kwargs):
        super().__init__()
        act = act or nn.SiLU()
        self.f1 = layers.Linear(hidden_size, ff_hidden_size, mode='la', act=act, **kwargs)
        self.f2 = layers.Linear(ff_hidden_size, hidden_size, mode='ld', drop_prob=drop_prob, **kwargs)
        self.f3 = layers.Linear(hidden_size, ff_hidden_size, mode='l', **kwargs)

    def forward(self, x):
        return self.f2(self.f1(x) * self.f3(x))
