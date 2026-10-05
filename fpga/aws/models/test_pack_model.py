#!/usr/bin/env python3
"""Small, deterministic tests; no network, checkpoint download, or accelerator."""
import dataclasses
import json
from pathlib import Path
import struct
import tempfile
import unittest

import pack_model as p
from reference_ops import bf16, pq2, dot, f32, rmsnorm, softmax


def make_fixture(root,encoding=p.BF16):
    # Unequal dim vs heads*head_dim exercises Qwen3-0.6B's geometry.
    c=p.Config(128,128,2,2,1,128,5,4)
    raw=root/"fixture.raw";tensors=[]
    with raw.open("wb") as f:
        for (role,layer),(rows,cols) in p.expected_shapes(c).items():
            enc=p.F32 if role in (2,10,15,16,17) else encoding
            offset=f.tell()
            if enc==p.PQ2:
                data=(struct.pack("<e",0.125)+bytes([0xe4]*32))*(rows*cols//128)
            elif enc==p.BF16:data=struct.pack("<H",0x3e00)*(rows*cols)
            else:data=struct.pack("<f",1.0)*(rows*cols)
            f.write(data)
            tensors.append(p.Tensor(f"fixture.{layer}.{role}",role,layer,enc,rows,cols,raw,offset,len(data)))
    return c,tensors


class PackingTests(unittest.TestCase):
    def test_abi_geometry_and_byte_preservation(self):
        for encoding in (p.BF16,p.PQ2):
            with self.subTest(encoding=encoding),tempfile.TemporaryDirectory() as d:
                root=Path(d); c,ts=make_fixture(root,encoding)
                manifest=p.write_package(root/"package",c,ts,[],"deterministic-fixture","fixture")
                data=(root/"package/model.bin").read_bytes()
                h=p.HEADER.unpack_from(data)
                self.assertEqual(p.HEADER.size,128);self.assertEqual(p.TENSOR.size,32)
                self.assertEqual(h[:6],(b"CORALM01",1,128,len(data),len(ts),128))
                self.assertEqual(h[6:14],(128,128,2,2,1,128,5,4))
                self.assertEqual(h[-9:],(0,)*9)
                for record in manifest["tensors"]:
                    original=next(t for t in ts if t.name==record["name"])
                    source=original.source.read_bytes()[original.source_offset:original.source_offset+original.bytes]
                    output=data[record["offset"]:record["offset"]+record["bytes"]]
                    self.assertEqual(source,output)
                    self.assertEqual(record["offset"]%64,0)
                self.assertEqual(manifest["memory"]["workspace_bytes"],c.workspace_bytes())
                self.assertEqual(manifest["validation"]["physical"],"NOT_RUN")
                with self.assertRaisesRegex(ValueError,"already exists"):
                    p.write_package(root/"package",c,ts,[],"fixture","fixture")

    def test_duplicate_missing_shape_and_alias_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            c,ts=make_fixture(Path(d))
            for invalid in (ts[:-1],ts+[ts[0]], [dataclasses.replace(ts[0],cols=127)]+ts[1:],
                [ts[0],dataclasses.replace(ts[1],source_offset=0)]+ts[2:]):
                with self.assertRaises(ValueError):p.validate_tensors(c,invalid)

    def test_fail_closed_original_checkpoint_identity(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"not-official.gguf";path.write_bytes(b"GGUF")
            with self.assertRaisesRegex(ValueError,"SHA256 mismatch"):
                p.bonsai_sources(path,1,(32,1,1.0))

    def test_ddr_bounds_context_and_alignment(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);c,ts=make_fixture(root)
            for address in (p.DDRB, p.DDRE-64, p.DEFAULT_ADDRESS+1):
                with self.assertRaises(ValueError):p.write_package(root/"unused",c,ts,[],"fixture","fixture",address)
            self.assertFalse((root/"unused").exists())
            for n in (0,2049):
                with self.assertRaises(ValueError):p.validate_tensors(dataclasses.replace(c,max_seq=n),ts)

    def test_highest_legal_ddr_byte_and_integer_overflow(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);c,ts=make_fixture(root)
            c.max_seq=16
            cursor=p.align(128+32*len(ts))
            for t in sorted(ts,key=lambda t:(t.layer,t.role)):
                cursor=p.align(cursor+t.bytes)
            # All scratch allocations and this fixture workspace end at64B.
            used=p.align(p.align(cursor+4*c.max_seq)+4*c.vocab)+c.workspace_bytes()
            address=p.DDRE-used
            self.assertEqual(address%64,0)
            result=p.write_package(root/"edge",c,ts,[],"fixture","fixture",address)
            self.assertEqual(result["memory"]["planned_end_address"],p.DDRE)
            for address in (address+64,2**32,2**64):
                with self.assertRaises(ValueError):
                    p.write_package(root/"bad",c,ts,[],"fixture","fixture",address)

    def test_gguf_header_rejects_truncated_and_unknown_types(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"bad.gguf"
            for data in (b"GGUF", b"NOPE"+struct.pack("<I",3),
                         b"GGUF"+struct.pack("<IQQ",3,5000,0)):
                path.write_bytes(data)
                with self.assertRaises(ValueError):p.gguf_header(path)

    def test_exact_model_memory_budgets(self):
        q=p.Config(1024,3072,28,16,8,128,151936,2048)
        b=p.Config(2048,6144,28,16,8,128,151669,2048,7)
        self.assertEqual(q.kv_bytes_per_token(),229376)
        self.assertEqual(b.kv_bytes_per_token(),229376)
        self.assertEqual(q.workspace_bytes(),469831936)
        self.assertEqual(b.workspace_bytes(),469868800)
        self.assertEqual(sum(r*c*2 for r,c in p.expected_shapes(q).values()),1192099840)


class IndependentOperatorTests(unittest.TestCase):
    def test_bf16_exact_widening_sign_subnormal_and_infinity(self):
        out=bf16(struct.pack("<6H",0x0000,0x8000,0x0001,0x3f80,0xbf80,0x7f80))
        self.assertEqual([struct.unpack("<I",struct.pack("<f",v))[0] for v in out],
                         [0,0x80000000,0x10000,0x3f800000,0xbf800000,0x7f800000])

    def test_all_native_codes_and_negative_scale(self):
        values=pq2(struct.pack("<e",0.5)+bytes([0xe4]*32))
        self.assertEqual(values,[-0.5,0.0,0.5,1.0]*32)
        negative=pq2(struct.pack("<e",-0.25)+bytes([0xe4]*32))
        self.assertEqual(negative,[0.25,-0.0,-0.25,-0.5]*32)
        self.assertEqual(dot(values,[1.,2.,3.,4.]*32),160.0)
        with self.assertRaises(ValueError):pq2(b"x")

    def test_fp32_order_no_hidden_fma(self):
        self.assertEqual(dot([1.,1.,1.],[16777216.,1.,-16777216.]),0.)
        x=rmsnorm([1.,-1.],[1.,1.],1e-6)
        self.assertAlmostEqual(x[0],0.9999995,places=6)
        self.assertEqual(x[0],-x[1])
        self.assertEqual(softmax([0.,0.]),[0.5,0.5])

if __name__=="__main__":unittest.main()
