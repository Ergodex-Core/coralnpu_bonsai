/* SPDX-License-Identifier: Apache-2.0 */
#include "decoder.h"
#include "math.h"
#include <stddef.h>
/* Matrix weights and activations are staged through bounded DTCM tiles.
 * Full activations, score vectors and KV remain in DDR. This scalar baseline
 * prioritizes correctness; it is neither an RVV throughput claim nor Q8_K. */
static unsigned char weight_tile[512];
static float input_tile[128];
static uint32_t u16(const unsigned char *p) { return p[0] | ((uint32_t)p[1]<<8); }
static uint32_t u32(const unsigned char *p) { return u16(p) | (u16(p+2)<<16); }
static float as_float(uint32_t u) { union { uint32_t u; float f; } v={u}; return v.f; }
static float half(uint32_t u) {
  uint32_t sign=(u&0x8000u)<<16, e=(u>>10)&31u, m=u&1023u;
  if (!e) {
    if (!m) return as_float(sign);
    e=113;
    while (!(m&1024u)) { m<<=1; --e; }
    return as_float(sign|(e<<23)|((m&1023u)<<13));
  }
  return as_float(sign|((e==31u ? 255u : e+112u)<<23)|(m<<13));
}
static int finite(float x) {
  union { uint32_t u; float f; } v; v.f=x;
  return (v.u&0x7f800000u)!=0x7f800000u;
}
static float clamp(float x, float lo, float hi) { return x<lo?lo:(x>hi?hi:x); }
static uint32_t row_bytes(const cm_tensor *t) {
  return t->encoding==CM_PQ2_0 ? (t->cols/128u)*34u : t->cols*(t->encoding==CM_F32 ? 4u:2u);
}
static float element(const unsigned char *p, uint32_t encoding, uint32_t col) {
  if (encoding==CM_F32) return as_float(u32(p+col*4));
  if (encoding==CM_BF16) return as_float(u16(p+col*2)<<16);
  p+=(col/128u)*34u;
  /* Code 3 has native value +2 and is deliberately preserved. */
  return half(u16(p))*(float)((int)((p[2+(col%128u)/4u]>>(2*(col%4u)))&3u)-1);
}
float cm_weight(const unsigned char *image, const cm_tensor *t, uint32_t row, uint32_t col) {
  return element(image+t->offset+row*row_bytes(t),t->encoding,col);
}
void cm_matvec(float *out, const unsigned char *image, const cm_tensor *t, const float *input) {
  uint32_t stride=row_bytes(t);
  for (uint32_t r=0;r<t->rows;++r) {
    const unsigned char *row=image+t->offset+r*stride;
    float sum=0;
    for (uint32_t c=0;c<t->cols;c+=128u) {
      uint32_t n=t->cols-c; if(n>128u)n=128u;
      uint32_t size=t->encoding==CM_PQ2_0 ? 34u:n*(t->encoding==CM_F32?4u:2u);
      uint32_t off=t->encoding==CM_PQ2_0 ? (c/128u)*34u:c*(t->encoding==CM_F32?4u:2u);
      for(uint32_t i=0;i<size;++i)weight_tile[i]=row[off+i];
      for(uint32_t i=0;i<n;++i)input_tile[i]=input[c+i];
      /* Ordered FP32 mul then add; compiler -ffp-contract=off is mandatory. */
      for(uint32_t i=0;i<n;++i)sum += element(weight_tile,t->encoding,i)*input_tile[i];
    }
    out[r]=sum;
  }
}
static const cm_tensor *tensor(const cm_state *s,uint32_t role,uint32_t layer) {
  for(uint32_t i=0;i<s->h->tensor_count;++i)
    if(s->directory[i].role==role && s->directory[i].layer==layer)return s->directory+i;
  return NULL;
}
void cm_rms(float *out,const float *input,const unsigned char *image,const cm_tensor *weight,uint32_t n,float eps) {
  float sum=0;
  for(uint32_t j=0;j<n;++j)sum += input[j]*input[j];
  float inverse=1.0f/cm_sqrt(sum/(float)n+eps);
  const unsigned char *w=image+weight->offset;
  for(uint32_t j=0;j<n;++j)out[j]=(input[j]*inverse)*element(w,weight->encoding,j);
}
void cm_softmax(float *x,uint32_t n) {
  float maximum=x[0],sum=0;
  for(uint32_t j=1;j<n;++j)if(x[j]>maximum)maximum=x[j];
  for(uint32_t j=0;j<n;++j) { x[j]=cm_exp(x[j]-maximum); sum+=x[j]; }
  float inv=1.0f/sum;
  for(uint32_t j=0;j<n;++j)x[j]*=inv;
}
static int config_valid(const cm_header *h,uint32_t capacity) {
  return h->dim && h->dim<=8192 && h->hidden_dim && h->hidden_dim<=32768 &&
    h->n_layers && h->n_layers<=64 && h->n_heads && h->n_heads<=64 &&
    h->n_kv_heads && h->n_kv_heads<=h->n_heads && h->n_heads%h->n_kv_heads==0 &&
    h->head_dim && h->head_dim<=256 && !(h->head_dim&1) && h->vocab && h->vocab<=200000 &&
    capacity && capacity<=2048 && capacity<=h->max_seq && !(h->flags&~7u) &&
    finite(h->rope_theta) && h->rope_theta>1 && finite(h->rms_eps) && h->rms_eps>0 &&
    (!(h->flags&CM_YARN) || (finite(h->rope_factor) && h->rope_factor>=1 &&
      finite(h->rope_original_context) && h->rope_original_context>=1 &&
      finite(h->yarn_beta_fast) && finite(h->yarn_beta_slow) && h->yarn_beta_fast>h->yarn_beta_slow &&
      h->yarn_beta_slow>0 && finite(h->yarn_attention_factor) && h->yarn_attention_factor>0));
}
uint32_t cm_workspace_bytes(const cm_header *h,uint32_t capacity) {
  if(!config_valid(h,capacity))return 0;
  uint32_t q=h->n_heads*h->head_dim,kv=h->n_kv_heads*h->head_dim;
  uint32_t small=3*h->dim+2*q+2*kv+2*h->hidden_dim+capacity+h->head_dim/2;
  uint32_t per=2*h->n_layers*kv;
  if(capacity>(0x3fffffffu-small)/per)return 0;
  return 4*(small+capacity*per);
}
static int shape(const cm_state *s,uint32_t role,uint32_t layer,uint32_t rows,uint32_t cols,int norm) {
  const cm_tensor *t=tensor(s,role,layer);
  if(!t)return 0;
  if(norm)return t->encoding!=CM_PQ2_0 && ((t->rows==1 && t->cols==cols)||(t->cols==1 && t->rows==cols));
  return t->rows==rows && t->cols==cols;
}
static int validate_tensors(cm_state *s) {
  const cm_header *h=s->h;
  uint32_t q=h->n_heads*h->head_dim,k=h->n_kv_heads*h->head_dim;
  uint32_t expected=2+((h->flags&CM_TIED_EMBEDDINGS)?0:1)+h->n_layers*(9+((h->flags&CM_QK_NORM)?2:0));
  if(h->tensor_count!=expected)return 0;
  uint32_t directory_end=h->dir_offset+h->tensor_count*(uint32_t)sizeof(cm_tensor);
  for(uint32_t i=0;i<h->tensor_count;++i) {
    const cm_tensor *t=s->directory+i;
    if(t->encoding<CM_F32 || t->encoding>CM_PQ2_0 || !t->rows || !t->cols ||
       t->cols>32768 || t->rows>200000 || t->reserved || t->offset<directory_end ||
       t->offset%64 || t->offset>h->file_bytes || t->bytes>h->file_bytes-t->offset ||
       (t->encoding==CM_PQ2_0 && t->cols%128))return 0;
    uint32_t stride=row_bytes(t);
    if(t->rows>0xffffffffu/stride || t->bytes!=t->rows*stride)return 0;
    for(uint32_t j=0;j<i;++j) {
      const cm_tensor *u=s->directory+j;
      if(t->role==u->role && t->layer==u->layer)return 0;
      if(t->offset<u->offset+u->bytes && u->offset<t->offset+t->bytes)return 0;
    }
  }
  if(!shape(s,CM_EMBEDDING,CM_GLOBAL_LAYER,h->vocab,h->dim,0) ||
     !shape(s,CM_FINAL_NORM,CM_GLOBAL_LAYER,1,h->dim,1) ||
     (!(h->flags&CM_TIED_EMBEDDINGS) && !shape(s,CM_OUTPUT,CM_GLOBAL_LAYER,h->vocab,h->dim,0)))return 0;
  for(uint32_t l=0;l<h->n_layers;++l) {
    if(!shape(s,CM_INPUT_NORM,l,1,h->dim,1) || !shape(s,CM_Q,l,q,h->dim,0) ||
       !shape(s,CM_K,l,k,h->dim,0) || !shape(s,CM_V,l,k,h->dim,0) ||
       !shape(s,CM_O,l,h->dim,q,0) || !shape(s,CM_POSTATTN_NORM,l,1,h->dim,1) ||
       !shape(s,CM_GATE,l,h->hidden_dim,h->dim,0) || !shape(s,CM_UP,l,h->hidden_dim,h->dim,0) ||
       !shape(s,CM_DOWN,l,h->dim,h->hidden_dim,0))return 0;
    if((h->flags&CM_QK_NORM) && (!shape(s,CM_Q_NORM,l,1,h->head_dim,1)||!shape(s,CM_K_NORM,l,1,h->head_dim,1)))return 0;
  }
  return 1;
}
int cm_init(cm_state *s,const void *image,uint32_t image_bytes,void *workspace,uint32_t workspace_bytes,uint32_t capacity) {
  if(!s || !image || ((uintptr_t)image&3u) || image_bytes<128)return CM_BAD_IMAGE;
  const cm_header *h=(const cm_header *)image;
  const char *magic=CM_MAGIC;
  for(unsigned i=0;i<8;++i)if(h->magic[i]!=magic[i])return CM_BAD_IMAGE;
  if(h->version!=CM_VERSION || h->header_bytes!=128 || h->file_bytes!=image_bytes ||
     h->dir_offset!=128 || h->tensor_count>1024 ||
     h->tensor_count>(image_bytes-128)/32)return CM_BAD_IMAGE;
  for(unsigned i=0;i<9;++i)if(h->reserved[i])return CM_BAD_IMAGE;
  uint32_t required=cm_workspace_bytes(h,capacity);
  if(!required)return CM_BAD_CONFIG;
  if(!workspace || ((uintptr_t)workspace&3u) || workspace_bytes<required)return CM_NO_WORKSPACE;
  s->h=h; s->image=image; s->directory=(const cm_tensor *)((const unsigned char *)image+128);
  if(!validate_tensors(s))return CM_BAD_TENSOR;
  s->capacity=capacity; s->position=0; s->trace=NULL; s->trace_context=NULL;
  float *p=workspace; uint32_t q=h->n_heads*h->head_dim,k=h->n_kv_heads*h->head_dim;
#define TAKE(member,count) do { s->member=p; p+=(count); } while(0)
  TAKE(x,h->dim); TAKE(norm,h->dim); TAKE(q,q); TAKE(k,k); TAKE(v,k);
  TAKE(att,q); TAKE(residual,h->dim); TAKE(gate,h->hidden_dim); TAKE(up,h->hidden_dim);
  TAKE(scores,capacity); TAKE(rope_inv,h->head_dim/2); TAKE(keys,h->n_layers*capacity*k); TAKE(values,h->n_layers*capacity*k);
#undef TAKE
  float logbase=cm_log(h->rope_theta),low=0,high=0;
  s->rope_magnitude=1;
  if(h->flags&CM_YARN) {
    low=(float)(int)((float)h->head_dim*cm_log(h->rope_original_context/(h->yarn_beta_fast*6.28318530718f))/(2*logbase));
    /* floor/ceil expressed without libc; correction dimensions can be negative. */
    float raw=(float)h->head_dim*cm_log(h->rope_original_context/(h->yarn_beta_fast*6.28318530718f))/(2*logbase);
    if(low>raw)low-=1;
    raw=(float)h->head_dim*cm_log(h->rope_original_context/(h->yarn_beta_slow*6.28318530718f))/(2*logbase);
    high=(float)(int)raw; if(high<raw)high+=1;
    low=clamp(low,0,(float)h->head_dim-1); high=clamp(high,0,(float)h->head_dim-1);
    /* yarn_attention_factor is the FINAL multiplier, explicitly resolved by
     * the packager/reference. Do not multiply by a hidden log(factor) default. */
    s->rope_magnitude=h->yarn_attention_factor;
  }
  for(uint32_t j=0;j<h->head_dim/2;++j) {
    float frequency=cm_exp(-2*(float)j*logbase/(float)h->head_dim);
    if(h->flags&CM_YARN) {
      float ramp=1-clamp(((float)j-low)/(high>low?high-low:.001f),0,1);
      frequency *= (1-ramp)/h->rope_factor+ramp;
    }
    s->rope_inv[j]=frequency;
  }
  return CM_OK;
}
static void trace(cm_state *s,unsigned stage,unsigned layer,const float *v,unsigned n) {
  if(s->trace)s->trace(stage,layer,s->position,v,n,s->trace_context);
}
static void rope(cm_state *s,float *x,uint32_t heads) {
  uint32_t d=s->h->head_dim;
  for(uint32_t j=0;j<d/2;++j) {
    float sn,cs; cm_sincos((float)s->position*s->rope_inv[j],&sn,&cs);
    sn*=s->rope_magnitude; cs*=s->rope_magnitude;
    for(uint32_t h=0;h<heads;++h) {
      float a=x[h*d+j],b=x[h*d+j+d/2];
      x[h*d+j]=a*cs-b*sn; x[h*d+j+d/2]=a*sn+b*cs;
    }
  }
}
int cm_step(cm_state *s,uint32_t token,float *logits,uint32_t logits_count) {
  if(!s || !s->h || !logits || logits_count<s->h->vocab)return CM_BAD_REQUEST;
  const cm_header *h=s->h;
  if(token>=h->vocab)return CM_BAD_TOKEN;
  if(s->position>=s->capacity)return CM_BAD_POSITION;
  uint32_t d=h->dim,hd=h->head_dim,q=h->n_heads*hd,kv=h->n_kv_heads*hd,pos=s->position;
  const cm_tensor *embedding=tensor(s,CM_EMBEDDING,CM_GLOBAL_LAYER);
  for(uint32_t j=0;j<d;++j)s->x[j]=cm_weight(s->image,embedding,token,j);
  for(uint32_t l=0;l<h->n_layers;++l) {
    cm_rms(s->norm,s->x,s->image,tensor(s,CM_INPUT_NORM,l),d,h->rms_eps);
    trace(s,CM_TRACE_ATTN_NORM,l,s->norm,d);
    cm_matvec(s->q,s->image,tensor(s,CM_Q,l),s->norm);
    cm_matvec(s->k,s->image,tensor(s,CM_K,l),s->norm);
    cm_matvec(s->v,s->image,tensor(s,CM_V,l),s->norm);
    if(h->flags&CM_QK_NORM) {
      for(uint32_t a=0;a<h->n_heads;++a)cm_rms(s->q+a*hd,s->q+a*hd,s->image,tensor(s,CM_Q_NORM,l),hd,h->rms_eps);
      for(uint32_t a=0;a<h->n_kv_heads;++a)cm_rms(s->k+a*hd,s->k+a*hd,s->image,tensor(s,CM_K_NORM,l),hd,h->rms_eps);
    }
    rope(s,s->q,h->n_heads); rope(s,s->k,h->n_kv_heads);
    trace(s,CM_TRACE_Q,l,s->q,q); trace(s,CM_TRACE_K,l,s->k,kv); trace(s,CM_TRACE_V,l,s->v,kv);
    uint32_t slot=(l*s->capacity+pos)*kv;
    for(uint32_t j=0;j<kv;++j) { s->keys[slot+j]=s->k[j]; s->values[slot+j]=s->v[j]; }
    for(uint32_t a=0;a<h->n_heads;++a) {
      uint32_t kh=a/(h->n_heads/h->n_kv_heads);
      for(uint32_t p=0;p<=pos;++p) {
        float dot=0; const float *k=s->keys+(l*s->capacity+p)*kv+kh*hd;
        for(uint32_t j=0;j<hd;++j)dot+=s->q[a*hd+j]*k[j];
        s->scores[p]=dot/cm_sqrt((float)hd);
      }
      cm_softmax(s->scores,pos+1);
      for(uint32_t j=0;j<hd;++j) {
        float value=0;
        for(uint32_t p=0;p<=pos;++p)value+=s->scores[p]*s->values[(l*s->capacity+p)*kv+kh*hd+j];
        s->att[a*hd+j]=value;
      }
    }
    trace(s,CM_TRACE_ATTN,l,s->att,q);
    cm_matvec(s->norm,s->image,tensor(s,CM_O,l),s->att);
    for(uint32_t j=0;j<d;++j)s->residual[j]=s->x[j]+s->norm[j];
    cm_rms(s->norm,s->residual,s->image,tensor(s,CM_POSTATTN_NORM,l),d,h->rms_eps);
    trace(s,CM_TRACE_FFN_NORM,l,s->norm,d);
    cm_matvec(s->gate,s->image,tensor(s,CM_GATE,l),s->norm);
    cm_matvec(s->up,s->image,tensor(s,CM_UP,l),s->norm);
    trace(s,CM_TRACE_GATE,l,s->gate,h->hidden_dim); trace(s,CM_TRACE_UP,l,s->up,h->hidden_dim);
    for(uint32_t j=0;j<h->hidden_dim;++j)s->up[j]*=s->gate[j]/(1+cm_exp(-s->gate[j]));
    cm_matvec(s->norm,s->image,tensor(s,CM_DOWN,l),s->up);
    for(uint32_t j=0;j<d;++j)s->x[j]=s->residual[j]+s->norm[j];
    trace(s,CM_TRACE_LAYER,l,s->x,d);
  }
  cm_rms(s->norm,s->x,s->image,tensor(s,CM_FINAL_NORM,CM_GLOBAL_LAYER),d,h->rms_eps);
  cm_matvec(logits,s->image,(h->flags&CM_TIED_EMBEDDINGS)?embedding:tensor(s,CM_OUTPUT,CM_GLOBAL_LAYER),s->norm);
  for(uint32_t j=0;j<h->vocab;++j)if(!finite(logits[j]))return CM_NUMERIC_ERROR;
  trace(s,CM_TRACE_LOGITS,h->n_layers,logits,h->vocab); ++s->position;
  return CM_OK;
}
