"""Complete non-encrypted HLS/DASH planning and resumable assembly."""

from __future__ import annotations

import json
import hashlib
import os
import posixpath
import re
import subprocess
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable

from .errors import NeedsUser
from .models import MediaPlan, MediaSegment
from . import route_http


class MediaAssembler:
    """Sequential segment assembler with a small durable progress manifest."""

    def __init__(self, retries: int = 3, chunk_size: int = 1024 * 1024) -> None:
        self.retries, self.chunk_size = max(0, retries), chunk_size

    def assemble(self, plan: MediaPlan, output: str | Path, *, pause: Callable[[], bool] | None = None,
                 progress: Callable[[int, int], None] | None = None, ffmpeg: str | None = None) -> dict[str, Any]:
        if plan.encrypted:
            raise NeedsUser("encrypted or DRM media is unsupported", "unsupported_encryption")
        path = Path(output).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        state_path = path.with_name(path.name + ".segments.json")
        part_path = path.with_name(path.name + ".part")
        plan_fingerprint = hashlib.sha256(json.dumps(
            {"kind": plan.kind, "manifest_url": plan.manifest_url,
             "segments": [segment.to_dict() for segment in plan.segments]},
            sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        if state.get("plan_fingerprint") != plan_fingerprint:
            state = {"plan_fingerprint": plan_fingerprint, "completed": []}
        completed = set(int(value) for value in state.get("completed", []))
        total = len(plan.segments)
        mode = "ab" if part_path.exists() else "wb"
        last_state_save = 0.0
        with part_path.open(mode) as dest:
            for segment in plan.segments:
                if segment.index in completed:
                    continue
                while pause and pause():
                    time.sleep(0.1)
                data = self._fetch(segment)
                dest.write(data)
                completed.add(segment.index)
                now = time.monotonic()
                if (now - last_state_save >= 5.0) or len(completed) == total:
                    state_path.write_text(json.dumps({"plan_fingerprint": plan_fingerprint,
                                                      "completed": sorted(completed)}), encoding="utf-8")
                    last_state_save = now
                if progress:
                    progress(len(completed), total)
            if len(completed) > 0 and len(completed) != total:
                state_path.write_text(json.dumps({"plan_fingerprint": plan_fingerprint,
                                                  "completed": sorted(completed)}), encoding="utf-8")
        if len(completed) != total:
            return {"output": str(path), "segments": total, "completed": len(completed), "paused": True}
        os.replace(part_path, path)
        result = {"output": str(path), "segments": total, "completed": len(completed), "paused": False}
        try:
            state_path.unlink()
        except OSError:
            pass
        if ffmpeg:
            remuxed = self._remux(ffmpeg, path, plan.output_format)
            if remuxed:
                result["remuxed_output"] = str(remuxed)
        return result

    def _fetch(self, segment: MediaSegment) -> bytes:
        headers: dict[str, str] = {}
        if segment.byte_range:
            match = re.match(r"\s*(\d+)(?:@(\d+))?", segment.byte_range)
            if match:
                length, start = int(match.group(1)), int(match.group(2) or 0)
                headers["Range"] = f"bytes={start}-{start + length - 1}"
        error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                request = urllib.request.Request(segment.url, headers=headers)
                with route_http.urlopen(request, timeout=30) as response:
                    return response.read()
            except Exception as exc:  # pragma: no cover - exercised with mocked urlopen
                error = exc
                if attempt < self.retries:
                    time.sleep(min(2 ** attempt, 8))
        raise RuntimeError(f"media segment failed: {segment.url}") from error

    @staticmethod
    def _remux(ffmpeg: str, input_path: Path, output_format: str | None) -> Path | None:
        if not output_format:
            return None
        output = input_path.with_suffix("." + output_format.lstrip("."))
        subprocess.run([ffmpeg, "-y", "-i", str(input_path), "-c", "copy", str(output)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return output


def _attrs(line: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for match in re.finditer(r"([A-Z0-9-]+)=((?:\"[^\"]*\")|[^,]*)", line):
        result[match.group(1)] = match.group(2).strip('"')
    return result


def parse_hls(url: str, body: str, *, max_segments: int = 10000, preferred_bandwidth: int | None = None) -> MediaPlan:
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    keys = [line for line in lines if line.startswith("#EXT-X-KEY:") and _attrs(line).get("METHOD", "NONE") != "NONE"]
    if keys:
        raise NeedsUser("encrypted HLS manifests are unsupported", "unsupported_encryption")
    variants: list[dict[str, Any]] = []
    for index, line in enumerate(lines[:-1]):
        if line.startswith("#EXT-X-STREAM-INF:") and not lines[index + 1].startswith("#"):
            attributes = _attrs(line.split(":", 1)[1])
            attributes["url"] = urllib.parse.urljoin(url, lines[index + 1])
            attributes["bandwidth"] = int(attributes.get("BANDWIDTH", 0) or 0)
            variants.append(attributes)
    selected = None
    if variants:
        selected = min(variants, key=lambda item: abs(item["bandwidth"] - preferred_bandwidth)) if preferred_bandwidth else max(variants, key=lambda item: item["bandwidth"])
        return MediaPlan("hls", url, variants=variants, selected_variant=selected)
    segments: list[MediaSegment] = []
    current_range: str | None = None
    total_duration = 0.0
    for line in lines:
        if line.startswith("#EXT-X-MAP:"):
            attrs = _attrs(line.split(":", 1)[1])
            segments.append(MediaSegment(len(segments), urllib.parse.urljoin(url, attrs.get("URI", "")),
                                         byte_range=attrs.get("BYTERANGE"), initialization=True))
        elif line.startswith("#EXT-X-BYTERANGE:"):
            current_range = line.split(":", 1)[1]
        elif line.startswith("#EXTINF:"):
            try:
                total_duration += float(line.split(":", 1)[1].split(",", 1)[0])
            except ValueError:
                pass
        elif not line.startswith("#"):
            segments.append(MediaSegment(len(segments), urllib.parse.urljoin(url, line), byte_range=current_range))
            current_range = None
    if len(segments) > max_segments:
        raise ValueError("HLS manifest exceeds the segment limit")
    return MediaPlan("hls", url, segments=segments, variants=[], encrypted=False, total_duration=total_duration or None)


def parse_dash(url: str, body: str, *, max_segments: int = 10000) -> MediaPlan:
    root = ET.fromstring(body)
    encrypted = any(element.tag.endswith("ContentProtection") for element in root.iter())
    if encrypted:
        raise NeedsUser("encrypted DASH manifests are unsupported", "unsupported_encryption")
    ns = {"m": root.tag.split("}", 1)[0][1:]} if "}" in root.tag else {}
    query = ".//m:Representation" if ns else ".//Representation"
    representations = root.findall(query, ns)
    variants: list[dict[str, Any]] = []
    for rep in representations:
        variants.append({key.lower(): value for key, value in rep.attrib.items()})
    chosen = max(representations, key=lambda item: int(item.attrib.get("bandwidth", "0") or 0)) if representations else root
    base = next((element.text for element in chosen.iter() if element.tag.endswith("BaseURL") and element.text), "")
    templates = list(chosen.iterfind(".//m:SegmentTemplate" if ns else ".//SegmentTemplate", ns))
    if not templates:
        templates = list(root.iterfind(".//m:SegmentTemplate" if ns else ".//SegmentTemplate", ns))
    segments: list[MediaSegment] = []
    if not templates:
        segment_list = next((child for child in chosen.iter() if child.tag.endswith("SegmentList")), None)
        if segment_list is not None:
            for entry in segment_list:
                if entry.tag.endswith("SegmentURL"):
                    target = entry.attrib.get("media") or ""
                    segments.append(MediaSegment(len(segments), urllib.parse.urljoin(url, posixpath.join(base, target)),
                                                 byte_range=entry.attrib.get("mediaRange")))
        return MediaPlan("dash", url, segments=segments, variants=variants,
                         selected_variant=(max(variants, key=lambda item: int(item.get("bandwidth", "0") or 0)) if variants else None),
                         encrypted=False)
    for template in templates[:1]:
        media = template.attrib.get("media")
        initialization = template.attrib.get("initialization")
        if initialization:
            segments.append(MediaSegment(len(segments), urllib.parse.urljoin(url, posixpath.join(base, initialization)), initialization=True))
        if not media:
            # SegmentList is common in small DASH fixtures and carries exact
            # byte ranges without requiring a generated URL template.
            segment_list = next((child for child in chosen.iter() if child.tag.endswith("SegmentList")), None)
            if segment_list is not None:
                for entry in segment_list:
                    if not entry.tag.endswith("SegmentURL"):
                        continue
                    target = entry.attrib.get("media") or ""
                    segments.append(MediaSegment(len(segments), urllib.parse.urljoin(url, posixpath.join(base, target)),
                                                  byte_range=entry.attrib.get("mediaRange")))
            continue
        start_number = int(template.attrib.get("startNumber", "1"))
        timeline = next((child for child in template if child.tag.endswith("SegmentTimeline")), None)
        numbers: list[int] = []
        total_duration = 0.0
        if timeline is not None:
            number = start_number
            for item in timeline:
                repeat = int(item.attrib.get("r", "0"))
                if repeat < 0:
                    # An open-ended repeat is bounded by the next S element
                    # or the global segment cap.
                    next_duration = next((int(next_item.attrib["d"]) for next_item in list(timeline)[list(timeline).index(item) + 1:]
                                          if "d" in next_item.attrib), None)
                    repeat = max(0, min(10000, (next_duration or int(item.attrib.get("d", "1"))) - 1))
                numbers.extend(number + offset for offset in range(repeat + 1))
                total_duration += (repeat + 1) * float(item.attrib.get("d", "0")) / float(template.attrib.get("timescale", "1"))
                number += repeat + 1
        else:
            numbers = list(range(start_number, start_number + 10000))
        for number in numbers[:max_segments - len(segments)]:
            target = media.replace("$Number$", str(number)).replace("$RepresentationID$", chosen.attrib.get("id", ""))
            if "$Time$" in target:
                target = target.replace("$Time$", str(number))
            segments.append(MediaSegment(len(segments), urllib.parse.urljoin(url, posixpath.join(base, target))))
    return MediaPlan("dash", url, segments=segments, variants=variants, selected_variant=(variants[0] if variants else None), encrypted=False,
                     total_duration=locals().get("total_duration") or None)


def parse_media(url: str, body: str, **kwargs: Any) -> MediaPlan:
    return parse_dash(url, body, **kwargs) if url.lower().split("?", 1)[0].endswith(".mpd") or body.lstrip().startswith("<MPD") else parse_hls(url, body, **kwargs)
