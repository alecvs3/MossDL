import {build} from 'esbuild';
import {readdir,mkdir} from 'node:fs/promises';
import {spawnSync} from 'node:child_process';
await mkdir('build/tests',{recursive:true});
const files=(await readdir('tests')).filter(name=>name.endsWith('.test.ts'));
for(const file of files) await build({entryPoints:['tests/'+file],outfile:'build/tests/'+file.replace('.ts','.mjs'),bundle:true,platform:'node',format:'esm',target:'node22',logLevel:'warning'});
const result=spawnSync(process.execPath,['--test',...files.map(file=>'build/tests/'+file.replace('.ts','.mjs'))],{stdio:'inherit'});
if(result.error) throw result.error;
process.exitCode=result.status ?? 1;
