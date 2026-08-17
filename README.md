# EDCS Corpus Compiler

**Version 0.9** — a tool for building reproducible corpora of Latin
inscriptions from Epigraphik-Datenbank Clauss-Slaby (EDCS) exports.

The Compiler takes plain-text EDCS exports, normalises them into a form
suitable for corpus-linguistic software , and selects a
fixed-size sample deterministically. Every corpus it produces is
identified by a short self-describing key, so that anyone holding the
same source exports can regenerate an identical corpus the same
inscriptions, in the same order. 

Written in standard-library Python. No third-party dependencies.

---

## Contents

- [Why this exists](#why-this-exists)
- [Requirements](#requirements)
- [Getting the data](#getting-the-data)
- [Running the Compiler](#running-the-compiler)
- [The key system](#the-key-system)
- [What the Compiler does to the text](#what-the-compiler-does-to-the-text)
- [Filters](#filters)
- [Output formats](#output-formats)
- [Snapshots](#snapshots)
- [Reproducing a published corpus](#reproducing-a-published-corpus)
- [Limitations](#limitations)
- [Licence and citation](#licence-and-citation)

---

## Why this exists

This tool was necessary for my dissertation, in which I need to generate several large corpora of ancient Roman funerary inscription, as it was for linguistic analysis each inscription had to be stripped of all author expansions and presented as they appeared in the stone they were carved. 

## Requirements

- Python 3.8 or later
- Tkinter (bundled with most Python installations on Windows and macOS;
  on Debian/Ubuntu install `python3-tk`)

Nothing else. The Compiler uses only the standard library.

## Getting the data

**Source data is not distributed with this tool.** this is for copyright reasons, so you will need to obtain the raw data
yourself from EDCS. This takes a few minutes.

1. Go to the EDCS search interface at <https://edcs.hist.uzh.ch/>
   (the database moved to the University of Zurich; older links to
   `db.edcs.eu` may still redirect).
2. Search for the material you want. For funerary epigraphy from a
   single town, set the province and place fields and restrict the
   inscription genus to funerary inscriptions.
3. Export the results as plain text and save the file into a folder
   named `data`, placed beside the Compiler script.
4. **Name the file after the site.** The Compiler infers the dataset
   code from the filename, so `thugga.txt` is recognised as Thugga and
   `uchi_maius.txt` as Uchi Maius. Recognised names are listed below.

```
EDCS_Corpus_Compiler_0_9.py
data/
    thugga.txt
    lambaesis.txt
    rome.txt
    ...
```
note: The current .txt files in the data folder in this repository are placeholders. 
### Recognised dataset names

| Code | Site | Accepted filenames |
|------|------|--------------------|
| `A`  | Ammaedara | `ammaedra`, `ammaedara` |
| `BR` | Bulla Regio | `bulla_regio`, `bullaregio`, `bulla-regio` |
| `C`  | Carthage | `carthage` |
| `H`  | Hadrumetum | `hadrumetum` |
| `L`  | Lambaesis | `lambaesis` |
| `MA` | Mactaris | `mactaris` |
| `MU` | Mustis | `mustis` |
| `R`  | Rome | `rome` |
| `S`  | Sufetula | `sufetla`, `sbeitla` |
| `T`  | Thugga | `thugga`, `dougga` |
| `TB` | Thibursicum Bure | `thibursicum_bure`, `thibursicumbure` |
| `UM` | Uchi Maius | `uchi_maius`, `uchimaius`, `uchi-maius` |

The Compiler recognises sites by matching filenames against a list of
aliases. 

If you want to add a whole new site, and thus a new alias you must edit two dictionaries near the top of the
script. 



**1. Add the code and its aliases** to `_DATASET_CODE_ALIASES`
(around line 42):

```python
_DATASET_CODE_ALIASES = {
    "A":  ["ammaedra", "ammaedara"],
    ...
    "TH": ["thamugadi", "timgad"],      # new entry
    "ALL": ["*"],
}
```

The key is the code that will appear in generated keys. The list holds
every filename you want matched to it — include modern names and common
spelling variants, since matching is exact after normalisation.

**2. Add a display name** to `DATASET_DISPLAY_NAMES` (around line 60),
which controls the label in the checkbox panel:

```python
DATASET_DISPLAY_NAMES = {
    ...
    "TH": "Thamugadi",
}
```

Save the file and restart the Compiler. Name your export to match one of
the aliases (`thamugadi.txt` or `timgad.txt`) and the site will appear
in the panel.

**Choosing a code.** Codes are used verbatim in keys, so keep them
short and uppercase. They must be unique, and must not contain `-`
(the key delimiter) or `+` (which joins multiple sites). Avoid `ALL`,
which is reserved.
Matching ignores case, spaces, hyphens and underscores. Two files
resolving to the same code will raise an error rather than silently
combining. 

## Running the Compiler

```
python EDCS_Corpus_Compiler_0_9.py
```

After running select one or more sites from the checkbox panel, set
the number of inscriptions and any filters, and build. The key is shown
as you work and updates when settings change.

Selecting more than one site combines them into a single corpus, encoded
in the key with `+` (for example `L+T`). `ALL` uses every file in the
data folder and cannot be combined with individual codes.

## The key system

A key encodes everything needed to reproduce a corpus:

```
T-A4GX-TH3Z-B7XC-MSKN-OAUA-2000-ND-GE-F1-L0-38
│ └──────────── seed ────────────┘ │   │  │  └ length filter
│                                  │   │  └─── fragment filter
│                                  │   └────── Greek handling
│                                  └────────── date filter
└───────────────────────────────────────────── site code, then n
```

| Component | Meaning |
|-----------|---------|
| `T` | dataset code (see table above); `+` joins multiple sites |
| `A4GX-…-OAUA` | seed: 96 bits of entropy, Base32-encoded as 20 characters in five groups of four |
| `2000` | number of inscriptions selected |
| `D<start>-<end>` or `ND` | date filter, or none. `N` prefixes a BCE year, so `DN50-100` is 50 BCE to 100 CE |
| `GI` / `GE` / `GO` | Greek text included, excluded, or only |
| `F0` / `F1` | fragments retained or excluded |
| `L<min>-<max>` | character-length filter; the whole component is absent when unused |

Only `-` is used as a delimiter, so a key is safe to use directly as a
filename. Keys are case-insensitive on input.

Keys in the older `|`/`:` format are still parsed correctly.

### How selection works

Each candidate inscription is scored as

```
score(id) = SHA-256(seed | dataset_fingerprint | id)
```

and the inscriptions are sorted by `(score, id)`, with the first *n*
taken. There is no random number generator in the selection path.

Three properties follow:

- **Order-independence.** The score depends only on the inscription ID,
  so the same sample results regardless of how the source file is
  ordered.
- **Tamper-evidence.** The `dataset_fingerprint` is a SHA-256 hash over
  the names and contents of all source files. If the source data changes
  in any way, every score changes, and the key produces a different
  corpus rather than silently appearing to work.
- **Uniformity.** For a fixed seed the sample is fully determined; the
  randomness lies in the seed, which is 12 bytes from `secrets`. Since
  SHA-256 has no known structure that would bias the ordering, the
  procedure is equivalent to simple random sampling without replacement
  from the eligible pool.

Note that "the eligible pool" means the pool *after* filtering. The
sample is unbiased with respect to what passed the filters, not with
respect to ancient epigraphic production: EDCS coverage, differential
survival and publication bias all sit upstream and are untouched.

## What the Compiler does to the text

The Compiler applies a text-normalisation process to raw EDCS exports, which should be saved as .txt files in the data folder and named after the site they contain (e.g. thugga.txt), since the Compiler identifies datasets by filename.

Normalisation is applied per line, in this order:

1. **Metadata lines removed.** Lines beginning `province:`, `place:`,
   `findspot:`, `publication:`, `status:`, `genus:`, `comment:`,
   `material`, `localisation`, `evidence`, `author`, `editor` and
   similar are dropped.
2. **Editorial expansions discarded.** Text in `(…)` and `<…>` is
   removed entirely, so `D(is) M(anibus)` becomes `D M`. This moves the
   text toward what is physically on the stone.
3. **Restorations retained.** Square brackets are stripped but their
   contents kept, so `[H]SE` becomes `HSE`. This is the one respect in
   which the output is not strictly diplomatic: it retains letters that
   are the editor's reconstruction rather than surviving text.
4. **u normalised to V**, and the text uppercased, restoring majuscule
   epigraphic convention.
5. **Abbreviations compacted.** Around thirty sequences common in
   funerary epigraphy are joined into single tokens — `D M S` becomes
   `DMS`, `P V A` becomes `PVA`, `H S E` becomes `HSE`. Without this,
   corpus software counts each letter as a separate type and frequency
   figures become uninterpretable. Longer patterns are applied before
   shorter ones so that `P V A N` is not first reduced to `PVA`.

Inscriptions reduced to nothing by cleaning are dropped. Duplicate EDCS
IDs are removed, keeping the first occurrence.

**These decisions are analytical, not neutral.** Any study using the
Compiler should state them, because they determine what the resulting
counts mean.

## Filters

Applied in this order, before selection:

| Filter | Effect |
|--------|--------|
| Greek | include, exclude, or restrict to inscriptions containing Greek characters |
| Date | requires a `dating:` field overlapping the given range; inscriptions without one are excluded |
| Fragments | drops inscriptions of 3 or fewer non-space characters after cleaning |
| Length | minimum and maximum non-space characters after cleaning |

Length is measured on the cleaned text, not the EDCS original.

If fewer inscriptions pass the filters than requested, the build fails
with a message rather than returning a short corpus.

## Output formats

The save dialogue's file extension determines the format:

- `.txt` — inscriptions separated by `****`, each preceded by its
  EDCS-ID. This is the format intended for AntConc.
- `.csv` — rank, EDCS ID and cleaned text
- `.json` — structured output including the key and token count

Token counting mirrors AntConc's default definition, counting only
maximal runs of letters, so separators, ID labels and numerals are
excluded exactly as AntConc excludes them.

## Snapshots

After a build the Compiler offers to write a snapshot to
`data/snapshots/seed-<key>/`, containing the corpus, a CSV of the
selection in rank order, and a manifest recording the key, filters,
timestamp, and the SHA-256 hash and size of every source file.

Snapshots are **backups, not a cache**. They are written only on request
and are never read back to short-circuit generation: a corpus is always
regenerated from source. The manifest is the useful part — it is what
allows a reader to confirm their EDCS export matches the one used.


## Limitations

**Version 0.9 has not undergone extensive bug testing.** The startup
self-test confirms that deterministic selection behaves as specified; it
cannot rule out design flaws that have yet to reveal themselves. The
tool has so far been used only to generate the corpora for one study.

Known constraints:

- **Overlap between corpora from the same site.** EDCS can assign a
  single inscription to more than one place, so two corpora may share
  inscriptions. This can be checked by cross-referencing EDCS IDs.
- **Parameters cannot be changed while retaining a key.** Any change to
  a filter after generation clears the seed and assigns a new one on the
  next build. Reproducing a prior corpus with altered settings requires
  reconstructing the key by hand — straightforward given the readable
  format, but not supported in the interface.
- **Date filtering excludes undated inscriptions entirely,** since it
  requires a `dating:` field to test against.
- **Restorations are retained,** as described above. Studies sensitive
  to the distinction between surviving and reconstructed text should
  account for this.

## Licence and citation

Licensed under the [MIT License](LICENSE).
EDCS itself should be cited separately. The database is edited by
Manfred Clauss, Anne Kolb, Wolfgang Slaby and Barbara Woitas, and is
continuously revised — record the date of your export.
