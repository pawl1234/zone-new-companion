# Release Checklist

Releases are published by `.github/workflows/release.yml` when `main` gets a
new version; see [BUILD.md](BUILD.md#automated-builds-github-actions).

## Before Merging

- [ ] Version set in:
  - `zone_new_companion/__init__.py` (`__version__`; the builds and the release tag read it)
  - `zone_new_companion/app.py` (AppUserModelID)
  - `README.md` (version badge, download commands)
  - `SECURITY.md` (supported versions)
  - `chocolatey/zone-new-companion.nuspec`
- [ ] `CHANGELOG.md`: `## [Unreleased]` renamed to `## [<version>] - <date>`, a new empty `## [Unreleased]` above it
- [ ] App tested from source: Xtream, Stalker and M3U accounts, built-in player, Open in VLC

## After Merging

- [ ] The *Release* workflow in the Actions tab finished
- [ ] The release `v<version>` lists `zone-new-companion-<version>.exe`, `Zone-New-Companion-<version>.dmg` and `SHA256SUMS.txt`
- [ ] The downloaded .exe and .dmg start, play a stream and show the new version

## Chocolatey (optional, manual)

1. Copy the release's .exe to `chocolatey/tools/zone-new-companion.exe` and update the checksum in `chocolatey/tools/VERIFICATION.txt`
2. Pack and push:
   ```powershell
   cd chocolatey
   choco pack
   choco push zone-new-companion-kidpoleon.<version>.nupkg --source https://push.chocolatey.org/
   ```
