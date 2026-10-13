# Copyright 2026 Google LLC
# SPDX-License-Identifier: Apache-2.0
"""Offline, lossless Hugging Face BF16 -> official Gemma JAX parameter mapping.

No model code, pickle, network, credentials, or checkpoint mutation is used.
RedHat compressed-tensors W8A8 is a different contract and is rejected.
"""
from pathlib import Path
import hashlib
import json
import mmap
import struct
import numpy as np

MODEL_SHA256 = '3d4ef8d71c14db7e448a09ebe891cfb6bf32c57a9b44499ae0d1c098e48516b6'
MODEL_REVISION = 'dcc83ea841ab6100d6b47a070329e1ba4cf78752'
EXPECTED_CONFIG = dict(model_type='gemma3_text', hidden_size=1152,
    intermediate_size=6912, num_hidden_layers=26, num_attention_heads=4,
    num_key_value_heads=1, head_dim=256, vocab_size=262144,
    sliding_window=512, sliding_window_pattern=6, rope_theta=1000000,
    rope_local_base_freq=10000, query_pre_attn_scalar=256,
    hidden_activation='gelu_pytorch_tanh', rms_norm_eps=1e-6,
    max_position_embeddings=32768)

def sha256(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(8<<20),b''):h.update(b)
    return h.hexdigest()

def validate_config(config):
    for key,value in EXPECTED_CONFIG.items():
        if config.get(key)!=value:
            raise ValueError(f'Unsupported {key}: {config.get(key)!r}; expected {value!r}')
    if config.get('quantization_config') or config.get('rope_scaling'):
        raise ValueError('Quantization/RoPE scaling requires a separate validated adapter')
    if config.get('attention_bias',False) or config.get('attn_logit_softcapping') is not None or config.get('final_logit_softcapping') is not None:
        raise ValueError('Unsupported bias/softcap configuration')
    return config

class Checkpoint:
    def __init__(self, directory, expected_sha256=MODEL_SHA256):
        self.directory=Path(directory)
        self.config=validate_config(json.loads((self.directory/'config.json').read_text()))
        self.path=self.directory/'model.safetensors'
        self.sha256=sha256(self.path)
        if self.sha256!=expected_sha256:
            raise ValueError('Checkpoint SHA256 differs from the explicitly selected revision')
        self.file=self.path.open('rb');self.mm=mmap.mmap(self.file.fileno(),0,access=mmap.ACCESS_READ)
        size=struct.unpack_from('<Q',self.mm)[0]
        self.header=json.loads(self.mm[8:8+size]);self.base=8+size;self.used=set()
        self.tensors={k:v for k,v in self.header.items() if k!='__metadata__'}
        if len(self.tensors)!=340 or any(v['dtype']!='BF16' for v in self.tensors.values()):
            raise ValueError('Expected all340 original Google BF16 tensors; W8A8 unsupported')
    def bits(self,name,shape):
        t=self.tensors[name]
        if t['shape']!=list(shape):raise ValueError(f'Shape mismatch: {name}')
        lo,hi=t['data_offsets']
        if hi-lo!=int(np.prod(shape))*2 or not (0<=lo<=hi<=len(self.mm)-self.base):
            raise ValueError(f'Invalid tensor byte range: {name}')
        self.used.add(name)
        return np.frombuffer(self.mm,dtype='<u2',count=int(np.prod(shape)),offset=self.base+lo).reshape(shape).copy()
    def close(self):self.mm.close();self.file.close()

def layer_bits(cp,index):
    """Exact bit rearrangements; no casting or weight quantization."""
    p=f'model.layers.{index}.'
    get=lambda suffix,shape:cp.bits(p+suffix,shape)
    q=get('self_attn.q_proj.weight',(1024,1152)).reshape(4,256,1152).transpose(0,2,1)
    kv=np.stack([get('self_attn.'+x+'_proj.weight',(256,1152)).reshape(1,256,1152).transpose(0,2,1) for x in ['k','v']])
    o=get('self_attn.o_proj.weight',(1152,1024)).T.reshape(4,256,1152)
    layer={'attn':{'q_einsum':{'w':q},'kv_einsum':{'w':kv},'attn_vec_einsum':{'w':o},
            '_query_norm':{'scale':get('self_attn.q_norm.weight',(256,))},
            '_key_norm':{'scale':get('self_attn.k_norm.weight',(256,))}},
        'mlp':{'gating_einsum':np.stack([get('mlp.'+x+'_proj.weight',(6912,1152)) for x in ['gate','up']]),
               'linear':get('mlp.down_proj.weight',(1152,6912)).T}}
    for src,dst in [('input_layernorm','pre_attention_norm'),('post_attention_layernorm','post_attention_norm'),('pre_feedforward_layernorm','pre_ffw_norm'),('post_feedforward_layernorm','post_ffw_norm')]:
        layer[dst]={'scale':get(src+'.weight',(1152,))}
    return layer

def load_params(directory, device=None):
    import jax
    import ml_dtypes
    cp=Checkpoint(directory)
    convert=lambda x:jax.device_put(np.ascontiguousarray(x).view(ml_dtypes.bfloat16),device)
    try:
        params={'embedder':{'input_embedding':convert(cp.bits('model.embed_tokens.weight',(262144,1152)))},
                'final_norm':{'scale':convert(cp.bits('model.norm.weight',(1152,)))}}
        for i in range(26):
            params[f'layer_{i}']=jax.tree.map(convert,layer_bits(cp,i))
        if cp.used!=set(cp.tensors):raise ValueError('Unmapped checkpoint tensors')
        return params
    finally:cp.close()

def local_tokenizer(directory):
    from tokenizers import Tokenizer
    # Use the pinned local HF tokenizer, never a remotely downloaded default.
    return Tokenizer.from_file(str(Path(directory)/'tokenizer.json'))
