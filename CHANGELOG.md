# Changelog

All notable changes to zone-new-companion will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.0.2] - 2026-10-22

### Fixed
- **Windows: no freeze after startup**: with many accounts the window stopped responding for several seconds right after starting and again whenever the search index moved on (about 9.5 s with 361 accounts); the *Search* column of Working Accounts now updates in one go
- **Windows: full-screen player no longer flickers**: the picture went black and froze each time the control bar was shown or hidden, because the graphics driver switched the window into exclusive full screen; the video now stays in windowed full screen

### Changed
- **Smaller Windows executable** (about 15 MB less): the build leaves out files the app never uses: Qt's software OpenGL (`opengl32sw.dll`), Qt PDF, Qt translations, unused image formats, the README screenshots and setuptools; Python code is built with docstrings removed
  - On machines without an OpenGL driver (some VMs and remote desktop sessions) the built-in player cannot start; streams play in VLC there

## [2.0.1] - 2026-10-22

### Fixed
- **Windows: the 2.0.0 executable did not start**: it closed right away without a message. The release build used the runner's preinstalled Python 3.15 instead of 3.12, and the exe lacked `python3t.dll`, which cryptography's extension needs (*DLL load failed while importing _rust*)
  - The build script uses the Python set up by the workflow (`pythonLocation`) before the `py` launcher
  - The build starts the finished exe with `--self-test` (imports the whole app, loads libmpv) and fails when that doesn't work, so a broken exe is no longer released
- **Startup errors are shown**: when the app cannot start, a Windows error dialog shows the reason, which is also saved to `~/.zone-new-companion/logs/startup-error.log`; before, the windowed exe had no console and the error was lost

## [2.0.0] - 2026-10-22

### Added
- **Import from base64 string**: *Add → Import from base64 string…* decodes a shared string, shows what's in it and offers to fetch the accounts from the link inside (or to check the accounts it contains directly); the found accounts open in the bulk check right away
  - paste.sh pastes are decrypted with the key from the link (`#…`), pastebin.com links read the raw paste, other links are downloaded as text
  - Account blocks with styled letters (`𝙐𝙨𝙚𝙧 : …`, `𝐌𝟑𝐔 : …`) are recognised; titles, notes and duplicates are left out
- **Search across accounts**: New *Search* tab (Ctrl+F) finds movies, series and live channels in every working XTREAM and M3U account at once, filtered by account (all, starred, the one being browsed) and type
  - Catalogs download in the background into `~/.zone-new-companion/catalog.db` and refresh daily; accounts on the same server with the same packages share one download
  - Accounts that are down (red) drop out of the results right away; removing an account deletes its data
  - Double-click plays a channel or movie on the account that has it, or opens a series' seasons
  - The same title on several accounts is one row ("in 7 accounts") with its year, quality and languages; double-click plays on the best account (starred, streaming, free connection, best quality), expanding the row lists every copy
  - Filters for quality, release year, recently added, category, streaming accounts, free connections and expiry; sorting by match, year, date added, number of accounts or title; the Language setting applies to the search as well
  - Only the first 5 working accounts are indexed by default (starred first, then the ones that stream); View → Search index changes the limit, and a new *Search* column in Working Accounts shows each account's state (Indexed, Indexing…, Queued, Not indexed, Paused)
  - Results that update in the background keep expanded rows, the selection and the scroll position
  - Catalogs also refresh while the app stays open (checked hourly, failed ones are retried); View → Search index sets how often (12 hours, 24 hours, 3 days), shows the index size, and offers *Refresh all now* and *Clear index…*
  - M3U accounts are searchable too: the playlist is downloaded once and read in one pass; `get.php` links are indexed through their Xtream API (movies and series apart, shared with the same account added as XTREAM) and fall back to the playlist when the panel refuses it
  - Right-click an account in Working Accounts → *Refresh search index* downloads its catalog again right away; a failed refresh keeps the older catalog and shows the error in the Search column's tooltip
  - Accounts you open (double-click) or add with right-click → *Add to search index* are indexed whatever the limit, also after a restart; the limit only caps the accounts indexed automatically, so a bulk check of 100 accounts still downloads just a few catalogs
  - Browsing an indexed XTREAM account lists categories from its catalog instead of asking the server, so they open at once; categories the catalog doesn't have yet (or a catalog from before the categories changed) still come from the server
  - Catalog schema 5 stores channel numbers and EPG ids for that; existing catalogs are kept for search and downloaded again in the background once

### Changed
- **Recheck is gentler on servers and more reliable**: at most 6 accounts are checked at once per server (24 overall), alternating between servers; panels answered bursts with *503 Service Temporarily Unavailable*, which turned working accounts red
  - One timeout no longer turns every remaining account of that server red: a server is skipped only after 3 failed connections in a row (12 if it answered earlier in the run), or at once when its name doesn't resolve; the same applies to stream servers
  - The progress count moves as each account finishes instead of waiting for the slowest one ahead of it
  - Recheck fetches each account's category lists and stores them in the search index (a partial index the full download later builds on); accounts with the same categories share them, and the stream test reuses channels already known for that catalog
- **Opening a working account is instant**: XTREAM accounts open with the categories a Recheck or an earlier visit stored; the server is asked in the background and the list updates when it changed
  - A newly added account opens with the categories its check already fetched, instead of asking the server again
- Accounts beyond the search index limit keep their categories; only their streams are left out of the index
  - Indexed accounts keep their place: a new or re-sorted account waits for a free slot instead of pushing another account's catalog out (starring one still brings it in)
  - Removing an account, or one dropping beyond the limit, stops its queued or running catalog download
- **Stream Verification without external tools**: Verify now plays a few seconds of each stream headless through the bundled libmpv and reports *OK (Video+Audio)*, *OK (Video)*, *OK (Audio)* or why it is *OFF* (Unreachable, Timeout, Stalled, No Video/Audio)
  - Without libmpv it reads the stream's container instead (MPEG-TS, MP4, HLS, ADTS) and checks that video and audio packets arrive
  - At most 4 streams are probed at once; streams refused at connect are retried one by one, so accounts limited to one connection verify correctly
- **Automatic releases**: Merging a version bump into `main` builds the Windows `.exe` and the macOS `.dmg` on GitHub Actions and publishes them as a GitHub release, with this changelog's section as the release notes
  - `scripts/build_windows.ps1` and `scripts/build_macos.sh` build each platform locally, with libmpv bundled

### Fixed
- **Closing the app during background work**: Closing while catalogs were indexing (or a check was running) crashed Python on exit; indexing now stops and the app closes cleanly

### Removed
- **FFprobe, FFmpeg and Tesseract OCR**: No longer used; the opencv-python, pytesseract, Pillow and numpy requirements are gone, too

## [1.2.2] - 2026-05-09

### Fixed
- **GitHub Actions Workflow**: Fixed release automation
  - Added proper permissions for release creation
  - Replaced deprecated actions with modern alternatives
  - Workflow now builds all platforms in parallel
  - Fixed 'Resource not accessible by integration' error

### Changed
- **Version bump**: Incremented to v1.2.2 to trigger new release workflow

## [1.2.1] - 2026-05-09

### Fixed
- **OptimizedSession Integration**: Fixed all services to properly use OptimizedSession
  - Fixed xtream_service.py to use `self._session.get()` instead of `self._session.session.get()`
  - Fixed m3u_service.py to use `self._session.get()` instead of `self._session.session.get()`
  - Fixed stream_verifier.py to use `self._session.get()` instead of `self._session.session.get()`
  - All services now benefit from protocol rotation and SSL fallback logic

### Changed
- **Build System**: Replaced manual build scripts with GitHub Actions workflow
  - Removed build-win.py and build-unix.py
  - Created comprehensive BUILD.md with build instructions for all platforms
  - Created .github/workflows/build-release.yml for automated cross-platform builds
  - Automated builds for Windows (.exe), Linux (.AppImage), and macOS (.dmg)

### Removed
- **Distribution Packages**: Removed Chocolatey and Snap packaging
  - Removed DISTRIBUTION.md
  - Removed chocolatey/ folder (keeping for reference but not maintained)
  - Users should now build from source or use GitHub Releases

### Documentation
- Updated README.md to remove package manager installation instructions
- Added comprehensive build instructions in BUILD.md
- Updated SECURITY.md with correct email (kidpoleon@proton.me)
- Developer name updated to "Kidpoleon" consistently across files

## [1.2.0] - 2026-05-09

### Fixed
- **Stalker Stream URL**: Fixed missing stream ID parameter causing playback failures
  - Changed from `stream=&extension=ts` to `stream=1924348&extension=ts`
  - Reordered resolution strategies to prioritize cmd field from metadata
  - Added validation to ensure stream= parameter is not empty before accepting URL

### Added
- **Chocolatey Package**: Created full Chocolatey distribution package for Windows
  - Added nuspec manifest with VLC dependency
  - Created PowerShell install/uninstall scripts
  - Included LICENSE.txt and VERIFICATION.txt for embedded binary
- **Snap Package**: Created Snap distribution package for Linux
  - Added snapcraft.yaml with proper desktop integration
  - Created desktop entry for application menu
  - Configured strict confinement with necessary plugs
- **Enhanced VLC Detection**: Added more VLC path locations across all platforms
  - Windows: Microsoft Store VLC paths
  - macOS: Homebrew paths for both Apple Silicon and Intel
  - Linux: Additional system and user-local paths

### Changed
- **Credits**: Added acknowledgments to referenced projects
  - [IPTV-MAC-STALKER-PLAYER-BY-MY-1](https://github.com/peterpt/IPTV-CHECK) by peterpt
  - [stalkerhek](https://github.com/CrazeeGhost/stalkerhek/) by CrazeeGhost

### Documentation
- Added comprehensive DISTRIBUTION.md guide for package maintainers
- Documented Chocolatey and Snap publishing procedures

## [1.1.4] - 2026-05-08

### Added
- **Advanced M3U8 Verification**: Resolved issue where playable M3U8 streams showed as red/unverified
- **Enhanced HLS Detection**: Improved M3U8 playlist validation with comprehensive marker detection
- **OCR Integration**: Integrated OCR functionality for stream validation when FFprobe fails
- **Multi-Stage FFprobe**: Implemented 3-stage FFprobe analysis for M3U8 streams with fallback strategies
- **Adaptive Stream Support**: Better handling of adaptive bitrate streams and live content
- **Permissive Validation**: More lenient M3U8 validation that recognizes valid stream structures
- **OCR Frame Analysis**: Automatic frame capture and text analysis for content validation
- **Comprehensive Logging**: Detailed debug logging for verification troubleshooting

### Fixed
- M3U8 verification false negatives
- Stream verification accuracy issues
- SSL session usage across all services

### Improved
- Stream verification reliability
- M3U8 playlist parsing
- Error handling and logging

## [1.1.3] - 2026-05-08

### Added
- **M3U URL Compatibility**: Fixed issues reading problematic M3U URLs including short URLs and redirects
- **Optimized Session Integration**: M3U service now uses optimized SSL sessions for better compatibility
- **Enhanced Redirect Handling**: Better support for short URLs (s.id, bit.ly, tinyurl.com) with proper user agents
- **Alphabetical Sorting**: All categories and items now sort alphabetically across all services (M3U, Xtream, Stalker)
- **Improved UI/UX**: Enhanced visual feedback with alternating row colors and better tooltips
- **Table Sorting**: Enabled table sorting and improved visual styling with modern dark theme
- **Better Error Handling**: More robust M3U parsing with enhanced logging and error recovery
- **User Experience**: Improved tooltips, visual indicators, and overall application polish

### Fixed
- M3U service connection issues
- Sorting inconsistencies across services
- UI visual feedback problems

## [1.1.2] - 2026-05-08

### Added
- **Fixed False Negatives**: Resolved issue where playable channels showed as red/unverified
- **Optimized Session Usage**: Stream verifier now uses optimized SSL sessions for better compatibility
- **Enhanced Progress Reporting**: Terminal now shows detailed verification progress for each channel
- **Improved Fallback Logic**: Added secondary reachability checks with longer timeouts
- **Better Error Handling**: More permissive verification logic reduces false positives
- **Detailed Logging**: Individual channel status updates with progress percentages
- **Optimized FFprobe**: Improved stream analysis with better timeout handling
- **Permissive Verification**: Streams marked as OK if reachable even if A/V probe fails

### Fixed
- Verification false negatives
- SSL connection issues in verification
- Progress reporting problems

## [1.1.1] - 2026-05-08

### Added
- **OCR Service Integration**: Complete OCR service based on IPTV-CHECK project for video stream validation
- **FFmpeg Frame Extraction**: Automatic frame capture from video streams for content analysis
- **Tesseract OCR Engine**: Multi-language text recognition with error detection patterns
- **Advanced DNS Resolution**: Multiple DNS server fallback (Google, Cloudflare, OpenDNS, Quad9)
- **Enhanced Connectivity Testing**: Multiple endpoint testing for better server compatibility
- **Connection Diagnostics**: Comprehensive connection testing with detailed diagnostics
- **Complete Logs Removal**: Fully removed logs functionality from title bar and application
- **Performance Optimization**: Improved connection success rates through advanced strategies

### Fixed
- Logs functionality removal
- DNS resolution issues
- Connection reliability problems

## [1.1.0] - 2026-05-08

### Added
- **Multi-Profile SSL Handling**: 4 SSL configuration profiles (default, legacy, permissive, modern) for maximum compatibility
- **SSL Fallback Strategy**: Automatic fallback through different SSL configurations when connection fails
- **HTTP/HTTPS Protocol Rotation**: Automatic protocol switching with caching for optimal connection method
- **Comprehensive Cipher Support**: Support for legacy and modern cipher suites including `ALL:@SECLEVEL=0`
- **Connection Method Caching**: Remembers successful protocols per domain for faster subsequent connections
- **Enhanced Error Recovery**: Better SSL error handling with multiple retry strategies
- **Server-Specific Optimization**: Adaptive SSL configuration based on server response patterns

### Fixed
- SSL connection issues
- Protocol switching problems
- Connection timeout issues

## [1.0.9] - 2026-05-08

### Added
- **Network Optimizations**: 50x speed improvement in connection handling
- **DNS Resolution**: Enhanced DNS resolution with multiple servers
- **Connection Pooling**: Improved connection reuse and management
- **Timeout Optimization**: Better timeout handling for different server types

### Fixed
- Connection speed issues
- DNS resolution failures
- Connection pooling problems

## [1.0.8] - 2026-05-08

### Added
- **Initial Release**: Base functionality for Xtream and Stalker portal support
- **Unified UI**: Single interface for multiple IPTV portal types
- **Modular Architecture**: Clean separation of concerns
- **Cross-Platform Support**: Windows, Linux, and macOS compatibility
- **VLC Integration**: Automatic VLC detection and launch
- **Configuration Management**: Persistent settings and history
- **Async Operations**: Non-blocking UI with background workers

---

## [Unreleased]

### Planned
- Enhanced EPG support
- Custom themes and UI customization
- Advanced filtering and search
- Stream recording capabilities
- Mobile app companion
- Plugin system for extensibility

---

**Note:** For detailed information about each version, please refer to the release notes on GitHub.
