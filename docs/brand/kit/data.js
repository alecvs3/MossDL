// Sample content for the mock UI. Legal, public downloads; sizes are the kind a
// real session shows. Nothing here is a benchmark or a claim.
window.KIT_DATA = {
  // size and done in MB (the app's view-model unit); speed in MB/s
  downloads: [
    { id: "ubuntu", name: "ubuntu-24.04.3-desktop-amd64.iso", host: "releases.ubuntu.com", kind: "disk_images", size: 6040, done: 3810, speed: 31.6, status: "downloading", added: "2 min ago" },
    { id: "osm", name: "germany-latest.osm.pbf", host: "download.geofabrik.de", kind: "code_data", size: 4420, done: 610, speed: 18.9, status: "downloading", added: "4 min ago" },
    { id: "bbb", name: "bbb_sunflower_2160p_60fps_normal.mp4", host: "download.blender.org", kind: "video", size: 642, done: 301, speed: 0, status: "paused", added: "9 min ago" },
    { id: "rec", name: "field-recordings-vol2.flac.zip", host: "pixeldrain.com", kind: "archives", size: 1380, done: 0, speed: 0, status: "queued", added: "9 min ago" },
    { id: "blender", name: "blender-4.5.3-windows-x64.msi", host: "download.blender.org", kind: "applications", size: 372, done: 372, speed: 0, status: "completed", added: "18 min ago" },
    { id: "hubble", name: "hubble-ultra-deep-field.tif", host: "esahubble.org", kind: "pictures", size: 188, done: 188, speed: 0, status: "completed", added: "26 min ago" },
    { id: "manual", name: "pipewire-handbook.pdf", host: "archive.org", kind: "documents", size: 14.2, done: 14.2, speed: 0, status: "completed", added: "31 min ago" },
  ],
  // The page explored in the demo and the Explore stills.
  explore: {
    url: "https://opencinema.org/films/sintel",
    site: "opencinema.org",
    page: "/films/sintel",
    buttons: [
      { id: "m1", label: "Mirror 1 — Free download", sub: "button · form post", host: "filemirror.net" },
      { id: "m2", label: "Mirror 2", sub: "filebin.cc/sintel-2010", host: "filebin.cc" },
      { id: "m3", label: "Internet Archive", sub: "archive.org/details/Sintel", host: "archive.org" },
    ],
    files: [
      { id: "f1", name: "Sintel.2010.1080p.mkv", ext: "mkv", kind: "video", size: 1223, host: "archive.org", match: 96, best: true },
      { id: "f2", name: "Sintel.2010.720p.mp4", ext: "mp4", kind: "video", size: 612, host: "archive.org", match: 88 },
      { id: "f3", name: "sintel-subtitles-all.zip", ext: "zip", kind: "subtitles", size: 0.4, host: "opencinema.org", match: 41 },
      { id: "f4", name: "sintel-original-score.flac", ext: "flac", kind: "audio", size: 318, host: "opencinema.org", match: 37 },
      { id: "f5", name: "sintel-poster.jpg", ext: "jpg", kind: "pictures", size: 2.8, host: "opencinema.org", match: 22, low: true },
    ],
    // What following Mirror 1 walks through, in the browser.
    steps: [
      { label: "Free download", sub: "filemirror.net/f/8Kq2 · clicked", icon: "step" },
      { label: "Wait for the countdown", sub: "15 s · 3 ads blocked", icon: "step" },
      { label: "Get link", sub: "form post · file found", icon: "step" },
    ],
    followed: { name: "Sintel.2010.1080p.mkv", ext: "mkv", kind: "video", size: 1223, host: "filemirror.net" },
  },
  // Mullvad locations for the Connections views. Addresses use documentation ranges.
  locations: [
    { id: "ams", name: "Amsterdam", ip: "198.51.100.23", ms: 24 },
    { id: "fra", name: "Frankfurt", ip: "198.51.100.61", ms: 19 },
    { id: "hel", name: "Helsinki", ip: "203.0.113.40", ms: 41 },
    { id: "lon", name: "London", ip: "203.0.113.88", ms: 28 },
    { id: "nyc", name: "New York", ip: "192.0.2.117", ms: 94 },
    { id: "sto", name: "Stockholm", ip: "192.0.2.14", ms: 33 },
    { id: "tyo", name: "Tokyo", ip: "192.0.2.201", ms: 248 },
    { id: "zrh", name: "Zurich", ip: "198.51.100.9", ms: 22 },
  ],
  history: [
    { name: "Sintel.2010.1080p.mkv", kind: "video", size: 1223, took: "58 s", host: "filemirror.net", activity: "3 steps", finished: "just now" },
    { name: "blender-4.5.3-windows-x64.msi", kind: "applications", size: 372, took: "14 s", host: "download.blender.org", finished: "18 min ago" },
    { name: "hubble-ultra-deep-field.tif", kind: "pictures", size: 188, took: "9 s", host: "esahubble.org", finished: "26 min ago" },
    { name: "pipewire-handbook.pdf", kind: "documents", size: 14.2, took: "2 s", host: "archive.org", finished: "31 min ago" },
    { name: "lecture-notes-week-06.pdf", kind: "documents", size: 6.1, took: "4 s", host: "courses.example.edu", activity: "captcha", finished: "1 h ago" },
    { name: "cc-sample-pack-drums.zip", kind: "archives", size: 244, took: "21 s", host: "filebin.cc", activity: "2 steps", finished: "2 h ago" },
  ],
};
