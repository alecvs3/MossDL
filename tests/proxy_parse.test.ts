import assert from "node:assert/strict";
import { parseProxyList, proxyEndpoint, proxyRouteId } from "../src/figma/pages/routes/proxyParse.ts";

const { proxies, rejected } = parseProxyList(`
socks5://jan:hunter2@91.203.114.99:1080
103.21.44.7:8080
103.21.44.8:8080:user7:pw7
alice:s3cret@proxy.example.net:3128
https://51.158.154.173:3128
socks4://72.195.34.60:4145
[2001:db8::1]:8080
103.21.44.7:8080
not a proxy line
1.2.3.4:99999
# comment
`);
assert.deepEqual(proxies.map((p) => [p.kind, p.host, p.port, p.username ?? null]), [
  ["socks5", "91.203.114.99", 1080, "jan"],
  ["http_proxy", "103.21.44.7", 8080, null],
  ["http_proxy", "103.21.44.8", 8080, "user7"],
  ["http_proxy", "proxy.example.net", 3128, "alice"],
  ["http_proxy", "51.158.154.173", 3128, null],
  ["http_proxy", "[2001:db8::1]", 8080, null],
]);
assert.deepEqual(rejected.map((r) => r.reason), [
  "SOCKS4 isn't supported yet — use SOCKS5 or HTTP", "duplicate", "expected host:port", "port 99999 is out of range",
]);
assert.equal(proxies[3].password, "s3cret");

const json = parseProxyList(JSON.stringify([{ ip: "8.8.8.8", port: 1080, protocol: "socks5" }, "9.9.9.9:80"]));
assert.deepEqual(json.proxies.map((p) => proxyEndpoint(p)), ["socks5://8.8.8.8:1080", "http://9.9.9.9:80"]);
assert.equal(proxyRouteId(json.proxies[0]), "px-s5-8-8-8-8-1080");

console.log("Proxy parse tests passed");
