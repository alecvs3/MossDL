# Third-party notices

MossDL is licensed under GPL-3.0-or-later. It ships with the direct dependencies
below, each under its own licence (their dependencies are listed in `Cargo.lock`
and `package-lock.json`).

Not shipped: RAR archives are extracted by the user's own 7-Zip if installed; the
solver browser (Clearcote) is downloaded on request from its own releases; ad-block
filter lists are downloaded from their publishers and keep their own licences.

## Rust (transfer core, archive worker, desktop shell)

| Name | Version | Licence |
|---|---|---|
| adblock | 0.13.3 | MPL-2.0 |
| aes | 0.8.4 | MIT OR Apache-2.0 |
| base64 | 0.22.1 | MIT OR Apache-2.0 |
| boringtun | 0.7.1 | BSD-3-Clause |
| bytes | 1.11.1 | MIT |
| ctr | 0.9.2 | MIT OR Apache-2.0 |
| flate2 | 1.1.10 | MIT OR Apache-2.0 |
| fs2 | 0.4.3 | MIT/Apache-2.0 |
| futures-util | 0.3.32 | MIT OR Apache-2.0 |
| md-5 | 0.10.6 | MIT OR Apache-2.0 |
| reqwest | 0.13.2 | MIT OR Apache-2.0 |
| rfd | 0.15.4 | MIT |
| serde | 1.0.228 | MIT OR Apache-2.0 |
| serde_json | 1.0.149 | MIT OR Apache-2.0 |
| sevenz-rust2 | 0.22.2 | Apache-2.0 |
| sha1 | 0.10.7 | MIT OR Apache-2.0 |
| sha2 | 0.10.9 | MIT OR Apache-2.0 |
| smoltcp | 0.14.0 | 0BSD |
| tar | 0.4.46 | MIT OR Apache-2.0 |
| tauri | 2.10.3 | Apache-2.0 OR MIT |
| tauri-plugin-updater | 2.12.0 | Apache-2.0 OR MIT |
| tokio | 1.51.1 | MIT |
| url | 2.5.8 | MIT OR Apache-2.0 |
| windows-sys | 0.61.2 | MIT OR Apache-2.0 |
| zip | 0.6.6 | MIT |

## Interface

| Name | Version | Licence |
|---|---|---|
| @tauri-apps/api | 2.10.1 | Apache-2.0 OR MIT |
| react | 19.2.8 | MIT |
| react-dom | 19.2.8 | MIT |

## Python engine

| Name | Version | Licence |
|---|---|---|
| PyCryptodome | 3.23.0 | BSD, Public Domain |
| PySocks | 1.7.1 | BSD |
| httpx | 0.28.1 | BSD-3-Clause |
| keyring | ? | see package |
