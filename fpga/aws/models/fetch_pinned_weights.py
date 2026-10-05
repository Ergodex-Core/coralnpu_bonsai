"""Pinned Q8_0 materialization; no credentials or unapproved redirects."""
import datetime, hashlib, json, os, urllib.error, urllib.parse, urllib.request
from pathlib import Path

MODELS = {
    'q8': ('Qwen/Qwen3-0.6B-GGUF', '23749fefcc72300e3a2ad315e1317431b06b590a',
           'Qwen3-0.6B-Q8_0.gguf', 639446688,
           '9465e63a22add5354d9bb4b99e90117043c7124007664907259bd16d043bb031'),
    'pq2': ('prism-ml/Ternary-Bonsai-1.7B-gguf', '983b5dec2ff16aab79990711ba0f828a499a7e6a',
            'Ternary-Bonsai-1.7B-PQ2_0.gguf', 463290464,
            'de68ba48a8dacb21979915991e7741b917869d71410a370df951c0c3a237ae50')
}
HOSTS = {'huggingface.co', 'us.aws.cdn.hf.co'}
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None
OPENER = urllib.request.build_opener(NoRedirect)
def request(url, method='GET'):
    hops = []
    for _ in range(5):
        p = urllib.parse.urlsplit(url)
        if p.scheme != 'https' or p.hostname not in HOSTS:
            raise RuntimeError('Unapproved redirect host: ' + str(p.hostname))
        try: r = OPENER.open(urllib.request.Request(url, method=method), timeout=60)
        except urllib.error.HTTPError as error: r = error
        hops.append({'host': p.hostname, 'path': p.path, 'status': r.status})
        if r.status in (301, 302, 303, 307, 308):
            url = urllib.parse.urljoin(url, r.headers['Location']); r.close(); continue
        if r.status != 200:
            status = r.status; r.close(); raise RuntimeError('HTTP ' + str(status))
        return r, hops
    raise RuntimeError('Redirect limit')
def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('model',choices=MODELS)
    parser.add_argument('output',type=Path)
    args=parser.parse_args()
    ROOT=args.output.resolve()
    REPO,REV,NAME,SIZE,SHA=MODELS[args.model]
    ROOT.mkdir(parents=True,exist_ok=True)
    dest = ROOT / NAME
    rec = {'repo': REPO, 'revision': REV, 'file': NAME,
           'utc': datetime.datetime.now(datetime.timezone.utc).isoformat()}
    if dest.exists():
        with dest.open('rb') as f: digest = hashlib.file_digest(f, 'sha256').hexdigest()
        if dest.stat().st_size != SIZE or digest != SHA: raise RuntimeError('Existing file mismatch')
        print('Already verified complete; no network retry'); return
    response, rec['metadata_hops'] = request(f'https://huggingface.co/api/models/{REPO}/revision/{REV}')
    with response: raw = response.read(4*1024*1024)
    metadata = json.loads(raw)
    if metadata.get('sha') != REV: raise RuntimeError('Revision mismatch')
    (ROOT/'hub-metadata.json').write_bytes(raw)
    rec['license'] = metadata.get('cardData', {}).get('license')
    if rec['license'] != 'apache-2.0': raise RuntimeError('Pinned model license metadata unexpected')
    url = f'https://huggingface.co/{REPO}/resolve/{REV}/{NAME}'
    response, rec['head_hops'] = request(url, 'HEAD')
    with response: rec['content_length'] = int(response.headers.get('Content-Length', 0))
    if rec['content_length'] != SIZE: raise RuntimeError('HEAD size mismatch')
    print(json.dumps({'head': rec['head_hops'], 'bytes': SIZE}), flush=True)
    response, rec['download_hops'] = request(url)
    h = hashlib.sha256(); size = 0
    with response, (ROOT/(NAME+'.part')).open('wb') as f:
        while data := response.read(4*1024*1024):
            f.write(data); h.update(data); size += len(data)
    if (size, h.hexdigest()) != (SIZE, SHA): raise RuntimeError('Complete download hash/size mismatch')
    os.replace(ROOT/(NAME+'.part'), dest)
    rec.update(bytes=size, sha256=h.hexdigest(), status='verified complete')
    for name in ('README.md', 'LICENSE'):
        if name in {entry['rfilename'] for entry in metadata['siblings']}:
            response, hops = request(f'https://huggingface.co/{REPO}/resolve/{REV}/{name}')
            with response: (ROOT/name).write_bytes(response.read(4*1024*1024))
    (ROOT/'download-receipt.json').write_text(json.dumps(rec, indent=2)+'\n')
    print(json.dumps(rec), flush=True)
if __name__ == '__main__': main()
