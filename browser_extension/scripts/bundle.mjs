import {build} from 'esbuild';
const [entry,outfile,format='iife']=process.argv.slice(2);
if(!entry || !outfile) throw new Error('Usage: node bundle.mjs entry outfile [format]');
await build({entryPoints:[entry],outfile,bundle:true,format,target:['chrome120','firefox140'],legalComments:'none',logLevel:'warning',charset:'utf8'});
