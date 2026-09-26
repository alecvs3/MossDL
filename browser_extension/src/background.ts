import {CaptureController} from './controller.ts';
import {callApi} from './browserApi.ts';
import {loadPreferences,DEFAULT_PREFERENCES, type Preferences} from './preferences.ts';
import {isMediaCandidate} from './protocol.ts';
import {report} from './diagnostics.ts';
import {menuAction,notify,registerMenus,shareSession} from './captureActions.ts';
export {CaptureController} from './controller.ts';

export function installBackgroundCapture(controller=new CaptureController()):CaptureController {
  const api=controller.adapter;
  let preferences:Preferences={...DEFAULT_PREFERENCES};
  const streams=new Map<number,Map<string,Record<string,unknown>>>();
  const ready=loadPreferences(api).then(value=>{preferences=value;}).catch(()=>report('preferences_unavailable','defaults_used'));
  api.addListener(api.storage.onChanged,(changes:any,area:string)=> {
    if(area==='local' && changes.preferences) {
      void loadPreferences(api).then(value=>{preferences=value;if(!value.streamSniffer) streams.clear();}).catch(()=>report('preferences_unavailable','change_event'));
    }
  });
  api.addListener(api.tabs.onRemoved,(id:number)=>streams.delete(id));
  api.addListener(api.tabs.onUpdated,(id:number,change:any)=>{if(change.status==='loading') streams.delete(id);});
  api.addListener(api.webRequest.onBeforeRequest,(details:any)=> {
    if(!preferences.streamSniffer || details.tabId<0 || !isMediaCandidate({...details,type:''})) return;
    const tabStreams=streams.get(details.tabId) || new Map();
    if(tabStreams.size>=128) {report('stream_candidate_skipped','tab_limit');return;}
    tabStreams.set(details.url,{url:details.url,documentUrl:details.documentUrl,method:details.method,type:'media'});
    streams.set(details.tabId,tabStreams);
  },{urls:['http://*/*','https://*/*']});

  api.addListener(api.downloads.onCreated,(details:any)=> {
    void (async()=> {
      await ready;
      // Never cancel or copy downloads when capture is off or the native app is unavailable.
      if(!preferences.interceptDownloads || !/^https?:/.test(String(details.finalUrl || details.url))) return;
      try {
        await controller.captureMany([{...details,url:details.finalUrl || details.url,method:'GET',download:true}]);
        const current=await callApi(api,api.downloads,'search',{id:details.id});
        if(current?.[0]?.state==='in_progress') await callApi(api,api.downloads,'cancel',details.id);
        await notify(controller,'Download accepted by MossDL. The browser history entry is kept.');
      } catch {report('download_handoff_failed','browser_download_preserved');await notify(controller,'MossDL could not accept this download. The browser download was left untouched.');}
    })();
  });
  registerMenus(controller);
  api.addListener(api.runtime.onInstalled,()=>registerMenus(controller));
  api.addListener(api.contextMenus.onClicked,(info:any,tab:any)=> {
    void menuAction(controller,info,tab).then(message=>notify(controller,message))
      .catch(error=>notify(controller,error instanceof Error?error.message:'Capture failed.'));
  });
  api.addListener(api.runtime.onMessage,(message:any,sender:any,respond:(data:any)=>void)=> {
    if(!message || typeof message.type!=='string' || sender.id!==api.runtime.id) return false;
    if(message.type==='capture_state') return false;
    // Privileged controls can only originate in our own extension pages.
    const privileged=['get_state','reconnect','sync_session','open_desktop','get_streams'];
    const extensionPage=String(sender.url || '').startsWith(api.runtime.getURL(''));
    if(privileged.includes(message.type) && !extensionPage) {
      respond({success:false,message:'This action must be started from the MossDL popup.'});return false;
    }
    void (async()=> {
      await ready;
      switch(message.type) {
        case 'get_state': {
          const data=await callApi(api,api.storage.local,'get','stats');
          return {success:true,state:controller.state,lastError:controller.lastError,capturedCount:Number(data?.stats?.capturedCount || 0)};
        }
        case 'open_desktop':
        case 'reconnect': await controller.connect();return {success:true,state:controller.state};
        case 'get_streams': return {success:true,candidates:[...(streams.get(Number(message.tabId))?.values() || [])]};
        case 'capture_candidate':
        case 'capture_batch': {
          const raw=message.type==='capture_batch'?message.candidates:[message.details];
          if(!Array.isArray(raw) || raw.some(item=>!item || typeof item!=='object')) throw new Error('Invalid capture request.');
          const pageUrl=sender.tab && !extensionPage ? sender.url : undefined;
          const count=await controller.captureMany(raw.map(item=>({...item,...(pageUrl?{documentUrl:pageUrl,pageUrl,originUrl:pageUrl}:{})})));
          return {success:true,status:'captured',capturedCount:count,count};
        }
        case 'sync_session': return await shareSession(controller,String(message.page_url),message.store_id);
        case 'challenge_solved': {
          if(!sender.tab || typeof message.ticket!=='string' || typeof message.token!=='string') throw new Error('Invalid browser handoff.');
          const origin=new URL(sender.url).origin;
          const cookies=await callApi(api,api.cookies,'getAll',{url:origin,...(sender.tab.cookieStoreId?{storeId:sender.tab.cookieStoreId}:{})});
          const host=new URL(origin).hostname;
          const scoped=(cookies || []).filter((c:any)=>{const domain=String(c.domain).replace(/^\./,'');return host===domain || host.endsWith('.'+domain);}).slice(0,50);
          await controller.returnChallenge({version:'mossdl-handoff/1',ticket:message.ticket,challenge_id:String(message.challengeId),generation:Number(message.generation),origin,profile:'default',token:message.token.slice(0,8192),cookies:scoped,user_agent:navigator.userAgent});
          return {success:true,status:'returned'};
        }
        default: throw new Error('Unknown extension request.');
      }
    })().then(respond,error=>{report('action_failed',message.type);respond({success:false,message:error instanceof Error?error.message:'MossDL action failed.'});});
    return true;
  });
  // Keep listeners registered synchronously, then attempt the native handshake.
  void ready.then(()=>controller.connect()).catch(()=>report('native_connection_unavailable','open_app_and_reconnect'));
  return controller;
}
if((globalThis as any).chrome?.runtime?.onStartup || (globalThis as any).browser?.runtime?.onStartup) installBackgroundCapture();
