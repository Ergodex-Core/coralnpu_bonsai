# Copyright 2026 Google LLC
# SPDX-License-Identifier: Apache-2.0
"""Export actual local Google Gemma3-1B to StableHLO, not a target executable."""
from pathlib import Path
import argparse,hashlib,json,time
import jax
import jax.numpy as jnp
from gemma3_util import GemmaKV,init_cache_layers,split_params,load_params
from checkpoint import MODEL_SHA256,MODEL_REVISION

def describe(tree):
    leaves,_=jax.tree_util.tree_flatten_with_path(tree)
    return [{'path':jax.tree_util.keystr(path),'shape':list(x.shape),'dtype':str(x.dtype)} for path,x in leaves]

def export(directory,output,cache_length=128,mode='split'):
    if not 1 <= cache_length <= 32768:raise ValueError('cache_length must be1..32768')
    jax.config.update('jax_use_shardy_partitioner',False)
    out=Path(output);out.mkdir(parents=True,exist_ok=True)
    (out/'export-manifest.json').unlink(missing_ok=True)
    params=load_params(directory);model=GemmaKV();cache=init_cache_layers(cache_length)
    token=jnp.array([[2]],dtype=jnp.int32);pos=jnp.zeros((1,1),jnp.int32)
    mask=jnp.zeros((1,1,cache_length),jnp.int8).at[:,:,0].set(1)
    x=jnp.zeros((1,1,1152),jnp.bfloat16)
    exports=[]
    def save(name,method,args):
        t=time.monotonic()
        fn=jax.jit(lambda p,*a:model.apply({'params':p},*a,method=method))
        lowered=fn.lower(*args)
        text=str(lowered.compiler_ir(dialect='stablehlo'));path=out/(name+'.mlir');path.write_text(text)
        if path.read_bytes()!=text.encode():raise IOError('Export readback differs from emitted StableHLO')
        exports.append({'name':name,'file':path.name,'bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'arguments':describe(args),'outputs':describe(lowered.out_info),'export_seconds':time.monotonic()-t,'status':'STABLEHLO_ONLY_NOT_CORAL_EXECUTABLE'})
        print(f'Exported {name}: {path.stat().st_size}bytes',flush=True)
    if mode in ('projection','all'):
        part={'layer_0':{'attn':{'kv_einsum':params['layer_0']['attn']['kv_einsum']}}}
        save('gemma3_1b_layer0_k_projection',lambda module,x:module.apply_k_projection(x,layer=0),(part,x))
    if mode in ('full','all'):
        save('gemma3_1b_logits',model.apply_logits,(params,token,pos,cache,mask))
    if mode in ('split','all'):
        p1,p2,p3=split_params(params)
        save('gemma3_1b_part1',model.apply_part1,(p1,token,pos,cache[:13],mask))
        save('gemma3_1b_part2',model.apply_part2,(p2,x,pos,cache[13:],mask))
        save('gemma3_1b_head',model.apply_part3,(p3,x))
    manifest={'model':'google/gemma-3-1b-it','revision':MODEL_REVISION,'checkpoint_sha256':MODEL_SHA256,
              'precision':'Original BF16; HF FP32 RMS math; original official Gemma attention/MLP and real RoPE patch',
              'cache_length':cache_length,'layers':26,'local_layers':22,'global_layers':[5,11,17,23],
              'cache_policy':'left-aligned full-sized caches; local512 mask; host rejects overflow',
              'cache_payload_bytes':26*cache_length*1*256*2*2,'jax':jax.__version__,'exports':exports,
              'target_compiler_status':'NOT_RUN','target_execution_status':'NOT_RUN','W8A8':'UNSUPPORTED, never relabeled'}
    (out/'export-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    return manifest
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--checkpoint',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--cache-length',type=int,default=128);p.add_argument('--mode',choices=['full','split','projection','all'],default='split')
    a=p.parse_args();export(a.checkpoint,a.output,a.cache_length,a.mode)
