# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Official Gemma3-1B architecture with offline HF weights and bounded cache.

Adapted from google-coral/coralnpu-compiler examples/gemma3-jax-aot at
76ed531d2389784c89961177251daf3653dbc513. No replacement attention/MLP model.
"""
from __future__ import annotations
import typing
from gemma import gm
import gemma.gm.math._positional_embeddings as _pe
import gemma.gm.nn._modules as _mod
import gemma.gm.nn._layers as _layers
from flax import linen as nn
import jax
import jax.numpy as jnp
import numpy as np
from checkpoint import load_params

def fast_rope(
    inputs: jax.Array,
    positions: jax.Array,
    *,
    base_frequency: float = 1000000.0,
    scale_factor: float = 1.0,
    rope_proportion: float = 1.0,
) -> jax.Array:
  """Applies RoPE using real float32 arithmetic instead of complex numbers.

  Args:
    inputs: Array of shape [B, L, N, H].
    positions: Array of shape [B, L].
    base_frequency: Base frequency used to compute rotations.
    scale_factor: The scale factor used for positional interpolation.
    rope_proportion: The proportion of the head dimension to apply RoPE to (1.0
      for Gemma 3-270M). Unrotated dimensions are padded with inf timescale so
      that sin=0, cos=1.
  """
  head_dim = inputs.shape[-1]
  rope_angles = int(rope_proportion * head_dim // 2)
  nope_angles = head_dim // 2 - rope_angles

  fraction = 2.0 * jnp.arange(0, rope_angles, dtype=jnp.float32) / head_dim
  timescale = base_frequency**fraction
  if nope_angles > 0:
    timescale = jnp.pad(
        timescale,
        (0, nope_angles),
        mode="constant",
        constant_values=(0, jnp.inf),
    )

  pos = positions.astype(jnp.float32)
  sinusoid_inp = (pos[:, :, None] / timescale[None, None, :]) / scale_factor

  cos = jnp.expand_dims(jnp.cos(sinusoid_inp), -2)
  sin = jnp.expand_dims(jnp.sin(sinusoid_inp), -2)

  inputs_f32 = inputs.astype(jnp.float32)
  first_half, second_half = jnp.split(inputs_f32, 2, axis=-1)
  first_part = first_half * cos - second_half * sin
  second_part = second_half * cos + first_half * sin
  out = jnp.concatenate([first_part, second_part], axis=-1)
  return out.astype(inputs.dtype)


def patch_fast_rope():
  """Monkey-patches Gemma RoPE and attention mask for CoralNPU."""
  _pe.apply_rope = fast_rope
  _mod.apply_rope = fast_rope
  _mod.K_MASK = -1e4


# Apply patch upon module import
patch_fast_rope()


class HFRMSNorm(nn.Module):
  """Preserve HF/MLX FP32 variance and scale math, then round to input dtype."""
  @nn.compact
  def __call__(self, x):
    scale = self.param('scale', nn.initializers.zeros_init(), (x.shape[-1],))
    y = x.astype(jnp.float32)
    y = y * jax.lax.rsqrt(jnp.mean(y*y, axis=-1, keepdims=True) + 1e-6)
    return (y * (1 + scale.astype(jnp.float32))).astype(x.dtype)

# Upstream also patches RoPE globally for real-arithmetic StableHLO export.
# This additional precision adaptation is explicit and covered by references.
_layers.RMSNorm = HFRMSNorm
NUM_LAYERS = 26
SPLIT = 13

class GemmaKV(gm.nn.Gemma3_1B):
  """26-layer official architecture, 13/13 split, complete logits available."""
  def apply_logits(self, tokens, positions, cache_list, attention_mask):
    x = self.embedder.encode(tokens)
    new_cache = []
    for i in range(NUM_LAYERS):
      c,x = self.blocks[i](x,positions,cache_list[i],attention_mask.astype(jnp.bool_))
      new_cache.append(c)
    return self.embedder.decode(self.final_norm(x).astype(jnp.bfloat16)),new_cache

  def apply_step(self, tokens, positions, cache_list, attention_mask):
    logits,cache = self.apply_logits(tokens,positions,cache_list,attention_mask)
    return jnp.argmax(logits,axis=-1).astype(jnp.int32),cache

  def apply_part1(self,tokens,positions,cache_list,attention_mask):
    x=self.embedder.encode(tokens);new_cache=[]
    for i in range(SPLIT):
      c,x=self.blocks[i](x,positions,cache_list[i],attention_mask.astype(jnp.bool_));new_cache.append(c)
    return x,new_cache

  def apply_part2(self,x,positions,cache_list,attention_mask):
    new_cache=[]
    for i in range(SPLIT,NUM_LAYERS):
      c,x=self.blocks[i](x,positions,cache_list[i-SPLIT],attention_mask.astype(jnp.bool_));new_cache.append(c)
    return self.final_norm(x),new_cache

  def apply_part3(self,x):
    return self.embedder.decode(x.astype(jnp.bfloat16))

  def apply_block(self,x,positions,cache,mask,*,layer):
    return self.blocks[layer](x,positions,cache,mask.astype(jnp.bool_))

  def apply_k_projection(self,x,*,layer):
    # Actual official Attention kv_einsum; output [B,T,1,256].
    return self.blocks[layer].attn.kv_einsum('BSD,CKDH->CBSKH',x)[0]


def init_cache_layers(cache_length=128):
  if not 1 <= cache_length <= 32768:raise ValueError('cache_length must be1..32768')
  # Allocate complete left-aligned caches for all26 layers. Local layers still
  # apply the official512-token sliding mask. No unvalidated ring-cache scheme.
  cache=GemmaKV().init_cache(batch_size=1,dtype=jnp.bfloat16,cache_length=cache_length)
  return [cache[f'layer_{i}'] for i in range(NUM_LAYERS)]


def init_split_cache_layers(cache_length=128):
  c=init_cache_layers(cache_length);return c[:SPLIT],c[SPLIT:]


def split_params(params):
  # Explicit subtrees match exported argument manifests; never rely on the
  # alphabetical position of layer_9 in a flattened full-model dictionary.
  a={'embedder':params['embedder'],**{f'layer_{i}':params[f'layer_{i}'] for i in range(SPLIT)}}
  b={'final_norm':params['final_norm'],**{f'layer_{i}':params[f'layer_{i}'] for i in range(SPLIT,NUM_LAYERS)}}
  return a,b,{'embedder':params['embedder']}


def make_mask(length,max_length=128):
  if not 1 <= length <= max_length <= 32768:raise ValueError('Prompt/cache overflow')
  return (np.arange(max_length)[None,None,:] < length).astype(np.int8)
