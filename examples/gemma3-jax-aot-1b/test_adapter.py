# Copyright 2026 Google LLC
# SPDX-License-Identifier: Apache-2.0
"""Small semantic tests without downloading/allocating checkpoint weights."""
import unittest
import numpy as np
import jax.numpy as jnp
from checkpoint import EXPECTED_CONFIG,validate_config
from gemma3_util import GemmaKV,fast_rope,init_cache_layers,make_mask,split_params
from gemma.gm.nn._modules import create_sliding_mask

class AdapterTests(unittest.TestCase):
    def test_wrong_model_and_quantizer_rejected(self):
        validate_config(dict(EXPECTED_CONFIG))
        for key,value in [('num_hidden_layers',18),('num_attention_heads',8),('sliding_window_pattern',5),('quantization_config',{'format':'int-quantized'})]:
            with self.assertRaises(ValueError):validate_config({**EXPECTED_CONFIG,key:value})
    def test_exact_architecture_and_cache(self):
        c=GemmaKV().config
        self.assertEqual((c.embed_dim,c.hidden_dim,c.num_heads,c.num_kv_heads,c.head_dim),(1152,6912,4,1,256))
        self.assertEqual([i for i,t in enumerate(c.attention_types) if t.name=='GLOBAL'],[5,11,17,23])
        cache=init_cache_layers(8);self.assertEqual(len(cache),26)
        for layer in cache:
            self.assertEqual(layer['k'].shape,(1,8,1,256));self.assertEqual(layer['v'].shape,(1,8,1,256))
            self.assertEqual(str(layer['k'].dtype),'bfloat16')
        for n in [0,32769]:
            with self.assertRaises(ValueError):init_cache_layers(n)
    def test_local_window_boundary_and_causal_mask(self):
        local=np.asarray(create_sliding_mask(jnp.array([[512]]),cache_positions=jnp.arange(513)[None,:],sliding_window_size=512))
        self.assertEqual(local.shape,(1,1,513));self.assertEqual(int(local.sum()),512)
        self.assertFalse(local[0,0,0]);self.assertTrue(local[0,0,1]);self.assertTrue(local[0,0,512])
        self.assertEqual(int(make_mask(513,513).sum()),513)
        for length,size in [(0,128),(129,128),(-1,128)]:
            with self.assertRaises(ValueError):make_mask(length,size)
    def test_rope_local_and_global_nonzero_positions(self):
        x=np.linspace(-1,1,4*256,dtype=np.float32).reshape(1,1,4,256)
        for base in [10000,1000000]:
            angles=511/(base**(np.arange(128,dtype=np.float64)*2/256))
            z=x[...,:128].astype(float)+1j*x[...,128:].astype(float)
            rot=z*np.exp(1j*angles);reference=np.concatenate([rot.real,rot.imag],axis=-1)
            result=np.asarray(fast_rope(jnp.asarray(x),jnp.array([[511]]),base_frequency=base))
            np.testing.assert_allclose(result,reference,rtol=1e-4,atol=3e-5)
    def test_split_parameter_identity_and_lexical_order(self):
        params={f'layer_{i}':object() for i in range(26)};params.update(embedder=object(),final_norm=object())
        a,b,c=split_params(params)
        self.assertEqual(set(a),{'embedder'}|{f'layer_{i}' for i in range(13)})
        self.assertEqual(set(b),{'final_norm'}|{f'layer_{i}' for i in range(13,26)})
        self.assertIs(c['embedder'],a['embedder']);self.assertIs(a['layer_9'],params['layer_9'])
if __name__=='__main__':unittest.main()
