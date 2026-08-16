import sys
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
import secrets
import base64
import re
import json, csv, hashlib, datetime
from typing import List, Tuple, Dict, Optional

# ---------------------------------------------
# EDCS CorpusBuilder v1.3.0
# Changes from v1.2.0:
#   - SHA-256 deterministic selection is now the PRIMARY path.
#     Snapshots are NEVER used as a short-circuit; they are
#     write-only backups created only on explicit user request.
#   - Character length filter (non-space chars, post-cleaning)
#     encoded in key as L<min>_<max>. Absent = no filter.
#   - Optional "Create corpus backup?" prompt after generation.
#   - Explicit SHA-256 self-test on startup.
# ---------------------------------------------

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).resolve().parent

DATA_FOLDER = BASE_DIR / "data"
SNAPSHOT_ROOT = DATA_FOLDER / "snapshots"

# Regexes
GREEK_RANGE_RE = re.compile(r"[\u0370-\u03FF\u1F00-\u1FFF]")
DATE_RE = re.compile(r"dating:\s*(-?\d+)\s+to\s+(-?\d+)", re.I)
EDCS_RE = re.compile(r"EDCS-ID\s*:\s*([A-Za-z0-9\-]+)", re.I)
ID_LINE_RE = re.compile(r"^\s*EDCS-ID\s*:\s*([A-Za-z0-9\-]+)\s*$", re.I)
EDCS_BARE_RE = re.compile(r"^\s*(EDCS-\d+)\s*$", re.I)

APP_VERSION = "1.6.6"

# ---------------------------------------------
# Dataset code aliases
# ---------------------------------------------
_DATASET_CODE_ALIASES = {
    "A":  ["ammaedra", "ammaedara"],
    "BR": ["bulla_regio", "bullaregio", "bulla-regio", "bulla regio"],
    "C":  ["carthage"],
    "H":  ["hadrumetum"],
    "L":  ["lambaesis"],
    "MA": ["mactaris"],
    "MU": ["mustis"],
    "UM": ["uchi_maius", "uchimaius", "uchi-maius", "uchi maius"],
    "R":  ["rome"],
    "S":  ["sufetla", "sbeitla"],
    "TB": ["thibursicum_bure", "thibursicumbure", "thibursicum bure", "thibursicum-bure"],
    "T":  ["thugga", "dougga"],
    "ALL": ["*"],
}

# Human-readable display names for the checkbox panel
DATASET_DISPLAY_NAMES = {
    "A":  "Ammaedara",
    "BR": "Bulla Regio",
    "C":  "Carthage",
    "H":  "Hadrumetum",
    "L":  "Lambaesis",
    "MA": "Mactaris",
    "MU": "Mustis",
    "UM": "Uchi Maius",
    "R":  "Rome",
    "S":  "Sufetula",
    "TB": "Thibursicum Bure",
    "T":  "Thugga",
}

def _norm_stem(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").strip().lower())

def infer_dataset_code_from_filename(filename: str) -> str:
    if not filename:
        return ""
    stem = _norm_stem(Path(filename).stem)
    for code, aliases in _DATASET_CODE_ALIASES.items():
        if code == "ALL":
            continue
        for a in aliases:
            if stem == _norm_stem(a):
                return code
    return ""

def resolve_dataset_code_to_files(code: str, all_files: List[Path]) -> List[Path]:
    code = (code or "").strip().upper()
    if not code:
        return []
    if code == "ALL":
        return list(all_files)
    if "+" in code:
        sub_codes = [c.strip() for c in code.split("+") if c.strip()]
        if "ALL" in sub_codes:
            raise ValueError("'ALL' cannot be combined with other dataset codes.")
        out: List[Path] = []
        for c in sub_codes:
            out.extend(resolve_dataset_code_to_files(c, all_files))
        return out
    aliases = _DATASET_CODE_ALIASES.get(code)
    if not aliases:
        raise ValueError(f"Unknown dataset code: {code}")
    matches: List[Path] = []
    for p in all_files:
        stem = _norm_stem(p.stem)
        for a in aliases:
            if stem == _norm_stem(a):
                matches.append(p)
                break
    if len(matches) > 1:
        names = ", ".join(m.name for m in matches)
        raise ValueError(f"Dataset code '{code}' matches multiple files: {names}")
    return matches

def normalize_edcs_id(id_or_header: str) -> str:
    m = EDCS_RE.search(id_or_header) or ID_LINE_RE.match(id_or_header)
    raw = (m.group(1) if m else id_or_header).strip()
    return re.sub(r"[^A-Za-z0-9\-_]", "_", raw)


# ---------------------------------------------
# Seed utilities
# ---------------------------------------------
_SEED_NBYTES = 12
_BASE32_RE = re.compile(r"^[A-Z2-7]+$")

def _base32_nopad(b: bytes) -> str:
    return base64.b32encode(b).decode("ascii").rstrip("=")

def _group_seed(s: str, group: int = 4) -> str:
    s = s.strip().upper().replace("-", "").replace(" ", "")
    return "-".join(s[i:i+group] for i in range(0, len(s), group))

def generate_seed() -> str:
    return _base32_nopad(secrets.token_bytes(_SEED_NBYTES))

def canonicalize_seed(seed_text: str) -> str:
    s = (seed_text or "").strip().upper().replace("-", "").replace(" ", "")
    if not s:
        return ""
    if s.isdigit():
        return s
    if not _BASE32_RE.match(s):
        raise ValueError("Key must be decimal digits or Base32 (A-Z, 2-7), optionally with hyphens.")
    return s

def display_seed(seed_canon: str) -> str:
    if not seed_canon:
        return ""
    if seed_canon.isdigit():
        return seed_canon
    return _group_seed(seed_canon, 4)


# ---------------------------------------------
# Params suffix  (D - G - F - L)   [v1.6.4: '-'-only format]
# ---------------------------------------------
# Format:  D<start>-<end>-GI/GE/GO-F0/F1[-L<min>-<max>]
# Example: D1-400-GE-F1-L0-50
# Absent L component = no length filter (fully backward compatible)
# This format uses ONLY '-' as a delimiter (no '|' or ':') so the
# full key is safe to use directly as a filename / snapshot folder name.

def _canonical_params_suffix(
    date_enabled: bool,
    start_year: Optional[int],
    end_year: Optional[int],
    greek_mode: str,
    exclude_fragments: bool,
    len_min: Optional[int] = None,
    len_max: Optional[int] = None,
) -> str:
    gmap = {"Include": "GI", "Exclude": "GE", "Greek Only": "GO"}
    greek_code = gmap.get((greek_mode or "").strip(), "GI")
    frag_code = "F1" if exclude_fragments else "F0"
    if date_enabled and start_year is not None and end_year is not None:
        start_tok = f"N{abs(int(start_year))}" if int(start_year) < 0 else str(int(start_year))
        end_tok = f"N{abs(int(end_year))}" if int(end_year) < 0 else str(int(end_year))
        date_code = f"D{start_tok}-{end_tok}"
    else:
        date_code = "ND"
    parts = [date_code, greek_code, frag_code]
    # Length filter slot — only included when at least one bound is set
    if len_min is not None or len_max is not None:
        lo = int(len_min) if len_min is not None else 0
        hi = int(len_max) if len_max is not None else 99999
        parts.append(f"L{lo}-{hi}")
    return "-".join(parts)


def _parse_params_parts(parts: List[str]) -> Dict:
    """
    Parse a flat list of '-'-split tokens into params.
    Handles multi-token fields (D<start>, <end> and L<min>, <max> each
    arrive as TWO separate list entries since '-' is the universal
    delimiter) by consuming a lookahead token where needed.
    """
    out = {
        "date_enabled": None,
        "start_year": None,
        "end_year": None,
        "greek_mode": None,
        "exclude_fragments": None,
        "len_min": None,
        "len_max": None,
    }
    i = 0
    while i < len(parts):
        p = (parts[i] or "").strip().upper()
        if not p:
            i += 1
            continue
        if p == "ND":
            out["date_enabled"] = False
            i += 1
            continue
        if p.startswith("D") and len(p) > 1 and (p[1:].isdigit() or p[1:].startswith("N")):
            # D<start> followed by a separate <end> token; either may carry
            # an 'N' prefix denoting a negative (BCE) year, e.g. DN600-400
            def _decode_year_tok(tok: str) -> int:
                if tok.startswith("N"):
                    if not tok[1:].isdigit():
                        raise ValueError
                    return -int(tok[1:])
                if not tok.isdigit():
                    raise ValueError
                return int(tok)
            try:
                start_val = _decode_year_tok(p[1:])
                if i + 1 >= len(parts):
                    raise ValueError
                end_val = _decode_year_tok(parts[i + 1].strip().upper())
                out["date_enabled"] = True
                out["start_year"] = start_val
                out["end_year"] = end_val
                i += 2
                continue
            except Exception:
                raise ValueError("Invalid dating parameter. Use ND or D<start>-<end> (prefix negative years with N, e.g. DN600-400).")
        if p in ("GI", "GE", "GO"):
            out["greek_mode"] = {"GI": "Include", "GE": "Exclude", "GO": "Greek Only"}[p]
            i += 1
            continue
        if p in ("F0", "F1"):
            out["exclude_fragments"] = (p == "F1")
            i += 1
            continue
        if p.startswith("L") and (p[1:].isdigit() or p[1:] == ""):
            try:
                lo_val = int(p[1:])
                if i + 1 >= len(parts):
                    raise ValueError
                hi_val = int(parts[i + 1])
                out["len_min"] = lo_val
                out["len_max"] = hi_val
                i += 2
                continue
            except Exception:
                raise ValueError("Invalid length parameter. Use L<min>-<max> e.g. L0-50.")
        raise ValueError(f"Unrecognized token parameter: '{parts[i]}'.")
    return out


_SEED_CHARS = 20  # fixed width: 12 bytes -> 20 chars Base32 (no padding)

def parse_seed_token(seed_text: str) -> Tuple[str, int, str, Dict]:
    """
    Parse a key of the form:
        CODE-SEEDGROUP-SEEDGROUP-SEEDGROUP-SEEDGROUP-SEEDGROUP-N-PARAMS...
    where CODE may itself contain '+' for multi-site combos (e.g. L+T),
    SEED is always exactly 20 alphanumeric Base32 characters (5 groups
    of 4, '-'-separated), N is the inscription count, and PARAMS is the
    '-'-delimited parameter tail (see _parse_params_parts).

    Also accepts legacy '|'/':' format for full backward compatibility
    with keys generated before v1.6.4.
    """
    empty_params = {
        "date_enabled": None, "start_year": None, "end_year": None,
        "greek_mode": None, "exclude_fragments": None,
        "len_min": None, "len_max": None,
    }
    if not seed_text:
        return "", 0, "", empty_params

    raw = seed_text.strip().upper()

    # ── Legacy format detection: presence of '|' or ':' means old key ──
    if "|" in raw or ":" in raw:
        return _parse_seed_token_legacy(raw)

    # ── New '-'-only format ─────────────────────────────────────────
    tokens = [t for t in raw.split("-") if t != ""]
    if not tokens:
        return "", 0, "", empty_params

    dataset_code = ""
    idx = 0

    # First token: dataset code, IF it is not itself a valid seed group.
    # A seed group is exactly 4 Base32 characters. The code is never a
    # bare 4-char Base32-looking group by construction (codes are 1-3
    # letters, or '+'-joined combos), but to be unambiguous we always
    # treat the first token as the code UNLESS the whole remaining
    # token stream doesn't have a code at all (very old/manual keys).
    first = tokens[0]
    is_known_code = (
        first == "ALL"
        or first in _DATASET_CODE_ALIASES
        or ("+" in first and all(
            c.strip() in _DATASET_CODE_ALIASES and c.strip() != "*"
            for c in first.split("+") if c.strip()
        ))
    )
    if is_known_code:
        if "+" in first:
            sub_codes = [c.strip() for c in first.split("+") if c.strip()]
            if "ALL" in sub_codes:
                raise ValueError("'ALL' cannot be combined with other dataset codes.")
            if sub_codes != sorted(sub_codes):
                raise ValueError(
                    f"Dataset codes must be alphabetically ordered: '{'+'.join(sorted(sub_codes))}'."
                )
        dataset_code = first
        idx = 1

    # Next 5 tokens (if Base32 groups) form the seed. Each group is
    # normally exactly 4 chars, but be lenient and just consume groups
    # until we hit _SEED_CHARS total length or run out of seed-looking
    # tokens (digits-only legacy seeds are handled in the legacy path).
    seed_chunks: List[str] = []
    seed_len = 0
    while idx < len(tokens) and seed_len < _SEED_CHARS:
        tok = tokens[idx]
        if _BASE32_RE.match(tok) or tok.isdigit():
            seed_chunks.append(tok)
            seed_len += len(tok)
            idx += 1
        else:
            break

    seed_part = "".join(seed_chunks)
    seed_canon = canonicalize_seed(seed_part)

    # Next token: N (inscription count)
    n_val = 0
    if idx < len(tokens):
        try:
            n_val = int(tokens[idx])
            idx += 1
        except ValueError:
            raise ValueError(f"Expected inscription count after seed, got '{tokens[idx]}'.")

    # Remaining tokens: params
    params_dict = _parse_params_parts(tokens[idx:]) if idx < len(tokens) else dict(empty_params)

    return seed_canon, n_val, dataset_code, params_dict


def _parse_seed_token_legacy(raw: str) -> Tuple[str, int, str, Dict]:
    """Legacy '|'/':' parser, preserved verbatim for backward compatibility
    with keys generated prior to v1.6.4."""
    empty_params = {
        "date_enabled": None, "start_year": None, "end_year": None,
        "greek_mode": None, "exclude_fragments": None,
        "len_min": None, "len_max": None,
    }
    dataset_code = ""
    rest = raw
    params_dict = dict(empty_params)

    if "|" in rest:
        rest, tail = rest.split("|", 1)
        tail_parts = [x for x in tail.split("|") if x.strip()]
        params_dict = _parse_params_parts_legacy(tail_parts)

    if "-" in rest:
        maybe_code, maybe_rest = rest.split("-", 1)
        maybe_code = maybe_code.strip()
        if "+" in maybe_code:
            sub_codes = [c.strip() for c in maybe_code.split("+") if c.strip()]
            if "ALL" in sub_codes:
                raise ValueError("'ALL' cannot be combined with other dataset codes.")
            if all(c in _DATASET_CODE_ALIASES and c != "*" for c in sub_codes):
                if sub_codes != sorted(sub_codes):
                    raise ValueError(
                        f"Dataset codes must be alphabetically ordered: '{'+'.join(sorted(sub_codes))}'."
                    )
                dataset_code = maybe_code
                rest = maybe_rest.strip()
        elif maybe_code in _DATASET_CODE_ALIASES and maybe_code != "*":
            dataset_code = maybe_code
            rest = maybe_rest.strip()

    if ":" in rest:
        seed_part, n_part = rest.split(":", 1)
        seed_part = seed_part.strip()
        n_part = n_part.strip()
        if not n_part:
            raise ValueError("Seed token has ':' but no number after it.")
        try:
            n_val = int(n_part)
        except ValueError:
            raise ValueError("Invalid number after ':' in seed token.")
    else:
        seed_part = rest.strip()
        n_val = 0

    seed_canon = canonicalize_seed(seed_part)
    return seed_canon, n_val, dataset_code, params_dict


def _parse_params_parts_legacy(parts: List[str]) -> Dict:
    """Legacy '|'-delimited params parser (D<start>_<end>, L<min>_<max>
    use internal '_' rather than being split across tokens)."""
    out = {
        "date_enabled": None,
        "start_year": None,
        "end_year": None,
        "greek_mode": None,
        "exclude_fragments": None,
        "len_min": None,
        "len_max": None,
    }
    for raw in parts:
        p = (raw or "").strip().upper()
        if not p:
            continue
        if p == "ND":
            out["date_enabled"] = False
            continue
        if p.startswith("D") and "_" in p[1:]:
            try:
                a, b = p[1:].split("_", 1)
                out["date_enabled"] = True
                out["start_year"] = int(a)
                out["end_year"] = int(b)
            except Exception:
                raise ValueError("Invalid dating parameter. Use ND or D<start>_<end>.")
            continue
        if p in ("GI", "GE", "GO"):
            out["greek_mode"] = {"GI": "Include", "GE": "Exclude", "GO": "Greek Only"}[p]
            continue
        if p in ("F0", "F1"):
            out["exclude_fragments"] = (p == "F1")
            continue
        if p.startswith("L") and "_" in p[1:]:
            try:
                lo, hi = p[1:].split("_", 1)
                out["len_min"] = int(lo)
                out["len_max"] = int(hi)
            except Exception:
                raise ValueError("Invalid length parameter. Use L<min>_<max> e.g. L0_50.")
            continue
        raise ValueError(f"Unrecognized token parameter: '{raw}'.")
    return out



# ---------------------------------------------
# SHA-256 deterministic selection  (PRIMARY PATH)
# Snapshots are NEVER consulted here.
# ---------------------------------------------

def dataset_fingerprint(files: List[Path]) -> str:
    pairs = []
    for p in sorted([Path(x) for x in files], key=lambda x: x.name.lower()):
        try:
            pairs.append((p.name, _sha256_file(p)))
        except Exception:
            pairs.append((p.name, "ERROR"))
    h = hashlib.sha256()
    for name, sh in pairs:
        h.update(name.encode("utf-8"))
        h.update(b"\0")
        h.update(sh.encode("ascii", errors="ignore"))
        h.update(b"\n")
    return h.hexdigest()


def deterministic_select(
    inscriptions: List[Tuple[str, str]],
    n: int,
    seed_canon: str,
    ds_fp: str,
) -> List[Tuple[str, str]]:
    """
    Deterministically select n inscriptions using hash-ranking.
    score(id) = SHA256(seed | dataset_fingerprint | id)
    This function has NO snapshot dependency whatsoever.
    """
    if n <= 0:
        return []

    def score(eid: str) -> bytes:
        msg = f"{seed_canon}|{ds_fp}|{eid}".encode("utf-8")
        return hashlib.sha256(msg).digest()

    ranked = sorted(inscriptions, key=lambda it: (score(it[0]), it[0]))
    return ranked[:n]


def sha256_self_test() -> bool:
    """
    Verify SHA-256 deterministic selection is working correctly and
    independently of any snapshot system. Returns True if all checks pass.
    """
    # Build a small synthetic corpus of 10 inscriptions
    fake = [(f"EDCS-{str(i).zfill(8)}", f"DMS FELIX PVA {i} HSE") for i in range(10)]
    seed = "TESTAAAA"
    fp = "fakefingerprint123"

    # Run twice with same inputs — must be identical
    run1 = deterministic_select(fake, 5, seed, fp)
    run2 = deterministic_select(fake, 5, seed, fp)
    if run1 != run2:
        return False

    # Run with different seed — must differ
    run3 = deterministic_select(fake, 5, "TESTBBBB", fp)
    if run1 == run3:
        return False

    # Run with different fingerprint — must differ
    run4 = deterministic_select(fake, 5, seed, "differentfingerprint")
    if run1 == run4:
        return False

    # Verify expected IDs for known seed (regression check)
    ids1 = [r[0] for r in run1]
    ids3 = [r[0] for r in run3]
    if set(ids1) == set(ids3):
        # Extremely unlikely unless broken
        return False

    return True


# ---------------------------------------------
# Character length utilities (non-space chars)
# ---------------------------------------------

def count_nonspace_chars(text: str) -> int:
    """Count non-space characters in cleaned inscription text."""
    return sum(1 for c in text if c != " " and c != "\n")


# AntConc's default Token (Word) Definition counts only maximal runs of
# letter characters — numbers, punctuation, and symbols are excluded
# entirely (they don't even act as separate tokens, just boundaries).
# This mirrors that default exactly so "****" separators, "EDCS-ID:"
# labels, ID numbers, and DATING numerals are excluded from the count,
# the same way AntConc excludes them when analysing the same file.
_ANTCONC_TOKEN_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def count_antconc_tokens(text: str) -> int:
    """Count tokens the way AntConc counts them under its default
    (letters-only) Token Definition, so Corpus Info stays consistent
    with what AntConc reports for the same file."""
    return len(_ANTCONC_TOKEN_RE.findall(text))


def passes_length_filter(
    text: str,
    len_min: Optional[int],
    len_max: Optional[int],
) -> bool:
    """Return True if inscription passes the character length filter."""
    if len_min is None and len_max is None:
        return True
    n = count_nonspace_chars(text)
    if len_min is not None and n < len_min:
        return False
    if len_max is not None and n > len_max:
        return False
    return True


# ---------------------------------------------
# Snapshot utilities  (WRITE-ONLY, opt-in)
# ---------------------------------------------

def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def write_seed_snapshot(
    seed: str,
    selection_ordered: List[Tuple[str, str]],
    corpus_text: str,
    source_files: List[Path],
    mode: str,
    filters: dict,
    app_version: str = APP_VERSION,
) -> Path:
    """
    Write a frozen snapshot of the corpus. Called ONLY when the user
    explicitly requests a backup — never automatically.
    """
    run_dir = SNAPSHOT_ROOT / f"seed-{seed}"
    run_dir.mkdir(parents=True, exist_ok=True)

    with (run_dir / "selection.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["rank", "edcs_id"])
        for i, (eid, _) in enumerate(selection_ordered, start=1):
            w.writerow([i, normalize_edcs_id(eid)])

    (run_dir / "corpus.txt").write_text(corpus_text, encoding="utf-8", newline="\n")

    ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    src_meta = []
    for s in source_files:
        try:
            s = Path(s)
            src_meta.append({
                "path": str(s),
                "sha256": _sha256_file(s),
                "size_bytes": s.stat().st_size,
            })
        except Exception as e:
            src_meta.append({"path": str(s), "error": repr(e)})

    manifest = {
        "seed": str(seed),
        "timestamp": ts,
        "mode": mode,
        "filters": filters,
        "n_selected": len(selection_ordered),
        "app": {"name": "EDCS Corpus Builder", "version": app_version},
        "sources": src_meta,
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8"
    )
    (run_dir / "README.txt").write_text(
        "Frozen corpus backup created on user request.\n"
        "This snapshot is for archival reference only.\n"
        "The corpus was generated by live SHA-256 deterministic selection.\n"
        "To regenerate: use the same key in CorpusBuilder.\n",
        "utf-8"
    )
    return run_dir


# ---------------------------------------------
# Text cleaning
# ---------------------------------------------

ABBREVIATION_PATTERNS = {
    r"\bD\s+M\s+S\b": "DMS",
    r"\bD\s+M\b": "DM",
    r"\bH\s+S\s+E\b": "HSE",
    r"\bH\s+S\b": "HS",
    r"\bH\s+E\s+S\b": "HES",
    r"\bS\s+T\s+T\s+L\b": "STTL",
    r"\bB\s+M\b": "BM",
    r"\bF\s+F\b": "FF",
    r"\bF\s+C\b": "FC",
    r"\bV\s+S\s+L\s+M\b": "VSLM",
    r"\bP\s+V\s+A\s+N\b": "PVAN",
    r"\bP\s+V\s+A\b": "PVA",
    r"\bV\s+A\b": "VA",
    r"\bA\s+N\b": "AN",
    r"\bC\s+R\b": "CR",
    r"\bC\s+I\b": "CI",
    r"\bC\s+S\b": "CS",
    r"\bC\s+O\b": "CO",
    r"\bD\s+D\b": "DD",
    r"\bD\s+E\b": "DE",
    r"\bD\s+F\b": "DF",
    r"\bD\s+I\b": "DI",
    r"\bD\s+O\b": "DO",
    r"\bD\s+S\b": "DS",
    r"\bE\s+M\b": "EM",
    r"\bE\s+Q\b": "EQ",
    r"\bF\s+A\b": "FA",
    r"\bM\s+F\b": "MF",
    r"\bM\s+L\b": "ML",
    r"\bS\s+T\b": "ST",
}

METADATA_PREFIXES = (
    "province:", "place:", "findspot:", "author", "editor",
    "status:", "genus:", "comment:", "comments:", "inscriptiones",
    "publication:", "material", "localisation", "evidence", "inscriptions",
)


def clean_inscription_lines(lines: List[str], greek_only: bool = False) -> str:
    cleaned: List[str] = []
    for line in lines:
        original = line.strip()
        if not original:
            continue
        lower = original.lower()
        if lower.startswith("inscription genus") or any(
            lower.startswith(p) for p in METADATA_PREFIXES
        ):
            continue
        line = re.sub(r"\([^)]*\)", "", original)
        line = re.sub(r"<[^>]*>", "", line)
        line = line.replace("[", "").replace("]", "")
        line = re.sub(r"[uU]", "V", line)
        if greek_only:
            line = re.sub(r"[^A-ZΑ-Ωα-ωΆ-ώ0-9\s\-–:.·]", "", line.upper())
        else:
            line = re.sub(r"[^A-Z0-9\s\-–:.·]", "", line.upper())
        line = re.sub(r"\s{2,}", " ", line).strip()
        for pattern, repl in ABBREVIATION_PATTERNS.items():
            line = re.sub(pattern, repl, line)
        if line:
            cleaned.append(line)
    return "\n".join(cleaned)


# ---------------------------------------------
# Block parsing
# ---------------------------------------------

def split_into_blocks(lines: List[str]) -> List[Tuple[str, List[str]]]:
    blocks: List[Tuple[str, List[str]]] = []
    current_lines: List[str] = []
    current_id: Optional[str] = None

    for raw in lines:
        line = raw.rstrip("\n")
        m = EDCS_RE.search(line)
        m_bare = EDCS_BARE_RE.match(line) if not m else None
        if m:
            if current_id and current_lines:
                blocks.append((current_id, current_lines))
                current_lines = []
            current_id = f"EDCS-ID: {m.group(1)}"
            left = line[:m.start()].strip()
            if left:
                current_lines.append(left)
        elif m_bare:
            if current_id and current_lines:
                blocks.append((current_id, current_lines))
                current_lines = []
            current_id = f"EDCS-ID: {m_bare.group(1)}"
        else:
            current_lines.append(line.strip())

    if current_id and current_lines:
        blocks.append((current_id, current_lines))

    return blocks


# ---------------------------------------------
# Core corpus generation  (no GUI dependency)
# ---------------------------------------------

def build_corpus_core(
    source_files: List[Path],
    n: int,
    seed_canon: str,
    greek_filter: str,
    date_enabled: bool,
    start_year: Optional[int],
    end_year: Optional[int],
    exclude_fragments: bool,
    len_min: Optional[int],
    len_max: Optional[int],
) -> Tuple[List[Tuple[str, str]], str, str]:
    """
    Pure corpus generation — no GUI, no snapshot.
    Returns (subset_pairs, corpus_text, dataset_fingerprint).
    subset_pairs: [(bare_edcs_id, cleaned_text), ...]
    corpus_text: full rendered output string
    """
    inscriptions: List[Tuple[str, str]] = []
    seen_ids = set()

    for p in source_files:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        blocks = split_into_blocks(lines)

        for edcs_id_line, raw_lines in blocks:
            original_text = "\n".join(raw_lines)

            # Greek filter
            contains_greek = bool(GREEK_RANGE_RE.search(original_text))
            if greek_filter == "Exclude" and contains_greek:
                continue
            if greek_filter == "Greek Only" and not contains_greek:
                continue

            # Date filter
            if date_enabled:
                m = DATE_RE.search(original_text)
                if not m:
                    continue
                ds, de = map(int, m.groups())
                if de < start_year or ds > end_year:
                    continue

            # Clean
            cleaned = clean_inscription_lines(raw_lines, greek_filter == "Greek Only")
            if not cleaned:
                continue

            # Fragment filter (>3 non-space chars)
            if exclude_fragments and count_nonspace_chars(cleaned) <= 3:
                continue

            # Length filter (non-space chars, post-cleaning)
            if not passes_length_filter(cleaned, len_min, len_max):
                continue

            bare_id = normalize_edcs_id(edcs_id_line)
            if bare_id in seen_ids:
                continue

            seen_ids.add(bare_id)
            inscriptions.append((bare_id, cleaned))

    total_found = len(inscriptions)
    if total_found == 0:
        raise ValueError("No inscriptions matched your criteria.")
    if total_found < n:
        raise ValueError(
            f"Only {total_found} inscriptions matched your filters, but you requested {n}."
        )

    ds_fp = dataset_fingerprint(source_files)
    subset_pairs = deterministic_select(inscriptions, n, seed_canon, ds_fp)

    rendered = [f"****\nEDCS-ID: {eid}\n{text}\n" for (eid, text) in subset_pairs]
    corpus_text = "\n".join(rendered)

    return subset_pairs, corpus_text, ds_fp


# ---------------------------------------------
# GUI
# ---------------------------------------------

# ---------------------------------------------
# GUI  —  AntConc-inspired layout  v1.4.0
# ---------------------------------------------
class CorpusBuilderApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"EDCS Corpus Builder  v{APP_VERSION}")
        self.geometry("1100x700")
        self.minsize(900, 580)
        self.configure(bg="#e8e8e8")
        self.protocol("WM_DELETE_WINDOW", self._on_closing)

        # Run SHA-256 self-test on startup
        self._sha256_ok = sha256_self_test()

        self._build_ui()

        if not self._sha256_ok:
            messagebox.showwarning(
                "SHA-256 Test Failed",
                "Deterministic selection self-test failed.\nResults may not be reproducible.\nPlease report this as a bug."
            )
        else:
            self._set_status(f"Ready  |  SHA-256 self-test: PASSED  |  v{APP_VERSION}")

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build_ui(self):
        BG = "#e8e8e8"
        PANEL_BG = "#f5f5f5"
        HEADER_BG = "#2b2b2b"
        HEADER_FG = "#ffffff"
        BTN_BG = "#4a7c59"
        BTN_FG = "#ffffff"
        ENTRY_BG = "#ffffff"
        BORDER = "#cccccc"
        LABEL_FONT = ("Segoe UI", 9)
        MONO_FONT = ("Courier New", 9)

        # ── Top header bar ──────────────────────────────────────────────
        header = tk.Frame(self, bg=HEADER_BG, height=36)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)
        tk.Label(
            header,
            text=f"EDCS Corpus Builder  v{APP_VERSION}",
            font=("Segoe UI", 11, "bold"),
            bg=HEADER_BG, fg=HEADER_FG,
            padx=12
        ).pack(side="left", fill="y")

        # ── Controls strip (below header) ───────────────────────────────
        ctrl = tk.Frame(self, bg=BG, pady=4)
        ctrl.pack(fill="x", side="top", padx=6)

        # Row 1: Number / Greek / Exclude fragments / Build
        row1 = tk.Frame(ctrl, bg=BG)
        row1.pack(fill="x", pady=2)

        tk.Label(row1, text="Number:", bg=BG, font=LABEL_FONT).pack(side="left", padx=(4,2))
        self.num_entry = tk.Entry(row1, width=7, bg=ENTRY_BG, font=LABEL_FONT, relief="solid", bd=1)
        self.num_entry.pack(side="left", padx=(0,8))
        self._add_context_menu(self.num_entry)

        tk.Label(row1, text="Greek:", bg=BG, font=LABEL_FONT).pack(side="left", padx=(0,2))
        self.greek_option = tk.StringVar(value="Include")
        self.greek_dropdown = ttk.Combobox(
            row1, textvariable=self.greek_option,
            values=["Include", "Exclude", "Greek Only"],
            width=11, state="readonly", font=LABEL_FONT
        )
        self.greek_dropdown.pack(side="left", padx=(0,12))

        self.exclude_short = tk.BooleanVar(value=False)
        tk.Checkbutton(
            row1, text="Exclude Fragments",
            variable=self.exclude_short,
            bg=BG, font=LABEL_FONT
        ).pack(side="left", padx=(0,12))

        # Build button — right-aligned in row1
        tk.Button(
            row1, text="Build Corpus",
            command=self.build_corpus,
            bg=BTN_BG, fg=BTN_FG,
            font=("Segoe UI", 9, "bold"),
            relief="flat", padx=14, pady=3,
            cursor="hand2"
        ).pack(side="right", padx=(0,4))

        # Row 2: Key / Copy / Save / Browse  +  date/length filters
        row2 = tk.Frame(ctrl, bg=BG)
        row2.pack(fill="x", pady=2)

        tk.Label(row2, text="Key:", bg=BG, font=LABEL_FONT).pack(side="left", padx=(4,2))
        self.seed_entry = tk.Entry(row2, width=32, bg=ENTRY_BG, font=MONO_FONT, relief="solid", bd=1)
        self.seed_entry.pack(side="left", padx=(0,3))
        self._add_context_menu(self.seed_entry)
        tk.Button(
            row2, text="Copy", command=self.copy_seed_to_clipboard,
            font=LABEL_FONT, relief="flat", bg="#d0d0d0", padx=6
        ).pack(side="left", padx=(0,10))

        tk.Label(row2, text="Save to:", bg=BG, font=LABEL_FONT).pack(side="left", padx=(0,2))
        self.save_entry = tk.Entry(row2, width=34, bg=ENTRY_BG, font=LABEL_FONT, relief="solid", bd=1)
        self.save_entry.pack(side="left", padx=(0,3))
        self._add_context_menu(self.save_entry)
        tk.Button(
            row2, text="Browse", command=self.select_save_location,
            font=LABEL_FONT, relief="flat", bg="#d0d0d0", padx=6
        ).pack(side="left", padx=(0,0))

        # Row 3: Date filter + Length filter — compact inline
        row3 = tk.Frame(ctrl, bg=BG)
        row3.pack(fill="x", pady=2)

        self.date_filter_enabled = tk.BooleanVar(value=False)
        tk.Checkbutton(
            row3, text="Date Filter",
            variable=self.date_filter_enabled,
            command=self.toggle_date_widgets,
            bg=BG, font=LABEL_FONT
        ).pack(side="left", padx=(4,2))

        tk.Label(row3, text="From:", bg=BG, font=LABEL_FONT).pack(side="left", padx=(0,2))
        self.start_year_spin = ttk.Spinbox(
            row3, from_=-600, to=1500, width=6, state="disabled", font=LABEL_FONT,
            command=lambda: self._invalidate_seed()
        )
        self.start_year_spin.set(1)
        self.start_year_spin.pack(side="left", padx=(0,4))

        tk.Label(row3, text="To:", bg=BG, font=LABEL_FONT).pack(side="left", padx=(0,2))
        self.end_year_spin = ttk.Spinbox(
            row3, from_=-600, to=1500, width=6, state="disabled", font=LABEL_FONT,
            command=lambda: self._invalidate_seed()
        )
        self.end_year_spin.set(400)
        self.end_year_spin.pack(side="left", padx=(0,16))

        # Separator
        tk.Frame(row3, bg=BORDER, width=1).pack(side="left", fill="y", padx=4)

        self.len_filter_enabled = tk.BooleanVar(value=False)
        tk.Checkbutton(
            row3, text="Length Filter",
            variable=self.len_filter_enabled,
            command=self.toggle_len_widgets,
            bg=BG, font=LABEL_FONT
        ).pack(side="left", padx=(8,2))

        tk.Label(row3, text="Min:", bg=BG, font=LABEL_FONT).pack(side="left", padx=(0,2))
        self.len_min_spin = ttk.Spinbox(
            row3, from_=0, to=99999, width=6, state="disabled", font=LABEL_FONT,
            command=lambda: self._invalidate_seed()
        )
        self.len_min_spin.set(0)
        self.len_min_spin.pack(side="left", padx=(0,4))

        tk.Label(row3, text="Max:", bg=BG, font=LABEL_FONT).pack(side="left", padx=(0,2))
        self.len_max_spin = ttk.Spinbox(
            row3, from_=0, to=99999, width=6, state="disabled", font=LABEL_FONT,
            command=lambda: self._invalidate_seed()
        )
        self.len_max_spin.set(50)
        self.len_max_spin.pack(side="left", padx=(0,4))

        self.len_hint = tk.Label(row3, text="", bg=BG, fg="#666", font=("Segoe UI", 8, "italic"))
        self.len_hint.pack(side="left", padx=(4,0))

        # ── Thin separator ───────────────────────────────────────────────
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x", side="top")

        # ── Main area: dataset panel + left info panel + corpus preview ──
        main = tk.Frame(self, bg=BG)
        main.pack(fill="both", expand=True, side="top")
        main.columnconfigure(2, weight=1)
        main.rowconfigure(0, weight=1)

        # Dataset selection panel (leftmost)
        ds_panel = tk.Frame(main, bg=PANEL_BG, width=170, relief="flat")
        ds_panel.grid(row=0, column=0, sticky="ns", padx=(4,0), pady=4)
        ds_panel.pack_propagate(False)

        tk.Label(
            ds_panel, text="Datasets",
            bg="#3a3a3a", fg="white",
            font=("Segoe UI", 9, "bold"),
            anchor="w", padx=8, pady=4
        ).pack(fill="x")

        # Select All / Deselect All buttons
        btn_row = tk.Frame(ds_panel, bg=PANEL_BG)
        btn_row.pack(fill="x", padx=4, pady=(4,2))
        tk.Button(
            btn_row, text="All", font=("Segoe UI", 7), relief="flat",
            bg="#d0d0d0", padx=4,
            command=self._select_all_datasets
        ).pack(side="left", padx=(0,2))
        tk.Button(
            btn_row, text="None", font=("Segoe UI", 7), relief="flat",
            bg="#d0d0d0", padx=4,
            command=self._deselect_all_datasets
        ).pack(side="left")

        # Scrollable checkbox area
        ds_canvas = tk.Canvas(ds_panel, bg=PANEL_BG, highlightthickness=0)
        ds_scroll = ttk.Scrollbar(ds_panel, orient="vertical", command=ds_canvas.yview)
        self._ds_checkbox_frame = tk.Frame(ds_canvas, bg=PANEL_BG)
        self._ds_checkbox_frame.bind(
            "<Configure>",
            lambda e: ds_canvas.configure(scrollregion=ds_canvas.bbox("all"))
        )
        ds_canvas.create_window((0, 0), window=self._ds_checkbox_frame, anchor="nw")
        ds_canvas.configure(yscrollcommand=ds_scroll.set)
        ds_canvas.pack(side="left", fill="both", expand=True, padx=(4,0), pady=2)
        ds_scroll.pack(side="right", fill="y")

        # Populate checkboxes from available datasets
        self._dataset_vars: Dict[str, tk.BooleanVar] = {}
        self._populate_dataset_checkboxes()

        # Left info panel (AntConc-style)
        left_panel = tk.Frame(main, bg=PANEL_BG, width=190, relief="flat")
        left_panel.grid(row=0, column=1, sticky="ns", padx=(4,0), pady=4)
        left_panel.pack_propagate(False)

        tk.Label(
            left_panel, text="Corpus Info",
            bg="#3a3a3a", fg="white",
            font=("Segoe UI", 9, "bold"),
            anchor="w", padx=8, pady=4
        ).pack(fill="x")

        self._info_labels = {}
        info_fields = [
            ("dataset",      "Dataset"),
            ("inscriptions", "Inscriptions"),
            ("tokens",       "Total Tokens"),
            ("avg_len",      "Avg Length"),
            ("date_range",   "Date Filter"),
            ("len_filter",   "Length Filter"),
            ("greek",        "Greek"),
        ]
        for key, label in info_fields:
            row = tk.Frame(left_panel, bg=PANEL_BG)
            row.pack(fill="x", padx=6, pady=2)
            tk.Label(row, text=label + ":", bg=PANEL_BG,
                     font=("Segoe UI", 8, "bold"), anchor="w", width=13
                     ).pack(side="left")
            val_lbl = tk.Label(row, text="—", bg=PANEL_BG,
                               font=("Segoe UI", 8), anchor="w",
                               wraplength=110, justify="left")
            val_lbl.pack(side="left", fill="x")
            self._info_labels[key] = val_lbl

        # Seed display at bottom of panel
        tk.Frame(left_panel, bg=BORDER, height=1).pack(fill="x", pady=(8,0))
        tk.Label(
            left_panel, text="Active Key",
            bg=PANEL_BG, fg="#555",
            font=("Segoe UI", 8, "bold"),
            anchor="w", padx=6
        ).pack(fill="x", pady=(4,1))
        self.key_display = tk.Text(
            left_panel, height=4, wrap="char",
            bg="#f0f0f0", fg="#222",
            font=("Courier New", 7),
            relief="flat", bd=0,
            state="disabled"
        )
        self.key_display.pack(fill="x", padx=6, pady=(0,6))

        # Right: corpus preview with scrollbar
        preview_frame = tk.Frame(main, bg=BG)
        preview_frame.grid(row=0, column=2, sticky="nsew", padx=4, pady=4)
        preview_frame.rowconfigure(0, weight=1)
        preview_frame.columnconfigure(0, weight=1)

        self.info_box = tk.Text(
            preview_frame,
            wrap="word",
            bg="white", fg="#111",
            font=("Courier New", 9),
            relief="solid", bd=1,
            state="normal",
            selectbackground="#b3d1ff"
        )
        self.info_box.grid(row=0, column=0, sticky="nsew")
        self._add_text_context_menu(self.info_box)

        scrollbar = ttk.Scrollbar(preview_frame, orient="vertical", command=self.info_box.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.info_box.configure(yscrollcommand=scrollbar.set)

        # ── Status bar ───────────────────────────────────────────────────
        self.status = tk.Label(
            self, text="Ready", bg="#2b2b2b", fg="#cccccc",
            anchor="w", font=("Segoe UI", 8), padx=8
        )
        self.status.pack(side="bottom", fill="x")

        # ── Seed invalidation hooks ───────────────────────────────────────
        # A genuine UI change after a generation triggers a fresh seed on
        # next build. Two safeguards keep this selective:
        #   1. self._syncing_from_key suppresses invalidation while the GUI
        #      is being programmatically synced to match a pasted key (that
        #      is reproducing settings, not editing them).
        #   2. Entry/Spinbox fields only invalidate if their value actually
        #      changed since last focus-in — merely tabbing/clicking through
        #      a field (e.g. to reach the Browse button) no longer wipes
        #      the key.
        self._active_seed: Optional[str] = None
        self._syncing_from_key = False
        self._focus_values: dict = {}
        self._preview_key: Optional[str] = None

        def _guarded_invalidate(*_):
            if not self._syncing_from_key:
                self._invalidate_seed()

        self.greek_option.trace_add("write", _guarded_invalidate)
        self.exclude_short.trace_add("write", _guarded_invalidate)
        self.date_filter_enabled.trace_add("write", _guarded_invalidate)
        self.len_filter_enabled.trace_add("write", _guarded_invalidate)

        def _track_focus_in(widget):
            def _handler(_e):
                self._focus_values[widget] = widget.get()
            return _handler

        def _check_field_changed(widget):
            def _handler(_e):
                current = widget.get()
                if self._focus_values.get(widget) != current:
                    self._focus_values[widget] = current
                    if not self._syncing_from_key:
                        self._invalidate_seed()
                else:
                    self._focus_values[widget] = current
            return _handler

        for _w in (self.num_entry, self.start_year_spin, self.end_year_spin,
                   self.len_min_spin, self.len_max_spin):
            self._focus_values[_w] = _w.get()
            _w.bind("<FocusIn>", _track_focus_in(_w))
            _w.bind("<FocusOut>", _check_field_changed(_w))
            _w.bind("<Return>", _check_field_changed(_w))

        # Show initial preview key on startup
        self.after(100, self._update_preview_key)

    # ------------------------------------------------------------------
    # Dataset checkbox helpers
    # ------------------------------------------------------------------

    def _invalidate_seed(self):
        """Called whenever any UI control changes after a generation.
        Clears the stored seed so the next build generates fresh (or
        re-parses a pasted key). The internal preview is recomputed for
        state-tracking purposes but is no longer displayed to the user."""
        self._active_seed = None
        self._update_preview_key()

    def _update_preview_key(self):
        """Build and display a preview key using '????' as the seed placeholder.
        This reflects the current UI state without requiring a full build."""
        try:
            # Resolve code prefix from current checkbox selection
            all_files = sorted(list(DATA_FOLDER.glob("*.txt")), key=lambda p: p.name.lower())
            selected_codes = self._get_selected_codes()
            if not selected_codes:
                code_prefix = ""
            elif len(selected_codes) == len(all_files) and all_files:
                code_prefix = "ALL"
            elif len(selected_codes) == 1:
                code_prefix = selected_codes[0]
            else:
                code_prefix = "+".join(sorted(selected_codes))

            # Read current filter state
            greek_filter = self.greek_option.get()
            date_enabled = bool(self.date_filter_enabled.get())
            exclude_fragments = bool(self.exclude_short.get())
            len_enabled = bool(self.len_filter_enabled.get())

            try:
                start_year = int(self.start_year_spin.get()) if date_enabled else None
                end_year = int(self.end_year_spin.get()) if date_enabled else None
            except ValueError:
                start_year = end_year = None

            try:
                len_min = int(self.len_min_spin.get()) if len_enabled else None
                len_max = int(self.len_max_spin.get()) if len_enabled else None
            except ValueError:
                len_min = len_max = None

            num_text = self.num_entry.get().strip()
            n_str = num_text if num_text.isdigit() else "?"

            params_suffix = _canonical_params_suffix(
                date_enabled=date_enabled,
                start_year=start_year,
                end_year=end_year,
                greek_mode=greek_filter,
                exclude_fragments=exclude_fragments,
                len_min=len_min,
                len_max=len_max,
            )

            # Build preview token with ???? seed placeholder
            seed_placeholder = "????-????-????-????-????"
            preview = seed_placeholder
            if code_prefix:
                preview = f"{code_prefix}-{preview}"
            preview = f"{preview}-{n_str}-{params_suffix}"

            # The preview string is still computed (and kept available
            # internally, since other logic may rely on its existence /
            # the invalidation state it represents), but it is
            # intentionally NOT written into the visible Key field.
            # Showing "????"-filled keys before a real build was
            # confusing rather than informative, so the field is left
            # untouched here — it only updates on an actual paste (by
            # the user) or a real build result (token_display).
            self._preview_key = preview
        except Exception:
            pass  # Never crash on a preview update

    def _populate_dataset_checkboxes(self):
        """Build checkboxes for all datasets found in DATA_FOLDER."""
        for widget in self._ds_checkbox_frame.winfo_children():
            widget.destroy()
        self._dataset_vars.clear()

        all_files = sorted(DATA_FOLDER.glob("*.txt"), key=lambda p: p.name.lower())
        found_codes = []
        for p in all_files:
            code = infer_dataset_code_from_filename(p.name)
            if code and code not in found_codes:
                found_codes.append(code)

        for code in found_codes:
            display = DATASET_DISPLAY_NAMES.get(code, code)
            var = tk.BooleanVar(value=False)
            var.trace_add("write", lambda *_: (None if self._syncing_from_key else self._invalidate_seed()))
            self._dataset_vars[code] = var
            tk.Checkbutton(
                self._ds_checkbox_frame,
                text=display,
                variable=var,
                bg="#f5f5f5",
                font=("Segoe UI", 8),
                anchor="w"
            ).pack(fill="x", padx=6, pady=1)

    def _select_all_datasets(self):
        for var in self._dataset_vars.values():
            var.set(True)

    def _deselect_all_datasets(self):
        for var in self._dataset_vars.values():
            var.set(False)

    def _get_selected_codes(self) -> List[str]:
        return [code for code, var in self._dataset_vars.items() if var.get()]

    # ------------------------------------------------------------------
    # Panel update helpers
    # ------------------------------------------------------------------

    def _set_status(self, text: str):
        self.status.config(text=text)

    def _update_info_panel(
        self, dataset: str, n: int, corpus_text: str,
        date_enabled: bool, start_year, end_year,
        len_min, len_max, greek: str, seed_canon: str, token_display: str
    ):
        # Count tokens the way AntConc counts them by default (letters
        # only — numbers, punctuation, and "****" separators excluded),
        # so this figure matches AntConc's Word List / Keyword List
        # totals for the same file rather than a raw whitespace split.
        total_tokens = count_antconc_tokens(corpus_text)
        avg = round(total_tokens / n, 1) if n else 0

        self._info_labels["dataset"].config(text=dataset if dataset else "—")
        self._info_labels["inscriptions"].config(text=str(n))
        self._info_labels["tokens"].config(text=f"{total_tokens:,}")
        self._info_labels["avg_len"].config(text=f"{avg} tokens")

        if date_enabled and start_year is not None:
            self._info_labels["date_range"].config(text=f"{start_year}–{end_year} CE")
        else:
            self._info_labels["date_range"].config(text="None")

        if len_min is not None or len_max is not None:
            lo = len_min if len_min is not None else 0
            hi = len_max if len_max is not None else "∞"
            self._info_labels["len_filter"].config(text=f"{lo}–{hi} chars")
        else:
            self._info_labels["len_filter"].config(text="None")

        self._info_labels["greek"].config(text=greek)

        # Key display box
        self.key_display.config(state="normal")
        self.key_display.delete("1.0", tk.END)
        self.key_display.insert(tk.END, token_display)
        self.key_display.config(state="disabled")

    # ------------------------------------------------------------------
    # UI toggle helpers
    # ------------------------------------------------------------------

    def toggle_date_widgets(self):
        state = "normal" if self.date_filter_enabled.get() else "disabled"
        self.start_year_spin.configure(state=state)
        self.end_year_spin.configure(state=state)

    def toggle_len_widgets(self):
        state = "normal" if self.len_filter_enabled.get() else "disabled"
        self.len_min_spin.configure(state=state)
        self.len_max_spin.configure(state=state)
        self.len_hint.config(
            text="e.g. DMS FELIX PVA XXX HSE = 17" if self.len_filter_enabled.get() else ""
        )

    def select_save_location(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[
                ("Text files", "*.txt"),
                ("CSV files", "*.csv"),
                ("JSON files", "*.json"),
                ("All files", "*.*"),
            ],
            title="Save Corpus As"
        )
        if path:
            self.save_entry.delete(0, tk.END)
            self.save_entry.insert(0, path)

    def copy_seed_to_clipboard(self):
        text = self.seed_entry.get().strip()
        if not text:
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.update()
            self._set_status("Key copied to clipboard")
            self.after(1500, lambda: self._set_status("Ready"))
        except Exception:
            pass

    def _on_closing(self):
        try:
            clip_data = self.clipboard_get()
            if clip_data:
                self.clipboard_clear()
                self.clipboard_append(clip_data)
                self.update()
        except Exception:
            pass
        finally:
            self.destroy()

    def _add_context_menu(self, widget):
        menu = tk.Menu(widget, tearoff=0)

        def do_cut():
            widget.focus_set()
            widget.event_generate("<<Cut>>")

        def do_copy():
            widget.focus_set()
            widget.event_generate("<<Copy>>")

        def do_paste():
            widget.focus_set()
            try:
                txt = widget.clipboard_get()
            except tk.TclError:
                return
            try:
                if widget.selection_present():
                    widget.delete(widget.index("sel.first"), widget.index("sel.last"))
            except Exception:
                pass
            widget.insert(widget.index(tk.INSERT), txt)

        def do_select_all():
            widget.focus_set()
            try:
                widget.select_range(0, tk.END)
                widget.icursor(tk.END)
            except Exception:
                pass

        menu.add_command(label="Cut", command=do_cut)
        menu.add_command(label="Copy", command=do_copy)
        menu.add_command(label="Paste", command=do_paste)
        menu.add_separator()
        menu.add_command(label="Select All", command=do_select_all)

        def show(event):
            widget.focus_set()
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

        widget.bind("<Button-3>", show)
        try:
            if self.tk.call("tk", "windowingsystem") == "aqua":
                widget.bind("<Button-2>", show)
                widget.bind("<Control-Button-1>", show)
        except Exception:
            pass

    def _add_text_context_menu(self, text_widget):
        menu = tk.Menu(text_widget, tearoff=0)
        menu.add_command(label="Copy", command=lambda: text_widget.event_generate("<<Copy>>"))
        menu.add_separator()
        menu.add_command(label="Select All", command=lambda: text_widget.tag_add(tk.SEL, "1.0", tk.END))

        def show(event):
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

        text_widget.bind("<Button-3>", show)
        try:
            if self.tk.call("tk", "windowingsystem") == "aqua":
                text_widget.bind("<Button-2>", show)
                text_widget.bind("<Control-Button-1>", show)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Main build logic
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Format-aware save helpers
    # ------------------------------------------------------------------

    def _save_txt(self, path: str, corpus_text: str, **_):
        Path(path).write_text(corpus_text, encoding="utf-8")

    def _save_csv(self, path: str, subset_pairs, **_):
        import csv as _csv
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = _csv.writer(f)
            w.writerow(["edcs_id", "inscription_text"])
            for eid, text in subset_pairs:
                w.writerow([eid, text.replace("\n", " ")])

    def _save_json(self, path: str, subset_pairs, token_display: str, **_):
        import json as _json
        records = [{"edcs_id": eid, "inscription_text": text} for eid, text in subset_pairs]
        out = {"key": token_display, "inscriptions": records}
        Path(path).write_text(_json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    def _save_corpus(self, fmt: str, path: str, corpus_text: str,
                     subset_pairs, token_display: str):
        # Infer format from file extension — extension is source of truth
        ext = Path(path).suffix.lower()
        if ext == ".csv":
            self._save_csv(path, subset_pairs=subset_pairs)
        elif ext == ".json":
            self._save_json(path, subset_pairs=subset_pairs, token_display=token_display)
        else:
            self._save_txt(path, corpus_text=corpus_text)

    def build_corpus(self):
        try:
            greek_filter = self.greek_option.get()

            ui_date_enabled = bool(self.date_filter_enabled.get())
            ui_start_year = int(self.start_year_spin.get())
            ui_end_year = int(self.end_year_spin.get())
            ui_exclude_fragments = bool(self.exclude_short.get())
            ui_len_enabled = bool(self.len_filter_enabled.get())
            ui_len_min = int(self.len_min_spin.get()) if ui_len_enabled else None
            ui_len_max = int(self.len_max_spin.get()) if ui_len_enabled else None

            num_text = self.num_entry.get().strip()
            n = None
            if num_text:
                try:
                    n = int(num_text)
                except ValueError:
                    messagebox.showerror("Invalid Number", "Please enter a valid integer.")
                    return

            # ── Read key field ────────────────────────────────────────────
            # If _active_seed is set, reuse it (no UI changes since last build).
            # Otherwise treat the field as blank and generate a fresh seed.
            seed_text_raw = self.seed_entry.get().strip()
            if self._active_seed:
                seed_text_raw = self._active_seed
            elif "|" in seed_text_raw or "?" in seed_text_raw:
                # Full token or ???? preview in field — UI changed or unbuilt, go fresh
                seed_text_raw = ""
            try:
                seed_canon, n_from_seed, ds_code_from_seed, token_params = parse_seed_token(seed_text_raw)
            except ValueError as ve:
                messagebox.showwarning("Invalid Key", str(ve))
                return

            if n is None and n_from_seed:
                n = n_from_seed

            greek_filter_eff = token_params.get("greek_mode") or greek_filter

            if token_params.get("date_enabled") is None:
                date_enabled_eff = ui_date_enabled
                start_year_eff = ui_start_year
                end_year_eff = ui_end_year
            else:
                date_enabled_eff = bool(token_params["date_enabled"])
                start_year_eff = token_params.get("start_year") if token_params.get("start_year") is not None else ui_start_year
                end_year_eff = token_params.get("end_year") if token_params.get("end_year") is not None else ui_end_year

            exclude_fragments_eff = (
                token_params["exclude_fragments"]
                if token_params.get("exclude_fragments") is not None
                else ui_exclude_fragments
            )

            if token_params.get("len_min") is not None or token_params.get("len_max") is not None:
                len_min_eff = token_params.get("len_min")
                len_max_eff = token_params.get("len_max")
            else:
                len_min_eff = ui_len_min
                len_max_eff = ui_len_max

            # ── Sync GUI controls to reflect what the key actually encodes ──
            # The key is the source of truth once parsed; the checkboxes and
            # spinboxes must visibly match what is about to be applied so the
            # user never sees a generated corpus that contradicts the UI.
            # Wrapped in _syncing_from_key so this reproduction of settings
            # is never mistaken for a manual edit and never wipes the key
            # back to a ???? placeholder.
            self._syncing_from_key = True
            try:
                if token_params.get("date_enabled") is not None:
                    self.date_filter_enabled.set(bool(date_enabled_eff))
                    self.toggle_date_widgets()
                    if date_enabled_eff:
                        self.start_year_spin.set(int(start_year_eff))
                        self.end_year_spin.set(int(end_year_eff))

                if token_params.get("exclude_fragments") is not None:
                    self.exclude_short.set(bool(exclude_fragments_eff))

                if token_params.get("len_min") is not None or token_params.get("len_max") is not None:
                    self.len_filter_enabled.set(True)
                    self.toggle_len_widgets()
                    self.len_min_spin.set(int(len_min_eff) if len_min_eff is not None else 0)
                    self.len_max_spin.set(int(len_max_eff) if len_max_eff is not None else 99999)

                if token_params.get("greek_mode"):
                    self.greek_option.set(token_params["greek_mode"])

                # Refresh tracked focus-values so a later focus-out on these
                # spinboxes compares against the synced value, not whatever
                # was there before the key was applied.
                for _w in (self.start_year_spin, self.end_year_spin,
                           self.len_min_spin, self.len_max_spin):
                    self._focus_values[_w] = _w.get()
            finally:
                self._syncing_from_key = False

            if date_enabled_eff and int(start_year_eff) > int(end_year_eff):
                messagebox.showerror("Invalid Date Range", "Start year cannot be greater than end year.")
                return

            if len_min_eff is not None and len_max_eff is not None and len_min_eff > len_max_eff:
                messagebox.showerror("Invalid Length Range", "Min chars cannot be greater than max chars.")
                return

            if not seed_canon and n is not None:
                seed_canon = generate_seed()

            if not seed_canon:
                messagebox.showerror("Missing Key/Number", "Enter a number of inscriptions (or a full SEED:N token).")
                return

            if n is None:
                messagebox.showerror("Missing Number", "Enter a number of inscriptions.")
                return

            save_path = self.save_entry.get().strip()
            if not save_path:
                messagebox.showerror("Missing Path", "Please choose where to save the output.")
                return

            all_files = sorted(list(DATA_FOLDER.glob("*.txt")), key=lambda p: p.name.lower())
            if not all_files:
                messagebox.showerror("No Data", f"No .txt datasets found in: {DATA_FOLDER}")
                return

            # ── Resolve source files from checkbox selection ──────────────
            # If a seed token encodes a dataset code, honour it;
            # otherwise use the checkbox panel selection.
            if ds_code_from_seed:
                selected_codes = [ds_code_from_seed]
                if ds_code_from_seed == "ALL":
                    source_files = all_files
                else:
                    try:
                        source_files = resolve_dataset_code_to_files(ds_code_from_seed, all_files)
                    except ValueError as ve:
                        messagebox.showerror("Dataset Code Error", str(ve))
                        return
                    if not source_files:
                        messagebox.showerror("Dataset Not Found", f"No dataset found for code: {ds_code_from_seed}")
                        return
            else:
                selected_codes = self._get_selected_codes()
                if not selected_codes:
                    messagebox.showerror("No Dataset Selected", "Please tick at least one dataset.")
                    return
                source_files = []
                for code in selected_codes:
                    try:
                        resolved = resolve_dataset_code_to_files(code, all_files)
                    except ValueError as ve:
                        messagebox.showerror("Dataset Code Error", str(ve))
                        return
                    if not resolved:
                        messagebox.showwarning("Dataset Not Found", f"No file found for: {code} — skipping.")
                        continue
                    source_files.extend(resolved)
                if not source_files:
                    messagebox.showerror("No Data", "None of the selected datasets could be resolved.")
                    return

            for p in source_files:
                if not p.exists():
                    messagebox.showerror("Missing File", f"Dataset not found: {p}")
                    return

            # ── Build code prefix: alphabetical, '+'-joined, e.g. L+T, R+T+UM ─
            # ALL is collapsed and never combined with other codes.
            if len(selected_codes) == len(all_files):
                code_prefix = "ALL"
            elif len(selected_codes) == 1:
                code_prefix = selected_codes[0]
            else:
                code_prefix = "+".join(sorted(selected_codes))

            params_suffix = _canonical_params_suffix(
                date_enabled=date_enabled_eff,
                start_year=int(start_year_eff) if date_enabled_eff else None,
                end_year=int(end_year_eff) if date_enabled_eff else None,
                greek_mode=greek_filter_eff,
                exclude_fragments=bool(exclude_fragments_eff),
                len_min=len_min_eff,
                len_max=len_max_eff,
            )
            token_display = display_seed(seed_canon)
            if code_prefix:
                token_display = f"{code_prefix}-{token_display}"
            token_display = f"{token_display}-{n}-{params_suffix}"

            self.seed_entry.delete(0, tk.END)
            self.seed_entry.insert(0, token_display)

            # ── Core generation ──────────────────────────────────────────
            self._set_status("Generating corpus via SHA-256 deterministic selection...")
            self.update()

            try:
                subset_pairs, corpus_text, ds_fp = build_corpus_core(
                    source_files=source_files,
                    n=n,
                    seed_canon=seed_canon,
                    greek_filter=greek_filter_eff,
                    date_enabled=date_enabled_eff,
                    start_year=int(start_year_eff) if date_enabled_eff else None,
                    end_year=int(end_year_eff) if date_enabled_eff else None,
                    exclude_fragments=bool(exclude_fragments_eff),
                    len_min=len_min_eff,
                    len_max=len_max_eff,
                )
            except ValueError as ve:
                messagebox.showerror("Build Error", str(ve))
                return

            self._save_corpus(
                fmt="",
                path=save_path,
                corpus_text=corpus_text,
                subset_pairs=subset_pairs,
                token_display=token_display,
            )

            # Update corpus preview
            self.info_box.delete("1.0", tk.END)
            self.info_box.insert(tk.END, corpus_text)
            self.info_box.see("1.0")

            # Build human-readable dataset label for info panel
            if code_prefix == "ALL":
                dataset_label = "All Sites"
            else:
                dataset_label = " + ".join(
                    DATASET_DISPLAY_NAMES.get(c, c) for c in selected_codes
                )

            # Update left info panel
            self._update_info_panel(
                dataset=dataset_label,
                n=n,
                corpus_text=corpus_text,
                date_enabled=date_enabled_eff,
                start_year=int(start_year_eff) if date_enabled_eff else None,
                end_year=int(end_year_eff) if date_enabled_eff else None,
                len_min=len_min_eff,
                len_max=len_max_eff,
                greek=greek_filter_eff,
                seed_canon=seed_canon,
                token_display=token_display,
            )

            self._set_status(
                f"Done  |  {n} inscriptions  |  Saved: {save_path}  |  Key: {token_display}"
            )

            # Store seed so re-building without UI changes reuses it
            self._active_seed = seed_canon            # ── Optional snapshot backup ─────────────────────────────────
            create_backup = messagebox.askyesno(
                "Create Corpus Backup?",
                f"Corpus generated successfully ({n} inscriptions).\n\n"
                f"Would you like to create a snapshot backup?\n"
                f"This saves a frozen copy of the corpus and its provenance metadata."
            )
            if create_backup:
                params_safe = re.sub(r"[^A-Z0-9_\-]+", "-", params_suffix.upper())
                snapshot_key = f"{code_prefix}-{seed_canon}-N{n}-{params_safe}"
                filters = {
                    "greek": greek_filter_eff,
                    "date_filter_enabled": bool(date_enabled_eff),
                    "start_year": int(start_year_eff) if date_enabled_eff else None,
                    "end_year": int(end_year_eff) if date_enabled_eff else None,
                    "exclude_fragments": exclude_fragments_eff,
                    "len_min": len_min_eff,
                    "len_max": len_max_eff,
                    "dataset": code_prefix,
                    "dataset_fingerprint": ds_fp,
                }
                run_dir = write_seed_snapshot(
                    seed=snapshot_key,
                    selection_ordered=subset_pairs,
                    corpus_text=corpus_text,
                    source_files=source_files,
                    mode="SHA-256 deterministic",
                    filters=filters,
                )
                self._set_status(self.status.cget("text") + f"  |  Backup: {run_dir}")

        except Exception as exc:
            import traceback
            traceback.print_exc()
            messagebox.showerror("Unexpected Error", str(exc))


# ---------------------------------------------
# Entry point
# ---------------------------------------------
if __name__ == "__main__":
    app = CorpusBuilderApp()
    app.mainloop()
