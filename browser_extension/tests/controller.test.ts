import {test} from 'node:test';
import assert from 'node:assert/strict';
import {CaptureController} from '../src/controller.ts';
import {installBackgroundCapture} from '../src/background.ts';
import {cleanTrackingUrl} from '../src/preferences.ts';
const event=()=>({listeners:[] as Function[],addListener(fn:Function){this.listeners.push(fn);},emit(...args:any[]){for(const fn of this.listeners) fn(...args);}});
function fixture(options:{offline?:boolean;reject?:boolean;autoHello?:boolean;preferences?:object}={}) {
  const frames:any[]=[];const order:string[]=[];const messages=event(),disconnect=event();
  const data:any={preferences:options.preferences || {},stats:{capturedCount:0}};
  const api:any={runtime:{id:'test-extension',getURL:(path:string)=>'chrome-extension://test-extension/'+path,lastError:undefined,
    sendMessage:(_message:any,cb:Function)=>cb(),onMessage:event(),onInstalled:event()},
    storage:{local:{get:(key:string,cb:Function)=>cb({[key]:data[key]}),set:(value:any,cb:Function)=>{Object.assign(data,value);cb();}},onChanged:event()},
    tabs:{onRemoved:event(),onUpdated:event()},webRequest:{onBeforeRequest:event()},
    downloads:{onCreated:event(),search:(_q:any,cb:Function)=>cb([{state:'in_progress'}]),cancel:(_id:number,cb:Function)=>{order.push('cancel');cb();}},
    notifications:{create:(_value:any,cb:Function)=>cb('n')},contextMenus:{removeAll:(cb:Function)=>cb(),create:()=>{},onClicked:event()},
    extensionOrigin:()=> 'chrome-extension://test-extension',addListener:(evt:any,fn:Function)=>evt?.addListener(fn)};
  const reply=(frame:any,type:string,result:any={})=>messages.emit({version:'browser-capture/1',type,request_id:frame.request_id,batch_id:frame.batch_id,status:'accepted',result});
  api.connectNative=()=> {
    if(options.offline) throw new Error('Native host not found');
    return {onMessage:messages,onDisconnect:disconnect,disconnect:()=>{},postMessage:(frame:any)=> {
      frames.push(frame);
      queueMicrotask(()=> {
        if(frame.type==='hello') {if(options.autoHello!==false) reply(frame,'hello_ack');}
        else if(options.reject) reply(frame,'error');
        else {order.push('ack');reply(frame,frame.type==='candidate_batch'?'ack':frame.type+'_ack',frame.type==='challenge_return'?{outcome:'accepted'}:{accepted:true});}
      });
    }};
  };
  return {api,frames,order,data,messages,disconnect,reply,controller:new CaptureController(api)};
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
test('delivery waits for native handshake, then acknowledgement',async()=> {
  const f=fixture({autoHello:false});const capture=f.controller.captureMany([{url:'https://example.com/file.zip'}]);
  await tick();assert.deepEqual(f.frames.map(f=>f.type),['hello']);
  f.reply(f.frames[0],'hello_ack');assert.equal(await capture,1);assert.equal(f.data.stats.capturedCount,1);
});
test('signed URLs are preserved exactly while optional tracking cleanup works',()=> {
  const signed='https://example.com/file?sig=A%2fb&x=1&utm_source=test';assert.equal(cleanTrackingUrl(signed,true),signed);
  assert.equal(cleanTrackingUrl('https://example.com/file?utm_source=test&keep=yes',true),'https://example.com/file?keep=yes');
});
test('embedded credentials and non-web protocols are rejected',()=> {
  assert.throws(()=>cleanTrackingUrl('https://user:secret@example.com/file',true));
  assert.throws(()=>cleanTrackingUrl('file:///private',true));
});
test('native denial is an error and never increments accepted statistics',async()=> {
  const f=fixture({reject:true});await assert.rejects(f.controller.captureMany([{url:'https://example.com/file.zip'}]));assert.equal(f.data.stats.capturedCount,0);
});
test('offline capture fails visibly instead of pretending to queue',async()=> {
  const f=fixture({offline:true});await assert.rejects(f.controller.captureMany([{url:'https://example.com/file.zip'}]),/Native host/);assert.equal(f.controller.state,'disconnected');
});
test('large pages are split into bounded batches without dropping files',async()=> {
  const f=fixture();assert.equal(await f.controller.captureMany(Array.from({length:260},(_,i)=>({url:`https://example.com/${i}.zip`}))),260);
  assert.deepEqual(f.frames.filter(x=>x.type==='candidate_batch').map(x=>x.candidates.length),[128,128,4]);
});
test('duplicate links are sent only once',async()=> {
  const f=fixture();assert.equal(await f.controller.captureMany([{url:'https://example.com/a'},{url:'https://example.com/a'}]),1);
});
test('session sharing waits for readiness and acknowledgement',async()=> {
  const f=fixture({autoHello:false});const share=f.controller.syncSession({domain:'example.com',page_url:'https://example.com/',cookies:[]});
  await tick();assert.equal(f.frames.length,1);f.reply(f.frames[0],'hello_ack');await share;assert.equal(f.frames[1].type,'session_sync');
});
test('disconnect rejects pending work',async()=> {
  const f=fixture({autoHello:false});const connecting=f.controller.connect();f.disconnect.emit();await assert.rejects(connecting,/disconnected/);
});
test('download capture is opt-in',async()=> {
  const f=fixture();installBackgroundCapture(f.controller);await tick();f.api.downloads.onCreated.emit({id:1,url:'https://example.com/a.zip'});await tick();
  assert.equal(f.frames.filter(x=>x.type==='candidate_batch').length,0);assert.deepEqual(f.order,[]);
});
test('offline handoff never cancels browser download',async()=> {
  const f=fixture({offline:true,preferences:{interceptDownloads:true}});installBackgroundCapture(f.controller);await tick();
  f.api.downloads.onCreated.emit({id:1,url:'https://example.com/a.zip'});await tick();assert.ok(!f.order.includes('cancel'));
});
test('browser download is cancelled only after native acknowledgement',async()=> {
  const f=fixture({preferences:{interceptDownloads:true}});installBackgroundCapture(f.controller);await tick();
  f.api.downloads.onCreated.emit({id:1,url:'https://example.com/a.zip'});await tick();assert.deepEqual(f.order,['ack','cancel']);
});
test('web content cannot invoke privileged session sharing',async()=> {
  const f=fixture();installBackgroundCapture(f.controller);await tick();let response:any;
  f.api.runtime.onMessage.emit({type:'sync_session',page_url:'https://other.example/'},{id:'test-extension',url:'https://example.com/',tab:{id:1}},(value:any)=>response=value);
  assert.equal(response.success,false);assert.equal(f.frames.filter(x=>x.type==='session_sync').length,0);
});
