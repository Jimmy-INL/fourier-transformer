try:
    from libs.layers import *
    from libs.utils_ft import *
except:
    from layers import *
    from utils_ft import *

import copy
import os
import sys
from collections import defaultdict
from typing import Any, Optional

import torch
import torch.nn as nn
from torch import Tensor
from torch.nn import MultiheadAttention
from torch.nn.init import constant_, xavier_normal_, xavier_uniform_
from torch.nn.modules import activation
from torchinfo import summary

current_path = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.dirname(current_path)
sys.path.append(HOME)

ADDITIONAL_ATTR = ['normalizer', 'raw_laplacian', 'return_ortho',
                   'residual_type', 'norm_type', 'boundary_condition',
                   'upscaler_size', 'downscaler_size',
                   'regressor_activation', 'attn_activation',
                   'downscaler_activation', 'upscaler_activation',
                   'encoder_dropout', 'decoder_dropout', ]


class FourierTransformerEncoderLayer(nn.Module):
    def __init__(self,
                 d_model=96,
                 pos_dim=1,
                 n_head=2,
                 dim_feedforward=512,
                 attention_type='fourier',
                 pos_emb=False,
                 layer_norm=True,
                 attn_norm=None,
                 norm_type='layer',
                 batch_norm=False,
                 attn_weight=False,
                 xavier_init=1e-4,
                 diagonal_weight: float = 1.,
                 symmetric_init=True,
                 residual_type='add',
                 activation_type='relu',
                 dropout=0.1,
                 debug=False,
                 ):
        super(FourierTransformerEncoderLayer, self).__init__()
        if dropout is None:
            dropout = 0.1
        if attention_type in ['linear', 'softmax']:
            dropout = 0.1
        attn_norm = not layer_norm if attn_norm is None else attn_norm
        norm_type = 'layer' if norm_type is None else norm_type
        self.attn = SimpleAttention(n_head=n_head,
                                    d_model=d_model,
                                    attention_type=attention_type,
                                    diagonal_weight=diagonal_weight,
                                    xavier_init=xavier_init,
                                    symmetric_init=symmetric_init,
                                    pos_dim=pos_dim,
                                    norm=attn_norm,
                                    norm_type=norm_type,
                                    dropout=dropout)
        self.d_model = d_model
        self.n_head = n_head
        self.pos_dim = pos_dim
        self.add_layer_norm = layer_norm
        if layer_norm:
            self.layer_norm1 = nn.LayerNorm(d_model, eps=1e-7)
            self.layer_norm2 = nn.LayerNorm(d_model, eps=1e-7)

        self.ff = FeedForward(d_model,
                              dim_feedforward=dim_feedforward,
                              batch_norm=batch_norm,
                              activation=activation_type)
        self.dropout1 = nn.Dropout(dropout)
        self.dropout2 = nn.Dropout(dropout)
        self.residual_type = residual_type  # plus or minus
        self.add_pos_emb = pos_emb
        if self.add_pos_emb:
            self.pos_emb = PositionalEncoding(d_model)

        self.debug = debug
        self.attn_weight = attn_weight

    def forward(self, x, pos=None, weight=None):
        '''
        - x: node feature, (n_batch, seq_len, n_feats)
        - pos: position coords, needed in every head

        Remark:
            - for n_head=1, no need to encode positional 
            information if coords are in features
        '''
        if self.add_pos_emb:
            x = x.permute((1, 0, 2))
            x = self.pos_emb(x)
            x = x.permute((1, 0, 2))

        if pos is not None and self.pos_dim > 0:
            att_output, attn_weight = self.attn(
                x, x, x, pos=pos, weight=weight)  # encoder no mask
        else:
            att_output, attn_weight = self.attn(x, x, x, weight=weight)

        if self.residual_type in ['add', 'plus'] or self.residual_type is None:
            x = x + self.dropout1(att_output)
        else:
            x = x - self.dropout1(att_output)
        if self.add_layer_norm:
            x = self.layer_norm1(x)

        x1 = self.ff(x)
        x = x + self.dropout2(x1)

        if self.add_layer_norm:
            x = self.layer_norm2(x)

        if self.attn_weight:
            return x, attn_weight
        else:
            return x


class TransformerEncoderLayer(nn.Module):
    r"""
    Taken from official torch implementation:
    https://pytorch.org/docs/stable/_modules/torch/nn/modules/transformer.html#TransformerEncoderLayer
        - add a layer norm switch
        - add an attn_weight output switch
        - batch first
    """

    def __init__(self, d_model, nhead,
                 dim_feedforward=2048,
                 dropout=0.1,
                 layer_norm=True,
                 attn_weight=False,
                 ):
        super(TransformerEncoderLayer, self).__init__()
        self.self_attn = MultiheadAttention(d_model, nhead, dropout=dropout)
        # Implementation of Feedforward model
        self.linear1 = nn.Linear(d_model, dim_feedforward)
        self.dropout = nn.Dropout(dropout)
        self.linear2 = nn.Linear(dim_feedforward, d_model)

        self.norm1 = nn.LayerNorm(d_model)
        self.norm2 = nn.LayerNorm(d_model)
        self.dropout1 = nn.Dropout(dropout)
        self.dropout2 = nn.Dropout(dropout)
        self.add_layer_norm = layer_norm
        self.attn_weight = attn_weight
        self.activation = nn.ReLU()

    def __setstate__(self, state):
        if 'activation' not in state:
            state['activation'] = F.relu
        super(TransformerEncoderLayer, self).__setstate__(state)

    def forward(self, src: Tensor,
                pos: Optional[Tensor] = None,
                weight: Optional[Tensor] = None,
                src_mask: Optional[Tensor] = None,
                src_key_padding_mask: Optional[Tensor] = None) -> Tensor:
        r"""Pass the input through the encoder layer.

        Args (modified from torch):
            src: the sequence to the encoder layer (required):  (n_batch, seq_len, d_model)
            src_mask: the mask for the src sequence (optional).
            src_key_padding_mask: the mask for the src keys per batch (optional).

        Shape:
            see the docs in Transformer class.

        Remark: 
            PyTorch official implementation: (seq_len, n_batch, d_model) as input
            here we permute the first two dims as input
            so in the first line the dim needs to be permuted then permuted back
        """
        if pos is not None:
            src = torch.cat([pos, src], dim=-1)

        src = src.permute(1, 0, 2)

        if (src_mask is None) or (src_key_padding_mask is None):
            src2, attn_weight = self.self_attn(src, src, src)
        else:
            src2, attn_weight = self.self_attn(src, src, src, attn_mask=src_mask,
                                               key_padding_mask=src_key_padding_mask)

        src = src + self.dropout1(src2)
        if self.add_layer_norm:
            src = self.norm1(src)
        src2 = self.linear2(self.dropout(self.activation(self.linear1(src))))
        src = src + self.dropout2(src2)
        if self.add_layer_norm:
            src = self.norm2(src)
        src = src.permute(1, 0, 2)
        if self.attn_weight:
            return src, attn_weight
        else:
            return src


class TransformerEncoderWrapper(nn.Module):
    r"""TransformerEncoder is a stack of N encoder layers
        Modified from pytorch official implementation
        TransformerEncoder's input and output shapes follow
        those of the encoder_layer fed into as this is essentially a wrapper

    Args:
        encoder_layer: an instance of the TransformerEncoderLayer() class (required).
        num_layers: the number of sub-encoder-layers in the encoder (required).
        norm: the layer normalization component (optional).

    Examples::
        >>> encoder_layer = nn.TransformerEncoderLayer(d_model=512, nhead=8)
        >>> transformer_encoder = nn.TransformerEncoder(encoder_layer, num_layers=6)
        >>> src = torch.rand(10, 32, 512)
        >>> out = transformer_encoder(src)
    """
    __constants__ = ['norm']

    def __init__(self, encoder_layer, num_layers,
                 norm=None,):
        super(TransformerEncoderWrapper, self).__init__()
        self.layers = nn.ModuleList(
            [copy.deepcopy(encoder_layer) for i in range(num_layers)])
        self.num_layers = num_layers
        self.norm = norm

    def forward(self, src: Tensor,
                mask: Optional[Tensor] = None,
                src_key_padding_mask: Optional[Tensor] = None) -> Tensor:
        r"""Pass the input through the encoder layers in turn.

        Args:
            src: the sequence to the encoder (required).
            mask: the mask for the src sequence (optional).
            src_key_padding_mask: the mask for the src keys per batch (optional).

        Shape:
            see the docs in Transformer class.
        """
        output = src

        for mod in self.layers:
            output = mod(output, src_mask=mask,
                         src_key_padding_mask=src_key_padding_mask)
        if self.norm is not None:
            output = self.norm(output)

        return output


class GCN(nn.Module):
    def __init__(self,
                 node_feats=4,
                 out_features=96,
                 num_gcn_layers=2,
                 edge_feats=6,
                 activation=True,
                 raw_laplacian=False,
                 dropout=0.1,
                 debug=False):
        super(GCN, self).__init__()
        '''
        A simple GCN, a wrapper for Kipf and Weiling's code
        '''
        self.edge_learner = EdgeEncoder(out_dim=out_features,
                                        edge_feats=edge_feats,
                                        raw_laplacian=raw_laplacian
                                        )
        self.gcn_layer0 = GraphConvolution(in_features=node_feats,  # hard coded
                                           out_features=out_features,
                                           debug=debug,
                                           )
        self.gcn_layers = nn.ModuleList([copy.deepcopy(GraphConvolution(
            in_features=out_features,  # hard coded
            out_features=out_features,
            debug=debug
        )) for _ in range(1, num_gcn_layers)])
        self.activation = activation
        self.relu = nn.ReLU()
        self.dropout = nn.Dropout(dropout)
        self.edge_feats = edge_feats
        self.debug = debug

    def forward(self, x, edge):
        x = x.permute(0, 2, 1).contiguous()
        edge = edge.permute([0, 3, 1, 2]).contiguous()
        assert edge.size(1) == self.edge_feats

        edge = self.edge_learner(edge)

        out = self.gcn_layer0(x, edge)
        for gc in self.gcn_layers[:-1]:
            out = gc(out, edge)
            if self.activation:
                out = self.relu(out)

        # last layer no activation
        out = self.gcn_layers[-1](out, edge)
        return out.permute(0, 2, 1)


class GAT(nn.Module):
    def __init__(self,
                 node_feats=4,
                 out_features=96,
                 num_gcn_layers=2,
                 edge_feats=None,
                 activation=False,
                 debug=False):
        super(GAT, self).__init__()
        '''
        A simple GAT: modified from the official implementation
        '''
        self.gat_layer0 = GraphAttention(in_features=node_feats,
                                         out_features=out_features,
                                         )
        self.gat_layers = nn.ModuleList([copy.deepcopy(GraphAttention(
            in_features=out_features,
            out_features=out_features,
        )) for _ in range(1, num_gcn_layers)])
        self.activation = activation
        self.relu = nn.ReLU()
        self.debug = debug

    def forward(self, x, edge):
        '''
        input: node feats (-1, seq_len, n_feats)
               edge only takes adj (-1, seq_len, seq_len)
               edge matrix first one in the last dim is graph Lap.
        '''
        edge = edge[..., 0].contiguous()

        out = self.gat_layer0(x, edge)

        for layer in self.gat_layers[:-1]:
            out = layer(out, edge)
            if self.activation:
                out = self.relu(out)

        # last layer no activation
        return self.gat_layers[-1](out, edge)


class PointwiseRegressor(nn.Module):
    def __init__(self, in_dim,  # input dimension
                 n_hidden,
                 out_dim,  # number of target dim
                 num_layers: int = 2,
                 spacial_fc: bool = False,
                 spacial_dim=1,
                 dropout=0.1,
                 activation='silu',
                 debug=False):
        super(PointwiseRegressor, self).__init__()
        '''
        A wrapper for a simple pointwise linear layers
        '''
        if dropout is None:
            dropout = 0.1
        self.spacial_fc = spacial_fc
        activ = nn.SiLU() if activation == 'silu' else nn.ReLU()
        if self.spacial_fc:
            in_dim = in_dim + spacial_dim
            self.fc = nn.Linear(in_dim, n_hidden)
        self.ff = nn.ModuleList([nn.Sequential(
                                nn.Linear(n_hidden, n_hidden),
                                activ,
                                )])
        for _ in range(num_layers - 1):
            self.ff.append(nn.Sequential(
                nn.Linear(n_hidden, n_hidden),
                activ,
            ))
        self.dropout = nn.Dropout(dropout)
        self.out = nn.Linear(n_hidden, out_dim)
        self.debug = debug

    def forward(self, x, grid=None):
        '''
        2D:
            Input: (-1, n, n, in_features)
            Output: (-1, n, n, n_targets)
        1D:
            Input: (-1, n, in_features)
            Output: (-1, n, n_targets)
        '''
        if self.spacial_fc:
            x = torch.cat([x, grid], dim=-1)
            x = self.fc(x)

        for layer in self.ff:
            x = layer(x)
            x = self.dropout(x)

        x = self.out(x)
        return x


class SpectralRegressor(nn.Module):
    def __init__(self, in_dim,
                 n_hidden,
                 freq_dim,  # number of frequency features
                 out_dim,  # number of target dim
                 modes: int,  # number of fourier modes
                 num_spectral_layers: int = 2,
                 n_grid=None,
                 dim_feedforward=None,
                 spacial_fc=False,
                 spacial_dim=2,
                 return_freq=False,
                 normalizer=None,
                 activation='silu',
                 last_activation=True,
                 dropout=0.1,
                 debug=False):
        super(SpectralRegressor, self).__init__()
        '''
        A wrapper for both SpectralConv1d and SpectralConv2d
        Ref: Li et 2020 FNO paper
        https://github.com/zongyi-li/fourier_neural_operator/blob/master/fourier_2d.py
        A new implementation incoporating all spacial-based FNO
        in_dim: input dimension, (either n_hidden or spacial dim)
        n_hidden: number of hidden features out from attention to the fourier conv or 
        '''
        if spacial_dim == 2:  # 2d, function + (x,y)
            spectral_conv = SpectralConv2d
        elif spacial_dim == 1:  # 1d, function + x
            spectral_conv = SpectralConv1d
        else:
            raise NotImplementedError("3D not implemented.")
        if activation == 'silu' or activation is None:
            self.activation = nn.SiLU()
        else:
            self.activation = nn.ReLU()
        if dropout is None:
            dropout = 0.1
        self.spacial_fc = spacial_fc  # False in Transformer
        if self.spacial_fc:
            self.fc = nn.Linear(in_dim + spacial_dim, n_hidden)
        self.spectral_conv = nn.ModuleList([spectral_conv(in_dim=n_hidden,
                                                          out_dim=freq_dim,
                                                          n_grid=n_grid,
                                                          modes=modes,
                                                          dropout=dropout,
                                                          activation=activation,
                                                          return_freq=return_freq,
                                                          debug=debug)])
        for _ in range(num_spectral_layers - 1):
            self.spectral_conv.append(spectral_conv(in_dim=freq_dim,
                                                    out_dim=freq_dim,
                                                    n_grid=n_grid,
                                                    modes=modes,
                                                    dropout=dropout,
                                                    activation=activation,
                                                    return_freq=return_freq,
                                                    debug=debug))
        if not last_activation:
            self.spectral_conv[-1].activation = Identity()

        self.n_grid = n_grid  # dummy for debug
        self.dim_feedforward = 2*spacial_dim * \
            freq_dim if dim_feedforward is None else dim_feedforward
        self.regressor = nn.Sequential(
            nn.Linear(freq_dim, self.dim_feedforward),
            self.activation,
            nn.Linear(self.dim_feedforward, out_dim),
        )
        self.normalizer = normalizer
        self.return_freq = return_freq
        self.debug = debug

    def forward(self, x, edge=None, pos=None, grid=None):
        '''
        2D:
            Input: (-1, n, n, in_features)
            Output: (-1, n, n, n_targets)
        1D:
            Input: (-1, n, in_features)
            Output: (-1, n, n_targets)
        '''
        if self.spacial_fc:
            x = torch.cat([x, grid], dim=-1)
            x = self.fc(x)

        for layer in self.spectral_conv:
            if self.return_freq:
                x_ft, x = layer(x)
            else:
                x = layer(x)

        x = self.regressor(x)

        if self.normalizer:
            x = self.normalizer.inverse_transform(x)

        if self.return_freq:
            return x_ft, x
        else:
            return x


class DownScaler(nn.Module):
    def __init__(self, in_dim,  # num of the orig feats
                 out_dim,  # hidden feats for GCN
                 dropout=0.1,
                 padding=5,
                 downsample_mode='conv',
                 activation_type='silu',
                 interp_size=None,
                 debug=False):
        super(DownScaler, self).__init__()
        '''
        A wrapper for conv2d/interp downscaler
        '''
        if downsample_mode == 'conv':
            self.downsample = nn.Sequential(Conv2dEncoder(in_dim=in_dim,
                                                          out_dim=out_dim,
                                                          activation_type=activation_type,
                                                          debug=debug),
                                            Conv2dEncoder(in_dim=out_dim,
                                                          out_dim=out_dim,
                                                          padding=padding,
                                                          activation_type=activation_type,
                                                          debug=debug))
        elif downsample_mode == 'interp':
            self.downsample = Interp2dEncoder(in_dim=in_dim,
                                              out_dim=out_dim,
                                              scale_factor=interp_size,
                                              activation_type=activation_type,
                                              debug=debug)
        else:
            raise NotImplementedError("downsample mode not implemented.")
        self.dropout = nn.Dropout(dropout)
        self.in_dim = in_dim
        self.out_dim = out_dim

    def forward(self, x):
        '''
        2D:
            Input: (-1, n, n, in_dim)
            Output: (-1, n_s, n_s, out_dim)
        '''
        n_grid = x.size(1)
        bsz = x.size(0)
        x = x.view(bsz, n_grid, n_grid, self.in_dim)
        x = x.permute(0, 3, 1, 2)
        x = self.downsample(x)
        x = x.permute(0, 2, 3, 1)
        return x


class UpScaler(nn.Module):
    def __init__(self, in_dim: int,
                 out_dim: int,
                 hidden_dim=None,
                 padding=2,
                 output_padding=0,
                 dropout=0.1,
                 upsample_mode='conv',
                 activation_type='silu',
                 interp_mode='bilinear',
                 interp_size=None,
                 debug=False):
        super(UpScaler, self).__init__()
        '''
        A wrapper for deConv2d upscaler
        Deconv: Conv1dTranspose
        Interp: interp->conv->interp
        '''
        hidden_dim = in_dim if hidden_dim is None else hidden_dim
        if upsample_mode in ['conv', 'deconv']:
            self.upsample = nn.Sequential(
                DeConv2dBlock(in_dim=in_dim,
                              out_dim=out_dim,
                              hidden_dim=hidden_dim,
                              padding=padding,
                              output_padding=output_padding,
                              dropout=dropout,
                              activation_type=activation_type,
                              debug=debug),
                DeConv2dBlock(in_dim=in_dim,
                              out_dim=out_dim,
                              hidden_dim=hidden_dim,
                              padding=padding*2,
                              output_padding=output_padding,
                              dropout=dropout,
                              activation_type=activation_type,
                              debug=debug))
        elif upsample_mode == 'interp':
            self.upsample = Interp2dUpsample(in_dim=in_dim,
                                             out_dim=out_dim,
                                             interp_mode=interp_mode,
                                             interp_size=interp_size,
                                             dropout=dropout,
                                             activation_type=activation_type,
                                             debug=debug)
        else:
            raise NotImplementedError("upsample mode not implemented.")
        self.in_dim = in_dim
        self.out_dim = out_dim

    def forward(self, x):
        '''
        2D:
            Input: (-1, n_s, n_s, in_dim)
            Output: (-1, n, n, out_dim)
        '''
        x = x.permute(0, 3, 1, 2)
        x = self.upsample(x)
        x = x.permute(0, 2, 3, 1)
        return x


class FourierTransformer(nn.Module):
    def __init__(self, **kwargs):
        super(FourierTransformer, self).__init__()
        self.config = defaultdict(lambda: None, **kwargs)
        self._get_setting()
        self._initialize()

    def forward(self, node, edge, pos, grid=None, weight=None):
        '''
        - node: (N, seq_len, node_feats)
        - pos: (N, seq_len, pos_dim)
        - edge: (N, seq_len, seq_len, edge_feats)
        - weight: (N, seq_len, seq_len): mass matrix prefered
            or (N, seq_len) when mass matrices are not provided
        '''
        x_ortho = []
        attn_weights = []

        x = self.feat_extract(node, edge)

        if self.spacial_residual or self.return_ortho:
            res = x.contiguous()
            x_ortho.append(res)

        for encoder in self.encoder_layers:
            if self.return_attn_weight:
                x, attn_weight = encoder(x, pos, weight)
                attn_weights.append(attn_weight)
            else:
                x = encoder(x, pos, weight)

            if self.return_ortho:
                x_ortho.append(x.contiguous())

        if self.spacial_residual:
            x = res + x

        x_freq = self.freq_regressor(
            x)[:, :self.pred_len, :] if self.n_freq_targets > 0 else None

        x = self.dp(x)
        x = self.regressor(x, grid=grid)

        return dict(preds=x,
                    preds_freq=x_freq,
                    preds_ortho=x_ortho,
                    attn_weights=attn_weights)

    def _initialize(self):
        self._get_graph()

        self._get_encoder()

        if self.n_freq_targets > 0:
            self._get_freq_regressor()

        self._get_regressor()

        if self.decoder_type in ['pointwise', 'convolution']:
            self._initialize_layer(self.regressor)

    @staticmethod
    def _initialize_layer(layer, gain=1e-2):
        for param in layer.parameters():
            if param.ndim > 1:
                xavier_uniform_(param, gain=gain)
            else:
                constant_(param, 0)

    def _get_setting(self):
        all_attr = list(self.config.keys()) + ADDITIONAL_ATTR
        for key in all_attr:
            setattr(self, key, self.config[key])

        self.dim_feedforward = 2 * \
            self.n_hidden if self.dim_feedforward is None else self.dim_feedforward
        self.spacial_dim = self.pos_dim if self.config['spacial_dim'] is None else self.spacial_dim
        self.spacial_fc = False if self.config['spacial_fc'] is None else self.spacial_fc
        self.dp = nn.Dropout(
            0.1 if self.dropout is None else self.dropout)
        if self.decoder_type == 'attention':
            self.num_ft_layers += 1
        self.attention_types = ['fourier', 'integral',
                                'cosine', 'galerkin', 'linear', 'softmax']

    def _get_graph(self):
        if self.num_feat_layers > 0 and self.feat_extract_type == 'gcn':
            self.feat_extract = GCN(node_feats=self.node_feats,
                                    edge_feats=self.edge_feats,
                                    num_gcn_layers=self.num_feat_layers,
                                    out_features=self.n_hidden,
                                    activation=self.graph_activation,
                                    raw_laplacian=self.raw_laplacian,
                                    debug=self.debug,
                                    )
        elif self.num_feat_layers > 0 and self.feat_extract_type == 'gat':
            self.feat_extract = GAT(node_feats=self.node_feats,
                                    out_features=self.n_hidden,
                                    num_gcn_layers=self.num_feat_layers,
                                    activation=self.graph_activation,
                                    debug=self.debug,
                                    )
        else:
            self.feat_extract = Identity(in_features=self.node_feats,
                                         out_features=self.n_hidden)

    def _get_encoder(self):
        if self.attention_type in self.attention_types:
            encoder_layer = FourierTransformerEncoderLayer(d_model=self.n_hidden,
                                                           n_head=self.n_head,
                                                           attention_type=self.attention_type,
                                                           dim_feedforward=self.dim_feedforward,
                                                           layer_norm=self.layer_norm,
                                                           attn_norm=self.attn_norm,
                                                           norm_type=self.norm_type,
                                                           batch_norm=self.batch_norm,
                                                           pos_dim=self.pos_dim,
                                                           xavier_init=self.xavier_init,
                                                           diagonal_weight=self.diagonal_weight,
                                                           symmetric_init=self.symmetric_init,
                                                           attn_weight=self.return_attn_weight,
                                                           residual_type=self.residual_type,
                                                           activation_type=self.attn_activation,
                                                           dropout=self.encoder_dropout,
                                                           debug=self.debug)
        else:
            encoder_layer = TransformerEncoderLayer(d_model=self.n_hidden,
                                                    nhead=self.n_head,
                                                    dim_feedforward=self.dim_feedforward,
                                                    layer_norm=self.layer_norm,
                                                    attn_weight=self.return_attn_weight,
                                                    dropout=self.encoder_dropout
                                                    )
        self.encoder_layers = nn.ModuleList(
            [copy.deepcopy(encoder_layer) for _ in range(self.num_ft_layers)])

    def _get_freq_regressor(self):
        if self.bulk_regression:
            self.freq_regressor = BulkRegressor(in_dim=self.seq_len,
                                                n_feats=self.n_hidden,
                                                n_targets=self.n_freq_targets,
                                                pred_len=self.pred_len)
        else:
            self.freq_regressor = nn.Sequential(
                nn.Linear(self.n_hidden, self.n_hidden),
                nn.ReLU(),
                nn.Linear(self.n_hidden, self.n_freq_targets),
            )

    def _get_regressor(self):
        if self.decoder_type == 'pointwise':
            self.regressor = PointwiseRegressor(in_dim=self.n_hidden,
                                                n_hidden=self.n_hidden,
                                                out_dim=self.n_targets,
                                                spacial_fc=self.spacial_fc,
                                                spacial_dim=self.spacial_dim,
                                                activation=self.regressor_activation,
                                                dropout=self.decoder_dropout,
                                                debug=self.debug)
        elif self.decoder_type == 'ifft':
            self.regressor = SpectralRegressor(in_dim=self.n_hidden,
                                               n_hidden=self.n_hidden,
                                               freq_dim=self.freq_dim,
                                               out_dim=self.n_targets,
                                               num_spectral_layers=self.num_regressor_layers,
                                               modes=self.fourier_modes,
                                               spacial_dim=self.spacial_dim,
                                               spacial_fc=self.spacial_fc,
                                               dim_feedforward=self.freq_dim,
                                               activation=self.regressor_activation,
                                               dropout=self.decoder_dropout,
                                               )
        else:
            raise NotImplementedError("Decoder type not implemented")

    def get_graph(self):
        return self.gragh

    def get_encoder(self):
        return self.encoder_layers


class FourierTransformer2D(nn.Module):
    def __init__(self, **kwargs):
        super(FourierTransformer2D, self).__init__()
        self.config = defaultdict(lambda: None, **kwargs)
        self._get_setting()
        self._initialize()

    def forward(self, node, edge, pos, grid, weight=None):
        '''
        - node: (N, n, n, node_feats)
        - pos: (N, n_s*n_s, pos_dim)
        - edge: (N, n_s*n_s, n_s*n_s, edge_feats)
        - weight: (N, n_s*n_s, n_s*n_s): mass matrix prefered
            or (N, n_s*n_s) when mass matrices are not provided (lumped mass)
        - grid: (N, n-2, n-2, 2) excluding boundary
        '''
        bsz = node.size(0)
        n_s = int(pos.size(1)**(0.5))
        x_ortho = []
        attn_weights = []

        if not self.scaler:
            node = torch.cat(
                [node, pos.contiguous().view(bsz, n_s, n_s, -1)], dim=-1)
        x = self.downscaler(node)
        x = x.view(bsz, -1, self.n_hidden)

        x = self.feat_extract(x, edge)
        x = self.dropout(x)

        for encoder in self.encoder_layers:
            if self.return_attn_weight:
                x, attn_weight = encoder(x, pos, weight)
                attn_weights.append(attn_weight)
            else:
                x = encoder(x, pos, weight)
            if self.return_ortho:
                x_ortho.append(x.contiguous())

        x = x.view(bsz, n_s, n_s, self.n_hidden)
        x = self.upscaler(x)

        x = self.dropout(x)
        if x.size(1) != grid.size(1):
            x = x[:, 1:-1, 1:-1].contiguous()
        x = self.regressor(x, grid=grid)
        if self.normalizer:
            x = self.normalizer.inverse_transform(x)

        if self.boundary_condition == 'dirichlet' or self.boundary_condition is None:
            x = F.pad(x, (0, 0, 1, 1, 1, 1), "constant", 0)

        return dict(preds=x,
                    preds_ortho=x_ortho,
                    attn_weights=attn_weights)

    def _initialize(self):
        self._get_graph()
        self._get_scaler()
        self._get_encoder()
        self._get_regressor()

    @staticmethod
    def _initialize_layer(layer, gain=1e-2):
        for param in layer.parameters():
            if param.ndim > 1:
                xavier_uniform_(param, gain=gain)
            else:
                constant_(param, 0)

    @staticmethod
    def get_pos(pos, downsample):
        '''
        get the downscaled position in 2d
        '''
        bsz = pos.size(0)
        n_grid = pos.size(1)
        x, y = pos[..., 0], pos[..., 1]
        x = x.view(bsz, n_grid, n_grid)
        y = y.view(bsz, n_grid, n_grid)
        x = x[:, ::downsample, ::downsample].contiguous()
        y = y[:, ::downsample, ::downsample].contiguous()
        return torch.stack([x, y], dim=-1)

    def _get_setting(self):
        all_attr = list(self.config.keys()) + ADDITIONAL_ATTR
        for key in all_attr:
            setattr(self, key, self.config[key])

        self.dim_feedforward = 2 * \
            self.n_hidden if self.dim_feedforward is None else self.dim_feedforward
        self.dropout = nn.Dropout(
            0.1 if self.dropout is None else self.dropout)
        if self.decoder_type == 'attention':
            self.num_ft_layers += 1
        self.attention_types = ['fourier', 'integral', 'local', 'global',
                                'cosine', 'galerkin', 'linear', 'softmax']
        self.scaler = self.upscaler_size and self.downscaler_size

    def _get_graph(self):
        if self.feat_extract_type == 'gcn' and self.num_feat_layers > 0:
            self.feat_extract = GCN(node_feats=self.n_hidden,
                                    edge_feats=self.edge_feats,
                                    num_gcn_layers=self.num_feat_layers,
                                    out_features=self.n_hidden,
                                    activation=self.graph_activation,
                                    raw_laplacian=self.raw_laplacian,
                                    debug=self.debug,
                                    )
        elif self.feat_extract_type == 'gat' and self.num_feat_layers > 0:
            self.feat_extract = GAT(node_feats=self.n_hidden,
                                    out_features=self.n_hidden,
                                    num_gcn_layers=self.num_feat_layers,
                                    activation=self.graph_activation,
                                    debug=self.debug,
                                    )
        else:
            self.feat_extract = Identity()

    def _get_scaler(self):
        if self.scaler:
            self.downscaler = DownScaler(in_dim=self.node_feats,
                                         out_dim=self.n_hidden,
                                         downsample_mode=self.downsample_mode,
                                         interp_size=self.downscaler_size,
                                         dropout=self.downscaler_dropout,
                                         activation_type=self.downscaler_activation)
            self.upscaler = UpScaler(in_dim=self.n_hidden,
                                     out_dim=self.n_hidden,
                                     upsample_mode=self.upsample_mode,
                                     interp_size=self.upscaler_size,
                                     dropout=self.upscaler_dropout,
                                     activation_type=self.upscaler_activation)
        else:
            self.downscaler = Identity(in_features=self.node_feats+self.spacial_dim,
                                       out_features=self.n_hidden)
            self.upscaler = Identity()

    def _get_encoder(self):
        if self.attention_type in self.attention_types:
            encoder_layer = FourierTransformerEncoderLayer(d_model=self.n_hidden,
                                                           n_head=self.n_head,
                                                           attention_type=self.attention_type,
                                                           dim_feedforward=self.dim_feedforward,
                                                           layer_norm=self.layer_norm,
                                                           attn_norm=self.attn_norm,
                                                           batch_norm=self.batch_norm,
                                                           pos_dim=self.pos_dim,
                                                           xavier_init=self.xavier_init,
                                                           diagonal_weight=self.diagonal_weight,
                                                           symmetric_init=self.symmetric_init,
                                                           attn_weight=self.return_attn_weight,
                                                           dropout=self.encoder_dropout,
                                                           debug=self.debug)
        elif self.attention_type == 'official':
            encoder_layer = TransformerEncoderLayer(d_model=self.n_hidden,
                                                    nhead=self.n_head,
                                                    dim_feedforward=self.dim_feedforward,
                                                    layer_norm=self.layer_norm,
                                                    attn_weight=self.return_attn_weight,
                                                    dropout=self.encoder_dropout,
                                                    )
        else:
            raise NotImplementedError("encoder type not implemented.")
        self.encoder_layers = nn.ModuleList(
            [copy.deepcopy(encoder_layer) for _ in range(self.num_ft_layers)])

    def _get_regressor(self):
        if self.decoder_type == 'pointwise':
            self.regressor = PointwiseRegressor(in_dim=self.n_hidden,
                                                n_hidden=self.n_hidden,
                                                out_dim=self.n_targets,
                                                num_layers=self.num_regressor_layers,
                                                spacial_fc=self.spacial_fc,
                                                spacial_dim=self.spacial_dim,
                                                activation=self.regressor_activation,
                                                dropout=self.decoder_dropout,
                                                debug=self.debug)
        elif self.decoder_type == 'ifft2':
            self.regressor = SpectralRegressor(in_dim=self.n_hidden,
                                               n_hidden=self.n_hidden,
                                               freq_dim=self.freq_dim,
                                               out_dim=self.n_targets,
                                               num_spectral_layers=self.num_regressor_layers,
                                               modes=self.fourier_modes,
                                               spacial_dim=self.spacial_dim,
                                               spacial_fc=self.spacial_fc,
                                               activation=self.regressor_activation,
                                               last_activation=self.last_activation,
                                               dropout=self.decoder_dropout,
                                               debug=self.debug
                                               )
        else:
            raise NotImplementedError("Decoder type not implemented")


if __name__ == '__main__':

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    config = defaultdict(lambda: None,
                         node_feats=1,
                         edge_feats=5,
                         pos_dim=1,
                         n_targets=1,
                         n_hidden=96,
                         num_feat_layers=2,
                         num_ft_layers=2,
                         n_head=2,
                         pred_len=0,
                         n_freq_targets=0,
                         dim_feedforward=96*2,
                         feat_extract_type='gcn',
                         graph_activation=True,
                         raw_laplacian=True,
                         attention_type='fourier',  # no softmax
                         xavier_init=1e-4,
                         diagonal_weight=1e-2,
                         symmetric_init=False,
                         layer_norm=True,
                         attn_norm=False,
                         batch_norm=False,
                         spacial_residual=False,
                         return_attn_weight=True,
                         seq_len=None,
                         bulk_regression=False,
                         decoder_type='ifft',
                         freq_dim=64,
                         num_regressor_layers=2,
                         fourier_modes=16,
                         spacial_dim=1,
                         spacial_fc=True,
                         dropout=0.1,
                         debug=False,
                         )

    ft = FourierTransformer(**config)
    ft.to(device)
    n_batch, seq_len = 8, 512
    summary(ft, input_size=[(n_batch, seq_len, 1),
                            (n_batch, seq_len, seq_len, 5),
                            (n_batch, seq_len, 1),
                            (n_batch, seq_len, 1)], device=device)

    layer = TransformerEncoderLayer(d_model=128, nhead=4)
    print(layer.__class__)