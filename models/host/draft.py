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
"""Draft token proposers for speculative decoding on the host CPU.

- `NgramDraft`: prompt-lookup decoding, no model: continues the longest
  n-gram match of the recent context found earlier in the context.
- `TransformersDraft`: a small Gemma 3 model (same tokenizer) through
  transformers on the CPU, greedy.
Both expose `__call__(context_ids, k) -> list[int]`.
"""


class NgramDraft:
  def __init__(self, max_n=4, min_n=2):
    self.max_n, self.min_n = max_n, min_n

  def __call__(self, context, k):
    ctx = list(context)
    for n in range(self.max_n, self.min_n - 1, -1):
      if len(ctx) <= n:
        continue
      tail = ctx[-n:]
      for i in range(len(ctx) - n - 1, -1, -1):
        if ctx[i:i + n] == tail:
          return ctx[i + n:i + n + k]
    return []


class TransformersDraft:
  def __init__(self, model_id, device="cpu", dtype="bfloat16", threads=None):
    import torch  # pylint: disable=g-import-not-at-top
    from transformers import AutoModelForCausalLM  # pylint: disable=g-import-not-at-top
    if threads:
      torch.set_num_threads(threads)
    self.torch = torch
    self.model = AutoModelForCausalLM.from_pretrained(
        model_id, torch_dtype=getattr(torch, dtype)).to(device).eval()
    self.device = device

  def __call__(self, context, k):
    torch = self.torch
    ids = torch.tensor([list(context)], dtype=torch.long, device=self.device)
    with torch.no_grad():
      out = self.model.generate(ids, max_new_tokens=k, do_sample=False,
                                pad_token_id=0)
    return [int(t) for t in out[0, ids.shape[1]:].tolist()]


def make_draft(spec: str):
  """'ngram' or 'transformers:<model id>'."""
  if spec == "ngram":
    return NgramDraft()
  if spec.startswith("transformers:"):
    return TransformersDraft(spec.split(":", 1)[1])
  raise ValueError(f"unknown draft {spec!r}")
