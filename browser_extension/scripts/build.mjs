import {build} from 'esbuild';
import {mkdir,readFile,writeFile,copyFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';
import {readdir} from 'node:fs/promises';
const root=new URL('../',import.meta.url);
const config=JSON.parse(await readFile(new URL('build.config.json',root),'utf8'));
const common=new URL('build/common/',root);await mkdir(common,{recursive:true});
for(const entry of ['protocol','browserApi','background','content','handoff','popup']) await build({entryPoints:[fileURLToPath(new URL(`src/${entry}.ts`,root))],outfile:fileURLToPath(new URL(`${entry}.js`,common)),bundle:true,format:'iife',target:['chrome120','firefox140'],legalComments:'none',charset:'utf8',logLevel:'warning'});
const sourceHash=createHash('sha256');
for(const name of (await readdir(new URL('src/',root))).filter(name=>name.endsWith('.ts')).sort()) sourceHash.update(name).update(await readFile(new URL('src/'+name,root)));
await writeFile(new URL('source.sha256',common),sourceHash.digest('hex'));
for(const browser of ['chrome','firefox']) {
  const output=new URL(`build/${browser}/`,root);await mkdir(output,{recursive:true});
  const manifest=JSON.parse(await readFile(new URL(`manifest.${browser}.json`,root),'utf8'));
  manifest.version=config.extension_version;
  if(browser==='firefox') manifest.browser_specific_settings.gecko.id=config.extension_ids.firefox;
  await writeFile(new URL('manifest.json',output),JSON.stringify(manifest,null,2)+'\n');
  for(const entry of ['background','content','handoff','popup']) await copyFile(new URL(`${entry}.js`,common),new URL(`${entry}.js`,output));
  for(const name of ['popup.html','popup.css']) await copyFile(new URL('src/'+name,root),new URL(name,output));
  await mkdir(new URL('icons/',output),{recursive:true});
  for(const size of [16,32,48,128]) await copyFile(new URL(`assets/icons/icon-${size}.png`,root),new URL(`icons/icon-${size}.png`,output));
}
console.info('Built Chrome and Firefox packages.');
