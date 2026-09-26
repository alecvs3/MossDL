import assert from 'node:assert/strict';
import { build } from 'esbuild';

const state = { values: [], cursor: 0, effects: [], check: { configured: true, available: true, version: '0.2.0' }, installs: 0 };
globalThis.bannerTest = state;
globalThis.window = { __TAURI_INTERNALS__: {} };
const compiled = await build({
  entryPoints: ['src/figma/ui/UpdateBanner.tsx'], bundle: true, write: false, platform: 'node', format: 'esm',
  jsx: 'automatic', plugins: [{ name: 'update-boundaries', setup(builder) {
    builder.onResolve({ filter: /^react$/ }, () => ({ path: 'react-hooks', namespace: 'test' }));
    builder.onResolve({ filter: /^react\/jsx-runtime$/ }, () => ({ path: 'jsx', namespace: 'test' }));
    builder.onResolve({ filter: /\/api$/ }, () => ({ path: 'update-api', namespace: 'test' }));
    builder.onLoad({ filter: /.*/, namespace: 'test' }, ({ path }) => ({ contents: path === 'react-hooks'
      ? `export function useState(initial) { const s=globalThis.bannerTest; const i=s.cursor++; if (!(i in s.values)) s.values[i]=initial; return [s.values[i], value=>{s.values[i]=value}]; } export function useEffect(fn) { const s=globalThis.bannerTest; if(!s.mounted)s.effects.push(fn); }`
      : path === 'jsx' ? `export const jsx=(type,props)=>({type,props}); export const jsxs=jsx;`
      : `export async function checkForUpdate() { return globalThis.bannerTest.check; } export async function installUpdate() { const s=globalThis.bannerTest; s.installs++; if(s.installError)throw new Error(s.installError); }`, loader: 'js' }));
  } }],
});
const { UpdateBanner } = await import('data:text/javascript;base64,' + Buffer.from(compiled.outputFiles[0].text).toString('base64'));
const render = () => { state.cursor = 0; return UpdateBanner(); };
assert.equal(render(), null);
for (const effect of state.effects) effect();
state.mounted = true;
await new Promise(resolve => setImmediate(resolve));
let banner = render();
assert.equal(banner.props.role, 'status');
assert.match(JSON.stringify(banner), /0.2.0/);
await banner.props.children[1].props.onClick();
await new Promise(resolve => setImmediate(resolve));
assert.equal(state.installs, 1);
state.installError = 'Signature invalid';
banner = render();
banner.props.children[1].props.onClick();
await new Promise(resolve => setImmediate(resolve));
assert.match(JSON.stringify(render()), /Signature invalid/);
render().props.children[2].props.onClick();
assert.equal(render(), null);
state.values[0] = { configured: true, available: false };
assert.equal(render(), null);
state.values[0] = { configured: false, available: true };
assert.equal(render(), null);
console.log('Update banner: availability, installation, error, dismissal and no-update states passed');
