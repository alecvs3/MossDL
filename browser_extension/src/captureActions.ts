import {callApi} from './browserApi.ts';
import {CaptureController} from './controller.ts';
import {report} from './diagnostics.ts';
export async function notify(controller:CaptureController,message:string):Promise<void> {
  const api=controller.adapter;
  try {await callApi(api,api.notifications,'create',{type:'basic',iconUrl:api.runtime.getURL('icons/icon-48.png'),title:'MossDL',message});}
  catch {report('notification_failed','browser_denied');}
}
export async function shareSession(controller:CaptureController,pageUrl:string,storeId?:string) {
  const url=new URL(pageUrl);
  if(!['http:','https:'].includes(url.protocol)) throw new Error('Session sharing is available only on web pages.');
  const api=controller.adapter;
  const cookies=await callApi(api,api.cookies,'getAll',{url:pageUrl,...(storeId?{storeId}:{})});
  const scoped=(cookies || []).filter((cookie:any)=> {
    const domain=String(cookie.domain).replace(/^\./,'').toLowerCase();
    return url.hostname===domain || url.hostname.endsWith('.'+domain);
  }).map((c:any)=>({name:c.name,value:c.value,domain:c.domain,path:c.path,secure:c.secure,httpOnly:c.httpOnly,expirationDate:c.expirationDate}));
  await controller.syncSession({domain:url.hostname,page_url:pageUrl,cookies:scoped});
  return {success:true,count:scoped.length,domain:url.hostname};
}
export async function sendToTab(controller:CaptureController,tab:any,type:string) {
  if(typeof tab?.id!=='number' || !/^https?:/.test(String(tab.url))) throw new Error('Open a regular web page and try again.');
  try {
    const response=await callApi(controller.adapter,controller.adapter.tabs,'sendMessage',tab.id,{type},{frameId:0});
    if(!response?.success) throw new Error(response?.message || 'No supported downloads found.');
    return response;
  } catch(error) {throw new Error(error instanceof Error?error.message:'Reload the page to enable MossDL capture.');}
}
export function registerMenus(controller:CaptureController):void {
  const api=controller.adapter;
  void callApi(api,api.contextMenus,'removeAll').then(()=> {
    api.contextMenus.create({id:'tm-parent',title:'MossDL',contexts:['all']});
    for(const [id,title,contexts] of [
      ['tm-download-link','Send link to MossDL',['link']],
      ['tm-download-selection','Send selected links',['selection']],
      ['tm-capture-all-links','Find downloads on this page',['page']],
      ['tm-capture-media','Grab media',['page','video','audio','image']],
      ['tm-capture-cookies',"Share this site's login with MossDL",['page','action']],
    ] as const) api.contextMenus.create({id,parentId:'tm-parent',title,contexts:[...contexts]});
  }).catch(()=>report('menu_registration_failed','browser_api'));
}
export async function menuAction(controller:CaptureController,info:any,tab:any) {
  const api=controller.adapter;
  switch(info.menuItemId) {
    case 'tm-download-link': return `${await controller.captureMany([{url:info.linkUrl,pageUrl:tab?.url}])} link accepted by MossDL.`;
    case 'tm-download-selection': {
      const urls=String(info.selectionText || '').match(/https?:\/\/[^\s"'<>]+/gi) || [];
      return `${await controller.captureMany(urls.map(url=>({url,pageUrl:tab?.url})))} links accepted by MossDL.`;
    }
    case 'tm-capture-all-links': return `${(await sendToTab(controller,tab,'scan_and_capture')).capturedCount} links accepted by MossDL.`;
    case 'tm-capture-media': {
      if(info.srcUrl && /^https?:/.test(info.srcUrl)) await controller.captureMany([{url:info.srcUrl,pageUrl:tab?.url,type:'media'}]);
      else await sendToTab(controller,tab,'capture_active_media');
      return 'Media accepted by MossDL.';
    }
    case 'tm-capture-cookies': {
      // Explicit in-page confirmation also explains that cookies grant session access.
      const res=await callApi(api,api.tabs,'sendMessage',tab.id,{type:'confirm_session_share'},{frameId:0});
      if(!res?.confirmed) return 'Session sharing cancelled.';
      await shareSession(controller,tab.url,tab.cookieStoreId);return 'Site session accepted by MossDL.';
    }
    default: throw new Error('Unknown MossDL action.');
  }
}
