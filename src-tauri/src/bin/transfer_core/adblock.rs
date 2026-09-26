//! Ad and tracker blocking with Brave's adblock engine, on the same filter
//! lists uBlock Origin uses (EasyList, EasyPrivacy, uBO filters, Peter Lowe).
//!
//! One engine serves every consumer: the engine's page crawl drops ad links,
//! Explore ranks them as noise, and the browser skips ad requests and hides
//! ad elements. The engine owns the list files; the core only reads them.

use adblock::lists::{FilterSet, ParseOptions};
use adblock::request::Request;
use adblock::Engine;
use serde::Deserialize;
use std::path::{Path, PathBuf};
use std::sync::RwLock;
use std::time::{Instant, SystemTime};

#[derive(Default)]
pub struct Blocker {
    engine: RwLock<Option<Engine>>,
}

#[derive(Deserialize)]
struct LoadParams {
    lists: Vec<PathBuf>,
    /// Where the compiled engine is kept; rebuilt when any list is newer.
    cache: PathBuf,
}

#[derive(Deserialize)]
struct CheckItem {
    url: String,
    #[serde(default)]
    source_url: String,
    /// "document", "sub_frame", "script", "image", "stylesheet", "xmlhttprequest", "other"...
    #[serde(default = "other")]
    r#type: String,
}

fn other() -> String {
    "other".into()
}

fn modified(path: &Path) -> Option<SystemTime> {
    std::fs::metadata(path).and_then(|m| m.modified()).ok()
}

impl Blocker {
    fn load(&self, params: LoadParams) -> Result<serde_json::Value, String> {
        let started = Instant::now();
        let newest_list = params.lists.iter().filter_map(|p| modified(p)).max();
        let cache_fresh = match (modified(&params.cache), newest_list) {
            (Some(cache), Some(list)) => cache >= list,
            (Some(_), None) => true,
            _ => false,
        };
        if cache_fresh {
            let bytes = std::fs::read(&params.cache).map_err(|e| format!("read {}: {e}", params.cache.display()))?;
            let mut engine = Engine::default();
            match engine.deserialize(&bytes) {
                Ok(()) => {
                    *self.engine.write().unwrap_or_else(|p| p.into_inner()) = Some(engine);
                    return Ok(serde_json::json!({"from_cache": true, "ms": started.elapsed().as_millis() as u64, "cache_bytes": bytes.len()}));
                }
                // A cache from an older engine version: rebuild from the lists.
                Err(error) => eprintln!("[adblock] cache unusable ({error:?}); rebuilding"),
            }
        }
        // No per-rule debug text: it doubles the compiled cache (12 MB vs 6.6 MB
        // on the full lists) for a detail only telemetry would show.
        let mut set = FilterSet::new(false);
        let mut loaded = Vec::new();
        for path in &params.lists {
            match std::fs::read_to_string(path) {
                Ok(text) => {
                    set.add_filter_list(text, ParseOptions::default());
                    loaded.push(path.display().to_string());
                }
                Err(error) => eprintln!("[adblock] skipped list {}: {error}", path.display()),
            }
        }
        if loaded.is_empty() {
            return Err("no filter list could be read".into());
        }
        let engine = Engine::new_with_filter_set(set);
        let bytes = engine.serialize();
        if let Some(parent) = params.cache.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        if let Err(error) = std::fs::write(&params.cache, &bytes) {
            eprintln!("[adblock] could not write cache {}: {error}", params.cache.display());
        }
        *self.engine.write().unwrap_or_else(|p| p.into_inner()) = Some(engine);
        Ok(serde_json::json!({
            "from_cache": false, "lists": loaded,
            "ms": started.elapsed().as_millis() as u64, "cache_bytes": bytes.len(),
        }))
    }

    fn check(&self, items: Vec<CheckItem>) -> Result<serde_json::Value, String> {
        let guard = self.engine.read().unwrap_or_else(|p| p.into_inner());
        let engine = guard.as_ref().ok_or("filter lists are not loaded")?;
        let results: Vec<serde_json::Value> = items
            .iter()
            .map(|item| {
                let source = if item.source_url.is_empty() { &item.url } else { &item.source_url };
                match Request::new(&item.url, source, &item.r#type, "GET") {
                    Ok(request) => {
                        let result = engine.check_network_request(&request);
                        serde_json::json!({
                            "blocked": result.should_block(),
                            "filter": result.filter.as_ref().and_then(|f| f.raw_line.clone()),
                            "exception": result.exception.as_ref().and_then(|f| f.raw_line.clone()),
                        })
                    }
                    Err(_) => serde_json::json!({"blocked": false, "error": "not a checkable URL"}),
                }
            })
            .collect();
        Ok(serde_json::json!({ "results": results }))
    }

    fn cosmetic(&self, url: &str, classes: Vec<String>, ids: Vec<String>) -> Result<serde_json::Value, String> {
        let guard = self.engine.read().unwrap_or_else(|p| p.into_inner());
        let engine = guard.as_ref().ok_or("filter lists are not loaded")?;
        let resources = engine.url_cosmetic_resources(url);
        let mut selectors: Vec<String> = resources.hide_selectors.iter().cloned().collect();
        if !resources.generichide {
            selectors.extend(engine.hidden_class_id_selectors(&classes, &ids, &resources.exceptions));
        }
        Ok(serde_json::json!({
            "hide_selectors": selectors,
            "injected_script": resources.injected_script,
            "generichide": resources.generichide,
        }))
    }

    pub fn request(&self, method: &str, params: &serde_json::Value) -> Result<serde_json::Value, String> {
        match method {
            "adblock_load" => self.load(serde_json::from_value(params.clone()).map_err(|e| format!("adblock_load: {e}"))?),
            "adblock_check" => {
                let items = serde_json::from_value(params.get("requests").cloned().unwrap_or_default())
                    .map_err(|e| format!("adblock_check: {e}"))?;
                self.check(items)
            }
            "adblock_cosmetic" => {
                let strings = |key: &str| -> Vec<String> {
                    serde_json::from_value(params.get(key).cloned().unwrap_or_default()).unwrap_or_default()
                };
                let url = params.get("url").and_then(serde_json::Value::as_str).unwrap_or_default();
                self.cosmetic(url, strings("classes"), strings("ids"))
            }
            other => Err(format!("unknown adblock method {other}")),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn loaded(rules: &str) -> (Blocker, tempdir::Dir) {
        let dir = tempdir::Dir::new();
        let list = dir.path.join("list.txt");
        std::fs::write(&list, rules).unwrap();
        let blocker = Blocker::default();
        blocker.load(LoadParams { lists: vec![list], cache: dir.path.join("engine.dat") }).unwrap();
        (blocker, dir)
    }

    mod tempdir {
        pub struct Dir { pub path: std::path::PathBuf }
        impl Dir {
            pub fn new() -> Self {
                let path = std::env::temp_dir().join(format!("adblock-test-{}-{:?}", std::process::id(), std::thread::current().id()));
                let _ = std::fs::remove_dir_all(&path);
                std::fs::create_dir_all(&path).unwrap();
                Self { path }
            }
        }
        impl Drop for Dir {
            fn drop(&mut self) { let _ = std::fs::remove_dir_all(&self.path); }
        }
    }

    #[test]
    fn blocks_ad_servers_and_honours_exceptions() {
        let (blocker, _dir) = loaded("||doubleclick.net^\n@@||doubleclick.net/allowed^\nexample.com##.ad-banner\n");
        let result = blocker.request("adblock_check", &serde_json::json!({"requests": [
            {"url": "https://ad.doubleclick.net/click?x=1", "source_url": "https://site.test/post", "type": "sub_frame"},
            {"url": "https://ad.doubleclick.net/allowed/x", "source_url": "https://site.test/post"},
            {"url": "https://files.example/download.zip", "source_url": "https://site.test/post"},
        ]})).unwrap();
        let r = &result["results"];
        assert_eq!(r[0]["blocked"], true);
        assert_eq!(r[1]["blocked"], false, "an exception rule wins");
        assert_eq!(r[2]["blocked"], false);
        let cosmetic = blocker.request("adblock_cosmetic", &serde_json::json!({"url": "https://example.com/"})).unwrap();
        assert_eq!(cosmetic["hide_selectors"], serde_json::json!([".ad-banner"]));
    }

    #[test]
    fn a_fresh_cache_is_reused_and_a_stale_one_rebuilt() {
        let (blocker, dir) = loaded("||ads.test^\n");
        let params = || LoadParams { lists: vec![dir.path.join("list.txt")], cache: dir.path.join("engine.dat") };
        assert_eq!(blocker.load(params()).unwrap()["from_cache"], true);
        std::thread::sleep(std::time::Duration::from_millis(20));
        std::fs::write(dir.path.join("list.txt"), "||tracker.test^\n").unwrap();
        assert_eq!(blocker.load(params()).unwrap()["from_cache"], false);
        let result = blocker.request("adblock_check", &serde_json::json!({"requests": [{"url": "https://tracker.test/p.js", "type": "script"}]})).unwrap();
        assert_eq!(result["results"][0]["blocked"], true);
    }
}
