# MossDL brand assets

Promotional graphics and the product demo for the README and website. Every
image is built in HTML/CSS from the app's real stylesheets
(`src/figma/index.css`, `ui/controls.css`, the Explore, Connections and
Captchas page CSS) and the real component structure, then rendered in a
headless browser. Nothing is a screenshot, and no numbers in them are
benchmarks: sizes, speeds and latencies are sample values of the kind a normal
session shows.

## Files

| File | Size | Use |
| --- | --- | --- |
| `social-preview.png` | 1280×640 | GitHub → Settings → Social preview (kept under 1 MB) |
| `hero-dark.png` / `hero-light.png` | 1600×800 | README hero, paired with `<picture>` |
| `feature-downloads.png` | 1200×700 | Segmented downloads on the Rust core |
| `feature-explore.png` | 1200×700 | Explore: a page as a tree, best match, filters |
| `feature-following.png` | 1200×700 | Following buttons through countdowns, ads blocked |
| `feature-connections.png` | 1200×700 | VPN/proxy routes, WireGuard in the app, Mullvad sign-in |
| `feature-captchas.png` | 1200×700 | Solvers first, verified browser handoff |
| `demo.mp4` | 1920×1080, ~25 s, H.264 | The full demo (dark app theme) |
| `demo-light.mp4` | 1920×1080 | The same session in the light (daylight) theme |
| `demo-dark.gif` / `demo-light.gif` | 880 px wide, 10 fps | README fallback; `demo.gif` is a copy of the dark one |
| `demo-poster.png` | 1920×1080 | Poster frame for the video (Downloads beat) |

The demo walks one session: paste a page into Explore, the tree builds, Mirror 1
is followed through a countdown in the background browser with ads blocked, the
found file goes to Downloads and starts, the route switches to a Mullvad
WireGuard location, and the finished file is in History. Kinetic title cards sit
between the beats; numbered captions name each step.

The GIFs start on the finished title card (logo, name, tagline), so their first
frame reads on its own: GitHub shows only that frame to people who prefer
reduced motion.

## Regenerating

```text
python docs/brand/render.py                 # everything (about 15 minutes)
python docs/brand/render.py icons           # kit/icons.js from src/figma/icons.tsx and src/lib/fileKinds.ts
python docs/brand/render.py stills          # all PNGs
python docs/brand/render.py stills hero-dark feature-explore
python docs/brand/render.py demo            # both themes: mp4, gif, poster
python docs/brand/render.py demo dark
```

Needs Python with `patchright` and Pillow, and ffmpeg (`PATH` or
`C:\ffmpeg\bin\ffmpeg.exe`). The browser is Clearcote when installed
(`engine.clearcote_manager.get_clearcote_executable()`), otherwise patchright's
Chromium. Stills render at device scale 2 and are downsampled with Lanczos. The
demo is seeked frame by frame (`window.__seek(t)`) at 30 fps and 1.2× scale, so
every frame is deterministic, then encoded with ffmpeg.

Because the kit links the live app stylesheets, a re-render picks up UI changes
(theme colours, spacing) automatically.

## Layout of the sources

```text
docs/brand/
  render.py              renders everything
  kit/                   one file per UI object
    load.js              links the app's CSS and the kit into any page
    icons.js             generated: the app's icon paths and file kinds
    data.js              sample downloads, the explored page, locations, history
    brand.css cards.css  the stage (type, colours, pills) and floating cards
    mock.css             layout glue the React tree gets from Tailwind
    topo.js              contour-line background
    ui/atoms.js          kind tiles, site icons, status badges, cells, sparklines
    ui/shell.js          title bar, menus, toolbar, sidebar, window frame
    ui/downloads.js explore.js tree.js connections.js history.js cards.js
  scenes/                social.html, hero.html?theme=dark|light, feature.html?f=<id> (+ features.js)
  demo/
    demo.html            open it to watch (click pauses, arrow keys step, ?theme=light)
    timeline.js          the story: beat times T, CAPTIONS, INTERSTITIALS, app state per t, camera, cursor path
    titles.js            intro, interstitial and outro cards, step captions
    engine.js text.js player.js   clock, DOM morphing, cursor, kinetic type, render loop
```

To re-edit a beat, change its time in `T` in `demo/timeline.js` (everything in
that beat is relative to it), its words in `CAPTIONS`/`INTERSTITIALS`, or what
the app shows in `app(t)`. Sample content lives in `kit/data.js`.

## Embedding in the README

Hero with light/dark pairing:

```html
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/brand/hero-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="docs/brand/hero-light.png">
  <img alt="MossDL: point it at a page, get the file" src="docs/brand/hero-dark.png" width="100%">
</picture>
```

Demo GIF, following the reader's theme:

```html
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/brand/demo-dark.gif">
  <source media="(prefers-color-scheme: light)" srcset="docs/brand/demo-light.gif">
  <img alt="A MossDL session: Explore a page, follow a mirror, download, switch VPN route, History" src="docs/brand/demo-dark.gif" width="100%">
</picture>
```

Video: GitHub does not play a `<video>` tag that points at a file in the
repository. To show `demo.mp4` with controls, drag it into the README editor on
github.com (or into an issue/release); GitHub uploads it and inserts a
`https://github.com/user-attachments/assets/…` link that renders as a player.
On the website use:

```html
<video src="demo.mp4" poster="demo-poster.png" autoplay muted loop playsinline width="1600"></video>
```

Feature cards work as a two-column table or stacked images:

```html
<img src="docs/brand/feature-explore.png" width="49%"> <img src="docs/brand/feature-following.png" width="49%">
```

## Notes

- Hosts and pages in the samples are public, legal downloads (Ubuntu, Blender,
  Geofabrik, Internet Archive) or made-up names (`opencinema.org`,
  `filemirror.net`, `filebin.cc`). Exit IPs use documentation address ranges.
- Provider and host logos are the ones the app itself bundles
  (`src/figma/assets/vpn`, `src/figma/assets/providers`).
- Fonts are the system's Segoe UI Variable and Cascadia Mono, as in the app;
  rendering on a machine without them falls back to other sans/mono fonts.
