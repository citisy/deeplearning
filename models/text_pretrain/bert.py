import math
import torch
from torch import nn
import torch.nn.functional as F
from utils import torch_utils
from ..layers import Linear
from einops.layers.torch import Rearrange


def convert_hf_weights(state_dict):
    """convert weights from huggingface model to my model

    Usage:
        .. code-block:: python

            from transformers import BertForPreTraining
            state_dict = BertForPreTraining.from_pretrained('...')
            state_dict = convert_hf_weights(state_dict)
            Model(...).load_state_dict(state_dict)
    """
    convert_dict = {
        'bert.embeddings.word_embeddings': 'backbone.embedding.token',
        'bert.encoder.layer.{0}.attention.self.query': 'backbone.encode.{0}.attention.to_qkv.0.0',
        'bert.encoder.layer.{0}.attention.self.key': 'backbone.encode.{0}.attention.to_qkv.1.0',
        'bert.encoder.layer.{0}.attention.self.value': 'backbone.encode.{0}.attention.to_qkv.2.0',
        'bert.encoder.layer.{0}.attention.output.dense': 'backbone.encode.{0}.attention.to_out.1',
        'bert.encoder.layer.{0}.intermediate.dense': 'backbone.encode.{0}.feed_forward.0.linear',
        'bert.encoder.layer.{0}.output.dense': 'backbone.encode.{0}.feed_forward.1.linear',
        'bert.encoder.layer.{0}.attention.output.LayerNorm': 'backbone.encode.{0}.res1.norm',
        'bert.encoder.layer.{0}.output.LayerNorm': 'backbone.encode.{0}.res2.norm'
    }
    state_dict = torch_utils.convert_state_dict(state_dict, convert_dict)

    return state_dict


class Config:
    tiny = dict(hidden_size=128, num_hidden_layers=2, num_attention_heads=2)
    mini = dict(hidden_size=256, num_hidden_layers=4, num_attention_heads=4)
    small = dict(hidden_size=512, num_hidden_layers=4, num_attention_heads=8)
    medium = dict(hidden_size=512, num_hidden_layers=8, num_attention_heads=8)
    base = dict(hidden_size=768, num_hidden_layers=12, num_attention_heads=12)
    large = dict(hidden_size=1024, num_hidden_layers=24, num_attention_heads=16)


class Model(nn.Module):
    """refer to
    paper:
        - [BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding](https://arxiv.org/pdf/1810.04805.pdf)
    code:
        - https://github.com/google-research/bert
        - https://github.com/huggingface/transformers/tree/main/src/transformers/models/bert
        - https://github.com/codertimo/BERT-pytorch
    """

    def __init__(
            self, vocab_size, sp_tag_dict,
            bert_config=Config.base,
            is_nsp=True, nsp_out_features=2,
            is_mlm=True
    ):
        super().__init__()
        self.sp_tag_dict = sp_tag_dict
        self.is_nsp = is_nsp
        self.is_mlm = is_mlm

        self.backbone = Bert(vocab_size, sp_tag_dict, **bert_config)
        if is_nsp:
            self.nsp = NSP(self.backbone.out_features, nsp_out_features)
        if is_mlm:
            self.mlm = MLM(self.backbone.out_features, vocab_size, sp_tag_dict['non_mask'])

        torch_utils.initialize_layers(self)

    def forward(self, x, segment_label, attention_mask=None, next_true=None, mask_true=None):
        x = self.backbone(x, segment_label, attention_mask)

        outputs = {}

        next_pred = None
        if self.is_nsp:
            next_pred = self.nsp(x)
            outputs['next_pred'] = next_pred

        mask_pred = None
        if self.is_mlm:
            mask_pred = self.mlm(x)
            outputs['mask_pred'] = mask_pred

        losses = {}
        if self.training:
            losses = self.loss(x.device, next_pred, mask_pred, next_true, mask_true)

        outputs.update(losses)
        return outputs

    def loss(self, device, next_pred=None, mask_pred=None, next_true=None, mask_true=None):
        losses = {}
        loss = torch.zeros(1, device=device)

        if self.is_nsp:
            next_loss = self.nsp.loss(next_pred, next_true)
            loss += next_loss
            losses['loss.next'] = next_loss

        if self.is_mlm:
            mask_loss = self.mlm.loss(mask_pred, mask_true)
            loss += mask_loss
            losses['loss.mask'] = mask_loss

        losses['loss'] = loss

        return losses


class NSP(nn.Module):
    """Next Sentence Prediction"""

    def __init__(self, in_features, out_features=2):
        super().__init__()
        self.fcn = nn.Linear(in_features, out_features)

    def forward(self, x):
        x = self.fcn(x[:, 0])  # select the first output
        return x

    def loss(self, next_pred, next_true):
        return F.cross_entropy(next_pred, next_true)


class MLM(nn.Module):
    """Masked Language Model"""

    def __init__(self, in_features, out_features, non_mask_tag):
        super().__init__()
        self.non_mask_tag = non_mask_tag
        self.fcn = nn.Linear(in_features, out_features)

    def forward(self, x):
        x = self.fcn(x)
        x = x.transpose(1, 2)  # seq first -> batch first
        return x

    def loss(self, mask_pred, mask_true):
        return F.cross_entropy(mask_pred, mask_true, ignore_index=self.non_mask_tag)


class Bert(nn.Module):
    def __init__(self, vocab_size, sp_tag_dict, max_seq_len=512, n_segment=2, hidden_size=768, num_hidden_layers=12, num_attention_heads=12, drop_prob=0.1):
        super().__init__()
        self.embedding = BERTEmbedding(vocab_size, hidden_size, sp_tag_dict, max_seq_len=max_seq_len, n_segment=n_segment, drop_prob=drop_prob)
        self.encode = nn.ModuleList([TransformerBlock(hidden_size, num_attention_heads, hidden_size * 4, drop_prob) for _ in range(num_hidden_layers)])

        self.out_features = hidden_size
        self.sp_tag_dict = sp_tag_dict

    def forward(self, x, segment_info, attention_mask=None):
        if attention_mask is not None:
            attention_mask = attention_mask[:, None, None].repeat(1, 1, x.size(1), 1)  # mask pad
        x = self.embedding(x, segment_info)
        for m in self.encode:
            x = m(x, attention_mask)

        return x


class BERTEmbedding(nn.Module):
    """TokenEmbedding + PositionalEmbedding + SegmentEmbedding"""

    def __init__(self, vocab_size, embedding_dim, sp_tag_dict, max_seq_len=512, n_segment=2, drop_prob=0.1):
        super().__init__()
        self.token = nn.Embedding(vocab_size, embedding_dim, padding_idx=sp_tag_dict['pad'])
        self.position = PositionalEmbedding(max_seq_len, embedding_dim)

        # note, there add 1 to apply pad token
        # but in `transformers.BertForPreTraining` does not add yet
        self.segment = nn.Embedding(n_segment + 1, embedding_dim, padding_idx=sp_tag_dict['seg_pad'])
        self.head = nn.Sequential(
            nn.LayerNorm(embedding_dim),
            nn.Dropout(drop_prob)
        )
        self.embedding_dim = embedding_dim

    def forward(self, sequence, segment_label):
        """(b, s) -> (b, s, h)
        note, s is a dynamic var"""
        x = self.token(sequence) + self.position(sequence) + self.segment(segment_label)
        return self.head(x)


class PositionalEmbedding(nn.Module):
    def __init__(self, num_embeddings, embedding_dim):
        super().__init__()
        pe = torch.zeros(num_embeddings, embedding_dim).float()
        position = torch.arange(0, num_embeddings).float().unsqueeze(1)
        div_term = (torch.arange(0, embedding_dim, 2).float() * -(math.log(10000.0) / embedding_dim)).exp()

        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)

        pe = pe.unsqueeze(0)
        self.register_buffer('pe', pe)
        self.num_embeddings = num_embeddings
        self.embedding_dim = embedding_dim

    def forward(self, x):
        return self.pe[:, :x.size(1)]


class TransformerBlock(nn.Module):
    """MultiHeadedAttention + PositionWiseFeedForward
    refer to [Attention is All You Need](https://arxiv.org/pdf/1706.03762.pdf)
    """

    def __init__(self, hidden_size, num_attention_heads, feed_forward_hidden, drop_prob=0.1):
        super().__init__()
        self.attention = MultiHeadedAttention(num_attention_heads, d_model=hidden_size)
        self.feed_forward = PositionWiseFeedForward(hidden_size, feed_forward_hidden, drop_prob=drop_prob)
        self.res1 = Residual(hidden_size, drop_prob=drop_prob)
        self.res2 = Residual(hidden_size, drop_prob=drop_prob)
        self.dropout = nn.Dropout(drop_prob)

    def forward(self, x, attention_mask=None):
        """(b, s, h) -> (b, s, h)"""
        x = self.res1(x, lambda _x: self.attention.forward(_x, _x, _x, attention_mask=attention_mask))
        x = self.res2(x, self.feed_forward)
        return self.dropout(x)


class Residual(nn.Module):
    def __init__(self, input_size, drop_prob=0.1):
        super().__init__()
        self.norm = nn.LayerNorm(input_size)
        self.dropout = nn.Dropout(drop_prob)

    def forward(self, x, sublayer):
        return x + self.dropout(sublayer(self.norm(x)))


class MultiHeadedAttention(nn.Module):
    def __init__(self, num_heads, d_model, dropout=0.1):
        super().__init__()
        assert d_model % num_heads == 0

        d_k = d_model // num_heads
        self.to_qkv = nn.ModuleList([nn.Sequential(
            nn.Linear(d_model, d_model),
            Rearrange('b s (n dk)-> b n s dk', dk=d_k, n=num_heads)
        ) for _ in range(3)])
        self.to_out = nn.Sequential(
            Rearrange('b n s dk -> b s (n dk)'),
            nn.Linear(d_model, d_model)
        )
        self.dropout = nn.Dropout(dropout)

    def attend(self, q, k, v, attention_mask=None):
        """Scaled Dot-Product Attention
        attn(q, k, v) = softmax(qk'/sqrt(dk))*v"""
        s = torch.matmul(q, k.transpose(-2, -1)) / math.sqrt(q.size(-1))

        if attention_mask is not None:  # mask pad
            s = s.masked_fill(attention_mask, torch.finfo(s.dtype).min)  # support fp16

        attn = F.softmax(s, dim=-1)
        attn = self.dropout(attn)
        attn = torch.matmul(attn, v)

        return attn

    def forward(self, q, k, v, attention_mask=None):
        q, k, v = [m(x) for m, x in zip(self.to_qkv, (q, k, v))]
        x = self.attend(q, k, v, attention_mask=attention_mask)

        return self.to_out(x)


class PositionWiseFeedForward(nn.Sequential):
    def __init__(self, d_model, d_ff, drop_prob=0.1):
        super().__init__(
            Linear(d_model, d_ff, mode='lad', act=nn.GELU(), drop_prob=drop_prob),
            Linear(d_ff, d_model, mode='l')
        )
