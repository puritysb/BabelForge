# Portable bilingual EPUB contract

BabelForge produces reflowable EPUB 3 for ordinary reading systems. The normative
baseline is [W3C EPUB 3.3](https://www.w3.org/TR/epub-33/). Firmware-specific parsing,
fonts, memory limits and language-toggle controls are outside this contract.

- ZIP begins with uncompressed `mimetype`; container.xml points to the OPF.
- OPF declares the original and translation languages, stable book identity, title,
  author, modification time, resources, spine and navigation. Known source URLs,
  rights metadata, source SHA-256 and translation profile are recorded as provenance.
- Logical source reading order comes from EPUB spine. Navigation documents are not
  retranslated as prose. Linked non-spine footnotes are included and links rewritten.
- A `Chapter` carries `title`, `paragraphs`, optional XHTML `template`, and packaged
  `assets`. Template blocks carry temporary `data-bf-unit` indices matching inline
  XHTML translation units. Template indices and visible source text are verified
  during assembly; the temporary attributes never enter the published file.
- Semantic containers (headings, paragraphs, lists, tables, verse, notes) are retained.
  Long paragraphs are subdivided into bounded translation units inside their existing
  container, preserving source text, whitespace and inline structure.
  Each translated block contains adjacent `span.bf-original` and
  `span.bf-translation` elements with `lang` and `xml:lang`; CSS displays both as
  blocks. Inline emphasis and links remain markup. These class names are only CSS
  hooks, not a custom reading-system requirement.
- Source IDs exist once; translation copies do not duplicate IDs. Images and opaque
  structures stay outside translated slots. Resource and fragment targets must exist.
- TXT becomes paragraph pairs without hardware-driven file splitting or short-chapter
  merging. Plain text is XML-escaped before entering the inline XHTML interface.
- No scripting or active embedded content is included. Unavailable source media causes
  an explicit extraction failure instead of silently producing an incomplete book.
- The navigation document lists chapters; optional translated titles remain supported.
  A declared NCX provides compatibility. Cover MIME matches its bytes.
- Output uses generic serif fonts and reader-adjustable layout. Custom fonts are not
  needed. CSS must not hide either language or require JavaScript for reading.
- Assembly writes a temporary archive, validates it, then replaces the destination
  atomically. Empty translations, broken anchors, unresolved resources and pair-count
  mismatches prevent publication. Final translation audits precede assembly.

`epub_validate.py` performs deterministic local checks and runs the official
[EPUBCheck](https://www.w3.org/publishing/epubcheck/) distribution when installed.
Local validation is not a claim of complete EPUB conformance. For a strict publishing
gate, set `BABELFORGE_REQUIRE_EPUBCHECK=1`; install with `scripts/setup_epubcheck.py`
or supply `EPUBCHECK_JAR`. CI installs the pinned checker and runs integration fixtures.

Legacy `cp-original`/`cp-translation` outputs remain readable archives but are not the
format emitted by new runs. Existing files are not rewritten automatically. Prompt,
extraction and quality-profile compatibility rules govern checkpoint reuse; changing
this contract must not silently reuse incompatible translations.
