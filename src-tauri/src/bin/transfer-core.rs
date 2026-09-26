use futures_util::StreamExt;
use reqwest::{header::HeaderMap, Client, StatusCode};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::io::{self, BufRead};
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::fs::{self, File, OpenOptions};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::sync::Semaphore;
#[path = "transfer_core/adblock.rs"]
mod adblock;
#[path = "transfer_core/checkpoint.rs"]
mod checkpoint;
#[path = "transfer_core/checksum.rs"]
mod checksum;
#[path = "transfer_core/mega.rs"]
mod mega;
#[path = "transfer_core/scheduler.rs"]
mod scheduler;
#[path = "transfer_core/session.rs"]
mod session;
#[path = "transfer_core/tunnel/mod.rs"]
mod tunnel;

struct WriteChunk {
    offset: u64,
    data: bytes::Bytes,
}

fn emit_progress(transfer_id: Option<&str>, bytes: u64, total: Option<u64>) {
    session::emit(&serde_json::json!({
        "jsonrpc": "2.0",
        "method": "progress",
        "params": {
            "transfer_id": transfer_id,
            "bytes": bytes,
            "total": total
        }
    }));
}

#[derive(Debug, Deserialize)]
struct RpcRequest {
    id: serde_json::Value,
    method: String,
    params: serde_json::Value,
}

#[derive(Debug, Serialize)]
struct RpcResponse<T: Serialize> {
    jsonrpc: &'static str,
    id: serde_json::Value,
    result: Option<T>,
    error: Option<RpcError>,
}

#[derive(Debug, Serialize)]
struct RpcError {
    code: String,
    message: String,
}

/// A provider transform applied to bytes as they arrive.
///
/// This mirrors the `postprocess` dict the Python providers already produce,
/// so MEGA and Transfer.it links need no new plumbing above the transport.
#[derive(Debug, Deserialize, Clone)]
struct TransformSpec {
    #[serde(rename = "type")]
    kind: String,
    #[serde(default)]
    key_a32: Vec<u32>,
}

impl TransformSpec {
    /// A cipher for one worker. Each range decrypts independently, so every
    /// worker needs its own keystream rather than sharing one.
    fn cipher(&self) -> Result<mega::MegaCtr, String> {
        match self.kind.as_str() {
            "mega-ctr" => mega::MegaCtr::new(&self.key_a32),
            other => Err(format!("unsupported transform: {other}")),
        }
    }
}

#[derive(Debug, Deserialize, Clone)]
struct DownloadParams {
    url: String,
    root: String,
    relative_path: String,
    #[serde(default)]
    headers: HashMap<String, String>,
    size: Option<u64>,
    #[serde(default = "default_segments")]
    max_segments: usize,
    #[serde(default = "default_min_segment")]
    min_segment_size: u64,
    #[serde(default = "default_retries")]
    max_retries: usize,
    checksum: Option<String>,
    #[serde(default)]
    bandwidth_bytes_per_second: u64,
    /// Proxy URL for the task's route profile: `http://`, `socks5://` or
    /// `socks5h://`. Absent for direct routes and for OS-level routes
    /// (system VPN, WireGuard) where the kernel already moves the packets.
    proxy: Option<String>,
    /// Decryption applied while the bytes are in flight, when the provider
    /// serves ciphertext.
    transform: Option<TransformSpec>,
    /// Names this transfer so progress can be attributed and pause or cancel
    /// can find it while many run in one process.
    #[serde(default)]
    transfer_id: Option<String>,
    #[serde(skip)]
    limiter: Option<Arc<RateLimiter>>,
    #[serde(skip)]
    control: Option<Arc<session::TransferControl>>,
}

impl DownloadParams {
    /// `Err` once the engine has asked this transfer to stop.
    fn check_control(&self) -> Result<(), String> {
        match self.control.as_ref() {
            Some(control) => control.check(),
            None => Ok(()),
        }
    }

    fn transfer_id(&self) -> Option<&str> {
        self.transfer_id.as_deref()
    }
}

#[derive(Debug)]
struct RateLimiter {
    rate: u64,
    next: tokio::sync::Mutex<Instant>,
}

impl RateLimiter {
    fn new(rate: u64) -> Self {
        Self { rate, next: tokio::sync::Mutex::new(Instant::now()) }
    }

    async fn acquire(&self, bytes: usize) {
        if self.rate == 0 || bytes == 0 {
            return;
        }
        let delay = Duration::from_secs_f64(bytes as f64 / self.rate as f64);
        let mut next = self.next.lock().await;
        let now = Instant::now();
        let start = (*next).max(now);
        *next = start + delay;
        drop(next);
        if start > now {
            tokio::time::sleep(start - now).await;
        }
    }
}

#[derive(Debug, Serialize)]
struct DownloadResult {
    path: String,
    bytes: u64,
    sha256: String,
    segments: usize,
}

fn default_segments() -> usize {
    8
}
fn default_min_segment() -> u64 {
    16 * 1024 * 1024
}
fn default_retries() -> usize {
    4
}

#[derive(Debug)]
struct Probe {
    size: Option<u64>,
    ranges: bool,
    validator: Option<String>,
}

fn safe_destination(root: &str, relative: &str) -> Result<PathBuf, String> {
    let relative_path = Path::new(relative);
    if relative_path.as_os_str().is_empty() {
        return Err("empty destination path".into());
    }
    for component in relative_path.components() {
        if matches!(
            component,
            Component::ParentDir | Component::RootDir | Component::Prefix(_)
        ) {
            return Err("destination path escapes root".into());
        }
    }
    Ok(Path::new(root).join(relative_path))
}

fn rejects_html_response(params: &DownloadParams, content_type: Option<&str>) -> bool {
    let lower = params.relative_path.to_ascii_lowercase();
    let html_target = lower.ends_with(".html") || lower.ends_with(".htm") || lower.ends_with(".xhtml");
    !html_target && content_type.unwrap_or_default().to_ascii_lowercase().starts_with("text/html")
}

fn extract_html_error_message(html: &str) -> Option<String> {
    let lower = html.to_ascii_lowercase();
    if lower.contains("quota exceeded") || lower.contains("too many users have viewed or downloaded") {
        return Some("Google Drive quota exceeded: Too many users have downloaded this file recently. Google limits public downloads per file/IP for up to 24 hours.".into());
    }
    if let Some(start) = lower.find("<title>") {
        if let Some(end) = lower[start + 7..].find("</title>") {
            let title = html[start + 7..start + 7 + end].trim();
            if !title.is_empty() {
                return Some(format!("provider returned error page: {title}"));
            }
        }
    }
    None
}

fn headers(values: &HashMap<String, String>) -> Result<HeaderMap, String> {
    let mut output = HeaderMap::new();
    for (name, value) in values {
        let name =
            reqwest::header::HeaderName::from_bytes(name.as_bytes()).map_err(|e| e.to_string())?;
        let value = reqwest::header::HeaderValue::from_str(value).map_err(|e| e.to_string())?;
        output.insert(name, value);
    }
    Ok(output)
}

async fn probe(client: &Client, params: &DownloadParams) -> Result<Probe, String> {
    let request_headers = headers(&params.headers)?;
    let mut head_probe = None;
    let response = client
        .head(&params.url)
        .headers(request_headers.clone())
        .send()
        .await;
    if let Ok(response) = response {
        if response.status().is_success() {
            let size = response
                .content_length()
                .filter(|size| *size > 0)
                .or(params.size);
            let ranges = response
                .headers()
                .get(reqwest::header::ACCEPT_RANGES)
                .and_then(|v| v.to_str().ok())
                .map(|v| v.eq_ignore_ascii_case("bytes"))
                .unwrap_or(false);
            let validator = response
                .headers()
                .get(reqwest::header::ETAG)
                .or_else(|| response.headers().get(reqwest::header::LAST_MODIFIED))
                .and_then(|v| v.to_str().ok())
                .map(|v| v.to_string());
            let result = Probe { size, ranges, validator };
            // A successful HEAD can still advertise a rounded or stale size or omit Accept-Ranges.
            // Range-capable storage nodes (e.g. MEGA) define the authoritative object size
            // in Content-Range, so verify one byte before planning segments.
            head_probe = Some(result);
        }
    }
    // The probe gets the same transport retries as the transfer itself: one
    // refused connection on a busy host must not fail a file before it starts.
    let mut backoff = Backoff::default();
    let mut attempt = 0;
    let response = loop {
        params.check_control()?;
        let sent = client
            .get(&params.url)
            .headers(request_headers.clone())
            .header(reqwest::header::RANGE, "bytes=0-0")
            .send()
            .await;
        match sent {
            Ok(response) => break response,
            Err(_) if head_probe.is_some() => return Ok(head_probe.unwrap()),
            Err(error) if attempt < params.max_retries => {
                eprintln!("probe attempt {attempt} failed: {}", describe_error(&error));
                attempt += 1;
                sleep_retry(None, &mut backoff).await;
            }
            Err(error) => return Err(describe_error(&error)),
        }
    };
    if rejects_html_response(params, response.headers().get(reqwest::header::CONTENT_TYPE).and_then(|v| v.to_str().ok())) {
        let chunk = response.bytes().await.unwrap_or_default();
        let text = String::from_utf8_lossy(&chunk);
        let msg = extract_html_error_message(&text).unwrap_or_else(|| "provider returned an HTML error page instead of a file".into());
        return Err(msg);
    }
    let ranges = response.status() == StatusCode::PARTIAL_CONTENT;
    let size = response
        .headers()
        .get(reqwest::header::CONTENT_RANGE)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.rsplit('/').next())
        .and_then(|v| v.parse().ok())
        .or_else(|| head_probe.as_ref().and_then(|probe| probe.size))
        .or(response.content_length())
        .or(params.size);
    let validator = response
        .headers()
        .get(reqwest::header::ETAG)
        .or_else(|| response.headers().get(reqwest::header::LAST_MODIFIED))
        .and_then(|v| v.to_str().ok())
        .map(|v| v.to_string());
    Ok(Probe {
        size,
        ranges,
        validator: validator.or_else(|| head_probe.and_then(|probe| probe.validator)),
    })
}

async fn sleep_retry(
    response: Option<&reqwest::Response>,
    backoff: &mut Backoff,
) {
    let retry_after = response.and_then(|r| parse_retry_after(r.headers()));
    let seconds = retry_after.unwrap_or_else(|| backoff.next(1.0, 60.0));
    tokio::time::sleep(Duration::from_secs_f64(seconds.min(120.0))).await;
}

fn parse_retry_after(headers: &HeaderMap) -> Option<f64> {
    headers
        .get(reqwest::header::RETRY_AFTER)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.trim().parse::<f64>().ok())
        .filter(|value| value.is_finite() && *value >= 0.0)
}

/// Structured, retryable transport classification shared with the Python engine.
fn transport_signal_code(status: StatusCode, headers: &HeaderMap) -> String {
    let retry_after = parse_retry_after(headers).unwrap_or(0.0);
    let code = status.as_u16();
    let category = if code == 429 || code == 403 || code == 509 {
        "THROTTLED"
    } else if status.is_server_error() || code == 408 || code == 425 {
        "RETRYABLE"
    } else if code == 401 || code == 407 {
        "AUTH"
    } else {
        "HTTP"
    };
    format!("{category}:{code}:{retry_after:.3}")
}

fn is_retryable_transport_error(message: &str) -> bool {
    message.starts_with("THROTTLED:") || message.starts_with("RETRYABLE:") || message.starts_with("AUTH:")
}

async fn structured_error(response: reqwest::Response, params: &DownloadParams) -> String {
    let status = response.status();
    let mut message = transport_signal_code(status, response.headers());
    if status == StatusCode::FORBIDDEN || status == StatusCode::TOO_MANY_REQUESTS {
        let content_type = response
            .headers()
            .get(reqwest::header::CONTENT_TYPE)
            .and_then(|value| value.to_str().ok());
        if rejects_html_response(params, content_type) {
            let body = response.bytes().await.unwrap_or_default();
            let text = String::from_utf8_lossy(&body);
            if let Some(detail) = extract_html_error_message(&text) {
                message.push(' ');
                message.push_str(&detail);
            }
        }
    }
    message
}

/// Decorrelated backoff mirror of `reliability.decorrelated_jitter`.
#[derive(Default)]
struct Backoff {
    previous: f64,
}

impl Backoff {
    fn next(&mut self, base: f64, cap: f64) -> f64 {
        let upper = (self.previous * 3.0).clamp(base, cap);
        let delay = base + (upper - base) * pseudo_random_unit();
        self.previous = delay;
        delay
    }
}

/// A transport error with its causes: reqwest's own message ("error sending
/// request") says nothing about whether the connect was refused, reset or timed out.
fn describe_error(error: &(dyn std::error::Error + 'static)) -> String {
    let mut message = error.to_string();
    let mut source = error.source();
    while let Some(cause) = source {
        message.push_str(": ");
        message.push_str(&cause.to_string());
        source = cause.source();
    }
    message
}

fn pseudo_random_unit() -> f64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.subsec_nanos())
        .unwrap_or(0);
    (nanos % 1_000_000) as f64 / 1_000_000.0
}

#[cfg(windows)]
fn enable_sparse(file: &std::fs::File) -> Result<(), String> {
    use std::os::windows::io::AsRawHandle;

    #[link(name = "kernel32")]
    extern "system" {
        fn DeviceIoControl(
            hdevice: *mut std::ffi::c_void,
            dwiocontrolcode: u32,
            lpinbuffer: *const std::ffi::c_void,
            ninbuffersize: u32,
            lpoutbuffer: *mut std::ffi::c_void,
            noutbuffersize: u32,
            lpbytesreturned: *mut u32,
            lpoverlapped: *mut std::ffi::c_void,
        ) -> i32;
    }

    const FSCTL_SET_SPARSE: u32 = 0x000900C4;
    let handle = file.as_raw_handle();
    let mut bytes_returned: u32 = 0;
    let ok = unsafe {
        DeviceIoControl(
            handle as *mut std::ffi::c_void,
            FSCTL_SET_SPARSE,
            std::ptr::null(),
            0,
            std::ptr::null_mut(),
            0,
            &mut bytes_returned,
            std::ptr::null_mut(),
        )
    };
    if ok == 0 {
        return Err(format!(
            "DeviceIoControl(FSCTL_SET_SPARSE) failed: {}",
            std::io::Error::last_os_error()
        ));
    }
    Ok(())
}

#[cfg(not(windows))]
fn enable_sparse(_file: &std::fs::File) -> Result<(), String> {
    Ok(())
}

fn write_at_offset(file: &std::fs::File, mut offset: u64, mut buf: &[u8]) -> Result<(), String> {
    #[cfg(windows)]
    {
        use std::os::windows::fs::FileExt;
        while !buf.is_empty() {
            let written = file
                .seek_write(buf, offset)
                .map_err(|e| format!("seek_write failed at offset {offset}: {e}"))?;
            if written == 0 {
                return Err(format!("seek_write returned 0 bytes at offset {offset}"));
            }
            buf = &buf[written..];
            offset += written as u64;
        }
        Ok(())
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::FileExt;
        file.write_all_at(buf, offset)
            .map_err(|e| format!("write_all_at failed at offset {offset}: {e}"))
    }
    #[cfg(not(any(windows, unix)))]
    {
        compile_error!("unsupported platform for positional I/O");
    }
}

/// Counts a range worker as streaming for as long as it holds a 206 response.
struct StreamingGuard(Arc<AtomicUsize>);

impl StreamingGuard {
    fn enter(counter: &Arc<AtomicUsize>) -> Self {
        counter.fetch_add(1, Ordering::AcqRel);
        Self(counter.clone())
    }
}

impl Drop for StreamingGuard {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::AcqRel);
    }
}

async fn download_range(
    client: Client,
    params: DownloadParams,
    start: u64,
    end: u64,
    writer_tx: tokio::sync::mpsc::Sender<WriteChunk>,
    dynamic_end: Option<Arc<AtomicU64>>,
    current_tracker: Option<Arc<AtomicU64>>,
    expected_validator: Option<Arc<String>>,
    streaming: Arc<AtomicUsize>,
) -> Result<u64, String> {
    let request_headers = headers(&params.headers)?;
    let _expected_total = end - start + 1;
    // One keystream per worker. Seeking to each chunk's absolute offset means
    // a retry or a stolen range needs no special handling here.
    let mut transform = params.transform.as_ref().map(TransformSpec::cipher).transpose()?;
    let mut current_offset = start;
    let mut backoff = Backoff::default();
    for attempt in 0..=params.max_retries {
        params.check_control()?;
        // Check dynamic end boundary (may have been truncated by a thief)
        let effective_end = dynamic_end
            .as_ref()
            .map(|e| e.load(Ordering::Relaxed))
            .unwrap_or(end);
        if current_offset > effective_end {
            let written = current_offset - start;
            return Ok(written);
        }
        let req = client
            .get(&params.url)
            .headers(request_headers.clone())
            .header(
                reqwest::header::RANGE,
                format!("bytes={current_offset}-{end}"),
            );
        let response = match req.send().await {
            Ok(response) => response,
            Err(error) if attempt < params.max_retries => {
                eprintln!("range attempt {attempt} failed: {}", describe_error(&error));
                sleep_retry(None, &mut backoff).await;
                continue;
            }
            Err(error) => return Err(describe_error(&error)),
        };
        if response.status() == StatusCode::TOO_MANY_REQUESTS
            || response.status() == StatusCode::FORBIDDEN
            || response.status().is_server_error()
        {
            // Refused while sibling connections are streaming: the host is at its
            // connection limit. Give the range back now; a streaming worker adopts
            // it when it finishes, instead of this one sleeping on a slot.
            if attempt == params.max_retries || streaming.load(Ordering::Acquire) > 0 {
                return Err(structured_error(response, &params).await);
            }
            sleep_retry(Some(&response), &mut backoff).await;
            continue;
        }
        if response.status() == StatusCode::OK {
            if attempt < params.max_retries {
                sleep_retry(Some(&response), &mut backoff).await;
                continue;
            }
            return Err("UNEXPECTED_200".into());
        }
        if response.status() != StatusCode::PARTIAL_CONTENT {
            return Err(structured_error(response, &params).await);
        }
        let _streaming = StreamingGuard::enter(&streaming);
        let content_range = response
            .headers()
            .get(reqwest::header::CONTENT_RANGE)
            .and_then(|v| v.to_str().ok())
            .unwrap_or_default();
        let valid_content_range = if let Some(stripped) = content_range.strip_prefix("bytes ") {
            if let Some((bounds, total_str)) = stripped.split_once('/') {
                if let Some((first, last)) = bounds.split_once('-') {
                    if let (Ok(start_byte), Ok(end_byte)) = (first.parse::<u64>(), last.parse::<u64>()) {
                        let total_ok = if total_str == "*" {
                            true
                        } else if let Ok(total) = total_str.parse::<u64>() {
                            total >= end_byte + 1
                        } else {
                            false
                        };
                        start_byte == current_offset && end_byte >= start_byte && total_ok
                    } else {
                        false
                    }
                } else {
                    false
                }
            } else {
                false
            }
        } else {
            false
        };
        if !valid_content_range {
            return Err("invalid Content-Range".into());
        }
        if let Some(expected) = expected_validator.as_ref() {
            let actual = response
                .headers()
                .get(reqwest::header::ETAG)
                .or_else(|| response.headers().get(reqwest::header::LAST_MODIFIED))
                .and_then(|v| v.to_str().ok());
            if let Some(actual_val) = actual {
                let norm_expected = expected.trim().trim_start_matches("W/").trim_matches('"');
                let norm_actual = actual_val.trim().trim_start_matches("W/").trim_matches('"');
                if !norm_expected.is_empty() && !norm_actual.is_empty() && norm_actual != norm_expected {
                    return Err("validator changed".into());
                }
            }
        }
        let mut stream = response.bytes_stream();
        let mut stream_error = None;
        loop {
            let next_chunk = match tokio::time::timeout(
                Duration::from_millis(15000),
                stream.next(),
            )
            .await
            {
                Ok(Some(chunk_res)) => chunk_res,
                Ok(None) => break,
                Err(_) => {
                    let expected_stop = dynamic_end
                        .as_ref()
                        .map(|e| e.load(Ordering::Relaxed))
                        .unwrap_or(end)
                        + 1;
                    if current_offset >= expected_stop {
                        break;
                    }
                    stream_error = Some("chunk read timed out after 15s".into());
                    break;
                }
            };
            // Check dynamic end before processing chunk — a thief may have
            // truncated our range while we were streaming.
            let effective_end = dynamic_end
                .as_ref()
                .map(|e| e.load(Ordering::Relaxed))
                .unwrap_or(end);
            if current_offset > effective_end {
                break; // Our range was stolen; stop gracefully
            }

            let chunk = match next_chunk {
                Ok(c) => c,
                Err(e) => {
                    stream_error = Some(e.to_string());
                    break;
                }
            };
            params.check_control()?;
            if let Some(limiter) = params.limiter.as_ref() {
                limiter.acquire(chunk.len()).await;
            }
            let mut chunk_data = chunk;
            let chunk_len = chunk_data.len() as u64;

            // Truncate chunk if it extends past the dynamic end boundary
            let bytes_allowed = if effective_end >= current_offset {
                effective_end - current_offset + 1
            } else {
                0
            };
            if chunk_len > bytes_allowed {
                chunk_data = chunk_data.slice(..bytes_allowed as usize);
            }
            if chunk_data.is_empty() {
                break;
            }

            let actual_len = chunk_data.len() as u64;
            if let Some(cipher) = transform.as_mut() {
                let mut plain = chunk_data.to_vec();
                cipher.seek(current_offset);
                cipher.apply(&mut plain);
                chunk_data = bytes::Bytes::from(plain);
            }
            writer_tx
                .send(WriteChunk {
                    offset: current_offset,
                    data: chunk_data,
                })
                .await
                .map_err(|_| "disk writer channel closed unexpectedly".to_string())?;
            current_offset += actual_len;

            // Update current tracker so the scheduler can see our progress
            if let Some(tracker) = current_tracker.as_ref() {
                tracker.store(current_offset, Ordering::Relaxed);
            }
        }
        drop(stream);
        if let Some(error) = stream_error {
            if attempt < params.max_retries {
                sleep_retry(None, &mut backoff).await;
                continue;
            }
            return Err(error);
        }
        // Verify: we may have stopped early due to dynamic end truncation
        let effective_end = dynamic_end
            .as_ref()
            .map(|e| e.load(Ordering::Relaxed))
            .unwrap_or(end);
        let expected_stop = effective_end + 1;
        if current_offset < expected_stop {
            if attempt < params.max_retries {
                sleep_retry(None, &mut backoff).await;
                continue;
            }
            return Err(format!(
                "range length mismatch: expected up to offset {expected_stop}, got {}",
                current_offset
            ));
        }
        return Ok(current_offset - start);
    }
    Err("range retries exhausted".into())
}

async fn download_single(
    client: &Client,
    params: &DownloadParams,
    destination: &Path,
) -> Result<u64, String> {
    let part = destination.with_file_name(format!(
        "{}.part",
        destination
            .file_name()
            .and_then(|v| v.to_str())
            .unwrap_or("download")
    ));
    let request_headers = headers(&params.headers)?;
    let mut transform = params.transform.as_ref().map(TransformSpec::cipher).transpose()?;
    let mut backoff = Backoff::default();
    for attempt in 0..=params.max_retries {
        params.check_control()?;
        let offset = fs::metadata(&part).await.map(|v| v.len()).unwrap_or(0);
        let mut request = client.get(&params.url).headers(request_headers.clone());
        if offset > 0 {
            request = request.header(reqwest::header::RANGE, format!("bytes={offset}-"));
        }
        let response = match request.send().await {
            Ok(response) => response,
            Err(error) if attempt < params.max_retries => {
                eprintln!("stream attempt {attempt} failed: {}", describe_error(&error));
                sleep_retry(None, &mut backoff).await;
                continue;
            }
            Err(error) => return Err(describe_error(&error)),
        };
        if response.status() == StatusCode::TOO_MANY_REQUESTS
            || response.status() == StatusCode::FORBIDDEN
            || response.status().is_server_error()
        {
            if attempt == params.max_retries {
                return Err(structured_error(response, params).await);
            }
            sleep_retry(Some(&response), &mut backoff).await;
            continue;
        }
        if !response.status().is_success() && response.status() != StatusCode::PARTIAL_CONTENT {
            return Err(structured_error(response, params).await);
        }
        let resumed = offset > 0 && response.status() == StatusCode::PARTIAL_CONTENT;
        if rejects_html_response(params, response.headers().get(reqwest::header::CONTENT_TYPE).and_then(|v| v.to_str().ok())) {
            let chunk = response.bytes().await.unwrap_or_default();
            let text = String::from_utf8_lossy(&chunk);
            let msg = extract_html_error_message(&text).unwrap_or_else(|| "provider returned an HTML error page instead of a file".into());
            return Err(msg);
        }
        if resumed {
            let content_range = response
                .headers()
                .get(reqwest::header::CONTENT_RANGE)
                .and_then(|v| v.to_str().ok())
                .unwrap_or_default();
            if !content_range.starts_with(&format!("bytes {offset}-")) {
                return Err("invalid Content-Range for resumed download".into());
            }
        }
        let mut file = if resumed {
            OpenOptions::new()
                .create(true)
                .append(true)
                .open(&part)
                .await
                .map_err(|e| e.to_string())?
        } else {
            File::create(&part).await.map_err(|e| e.to_string())?
        };
        let effective_size = params.size.or_else(|| {
            if resumed {
                response.content_length().map(|rem| offset + rem)
            } else {
                response.content_length()
            }
        });
        let mut total = if resumed { offset } else { 0 };
        let mut stream = response.bytes_stream();
        let mut stream_error = None;
        let mut last_report = Instant::now();
        loop {
            let next_chunk = match tokio::time::timeout(
                Duration::from_millis(15000),
                stream.next(),
            )
            .await
            {
                Ok(Some(chunk_res)) => chunk_res,
                Ok(None) => {
                    // True socket EOF reached (EOF-01)
                    break;
                }
                Err(_elapsed) => {
                    let reached_known_size = effective_size
                        .map(|expected| total >= expected)
                        .unwrap_or(false);
                    if reached_known_size {
                        // EOF-04: Treat an idle keepalive socket as EOF after meaningful
                        // progress, or once the known response size has been received.
                        break;
                    } else {
                        stream_error = Some("chunk read timed out after 15s".into());
                        break;
                    }
                }
            };

            match next_chunk {
                Ok(chunk) => {
                    if chunk.is_empty() {
                        // Empty byte slice EOF (EOF-01)
                        break;
                    }
                    params.check_control()?;
                    if let Some(limiter) = params.limiter.as_ref() {
                        limiter.acquire(chunk.len()).await;
                    }
                    if let Some(cipher) = transform.as_mut() {
                        let mut plain = chunk.to_vec();
                        cipher.seek(total);
                        cipher.apply(&mut plain);
                        file.write_all(&plain).await.map_err(|e| e.to_string())?;
                    } else {
                        file.write_all(&chunk).await.map_err(|e| e.to_string())?;
                    }
                    total += chunk.len() as u64;
                    if last_report.elapsed() >= Duration::from_millis(250) {
                        let reported = effective_size.map(|s| total.min(s)).unwrap_or(total);
                        emit_progress(params.transfer_id(), reported, effective_size);
                        last_report = Instant::now();
                    }
                    if let Some(expected) = effective_size {
                        if total >= expected {
                            break;
                        }
                    }
                }
                Err(error) => {
                    stream_error = Some(error.to_string());
                    break;
                }
            }
        }
        drop(stream); // EOF-02: close the keepalive response before flushing/renaming
        let final_total = effective_size.unwrap_or(total);
        emit_progress(params.transfer_id(), total, Some(final_total));
        file.flush().await.map_err(|e| e.to_string())?;
        drop(file); // EOF-02: explicitly close file descriptor immediately
        if let Some(error) = stream_error {
            if attempt < params.max_retries {
                sleep_retry(None, &mut backoff).await;
                continue;
            }
            return Err(error);
        }
        if let Some(expected) = effective_size {
            if total != expected {
                if attempt < params.max_retries {
                    sleep_retry(None, &mut backoff).await;
                    continue;
                }
                return Err(format!("size mismatch: expected {expected}, got {total}"));
            }
        }
        if params.checksum.is_some() {
            let (_, _, comparable) = hash_file(&part, params.checksum.as_deref()).await?;
            if !checksum::matches(params.checksum.as_deref(), &comparable) {
                return Err("download checksum mismatch".into());
            }
        } else {
            let actual = fs::metadata(&part).await.map_err(|e| e.to_string())?.len();
            if let Some(expected) = effective_size {
                if actual != expected {
                    return Err(format!("size mismatch: expected {expected}, got {actual}"));
                }
            }
        }
        checkpoint::replace(&part, destination)?;
        return Ok(total);
    }
    Err("single-stream retries exhausted".into())
}

/// Hash a finished file, returning `(bytes, sha256, digest_to_compare)`.
///
/// The comparison digest uses whatever algorithm the expected checksum names,
/// while sha256 is always reported so the engine keeps one stable identity for
/// the artifact.
async fn hash_file(path: &Path, expected: Option<&str>) -> Result<(u64, String, String), String> {
    let algorithm = match expected {
        Some(value) => Some(checksum::split(value)?.0),
        None => None,
    };
    let mut file = File::open(path).await.map_err(|e| e.to_string())?;
    let mut hasher = checksum::MultiHasher::new(algorithm);
    let mut bytes = 0_u64;
    let mut buffer = vec![0_u8; 1024 * 1024];
    loop {
        let read = file.read(&mut buffer).await.map_err(|e| e.to_string())?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
        bytes += read as u64;
    }
    let (sha256, comparable) = hasher.finish();
    Ok((bytes, sha256, comparable))
}

async fn download(client: Client, params: DownloadParams) -> Result<DownloadResult, String> {
    if let Some(value) = params.checksum.as_deref() {
        checksum::split(value)?;
    }
    let destination = safe_destination(&params.root, &params.relative_path)?;
    // Segmented downloads write to a ".part" sibling and are atomically promoted
    // to the final path only after every range completes and the payload is
    // verified. This prevents a cancelled/preallocated sparse file from ever
    // being mistaken for a completed artifact on a later attempt.
    let part_path = destination.with_file_name(format!(
        "{}.part",
        destination
            .file_name()
            .and_then(|v| v.to_str())
            .unwrap_or("download")
    ));
    if !params.url.starts_with("http://") && !params.url.starts_with("https://") {
        return Err(
            "transfer-core currently supports HTTP(S); the Python transport handles FTP".into(),
        );
    }
    if !params.headers.is_empty()
        && params
            .headers
            .keys()
            .any(|key| key.eq_ignore_ascii_case("host"))
    {
        return Err("custom Host headers are not allowed".into());
    }
    if let Some(parent) = destination.parent() {
        fs::create_dir_all(parent)
            .await
            .map_err(|e| e.to_string())?;
    }
    let mut params = params;
    if params.bandwidth_bytes_per_second > 0 {
        params.limiter = Some(Arc::new(RateLimiter::new(params.bandwidth_bytes_per_second)));
    }
    let probe = probe(&client, &params).await?;
    // The transport probe is authoritative. Provider page metadata is often a
    // rounded display value (for example 2 GiB for a 2,000 MiB object).
    params.size = probe.size.or(params.size);
    if let Some(size) = probe.size {
        if size == 0 {
            return Err("remote file is empty".into());
        }
    }
    if let Some(existing_size) = fs::metadata(&destination)
        .await
        .ok()
        .map(|value| value.len())
    {
        if Some(existing_size) == probe.size {
            if params.checksum.is_some() {
                let (bytes, sha256, comparable) = hash_file(&destination, params.checksum.as_deref()).await?;
                if checksum::matches(params.checksum.as_deref(), &comparable) {
                    return Ok(DownloadResult {
                        path: destination.display().to_string(),
                        bytes,
                        sha256,
                        segments: 0,
                    });
                }
            } else {
                return Ok(DownloadResult {
                    path: destination.display().to_string(),
                    bytes: existing_size,
                    sha256: String::new(),
                    segments: 0,
                });
            }
        }
    }
    let Some(size) = probe.size else {
        let bytes = download_single(&client, &params, &destination).await?;
        let (actual, digest) = if params.checksum.is_some() {
            let (actual, sha256, comparable) = hash_file(&destination, params.checksum.as_deref()).await?;
            if !checksum::matches(params.checksum.as_deref(), &comparable) {
                return Err("download checksum mismatch".into());
            }
            (actual, sha256)
        } else {
            let actual = fs::metadata(&destination).await.map_err(|e| e.to_string())?.len();
            (actual, String::new())
        };
        return Ok(DownloadResult {
            path: destination.display().to_string(),
            bytes: actual.max(bytes),
            sha256: digest,
            segments: 1,
        });
    };
    let use_ranges = probe.ranges && size > params.min_segment_size;
    if !use_ranges {
        let bytes = download_single(&client, &params, &destination).await?;
        if bytes != size {
            return Err(format!("size mismatch: expected {size}, got {bytes}"));
        }
        let (actual, digest) = if params.checksum.is_some() {
            let (actual, sha256, comparable) = hash_file(&destination, params.checksum.as_deref()).await?;
            if !checksum::matches(params.checksum.as_deref(), &comparable) {
                return Err("download checksum mismatch".into());
            }
            (actual, sha256)
        } else {
            let actual = fs::metadata(&destination).await.map_err(|e| e.to_string())?.len();
            (actual, String::new())
        };
        return Ok(DownloadResult {
            path: destination.display().to_string(),
            bytes: actual,
            sha256: digest,
            segments: 1,
        });
    }
    let count = params.max_segments.max(1).min(size as usize);
    let dest_file = std::fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(false)
        .open(&part_path)
        .map_err(|e| format!("failed to open destination {}: {e}", part_path.display()))?;
    #[cfg(windows)]
    enable_sparse(&dest_file)?;
    let journal_path = part_path.with_extension("part.ranges.json");
    let identity = probe.validator.as_ref().filter(|v| !v.starts_with("W/"))
        .or(params.checksum.as_ref()).map(|v| format!("{size}:{v}")).unwrap_or_default();
    let mut journal = checkpoint::Journal::open(journal_path.clone(), &dest_file, size, identity);
    let ranges = journal.pending(count);
    let resumed_bytes = journal.completed();
    dest_file
        .set_len(size)
        .map_err(|e| format!("failed to set destination file length to {size}: {e}"))?;
    let (writer_tx, mut writer_rx) = tokio::sync::mpsc::channel::<WriteChunk>(2048);
    let total_size = size;
    let writer_transfer_id = params.transfer_id.clone();
    let writer_thread = std::thread::spawn(move || -> Result<(), String> {
        let mut total_written = resumed_bytes;
        let mut last_report = Instant::now();
        let mut last_checkpoint = Instant::now();
        emit_progress(writer_transfer_id.as_deref(), total_written, Some(total_size));
        while let Some(chunk) = writer_rx.blocking_recv() {
            write_at_offset(&dest_file, chunk.offset, &chunk.data)?;
            journal.record(chunk.offset, chunk.data.len() as u64)?;
            total_written = journal.completed();
            if checkpoint::checkpoint_due(last_checkpoint) {
                journal.save(&dest_file)?;
                last_checkpoint = Instant::now();
            }
            let reported = if total_size > 0 { total_written.min(total_size) } else { total_written };
            if last_report.elapsed() >= Duration::from_millis(250) {
                emit_progress(writer_transfer_id.as_deref(), reported, Some(total_size));
                last_report = Instant::now();
            }
        }
        let reported = if total_size > 0 { total_written.min(total_size) } else { total_written };
        emit_progress(writer_transfer_id.as_deref(), reported, Some(total_size));
        journal.save(&dest_file)?;
        if journal.completed() != total_size {
            return Err("incomplete durable range coverage".into());
        }
        Ok(())
    });

    // Build range plan and RangeScheduler for dynamic work-stealing

    let scheduler = Arc::new(scheduler::RangeScheduler::new(
        &ranges,
        8 * 1024 * 1024, // 8 MB minimum steal threshold
    ));
    let expected_validator = probe.validator.map(Arc::new);

    let semaphore = std::sync::Arc::new(Semaphore::new(count));
    let mut jobs = tokio::task::JoinSet::new();
    let mut total_segments = count;
    // Workers currently receiving body bytes (see download_range).
    let streaming = Arc::new(AtomicUsize::new(0));

    // Every worker, initial or stolen/adopted, owns a scheduler slot and
    // reports which one when it ends, so a refused connection's bytes can be
    // handed to a worker that is still allowed in.
    let spawn_worker = |jobs: &mut tokio::task::JoinSet<(usize, Result<u64, String>)>,
                        trackers: scheduler::Trackers,
                        start: u64,
                        end: u64,
                        tx: tokio::sync::mpsc::Sender<WriteChunk>,
                        permit: Option<tokio::sync::OwnedSemaphorePermit>| {
        let client_clone = client.clone();
        let params_clone = params.clone();
        let val_clone = expected_validator.clone();
        let streaming_clone = streaming.clone();
        jobs.spawn(async move {
            let result = download_range(
                client_clone,
                params_clone,
                start,
                end,
                tx,
                Some(trackers.end),
                Some(trackers.current),
                val_clone,
                streaming_clone,
            )
            .await;
            drop(permit);
            (trackers.index, result)
        });
    };

    for (slot_index, &(seg_start, seg_end)) in ranges.iter().enumerate() {
        let permit = semaphore
            .clone()
            .acquire_owned()
            .await
            .map_err(|e| e.to_string())?;
        let trackers = scheduler.trackers(slot_index).ok_or("range scheduler lost a slot")?;
        spawn_worker(&mut jobs, trackers, seg_start, seg_end, writer_tx.clone(), Some(permit));
    }

    // Don't drop writer_tx yet — we may spawn steal tasks that need it
    let steal_tx = writer_tx.clone();
    drop(writer_tx);

    let mut range_error = None;
    let mut unexpected_200 = false;
    let mut throttle_error: Option<String> = None;

    // Process completed workers: adopt refused ranges first, then steal.
    while let Some(result) = jobs.join_next().await {
        match result {
            Ok((_, Ok(_))) => {
                if unexpected_200 {
                    continue;
                }
                // After a refusal the host has told us its limit: only take
                // over orphaned bytes, never open an extra connection.
                let next = scheduler
                    .adopt()
                    .or_else(|| if throttle_error.is_none() { scheduler.try_steal() } else { None });
                if let Some((next_start, next_end)) = next {
                    total_segments += 1;
                    let trackers = scheduler.add_slot(next_start, next_end);
                    spawn_worker(&mut jobs, trackers, next_start, next_end, steal_tx.clone(), None);
                }
            }
            Ok((index, Err(e))) => {
                if e == "UNEXPECTED_200" {
                    unexpected_200 = true;
                    jobs.abort_all();
                    break;
                }
                if is_retryable_transport_error(&e) {
                    // Refused or failing connection: its bytes go to a survivor.
                    eprintln!("[RANGE_ABANDONED] slot {index}: {e}; its remaining bytes will be adopted");
                    scheduler.abandon(index);
                    if throttle_error.is_none() {
                        throttle_error = Some(e);
                    }
                } else if range_error.is_none() {
                    range_error = Some(e);
                }
            }
            Err(e) => {
                if range_error.is_none() {
                    range_error = Some(e.to_string());
                }
            }
        }
    }
    drop(steal_tx);
    let writer_result = tokio::task::spawn_blocking(move || {
        writer_thread
            .join()
            .map_err(|_| "disk writer actor thread panicked".to_string())
    })
    .await
    .map_err(|e| e.to_string())??;

    if unexpected_200 {
        while jobs.join_next().await.is_some() {}
        // Discard the preallocated segmented partial before the single-stream
        // fallback, which manages its own ".part" resume state.
        fs::remove_file(&part_path).await.ok();
        let bytes = download_single(&client, &params, &destination).await?;
        let (actual, digest) = if params.checksum.is_some() {
            let (actual, sha256, comparable) = hash_file(&destination, params.checksum.as_deref()).await?;
            if !checksum::matches(params.checksum.as_deref(), &comparable) {
                return Err("download checksum mismatch".into());
            }
            (actual, sha256)
        } else {
            let actual = fs::metadata(&destination).await.map_err(|e| e.to_string())?.len();
            (actual, String::new())
        };
        let temporary = destination.with_extension("transfer-parts");
        fs::remove_dir_all(&temporary).await.ok();
        return Ok(DownloadResult {
            path: destination.display().to_string(),
            bytes: actual.max(bytes),
            sha256: digest,
            segments: 1,
        });
    }

    if let Some(err) = throttle_error {
        let orphaned = scheduler.orphaned_bytes();
        if orphaned > 0 {
            return Err(err);
        }
        eprintln!("[RANGE_THROTTLE_ABSORBED] {err}; refused ranges were finished by the remaining connections");
    }
    if let Some(err) = range_error {
        return Err(err);
    }
    writer_result?;
    let temporary = destination.with_extension("transfer-parts");
    fs::remove_dir_all(&temporary).await.ok();
    // Atomically promote the fully-written segmented partial to the final path.
    //
    // Idempotent and non-destructive. Removing the destination BEFORE checking
    // that the source exists destroys a finished file whenever the partial is
    // already gone -- a retry after a promote that in fact succeeded, or another
    // process sharing this destination directory. That path deleted a complete
    // 2 GB volume and then failed with "cannot find the file specified".
    // Either branch falls through to the size/checksum verification below, so an
    // already-promoted file is still proven correct rather than assumed.
    let (bytes, digest) = if params.checksum.is_some() {
        let (bytes, sha256, comparable) = hash_file(&part_path, params.checksum.as_deref()).await?;
        if bytes != size {
            return Err(format!(
                "downloaded size mismatch: expected {size}, got {bytes}"
            ));
        }
        if !checksum::matches(params.checksum.as_deref(), &comparable) {
            return Err("download checksum mismatch".into());
        }
        (bytes, sha256)
    } else {
        let bytes = fs::metadata(&part_path)
            .await
            .map_err(|e| e.to_string())?
            .len();
        if bytes != size {
            return Err(format!(
                "downloaded size mismatch: expected {size}, got {bytes}"
            ));
        }
        (bytes, String::new())
    };
    checkpoint::replace(&part_path, &destination)?;
    if let Err(error) = fs::remove_file(&journal_path).await {
        eprintln!("[CHECKPOINT_CLEANUP] {error}");
    }
    Ok(DownloadResult {
        path: destination.display().to_string(),
        bytes,
        sha256: digest,
        segments: total_segments,
    })
}

/// Run one download, registered so it can be paused or cancelled while it runs.
async fn dispatch(
    request: RpcRequest,
    pool: Arc<session::ClientPool>,
    registry: Arc<session::Registry>,
) -> RpcResponse<DownloadResult> {
    let mut params = match serde_json::from_value::<DownloadParams>(request.params) {
        Ok(params) => params,
        Err(error) => {
            return RpcResponse {
                jsonrpc: "2.0",
                id: request.id,
                result: None,
                error: Some(RpcError {
                    code: "INVALID_PARAMS".into(),
                    message: error.to_string(),
                }),
            }
        }
    };
    let client = match pool.get(params.proxy.as_deref()) {
        Ok(client) => client,
        Err(message) => {
            return RpcResponse {
                jsonrpc: "2.0",
                id: request.id,
                result: None,
                error: Some(RpcError {
                    code: "ROUTE_UNUSABLE".into(),
                    message,
                }),
            }
        }
    };
    let transfer_id = params.transfer_id.clone();
    if let Some(id) = transfer_id.as_deref() {
        params.control = Some(registry.register(id));
    }
    let outcome = download(client, params).await;
    if let Some(id) = transfer_id.as_deref() {
        registry.forget(id);
    }
    match outcome {
        Ok(result) => RpcResponse {
            jsonrpc: "2.0",
            id: request.id,
            result: Some(result),
            error: None,
        },
        Err(message) => {
            let code = if message == session::PAUSED_MESSAGE {
                "PAUSED"
            } else if message == session::CANCELED_MESSAGE {
                "CANCELED"
            } else if message.starts_with("THROTTLED:") {
                "THROTTLED"
            } else if message.starts_with("RETRYABLE:") {
                "RETRYABLE"
            } else if message.starts_with("AUTH:") {
                "AUTH"
            } else {
                "TRANSFER_FAILED"
            };
            RpcResponse {
                jsonrpc: "2.0",
                id: request.id,
                result: None,
                error: Some(RpcError {
                    code: code.into(),
                    message,
                }),
            }
        }
    }
}

fn error_response(id: serde_json::Value, code: &str, message: &str) -> serde_json::Value {
    serde_json::json!({
        "jsonrpc": "2.0",
        "id": id,
        "error": {"code": code, "message": message},
    })
}

async fn tunnel_request(
    tunnels: &tunnel::Tunnels,
    method: &str,
    params: &serde_json::Value,
) -> Result<serde_json::Value, String> {
    let id = params.get("tunnel_id").and_then(serde_json::Value::as_str).unwrap_or_default();
    match method {
        "tunnel_start" => {
            if id.is_empty() {
                return Err("tunnel_id is required".into());
            }
            let config: tunnel::TunnelConfig = serde_json::from_value(params.get("config").cloned().unwrap_or_default())
                .map_err(|e| format!("tunnel config: {e}"))?;
            tunnels.start(id, config).await
        }
        "tunnel_stop" => Ok(serde_json::json!({"stopped": tunnels.stop(id).await})),
        _ => Ok(tunnels.status().await),
    }
}

#[tokio::main]
async fn main() {
    let pool = Arc::new(session::ClientPool::default());
    let registry = Arc::new(session::Registry::default());
    let mut running = tokio::task::JoinSet::new();
    let tunnels = Arc::new(tunnel::Tunnels::default());
    let blocker = Arc::new(adblock::Blocker::default());

    // stdin blocks, so it is drained on its own thread and requests arrive here
    // as messages. That keeps the runtime free to serve transfers already in
    // flight while the engine sends the next one.
    let (line_tx, mut line_rx) = tokio::sync::mpsc::unbounded_channel::<String>();
    std::thread::spawn(move || {
        for line in io::stdin().lock().lines() {
            match line {
                Ok(line) => {
                    if line_tx.send(line).is_err() {
                        return;
                    }
                }
                Err(_) => return,
            }
        }
    });

    while let Some(line) = line_rx.recv().await {
        let line = line.trim().to_string();
        if line.is_empty() {
            continue;
        }
        let request = match serde_json::from_str::<RpcRequest>(&line) {
            Ok(request) => request,
            Err(error) => {
                session::emit(&error_response(
                    serde_json::Value::Null,
                    "INVALID_JSON",
                    &error.to_string(),
                ));
                continue;
            }
        };
        match request.method.as_str() {
            "download" => {
                let pool = pool.clone();
                let registry = registry.clone();
                running.spawn(async move {
                    let response = dispatch(request, pool, registry).await;
                    session::emit(
                        &serde_json::to_value(response).expect("serialize response"),
                    );
                });
            }
            // Stopping is answered immediately; the transfer itself unwinds at
            // its next chunk boundary and reports its own outcome.
            "pause" | "cancel" => {
                let cancel = request.method == "cancel";
                let transfer_id = request
                    .params
                    .get("transfer_id")
                    .and_then(serde_json::Value::as_str)
                    .unwrap_or_default()
                    .to_string();
                let stopping = registry.stop(&transfer_id, cancel);
                session::emit(&serde_json::json!({
                    "jsonrpc": "2.0",
                    "id": request.id,
                    "result": {"transfer_id": transfer_id, "stopping": stopping},
                }));
            }
            // Loading lists takes a moment and checks can be large batches:
            // both run off the request loop.
            "adblock_load" | "adblock_check" | "adblock_cosmetic" => {
                let blocker = blocker.clone();
                running.spawn(async move {
                    let result = tokio::task::spawn_blocking(move || blocker.request(&request.method, &request.params).map(|r| (request.id.clone(), r)).map_err(|e| (request.id, e)))
                        .await
                        .unwrap_or_else(|join| Err((serde_json::Value::Null, join.to_string())));
                    session::emit(&match result {
                        Ok((id, result)) => serde_json::json!({"jsonrpc": "2.0", "id": id, "result": result}),
                        Err((id, message)) => error_response(id, "ADBLOCK_FAILED", &message),
                    });
                });
            }
            // Tunnels start on their own task: the first handshake can take
            // seconds and must not hold up transfers or other requests.
            "tunnel_start" | "tunnel_stop" | "tunnel_status" => {
                let tunnels = tunnels.clone();
                running.spawn(async move {
                    let result = tunnel_request(&tunnels, &request.method, &request.params).await;
                    session::emit(&match result {
                        Ok(result) => serde_json::json!({"jsonrpc": "2.0", "id": request.id, "result": result}),
                        Err(message) => error_response(request.id, "TUNNEL_FAILED", &message),
                    });
                });
            }
            other => {
                let message = other.to_string();
                session::emit(&error_response(request.id, "METHOD_NOT_FOUND", &message));
            }
        }
    }

    // stdin closed: let anything still in flight finish and report.
    while running.join_next().await.is_some() {}
}
