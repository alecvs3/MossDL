import {createBrowserAdapter, callApi} from './browserApi.ts';
export interface Preferences {mediaOverlays:boolean;interceptDownloads:boolean;cleanTracking:boolean;streamSniffer:boolean}
export const DEFAULT_PREFERENCES: Preferences = {mediaOverlays:true,interceptDownloads:false,cleanTracking:true,streamSniffer:false};
export async function loadPreferences(adapter = createBrowserAdapter()): Promise<Preferences> {
  const data = await callApi(adapter,adapter.storage.local,'get','preferences');
  const prefs = {...DEFAULT_PREFERENCES};
  for(const key of Object.keys(prefs) as (keyof Preferences)[]) {
    if(typeof data?.preferences?.[key] === 'boolean') prefs[key] = data.preferences[key];
  }
  return prefs;
}
export function cleanTrackingUrl(raw:string,enabled:boolean):string {
  const url = new URL(raw);
  if(!['https:','http:'].includes(url.protocol) || url.username || url.password) throw new Error('Use an HTTP or HTTPS link without embedded credentials.');
  // Signed URLs must remain byte-for-byte intact, including order and encoding.
  if(!enabled || [...url.searchParams.keys()].some(key=>/^(sig(nature)?|token|auth|expires|x-amz-|x-goog-)/i.test(key))) return raw;
  const removed=[...url.searchParams.keys()].filter(key=>/^(utm_|fbclid$|gclid$)/i.test(key));
  if(!removed.length) return raw;
  for(const key of removed) url.searchParams.delete(key);
  return url.href;
}
