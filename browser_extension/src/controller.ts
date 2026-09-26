import {PROTOCOL_VERSION,MAX_CANDIDATES,createOpaqueRef,createRequestId,createBatchId,frameHello,frameBatch,isEligibleRequest,normalizeCandidate} from './protocol.ts';
import {createBrowserAdapter,detectBrowserTarget,callApi} from './browserApi.ts';
import {cleanTrackingUrl,loadPreferences} from './preferences.ts';
import {report} from './diagnostics.ts';
export type ConnectionState = 'disconnected'|'reconnecting'|'connected';
type Pending = {resolve:(value:any)=>void;reject:(reason:Error)=>void;timer:ReturnType<typeof setTimeout>;type:string};
export class CaptureController {
  readonly sessionRef=createOpaqueRef('session');
  state:ConnectionState='disconnected';
  lastError='Open MossDL and enable its browser connection.';
  private port:any;
  private connecting:Promise<void>|undefined;
  private pending=new Map<string,Pending>();
  private statsTail:Promise<void>=Promise.resolve();
  constructor(readonly adapter=createBrowserAdapter(detectBrowserTarget()),private hostName='ai.transfer.manager.browser') {}

  private publish(state:ConnectionState):void {
    this.state=state;
    // A closed popup is expected; never overwrite the actual native-host error.
    void callApi(this.adapter,this.adapter.runtime,'sendMessage',{type:'capture_state',state})
      .catch(()=>report('state_notification_unavailable','popup_closed'));
  }

  async connect():Promise<void> {
    if(this.state==='connected' && this.port) return;
    if(this.connecting) return this.connecting;
    this.publish('reconnecting');
    this.connecting=this.open();
    try {await this.connecting;} finally {this.connecting=undefined;}
  }
  private async open():Promise<void> {
    try {
      const port=this.adapter.connectNative(this.hostName);
      this.port=port;
      this.adapter.addListener(port.onMessage,(message:any)=>this.receive(message));
      this.adapter.addListener(port.onDisconnect,()=> {
        const error=this.adapter.runtime?.lastError;
        if(this.port!==port) return;
        this.disconnect(error?.message || 'MossDL disconnected. Open the app and try again.');
      });
      await this.request(frameHello(this.adapter.extensionOrigin(),'',this.sessionRef),'hello_ack');
      this.lastError=''; this.publish('connected');
    } catch(error) {
      this.disconnect(error instanceof Error ? error.message : 'Could not connect to MossDL.');
      throw new Error(this.lastError);
    }
  }
  private disconnect(reason:string):void {
    const port=this.port; this.port=undefined; this.lastError=reason;
    for(const pending of this.pending.values()) {clearTimeout(pending.timer);pending.reject(new Error(reason));}
    this.pending.clear();
    try {port?.disconnect();} catch {report('port_cleanup_failed','already_disconnected');}
    this.publish('disconnected');
  }
  private request(frame:Record<string,unknown>,type:string):Promise<any> {
    return new Promise((resolve,reject)=> {
      if(!this.port) {reject(new Error('MossDL is not connected.'));return;}
      if(this.pending.size>=64) {reject(new Error('MossDL is busy. Wait for current requests to finish.'));return;}
      const id=String(frame.request_id);
      // Bounded acknowledgement deadline, not a delay used to guess readiness.
      const timer=setTimeout(()=> {this.pending.delete(id);reject(new Error('MossDL did not acknowledge the request. Try again in the app.'));},10000);
      this.pending.set(id,{resolve,reject,timer,type});
      try {this.port.postMessage(frame);} catch(error) {
        clearTimeout(timer);this.pending.delete(id);reject(error);
      }
    });
  }
  private receive(message:any):void {
    if(message?.version!==PROTOCOL_VERSION) {report('native_message_rejected','protocol_version');return;}
    const pending=this.pending.get(String(message.request_id));
    if(!pending) {report('native_message_ignored','unknown_request');return;}
    if(message.type!==pending.type && message.type!=='error') {report('native_message_rejected','unexpected_response_type');return;}
    clearTimeout(pending.timer);this.pending.delete(String(message.request_id));
    if(message.type==='error') pending.reject(new Error('MossDL rejected the request. Check the application for details.'));
    else pending.resolve(message);
  }
  async captureMany(items:Record<string,unknown>[]):Promise<number> {
    if(items.length>2048) throw new Error('Select fewer than 2,049 links at a time.');
    const prefs=await loadPreferences(this.adapter);
    const unique=new Map<string,Record<string,unknown>>();
    for(const item of items) {
      if(!isEligibleRequest(item,{explicit:true})) {report('capture_rejected','unsupported_url_or_method');continue;}
      const candidate=normalizeCandidate(item,this.sessionRef);
      candidate.url=cleanTrackingUrl(String(item.url),prefs.cleanTracking);
      unique.set(String(candidate.url),candidate);
    }
    if(!unique.size) throw new Error('No supported HTTP or HTTPS download links were found.');
    await this.connect();
    const candidates=[...unique.values()];let count=0;
    for(let offset=0;offset<candidates.length;offset+=MAX_CANDIDATES) {
      const chunk=candidates.slice(offset,offset+MAX_CANDIDATES);
      const pageUrl=String(chunk[0].page_url || chunk[0].url);
      const frame=frameBatch({request_id:createRequestId(),batch_id:createBatchId(),
        origin:{extension_origin:this.adapter.extensionOrigin(),page_origin:new URL(pageUrl).origin},
        page:{url:pageUrl},session_ref:this.sessionRef,candidates:chunk});
      try {
        const ack=await this.request(frame,'ack');
        if(!['accepted','replayed'].includes(ack.status) || ack.result?.accepted===false) throw new Error('MossDL declined the capture.');
        count+=chunk.length;
      } catch(error) {throw new Error(`${count ? `${count} links were accepted; the remaining links were not confirmed. ` : ''}${error instanceof Error?error.message:'Capture failed.'}`);}
    }
    this.statsTail=this.statsTail.then(async()=> {
      const data=await callApi(this.adapter,this.adapter.storage.local,'get','stats');
      await callApi(this.adapter,this.adapter.storage.local,'set',{stats:{capturedCount:Number(data?.stats?.capturedCount || 0)+count}});
    }).catch(()=>report('stats_update_failed','browser_storage'));
    await this.statsTail;
    return count;
  }
  async syncSession(session:{domain:string;page_url:string;cookies:Record<string,unknown>[]}):Promise<void> {
    await this.connect();
    await this.request({version:PROTOCOL_VERSION,type:'session_sync',request_id:createRequestId(),
      origin:{extension_origin:this.adapter.extensionOrigin(),page_origin:new URL(session.page_url).origin},
      session:{...session,user_agent:globalThis.navigator?.userAgent || ''}},'session_sync_ack');
  }
  async returnChallenge(handoff:Record<string,unknown>):Promise<void> {
    await this.connect();
    const ack=await this.request({version:PROTOCOL_VERSION,type:'challenge_return',request_id:createRequestId(),handoff},'challenge_return_ack');
    if(ack.result?.outcome!=='accepted') throw new Error('MossDL rejected or expired this browser handoff.');
  }
}
