import {test,expect,chromium} from '@playwright/test';
import {resolve,dirname,basename} from 'node:path';
import {mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
test('real Chromium extension loads, popup works, disabled capture leaves browser alone',async()=> {
  const directory=await mkdtemp(resolve(tmpdir(),'mossdl-extension-test-'));
  const extension=resolve('build/chrome');
  const context=await chromium.launchPersistentContext(directory,{channel:'chromium',headless:true,args:[`--disable-extensions-except=${extension}`,`--load-extension=${extension}`]});
  try {
    const worker=context.serviceWorkers()[0] || await context.waitForEvent('serviceworker');
    const id=new URL(worker.url()).host;
    const popup=await context.newPage();const errors:string[]=[];
    popup.on('pageerror',error=>errors.push(error.message));
    await popup.goto(`chrome-extension://${id}/popup.html`);
    await expect(popup.locator('#page-host')).not.toHaveText('—');
    await expect(popup.locator('#pref-intercept-downloads')).not.toBeChecked();
    await expect(popup.locator('#pref-stream-sniffer')).not.toBeChecked();
    await popup.locator('label[for="pref-media-overlays"]').click();
    await expect(popup.locator('#pref-media-overlays')).not.toBeChecked();
    await expect(popup.getByRole('status')).toHaveText('Preferences saved.');
    await popup.reload();await expect(popup.locator('#pref-media-overlays')).not.toBeChecked();
    await popup.locator('#btn-scan-page').click();
    await expect(popup.getByRole('status')).toContainText('Open a regular web page');
    expect(errors).toEqual([]);
    await popup.reload();
    await expect(popup.locator('#page-host')).not.toHaveText('—');
    await popup.setViewportSize({width:1280,height:800});
    // Capture real extension controls. Native host is absent in this isolated profile.
    await popup.screenshot({path:'release/listing/chrome-popup.png'});
  } finally {
    await context.close();
    if(dirname(directory)!==resolve(tmpdir()) || !basename(directory).startsWith('mossdl-extension-test-')) throw new Error('Unsafe test-profile cleanup path');
    await rm(directory,{recursive:true,force:true});
  }
});
