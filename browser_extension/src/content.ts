import {createBrowserAdapter,callApi} from './browserApi.ts';
import {loadPreferences} from './preferences.ts';
import {report} from './diagnostics.ts';
import {KNOWN_HOSTER_DOMAINS} from './sources.ts';
const api=createBrowserAdapter();
const ATTRIBUTE='data-mossdl-capture';
const FILE=/\.(zip|rar|7z|tar|gz|xz|bz2|iso|dmg|exe|msi|apk|bin|mp4|mkv|webm|avi|mov|mp3|flac|wav|aac|ogg|m3u8|mpd|pdf|torrent)$/i;
function valid(raw:string):boolean {try{return ['http:','https:'].includes(new URL(raw).protocol);}catch{return false;}}
function links():string[] {
  const direct=new Set<string>();const external=new Set<string>();
  for(const anchor of document.querySelectorAll<HTMLAnchorElement>('a[href]')) {
    if(!valid(anchor.href)) continue;
    const url=new URL(anchor.href);
    if(anchor.hasAttribute('download') || FILE.test(url.pathname) || [...KNOWN_HOSTER_DOMAINS].some(host=>url.hostname===host || url.hostname.endsWith('.'+host))) direct.add(url.href);
    else if(url.hostname!==location.hostname) external.add(url.href);
  }
  return [...(direct.size?direct:external)];
}
async function capture(urls:string[]):Promise<any> {
  if(!urls.length) throw new Error('No direct HTTP/HTTPS download links found.');
  const result=await callApi(api,api.runtime,'sendMessage',{type:'capture_batch',candidates:urls.map(url=>({url,method:'GET',documentUrl:location.href,download:true}))});
  if(!result?.success) throw new Error(result?.message || 'MossDL did not acknowledge the capture.');
  return result;
}
function media():HTMLMediaElement[] {return [...document.querySelectorAll<HTMLMediaElement>('video,audio')];}
function mediaUrl():string|undefined {
  const elements=media();const active=elements.find(item=>!item.paused) || elements[0];
  const url=active?.currentSrc || active?.src;
  return url && valid(url)?url:undefined;
}
api.addListener(api.runtime.onMessage,(message:any,sender:any,respond:(data:any)=>void)=> {
  if(sender.id!==api.runtime.id || !message || !['inspect_tab','scan_and_capture','capture_active_media','confirm_session_share'].includes(message.type)) return false;
  void (async()=> {
    if(message.type==='inspect_tab') return {success:true,mediaElementsCount:media().length,downloadLinksCount:links().length};
    if(message.type==='confirm_session_share') return {confirmed:window.confirm(`Share ${location.hostname}'s cookies with the local MossDL app? They can grant access to your signed-in session.`)};
    if(message.type==='scan_and_capture') return await capture(links());
    const url=mediaUrl();if(!url) throw new Error('No direct media URL. Enable Find streams, play the media, and use Grab media in the popup.');
    return await capture([url]);
  })().then(respond,error=>respond({success:false,message:error instanceof Error?error.message:'Capture failed.'}));
  return true;
});
const overlays=new Map<HTMLMediaElement,HTMLButtonElement>();
let observer:MutationObserver|undefined;
let scheduled=false;
export function installMediaOverlay():void {
  for(const [element,button] of overlays) if(!element.isConnected) {button.remove();overlays.delete(element);}
  for(const element of media()) {
    if(overlays.has(element)) continue;
    const button=document.createElement('button');button.type='button';button.textContent='Send media to MossDL';button.setAttribute(ATTRIBUTE,'');
    Object.assign(button.style,{display:'block',padding:'5px 10px',font:'12px system-ui',color:'#e8f3ec',background:'#182b24',border:'1px solid #6e8062',borderRadius:'4px',cursor:'pointer'});
    button.addEventListener('click',async(event)=> {
      event.preventDefault();event.stopPropagation();button.disabled=true;
      try {await capture([element.currentSrc || element.src]);button.textContent='Accepted by MossDL';}
      catch(error) {button.textContent=error instanceof Error?error.message:'Capture failed. Try again.';}
      finally {button.disabled=false;}
    });
    element.insertAdjacentElement('afterend',button);overlays.set(element,button);
  }
}
async function updateOverlays():Promise<void> {
  const prefs=await loadPreferences(api);observer?.disconnect();observer=undefined;
  if(!prefs.mediaOverlays) {for(const button of overlays.values()) button.remove();overlays.clear();return;}
  installMediaOverlay();
  observer=new MutationObserver(()=> {
    if(scheduled) return;scheduled=true;
    requestAnimationFrame(()=>{scheduled=false;if(observer) installMediaOverlay();});
  });
  observer.observe(document.documentElement,{childList:true,subtree:true});
}
api.addListener(api.storage.onChanged,(changes:any,area:string)=> {if(area==='local' && changes.preferences) void updateOverlays().catch(()=>report('overlay_update_failed','browser_storage'));});
void updateOverlays().catch(()=>report('overlay_initialization_failed','browser_storage'));
