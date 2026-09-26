# Code signing policy

Free code signing provided by [SignPath.io](https://about.signpath.io), certificate by [SignPath Foundation](https://signpath.org).

## What gets signed

Only the Windows installers (`.exe`, `.msi`) and the programs inside them, built from this repository's source by the
[Release workflow](../.github/workflows/release.yml) on GitHub-hosted runners. Nothing built on a personal machine is
signed, and no third-party binaries are signed under this certificate.

## Team roles

- **Committers and reviewers:** [members of this repository](https://github.com/alecvs3/MossDL/graphs/contributors)
- **Approvers:** [@alecvs3](https://github.com/alecvs3)

Every signing request is approved by hand in SignPath before a release is published.

## Privacy

See the [privacy policy](PRIVACY.md): the hosts MossDL contacts on its own, when, and how to turn each one off.
