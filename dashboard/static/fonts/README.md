# Bundled fonts

All WOFF2 files are stored in the repository and copied into the Docker image.
The browser loads them from the panel's own server, without Google Fonts or a CDN.
Shared `@font-face` rules live in `fonts.css`, loaded before `style.css`.

| Family | Weights | Character sets | License |
| --- | --- | --- | --- |
| Golos Text | 400, 500, 600 | Latin, Cyrillic | [SIL OFL 1.1](golostext-OFL.txt) |
| JetBrains Mono | 400, 500, 700 | Latin, Cyrillic | [SIL OFL 1.1](jetbrainsmono-OFL.txt) |
| Russo One | 400 | Latin, Cyrillic | [SIL OFL 1.1](russoone-OFL.txt) |

Golos Text and JetBrains Mono are used by the current UI. Russo One is retained
for compatibility with the earlier design and is loaded only when used.
Preserve license copies and font filenames.

Font sources and license provenance:
[Golos Text](https://github.com/google/fonts/tree/main/ofl/golostext),
[JetBrains Mono](https://github.com/google/fonts/tree/main/ofl/jetbrainsmono),
[Russo One](https://github.com/google/fonts/tree/main/ofl/russoone).
These are reference links; the running application does not request them.

`tests/browser/test_bundled_assets.py` blocks external requests, opens all application
surfaces, and verifies actual local font loading with Latin and Cyrillic samples.
Do not add external imports, font URLs, CDN scripts, or preconnects for UI assets.
