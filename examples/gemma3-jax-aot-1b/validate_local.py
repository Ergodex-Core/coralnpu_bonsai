# Copyright 2026 Google LLC
# SPDX-License-Identifier: Apache-2.0
"""Actual-weight validation; needs local checkpoint and retained MLX fixtures."""
from pathlib import Path
import argparse,json,hashlib
import jax,jax.numpy as jnp
import numpy as np
from gemma3_util import GemmaKV,init_cache_layers,make_mask,load_params
from checkpoint import Checkpoint,layer_bits,local_tokenizer

def validate(directory,fixtures,output):
    manifest=json.loads((fixtures/'manifest.json').read_text())
    records={r['path']:r for r in manifest['files']}
    def read(name):
        raw=(fixtures/name).read_bytes();record=records[name]
        if len(raw)!=record['bytes'] or hashlib.sha256(raw).hexdigest()!=record['sha256']:
            raise ValueError('Reference size/hash failure: '+name)
        a=np.frombuffer(raw,dtype='<f4').copy()
        if not np.isfinite(a).all():raise ValueError('Nonfinite reference: '+name)
        return a
    model=GemmaKV();params=load_params(directory)
    abstract=jax.eval_shape(lambda key:model.init(key,jnp.array([[2]],jnp.int32),positions=jnp.zeros((1,1),jnp.int32)),jax.random.key(0))['params']
    actual_paths=jax.tree_util.tree_flatten_with_path(params)[0];expected_paths=jax.tree_util.tree_flatten_with_path(abstract)[0]
    assert len(actual_paths)==len(expected_paths)
    for (pa,a),(pe,e) in zip(actual_paths,expected_paths):
        assert pa==pe and a.shape==e.shape and a.dtype==e.dtype,(pa,a.shape,e.shape)
    types=[x.name for x in model.config.attention_types]
    assert [i for i,t in enumerate(types) if t=='GLOBAL']==[5,11,17,23]
    assert model.config.sliding_window_size==512 and model.config.local_base_frequency==10000 and model.config.global_base_frequency==1000000
    tokenizer=local_tokenizer(directory)
    prompt='<bos><start_of_turn>user\nHello<end_of_turn>\n<start_of_turn>model\n'
    ids=tokenizer.encode(prompt,add_special_tokens=False).ids
    assert ids==[2,105,2364,107,9259,106,107,105,4368,107],ids
    # Check every original weight bit against the inverse layout, not just shapes.
    cp=Checkpoint(directory)
    for i in range(26):
        p=f'model.layers.{i}.';mapped=layer_bits(cp,i)
        q=mapped['attn']['q_einsum']['w'].transpose(0,2,1).reshape(1024,1152)
        assert np.array_equal(q,cp.bits(p+'self_attn.q_proj.weight',(1024,1152)))
        for idx,name in enumerate(['k','v']):
            b=mapped['attn']['kv_einsum']['w'][idx].transpose(0,2,1).reshape(256,1152)
            assert np.array_equal(b,cp.bits(p+'self_attn.'+name+'_proj.weight',(256,1152)))
        assert np.array_equal(mapped['attn']['attn_vec_einsum']['w'].reshape(1024,1152).T,cp.bits(p+'self_attn.o_proj.weight',(1152,1024)))
        for idx,name in enumerate(['gate','up']):assert np.array_equal(mapped['mlp']['gating_einsum'][idx],cp.bits(p+'mlp.'+name+'_proj.weight',(6912,1152)))
        assert np.array_equal(mapped['mlp']['linear'].T,cp.bits(p+'mlp.down_proj.weight',(1152,6912)))
    cp.close();rows=[]
    for case,m in [('bos_m1',1),('hello_m10',10)]:
      for layer in [0,5]:
        prefix=f'{case}__model.layers.{layer}'
        x=read(prefix+'.self_attn.k_proj__input.f32').reshape(1,m,1152)
        reference=read(prefix+'.self_attn.k_proj__output.f32').reshape(1,m,1,256)
        fn=jax.jit(lambda a:model.apply({'params':params},a,layer=layer,method=model.apply_k_projection))
        got=np.asarray(fn(jnp.asarray(x,dtype=jnp.bfloat16)),dtype=np.float32)
        assert np.isfinite(got).all()
        # BF16 rounding plus FP32 dot reduction bound; no tolerance tuning from target outputs.
        weight=np.asarray(params[f'layer_{layer}']['attn']['kv_einsum']['w'][0,0],dtype=np.float64)
        exact=x.astype(np.float64)@weight
        error=np.abs(got.reshape(m,256)-exact.reshape(m,256))
        u=2**-24;gamma=2304*u/(1-2304*u)
        bound=(np.abs(x.astype(np.float64))@np.abs(weight))*gamma + np.abs(exact)*2**-8 + 2**-126
        assert np.all(error<=bound.reshape(m,256)),'BF16 output exceeds reduction+rounding bound'
        rows.append({'kind':'k_projection','layer':layer,'M':m,'outputs':m*256,'analytic_bound':'PASS','MLX_bitwise_equal':bool(np.array_equal(got,reference)),'MLX_max_absolute_error':float(abs(got-reference).max())})
        # Full official block vs independently retained MLX block; mismatch is reported, not hidden.
        inp=read(prefix+'__block_input.f32').reshape(1,m,1152)
        ref=read(prefix+'__block_output.f32').reshape(1,m,1152)
        positions=jnp.arange(m,dtype=jnp.int32)[None,:]
        causal=jnp.tril(jnp.ones((1,m,m),dtype=jnp.bool_))
        _,value=jax.jit(lambda a:model.apply({'params':params},a,positions,None,causal,layer=layer,method=model.apply_block))(jnp.asarray(inp,dtype=jnp.bfloat16))
        value=np.asarray(value,dtype=np.float32);assert np.isfinite(value).all()
        rows.append({'kind':'block','layer':layer,'M':m,'outputs':m*1152,'MLX_bitwise_equal':bool(np.array_equal(value,ref)),'MLX_max_absolute_error':float(abs(value-ref).max()),'MLX_relative_L2':float(np.linalg.norm(value-ref)/max(np.linalg.norm(ref),1e-30)),'status':'EXACT' if np.array_equal(value,ref) else 'NUMERICAL_DIFFERENCE_REQUIRES_REVIEW'})
    result={'reference_contract':manifest.get('reference',manifest.get('backend')),'mapping':'PASS all182 projection matrices inverse-bit-exact and full parameter tree shapes/dtypes','architecture':'PASS26 layers,22local/4global','tokenizer':'PASS exact10-token prompt','rows':rows,'target_execution':'NOT_RUN','end_to_end_model_equivalence':'NOT_ESTABLISHED'}
    output.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n');print(json.dumps(result,indent=2,allow_nan=False))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--checkpoint',type=Path,required=True);p.add_argument('--fixtures',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();validate(a.checkpoint,a.fixtures,a.output)
