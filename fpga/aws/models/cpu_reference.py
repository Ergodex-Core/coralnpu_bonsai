#!/usr/bin/env python3
"""Independent NumPy CPU oracle. Never called by hardware inference.

Streams FP32-decoded rows; ordered reductions round each add to FP32.
Requires numpy==2.4.2. Tokenization is a separate explicitly pinned input.
"""
import argparse
import hashlib
import json
import math
import mmap
from pathlib import Path
import struct
import sys
import time
import numpy as np
from pack_model import HEADER, TENSOR, Config, GLOBAL, F32, BF16, PQ2, TIED, QKNORM, YARN, sha256, require


class Reference:
    def __init__(self, manifest_path):
        self.manifest=json.loads(Path(manifest_path).read_text())
        seg=self.manifest['segments'][0]
        path=Path(manifest_path).parent/seg['file']
        require(path.stat().st_size==seg['bytes'] and sha256(path)==seg['sha256'],'package size/hash mismatch')
        with path.open('rb') as source:
            self.data=mmap.mmap(source.fileno(),0,access=mmap.ACCESS_READ)
        h=HEADER.unpack_from(self.data)
        require(h[:4]==(b'CORALM01',1,128,len(self.data)),'bad image')
        self.c=Config(*h[6:15],*h[15:22])
        require(1<=self.c.max_seq<=2048,'unsupported context')
        self.ts={}
        for i in range(h[4]):
            t=TENSOR.unpack_from(self.data,128+32*i)
            require(t[5]+t[6]<=len(self.data),'tensor outside image')
            self.ts[t[:2]]=t
        self.position=0
        self.keys=np.zeros((self.c.n_layers,self.c.max_seq,self.c.n_kv_heads,self.c.head_dim),np.float32)
        self.values=np.zeros_like(self.keys)
        c=self.c
        freq=np.power(c.rope_theta,-2*np.arange(c.head_dim//2,dtype=np.float64)/c.head_dim)
        if c.flags&YARN:
            low=max(0,math.floor(c.head_dim*math.log(c.rope_original_context/(c.yarn_beta_fast*2*math.pi))/(2*math.log(c.rope_theta))))
            high=min(c.head_dim-1,math.ceil(c.head_dim*math.log(c.rope_original_context/(c.yarn_beta_slow*2*math.pi))/(2*math.log(c.rope_theta))))
            ramp=1-np.clip((np.arange(c.head_dim//2)-low)/max(.001,high-low),0,1)
            freq*=ramp+(1-ramp)/c.rope_factor
        self.freq=freq.astype(np.float32)
        self.trace={}

    def rows(self, role, layer, first=0, count=None):
        t=self.ts[role,layer];_,_,enc,rows,cols,offset,_,_=t
        if count is None:count=rows-first
        require(0<=first<=rows and 0<=count<=rows-first,'row bounds')
        if enc==F32:
            return np.frombuffer(self.data,dtype='<f4',count=count*cols,offset=offset+first*cols*4).reshape(count,cols)
        if enc==BF16:
            x=np.frombuffer(self.data,dtype='<u2',count=count*cols,offset=offset+first*cols*2)
            return np.left_shift(x.astype(np.uint32),16).view(np.float32).reshape(count,cols)
        require(enc==PQ2 and cols%128==0,'encoding')
        blocks=count*cols//128
        x=np.frombuffer(self.data,dtype=np.uint8,count=blocks*34,offset=offset+first*(cols//128)*34).reshape(blocks,34)
        scales=x[:,:2].copy().view('<f2').astype(np.float32).reshape(blocks,1)
        codes=((x[:,2:,None]>>np.array([0,2,4,6],np.uint8))&3).reshape(blocks,128).astype(np.float32)-1
        return (codes*scales).reshape(count,cols)

    @staticmethod
    def total(x,axis=-1):
        return np.add.accumulate(x,axis=axis,dtype=np.float32).take(-1,axis=axis)

    def mat(self,role,layer,x):
        t=self.ts[role,layer];result=np.empty(t[3],np.float32)
        for row in range(0,t[3],128):
            n=min(128,t[3]-row)
            result[row:row+n]=self.total(self.rows(role,layer,row,n)*x)
        return result

    def norm(self,x,role,layer):
        w=self.rows(role,layer).reshape(-1)
        inv=np.float32(1)/np.sqrt(self.total(x*x)/np.float32(x.shape[-1])+np.float32(self.c.rms_eps))
        return (x*inv[...,None])*w if x.ndim>1 else (x*inv)*w

    def rope(self,x):
        c=self.c;d=c.head_dim//2
        a=np.float32(self.position)*self.freq
        magnitude=np.float32(c.yarn_attention_factor if c.flags&YARN else 1)
        cs=np.cos(a)*magnitude;sn=np.sin(a)*magnitude
        return np.concatenate((x[:,:d]*cs-x[:,d:]*sn,x[:,:d]*sn+x[:,d:]*cs),axis=1)

    def record(self,name,layer,x):
        self.trace[f'{self.position}:{layer}:{name}']=hashlib.sha256(x.astype('<f4').tobytes()).hexdigest()

    def step(self,token):
        c=self.c;p=self.position
        require(0<=token<c.vocab and p<c.max_seq,'token/context bounds')
        x=self.rows(1,GLOBAL,token,1).reshape(-1).copy()
        for layer in range(c.n_layers):
            n=self.norm(x,10,layer);self.record('attn_norm',layer,n)
            q=self.mat(11,layer,n).reshape(c.n_heads,c.head_dim)
            k=self.mat(12,layer,n).reshape(c.n_kv_heads,c.head_dim)
            v=self.mat(13,layer,n).reshape(c.n_kv_heads,c.head_dim)
            if c.flags&QKNORM:q=self.norm(q,15,layer);k=self.norm(k,16,layer)
            q=self.rope(q);k=self.rope(k)
            for name,a in [('q',q),('k',k),('v',v)]:self.record(name,layer,a)
            self.keys[layer,p]=k;self.values[layer,p]=v
            att=np.empty_like(q)
            for head in range(c.n_heads):
                kh=head//(c.n_heads//c.n_kv_heads)
                score=self.total(self.keys[layer,:p+1,kh]*q[head])/np.float32(math.sqrt(c.head_dim))
                score=np.exp(score-np.max(score));score*=np.float32(1)/self.total(score)
                att[head]=self.total(self.values[layer,:p+1,kh]*score[:,None],axis=0)
            self.record('attention',layer,att)
            residual=x+self.mat(14,layer,att.reshape(-1))
            n=self.norm(residual,17,layer);self.record('ffn_norm',layer,n)
            gate=self.mat(18,layer,n);up=self.mat(19,layer,n)
            self.record('gate',layer,gate);self.record('up',layer,up)
            with np.errstate(over='ignore'):activated=gate/(np.float32(1)+np.exp(-gate))
            x=residual+self.mat(20,layer,up*activated);self.record('layer',layer,x)
        logits=self.mat(1 if c.flags&TIED else 3,GLOBAL,self.norm(x,2,GLOBAL))
        require(np.all(np.isfinite(logits)),'nonfinite logits')
        self.record('logits',c.n_layers,logits);self.position+=1
        return logits

    def generate(self,tokens,count,eos=()):
        require(tokens and count>0 and len(tokens)+count-1<=self.c.max_seq,'invalid prompt/decode length')
        for t in tokens:logits=self.step(t)
        output=[];steps=[]
        for i in range(count):
            token=int(np.argmax(logits));output.append(token)
            top=np.argsort(logits)[-5:][::-1]
            steps.append({'position':self.position-1,'token':token,'logits_sha256':hashlib.sha256(logits.astype('<f4').tobytes()).hexdigest(),
                'top5':[{'token':int(t),'logit':float(logits[t])} for t in top]})
            if token in eos:return output,steps,'eos'
            if i+1<count:logits=self.step(token)
        return output,steps,'length'


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('manifest',type=Path);p.add_argument('--tokens',required=True,help='comma-separated IDs from independently pinned tokenizer')
    p.add_argument('--max-new-tokens',type=int,default=4);p.add_argument('--eos',default='151645,151643')
    p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    t=time.monotonic();r=Reference(a.manifest)
    tokens=[int(v) for v in a.tokens.split(',')];eos=[int(v) for v in a.eos.split(',') if v]
    generated,steps,reason=r.generate(tokens,a.max_new_tokens,eos)
    report={'backend':'independent NumPy FP32 ordered reference','model_id':r.manifest['model_id'],
        'package_sha256':r.manifest['segments'][0]['sha256'],'config':r.manifest['config'],
        'input_ids':tokens,'generated_ids':generated,'stop_reason':reason,'eos_ids':eos,
        'steps':steps,'operator_trace_sha256':r.trace,'elapsed_seconds':time.monotonic()-t,
        'physical':'NOT_RUN','coral_simulation':'NOT_RUN'}
    a.output.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({'generated_ids':generated,'stop_reason':reason}))

if __name__=='__main__':main()
