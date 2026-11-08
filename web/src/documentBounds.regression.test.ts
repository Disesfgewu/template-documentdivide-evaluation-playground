import { it, expect, vi } from 'vitest';
import { uploadDocument, downloadDocument, readBoundedStream } from './documents';
const id='11111111-1111-1111-1111-111111111111';
const token='secret-canary';
const pending = Symbol('pending');
const within = <T,>(p: Promise<T>) => Promise.race([p, new Promise<typeof pending>(r=>setTimeout(()=>r(pending),100))]);
it('upload deadline covers headers plus stalled body', async()=>{
 const res = await within(uploadDocument({projectId:id, token, file:new File(['%PDF-1.4'], 'a.pdf'),timeoutMs:10,fetchFn:vi.fn(async()=>new Response(new ReadableStream({pull(){return new Promise(()=>{});}}),{status:201})) as typeof fetch}));
 expect(res).not.toBe(pending); if(res!==pending){expect(res.ok).toBe(false);if(!res.ok)expect(res.error.code).toBe('timeout');}
});
it('abort does not await a stalled stream cancel promise',async()=>{
 const ac=new AbortController();const body=new ReadableStream<Uint8Array>({pull(){return new Promise(()=>{});},cancel(){return new Promise(()=>{});}});
 const p=readBoundedStream(body,new Response(null),100,ac.signal).catch(()=> 'aborted');ac.abort();expect(await within(p)).toBe('aborted');
});
it('server errors never display the supplied token',async()=>{
 for(const fn of ['upload','download']){
 const fetchFn=vi.fn(async()=>new Response(JSON.stringify({error:{message:`oops ${token}`,code:token,details:{token}}}),{status:400})) as typeof fetch;
 const res=await(fn==='upload'? uploadDocument({projectId:id,token,file:new File(['%PDF-1.4'],'a.pdf'),fetchFn}):downloadDocument({documentId:id,token,fetchFn}));
 expect(JSON.stringify(res)).not.toContain(token);
 }
});
it('oversize and invalid-media cancellation never block completion',async()=>{
 for(const contentType of ['application/pdf','text/html']){
 const body=new ReadableStream<Uint8Array>({start(c){c.enqueue(new Uint8Array(20));},cancel(){return new Promise(()=>{});}});
 const res=await within(downloadDocument({documentId:id,token,maxBytes:10,timeoutMs:10,fetchFn:vi.fn(async()=>new Response(body,{headers:{'Content-Type':contentType}})) as typeof fetch}));expect(res).not.toBe(pending);
 }
});
it('non-streaming responses fail closed without calling buffering fallbacks',async()=>{
 const blob=vi.fn(()=>{throw new Error('unbounded blob');});
 const fake={ok:true,status:200,headers:new Headers({'Content-Type':'application/pdf'}),body:undefined,blob} as unknown as Response;
 const res=await downloadDocument({documentId:id,token,fetchFn:vi.fn(async()=>fake) as typeof fetch});expect(res.ok).toBe(false);expect(blob).not.toHaveBeenCalled();
});
