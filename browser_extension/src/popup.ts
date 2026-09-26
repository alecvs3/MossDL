import {createBrowserAdapter,callApi} from './browserApi.ts';
import {loadPreferences,type Preferences} from './preferences.ts';
import {report} from './diagnostics.ts';
const api=createBrowserAdapter();
function element<T extends HTMLElement>(id:string):T {return document.getElementById(id) as T;}
function show(message:string):void {const box=element('toast-message');box.textContent=message;box.style.display='block';}
function connection(state:string):void {
  const pill=element('connection-status');pill.className=`status status-${state}`;
  element('connection-label').textContent=state==='connected'?'Connected':state==='reconnecting'?'Connecting…':'Offline';
  element('stat-bridge-status').textContent=state==='connected'?'Connected':'Open MossDL to connect';
}
async function request(message:Record<string,unknown>):Promise<any> {
  const response=await callApi(api,api.runtime,'sendMessage',message);
  if(!response?.success) throw new Error(response?.message || 'MossDL did not respond.');
  return response;
}
async function activeTab():Promise<any> {
  const tabs=await callApi(api,api.tabs,'query',{active:true,currentWindow:true});
  const tab=tabs?.[0];
  if(typeof tab?.id!=='number' || !/^https?:/.test(String(tab.url))) throw new Error('Open a regular web page first. Browser settings and store pages cannot be captured.');
  return tab;
}
async function refreshState():Promise<void> {
  const state=await request({type:'get_state'});connection(state.state);
  element('stat-captured-count').textContent=String(state.capturedCount || 0);
}
function action(id:string,run:()=>Promise<void>):void {
  element<HTMLButtonElement>(id).addEventListener('click',async()=> {
    const button=element<HTMLButtonElement>(id);button.disabled=true;
    try {await run();} catch(error) {show(error instanceof Error?error.message:'Action failed.');}
    finally {button.disabled=false;}
  });
}
async function initPopup():Promise<void> {
  const prefs=await loadPreferences(api);
  const fields:Record<keyof Preferences,string>={mediaOverlays:'pref-media-overlays',interceptDownloads:'pref-intercept-downloads',cleanTracking:'pref-clean-tracking',streamSniffer:'pref-stream-sniffer'};
  for(const [key,id] of Object.entries(fields)) {
    const input=element<HTMLInputElement>(id);input.checked=prefs[key as keyof Preferences];
    input.addEventListener('change',async()=> {
      const updated=Object.fromEntries(Object.entries(fields).map(([name,field])=>[name,element<HTMLInputElement>(field).checked]));
      try {await callApi(api,api.storage.local,'set',{preferences:updated});show('Preferences saved.');}
      catch {input.checked=!input.checked;show('Could not save preferences. Try again.');}
    });
  }
  api.addListener(api.runtime.onMessage,(message:any)=>{if(message?.type==='capture_state') connection(message.state);});
  await refreshState();
  try {
    const tab=await activeTab();element('page-host').textContent=new URL(tab.url).hostname;
    const info=await callApi(api,api.tabs,'sendMessage',tab.id,{type:'inspect_tab'},{frameId:0});
    element('scan-badge').textContent=String(info?.downloadLinksCount || 0);
    element('media-badge').textContent=String(info?.mediaElementsCount || 0);
  } catch {element('page-host').textContent='Reload a web page to capture';}
  const reconnect=async()=> {connection('reconnecting');try {await request({type:'reconnect'});show('Connected to MossDL.');} finally {await refreshState();}};
  action('connection-status',reconnect);action('btn-open-desktop',reconnect);
  action('btn-scan-page',async()=> {
    const tab=await activeTab();show('Sending links to MossDL…');
    const result=await callApi(api,api.tabs,'sendMessage',tab.id,{type:'scan_and_capture'},{frameId:0});
    if(!result?.success) throw new Error(result?.message || 'Reload this page to enable capture.');
    show(`${result.capturedCount} links accepted by MossDL.`);await refreshState();
  });
  action('btn-capture-media',async()=> {
    const tab=await activeTab();
    const detected=await request({type:'get_streams',tabId:tab.id});
    let result;
    if(detected.candidates?.length) result=await request({type:'capture_batch',candidates:detected.candidates});
    else result=await callApi(api,api.tabs,'sendMessage',tab.id,{type:'capture_active_media'},{frameId:0});
    if(!result?.success) throw new Error(result?.message || 'No direct media URL found. Enable Find streams, play the media, then try again.');
    show('Media accepted by MossDL.');await refreshState();
  });
  action('btn-sync-session',async()=> {
    const tab=await activeTab();const host=new URL(tab.url).hostname;
    if(!window.confirm(`Share ${host}'s cookies with the local MossDL app? They can allow the app to use your signed-in session. Share only with your own trusted installation.`)) return;
    show('Sharing site session…');await request({type:'sync_session',page_url:tab.url,store_id:tab.cookieStoreId});
    show('Site session accepted by MossDL. Only cookies supported by the app are retained.');
  });
}
if(typeof document!=='undefined') document.addEventListener('DOMContentLoaded',()=>{
  void initPopup().catch(()=>{report('popup_initialization_failed','browser_api');show('Could not initialize MossDL. Reopen this popup or reload the extension.');});
});
