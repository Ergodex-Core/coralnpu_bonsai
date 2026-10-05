#!/usr/bin/env python3
"""Tokenize raw completion prompts with pinned artifacts; no model computation."""
import argparse
import json
from pathlib import Path
from tokenizers import Tokenizer
from pack_model import sha256, require, gguf_header, BONSAI_SHA

QWEN_TOKENIZER_SHA = 'aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4'


def tokenizer(path, bonsai=None):
    require(sha256(path) == QWEN_TOKENIZER_SHA, 'tokenizer SHA256 mismatch')
    t = Tokenizer.from_file(str(path))
    if bonsai:
        require(
            sha256(bonsai) == BONSAI_SHA, 'Bonsai checkpoint SHA256 mismatch'
        )
        meta, _, _ = gguf_header(bonsai, keep_tokenizer=True)
        require(
            meta.get('tokenizer.ggml.model') == 'gpt2'
            and meta.get('tokenizer.ggml.pre') == 'qwen2',
            'unsupported GGUF tokenizer'
        )
        tokens = meta['tokenizer.ggml.tokens']
        require(
            len(tokens) == t.get_vocab_size()
            and all(t.id_to_token(i) == v for i, v in enumerate(tokens)),
            'GGUF token vocabulary differs from pinned tokenizer'
        )
        raw = json.loads(path.read_text())
        merges = [
            ' '.join(v) if isinstance(v, list) else v
            for v in raw['model']['merges']
        ]
        require(
            merges == meta['tokenizer.ggml.merges'], 'GGUF merge order differs'
        )
    return t


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('tokenizer_json', type=Path)
    p.add_argument('--bonsai-gguf', type=Path)
    p.add_argument('--text', required=True)
    p.add_argument(
        '--decode-ids', help='comma-separated generated IDs to decode'
    )
    a = p.parse_args()
    t = tokenizer(a.tokenizer_json, a.bonsai_gguf)
    ids = t.encode(a.text, add_special_tokens=False).ids
    result = {
        'prompt_mode': 'raw_completion',
        'prompt': a.text,
        'input_ids': ids,
        'tokenizer_sha256': QWEN_TOKENIZER_SHA,
        'roundtrip': t.decode(ids, skip_special_tokens=False)
    }
    if a.decode_ids:
        result['generated_text'] = t.decode([
            int(v) for v in a.decode_ids.split(',')
        ],
                                            skip_special_tokens=False)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__': main()
