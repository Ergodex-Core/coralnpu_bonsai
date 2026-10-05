#!/usr/bin/env python3
"""Fresh synthetic references, never historical full-model or physical evidence.

Independent Python math/FP64 equations compare every exposed operator boundary,
layer and logits against the exact C decoder used by firmware. Files/fixtures
are generated locally, with no downloaded weights or tensor-framework runtime.
"""
import ctypes as C
import json
import math
from pathlib import Path
import random
import struct
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
F = C.POINTER(C.c_float)
TRACE = C.CFUNCTYPE(None, C.c_uint, C.c_uint, C.c_uint, F, C.c_uint, C.c_void_p)

class State(C.Structure):
    _fields_ = [(n, C.c_void_p) for n in ('h', 'image', 'directory')] + [
        ('capacity', C.c_uint32), ('position', C.c_uint32)] + [
        (n, F) for n in ('x','norm','q','k','v','att','residual','gate','up','scores','rope_inv','keys','values')] + [
        ('rope_magnitude', C.c_float), ('trace', TRACE), ('trace_context', C.c_void_p)]

class GenerationResult(C.Structure):
    _fields_=[(n,C.c_uint32) for n in ('completed_tokens','generated_count','last_argmax','stop_reason')]+[(n,C.c_uint64) for n in ('prefill_cycles','decode_cycles','total_cycles','first_token_cycles')]

class Tensor(C.Structure):
    _fields_ = [(n, C.c_uint32) for n in ('role','layer','encoding','rows','cols','offset','bytes','reserved')]

def fp32(x):
    return struct.unpack('<f', struct.pack('<f', x))[0]

def norm(x,w,eps):
    inv = 1/math.sqrt(sum(a*a for a in x)/len(x)+eps)
    return [a*inv*b for a,b in zip(x,w)]

def mat(m,x):
    return [sum(a*b for a,b in zip(row,x)) for row in m]

def fixture(encoding=2, yarn=False, tied=True, capacity=3):
    # BF16 case deliberately has q_dim != hidden state dim. PQ2 uses native
    # complete blocks and all four codes, including native code3=+2.
    d, ff, nh, nk, hd = (8,16,4,2,4) if encoding != 3 else (128,256,16,8,8)
    cfg=dict(dim=d,hidden_dim=ff,n_layers=2,n_heads=nh,n_kv_heads=nk,head_dim=hd,vocab=9,max_seq=capacity,
             flags=(1 if tied else 0)|2|(4 if yarn else 0),rope_theta=10000.,rms_eps=1e-6,
             rope_factor=4.,rope_original_context=8.,yarn_beta_fast=4.,yarn_beta_slow=1.,yarn_attention_factor=1.1386294)
    rng=random.Random(817+encoding)
    tensors=[]; decoded={}
    def add(role,layer,rows,cols,isnorm=False):
        enc=1 if isnorm else encoding
        data=bytearray(); weights=[]
        for r in range(rows):
            row=[]
            if enc==3:
                for b in range(cols//128):
                    scale=struct.unpack('<e',struct.pack('<e',.03125+(r+b)%3*.00390625))[0]
                    data+=struct.pack('<e',scale)
                    codes=[rng.randrange(4) for _ in range(128)]
                    data+=bytes(sum(codes[i+j]<<(2*j) for j in range(4)) for i in range(0,128,4))
                    row += [scale*(c-1) for c in codes]
            else:
                for _ in range(cols):
                    value=1+rng.uniform(-.2,.2) if isnorm else rng.uniform(-.3,.3)
                    if enc==1:
                        data+=struct.pack('<f',value); row.append(fp32(value))
                    else:
                        bits=struct.unpack('<I',struct.pack('<f',value))[0]>>16
                        data+=struct.pack('<H',bits); row.append(struct.unpack('<f',struct.pack('<I',bits<<16))[0])
            weights.append(row)
        tensors.append((role,layer,enc,rows,cols,bytes(data))); decoded[role,layer]=weights
    G=0xffffffff
    add(1,G,9,d); add(2,G,1,d,True)
    if not tied:add(3,G,9,d)
    for l in range(2):
        for role,rows,cols,isnorm in [(10,1,d,True),(11,nh*hd,d,False),(12,nk*hd,d,False),
            (13,nk*hd,d,False),(14,d,nh*hd,False),(15,1,hd,True),(16,1,hd,True),
            (17,1,d,True),(18,ff,d,False),(19,ff,d,False),(20,d,ff,False)]:
            add(role,l,rows,cols,isnorm)
    offset=(128+len(tensors)*32+63)//64*64
    image=bytearray(offset); directory=[]
    for role,layer,enc,rows,cols,data in tensors:
        directory.append((role,layer,enc,rows,cols,len(image),len(data),0))
        image+=data; image+=bytes((-len(image))%64)
    struct.pack_into('<8s14I7f9I',image,0,b'CORALM01',1,128,len(image),len(tensors),128,
        *[cfg[k] for k in ('dim','hidden_dim','n_layers','n_heads','n_kv_heads','head_dim','vocab','max_seq','flags')],
        *[cfg[k] for k in ('rope_theta','rms_eps','rope_factor','rope_original_context','yarn_beta_fast','yarn_beta_slow','yarn_attention_factor')],*([0]*9))
    for i,t in enumerate(directory):struct.pack_into('<8I',image,128+32*i,*t)
    # Round serialized config floats, so references use exact package values.
    for k in ('rope_theta','rms_eps','rope_factor','rope_original_context','yarn_beta_fast','yarn_beta_slow','yarn_attention_factor'):cfg[k]=fp32(cfg[k])
    return bytes(image),cfg,decoded

def reference(cfg,w,tokens):
    """Independent dense equations: Python decoded weights + standard libm."""
    d,hd,nh,nk,L=map(cfg.get,('dim','head_dim','n_heads','n_kv_heads','n_layers'))
    G=0xffffffff; traces={}; keys=[[] for _ in range(L)]; vals=[[] for _ in range(L)]
    inv=[cfg['rope_theta']**(-2*j/hd) for j in range(hd//2)]; magnitude=1
    if cfg['flags']&4:
        low=max(0,math.floor(hd*math.log(cfg['rope_original_context']/(cfg['yarn_beta_fast']*2*math.pi))/(2*math.log(cfg['rope_theta']))))
        high=min(hd-1,math.ceil(hd*math.log(cfg['rope_original_context']/(cfg['yarn_beta_slow']*2*math.pi))/(2*math.log(cfg['rope_theta']))))
        for j in range(hd//2):
            ramp=1-min(1,max(0,(j-low)/max(.001,high-low)))
            inv[j]*=ramp+(1-ramp)/cfg['rope_factor']
        magnitude=cfg['yarn_attention_factor']
    def rope(x,pos):
        out=list(x)
        for h in range(len(x)//hd):
            for j in range(hd//2):
                a,b=x[h*hd+j],x[h*hd+j+hd//2]
                c,s=math.cos(pos*inv[j])*magnitude,math.sin(pos*inv[j])*magnitude
                out[h*hd+j],out[h*hd+j+hd//2]=a*c-b*s,a*s+b*c
        return out
    for pos,token in enumerate(tokens):
        def save(stage,layer,v):traces[stage,layer,pos]=list(v)
        x=w[1,G][token]
        for l in range(L):
            z=norm(x,w[10,l][0],cfg['rms_eps']); save(1,l,z)
            q,k,v=mat(w[11,l],z),mat(w[12,l],z),mat(w[13,l],z)
            q=[a for h in range(nh) for a in norm(q[h*hd:(h+1)*hd],w[15,l][0],cfg['rms_eps'])]
            k=[a for h in range(nk) for a in norm(k[h*hd:(h+1)*hd],w[16,l][0],cfg['rms_eps'])]
            q,k=rope(q,pos),rope(k,pos); save(2,l,q); save(3,l,k); save(4,l,v)
            keys[l].append(k); vals[l].append(v); att=[]
            for h in range(nh):
                kh=h//(nh//nk)
                scores=[sum(q[h*hd+j]*pk[kh*hd+j] for j in range(hd))/math.sqrt(hd) for pk in keys[l]]
                probs=[math.exp(a-max(scores)) for a in scores]; total=sum(probs); probs=[a/total for a in probs]
                att += [sum(p*pv[kh*hd+j] for p,pv in zip(probs,vals[l])) for j in range(hd)]
            save(5,l,att); residual=[a+b for a,b in zip(x,mat(w[14,l],att))]
            z=norm(residual,w[17,l][0],cfg['rms_eps']); save(6,l,z)
            gate,up=mat(w[18,l],z),mat(w[19,l],z); save(7,l,gate); save(8,l,up)
            act=[u*g/(1+math.exp(-g)) for u,g in zip(up,gate)]
            x=[a+b for a,b in zip(residual,mat(w[20,l],act))]; save(9,l,x)
        logits=mat(w[1 if cfg['flags']&1 else 3,G],norm(x,w[2,G][0],cfg['rms_eps'])); save(10,L,logits)
    return traces

def reference_generation(cfg,weights,prompt,max_new,eos=()):
    # Intentionally recompute the dense reference prefix for each prediction;
    # this independently checks that the C path reuses its KV correctly.
    prefix=list(prompt); generated=[]; predictions=[]
    for _ in range(max(1,max_new)):
        logits=reference(cfg,weights,prefix)[10,cfg['n_layers'],len(prefix)-1]
        predictions.append(logits)
        if not max_new:break
        token=max(range(cfg['vocab']),key=lambda j:logits[j]); generated.append(token)
        if token in eos:break
        prefix.append(token)
    return generated,predictions

class RuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory(prefix='coral-decoder-')
        so=Path(cls.tmp.name)/'decoder.so'
        subprocess.run(['clang','-shared','-fPIC','-O2','-std=c11','-Wall','-Wextra','-Werror',
                        '-ffp-contract=off','-fno-fast-math',str(HERE/'decoder.c'),str(HERE/'math.c'),str(HERE/'generate.c'),'-o',str(so)],check=True)
        cls.lib=C.CDLL(str(so))
        cls.lib.cm_workspace_bytes.argtypes=[C.c_void_p,C.c_uint32]; cls.lib.cm_workspace_bytes.restype=C.c_uint32
        cls.lib.cm_init.argtypes=[C.POINTER(State),C.c_void_p,C.c_uint32,C.c_void_p,C.c_uint32,C.c_uint32]
        cls.lib.cm_step.argtypes=[C.POINTER(State),C.c_uint32,F,C.c_uint32]
        cls.lib.cm_generate.argtypes=[C.POINTER(State),C.POINTER(C.c_uint32),C.c_uint32,C.c_uint32,C.POINTER(C.c_uint32),C.c_uint32,C.POINTER(C.c_uint32),C.c_uint32,F,C.c_uint32,C.POINTER(GenerationResult)]
        cls.lib.cm_exp.argtypes=[C.c_float]; cls.lib.cm_exp.restype=C.c_float
        cls.lib.cm_log.argtypes=[C.c_float]; cls.lib.cm_log.restype=C.c_float
        cls.lib.cm_sincos.argtypes=[C.c_float,F,F]
        cls.lib.cm_weight.argtypes=[C.c_void_p,C.POINTER(Tensor),C.c_uint32,C.c_uint32]; cls.lib.cm_weight.restype=C.c_float
        cls.lib.cm_matvec.argtypes=[F,C.c_void_p,C.POINTER(Tensor),F]
    @classmethod
    def tearDownClass(cls):cls.tmp.cleanup()
    def setup_model(self,image,capacity=3):
        self.blob=C.create_string_buffer(image)
        n=self.lib.cm_workspace_bytes(self.blob,capacity); self.assertGreater(n,0)
        self.work=(C.c_float*(n//4))(); self.state=State()
        self.assertEqual(self.lib.cm_init(C.byref(self.state),self.blob,len(image),self.work,n,capacity),0)
        return n
    def test_math_error_budget(self):
        for i in range(-2000,2001):
            x=fp32(i*.04)
            self.assertLess(abs(self.lib.cm_exp(x)/math.exp(x)-1),8e-7)
        for i in range(1,1000):
            x=fp32(i*.01)
            self.assertLess(abs(self.lib.cm_log(x)-math.log(x)),7e-7)
        for i in range(0,4097):
            sn=C.c_float(); cs=C.c_float(); self.lib.cm_sincos(i,C.byref(sn),C.byref(cs))
            self.assertLess(abs(sn.value-math.sin(i)),.00025)
            self.assertLess(abs(cs.value-math.cos(i)),.00025)
    def test_native_code3_scale_and_order(self):
        image,cfg,w=fixture(3); self.setup_model(image)
        t=Tensor.from_buffer_copy(image[128:160])
        for row in (0,3,8):
            for col in range(cfg['dim']):self.assertEqual(self.lib.cm_weight(self.blob,C.byref(t),row,col),w[1,0xffffffff][row][col])
        x=(C.c_float*128)(*[fp32(math.sin(i)) for i in range(128)]); out=(C.c_float*9)()
        self.lib.cm_matvec(out,self.blob,C.byref(t),x)
        for r in range(9):
            expected=0
            for a,b in zip(w[1,0xffffffff][r],x):expected=fp32(expected+fp32(a*b))
            self.assertEqual(out[r],expected)
    def test_operator_layer_multitoken_goldens(self):
        for encoding,yarn,tied in ((1,False,False),(2,False,True),(3,True,True)):
            with self.subTest(encoding=encoding,yarn=yarn,tied=tied):
                image,cfg,w=fixture(encoding,yarn,tied); self.setup_model(image)
                actual={}
                callback=TRACE(lambda stage,layer,pos,data,n,ctx:actual.__setitem__((stage,layer,pos),list(data[:n])))
                self.state.trace=callback
                tokens=[1,2,3]; expected=reference(cfg,w,tokens); out=(C.c_float*cfg['vocab'])()
                for token in tokens:self.assertEqual(self.lib.cm_step(C.byref(self.state),token,out,cfg['vocab']),0)
                self.assertEqual(set(actual),set(expected))
                for key,gold in expected.items():
                    worst=max(abs(a-b)/(1+abs(b)) for a,b in zip(actual[key],gold))
                    self.assertLess(worst,3e-5,(key,worst))
                for p in range(3):
                    key=(10,cfg['n_layers'],p)
                    self.assertEqual(max(range(9),key=lambda j:actual[key][j]),max(range(9),key=lambda j:expected[key][j]))
                self.assertEqual(self.lib.cm_step(C.byref(self.state),1,out,9),6)
    def test_autoregressive_generation_eos_length_repeatability(self):
        for encoding,yarn in ((2,False),(3,True)):
            image,cfg,w=fixture(encoding,yarn,True,capacity=8)
            for prompt in ([1,2],[3]):
                expected,predictions=reference_generation(cfg,w,prompt,4)
                observed=[]
                for eos in ((),(),(expected[0],)):
                    self.setup_model(image,8)
                    inp=(C.c_uint32*len(prompt))(*prompt); stop=(C.c_uint32*len(eos))(*eos)
                    generated=(C.c_uint32*4)(); logits=(C.c_float*36)(); result=GenerationResult()
                    rc=self.lib.cm_generate(C.byref(self.state),inp,len(prompt),4,stop,len(eos),generated,4,logits,36,C.byref(result))
                    self.assertEqual(rc,0)
                    count=1 if eos else 4
                    self.assertEqual(list(generated[:count]),expected[:count])
                    self.assertEqual(result.generated_count,count)
                    self.assertEqual(result.completed_tokens,len(prompt)+count-1)
                    self.assertEqual(self.state.position,result.completed_tokens)
                    self.assertEqual(result.stop_reason,2 if eos else 1)
                    for i in range(count):
                        worst=max(abs(logits[i*9+j]-predictions[i][j])/(1+abs(predictions[i][j])) for j in range(9))
                        self.assertLess(worst,3e-5)
                    if not eos:observed.append(bytes(logits))
                self.assertEqual(observed[0],observed[1])
        # Prompt-only contract and refusal to restart/reuse a nonempty state.
        image,cfg,w=fixture(capacity=8); self.setup_model(image,8)
        inp=(C.c_uint32*2)(1,2); logits=(C.c_float*9)(); result=GenerationResult()
        self.assertEqual(self.lib.cm_generate(C.byref(self.state),inp,2,0,None,0,None,0,logits,9,C.byref(result)),0)
        self.assertEqual((result.completed_tokens,result.generated_count,result.stop_reason),(2,0,0))
        self.assertEqual(self.lib.cm_generate(C.byref(self.state),inp,2,0,None,0,None,0,logits,9,C.byref(result)),7)

    def test_validation_and_workspace_bounds(self):
        image,cfg,_=fixture(); n=self.setup_model(image); out=(C.c_float*9)()
        self.assertEqual(n,4*(3*8+2*16+2*8+2*16+3+2+2*2*3*8))
        self.assertEqual(self.lib.cm_step(C.byref(self.state),9,out,9),5)
        self.assertEqual(self.lib.cm_step(C.byref(self.state),1,out,8),7)
        self.assertEqual(self.lib.cm_init(C.byref(self.state),self.blob,len(image),self.work,n-1,3),4)
        for offset,value in ((0,0),(8,2),(16,len(image)+4),(24,129),(44,3),(128+20,0),(128+24,4)):
            bad=bytearray(image); struct.pack_into('<I',bad,offset,value); blob=C.create_string_buffer(bytes(bad))
            self.assertNotEqual(self.lib.cm_init(C.byref(self.state),blob,len(image),self.work,n,3),0,offset)
        # NaN epsilon, overflowing workspace, and duplicate tensor role rejected.
        for offset,value in ((68,0x7fc00000),(36,64),(128+32,1)):
            bad=bytearray(image); struct.pack_into('<I',bad,offset,value); blob=C.create_string_buffer(bytes(bad))
            self.assertNotEqual(self.lib.cm_init(C.byref(self.state),blob,len(image),self.work,n,3),0)

if __name__=='__main__':unittest.main(verbosity=2)
