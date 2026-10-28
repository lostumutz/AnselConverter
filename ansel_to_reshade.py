"""
Ansel to ReShade preset converter.

Rewrites the Techniques= and TechniqueSorting= lines of Ansel / Freestyle
presets into ReShade's TechniqueName@ShaderFile.fx format. The result is
written next to the original as <name>_reshade.ini.

Presets inside .zip files are read too (the zip itself is never modified).
Their output goes to a <zipname>_reshade folder next to the zip.

Usage:
    python ansel_to_reshade.py                       interactive mode
    python ansel_to_reshade.py preset.ini [...]      convert files directly
    python ansel_to_reshade.py C:\\path\\to\\presets   convert every .ini in a folder
    python ansel_to_reshade.py preset.ini -n         preview only, write nothing
"""

import argparse
import glob
import importlib.util
import os
import re
import subprocess
import sys
import zipfile

# Third-party packages installed on first run, as {import name: pip name}.
# Empty for now: everything this script uses ships with Python.
PIP_PACKAGES = {}

# Name in preset -> (technique, shader file).
# Technique names checked against the shaders that ship with Ansel.
RULES = {
    "AdaptiveRim": ("RimLight", "AdaptiveRim.fx"),
    "Zeal_RimLight": ("RimLight", "Zeal_RimLight.fx"),
    "SSR": ("SSR", "qUINT_ssr.fx"),
    "qUINT_ssr": ("SSR", "qUINT_ssr.fx"),
    "MXAO": ("MXAO", "MXAO.fx"),
    "qUINT_mxao": ("MXAO", "qUINT_mxao.fx"),
    "qUINT_dof": ("ADOF", "qUINT_dof.fx"),
    "qUINT_lightroom": ("Lightroom", "qUINT_lightroom.fx"),
    "qUINT_bloom": ("Bloom", "qUINT_bloom.fx"),
    "FakeHDR": ("HDR", "FakeHDR.fx"),
    "HDR": ("HDR", "FakeHDR.fx"),
    "SMAA": ("SMAA", "SMAA.fx"),
    "FXAA": ("FXAA", "FXAA.fx"),
    "CAS": ("ContrastAdaptiveSharpen", "CAS.fx"),
    "BloomingHDR": ("Blooming_HDR", "BloomingHDR.fx"),
    "qUINT_deband": ("Debanding", "qUINT_deband.fx"),
    "PPFX_Bloom": ("PPFXBloom", "PPFX_Bloom.fx"),
    "Colourfulness": ("Colourfulness", "Colourfulness.fx"),
    "AmbientLight": ("AmbientLight", "AmbientLight.fx"),
    "LumaSharpen": ("LumaSharpen", "LumaSharpen.fx"),
    "Vibrance": ("Vibrance", "Vibrance.fx"),
    "Clarity": ("Clarity", "Clarity.fx"),
    "Curves": ("Curves", "Curves.fx"),
    "Levels": ("Levels", "Levels.fx"),
    "Tonemap": ("Tonemap", "Tonemap.fx"),
    "Deband": ("Deband", "Deband.fx"),
    "Vignette": ("Vignette", "Vignette.fx"),
    "FilmGrain": ("FilmGrain", "FilmGrain.fx"),
}
RULES_LOWER = {k.lower(): v for k, v in RULES.items()}

# NVIDIA's own Ansel filters. They are locked binaries with no ReShade version,
# so they are removed from the converted preset.
NVIDIA_ONLY = {
    "beautifyao", "beautifyssgi", "blacknwhite", "details", "dof",
    "greenscreen", "nvnewsharpen", "sharpen", "splitscreen",
}

# Folders searched for .fx files to read real technique names from.
DEFAULT_SHADER_DIRS = [
    os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "NVIDIA Corporation", "Ansel"),
]

# Ansel writes "TechniquesSorting"; ReShade reads "TechniqueSorting".
TECH_LINE = re.compile(r"^(\s*)(Techniques|TechniqueSorting|TechniquesSorting)(\s*)=(.*)$")
OUTPUT_KEY = {"Techniques": "Techniques", "TechniqueSorting": "TechniqueSorting", "TechniquesSorting": "TechniqueSorting"}
TECHNIQUE_DECL = re.compile(r"^\s*technique\s+(\w+)", re.MULTILINE)

# shader file name (lower case) -> first technique declared in it; filled by load_shader_index()
SHADER_INDEX = {}

# Terminal colours: enabled on Windows 10+ cmd, off when piped or NO_COLOR is set.
if os.name == "nt":
    os.system("")
USE_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ


def paint(text, code):
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


def green(t): return paint(t, "32")
def yellow(t): return paint(t, "33")
def red(t): return paint(t, "31")
def dim(t): return paint(t, "90")
def bold(t): return paint(t, "1")


# --- dependencies ---
def ensure_dependencies(need_picker):
    missing = [pip for mod, pip in PIP_PACKAGES.items() if importlib.util.find_spec(mod) is None]
    if missing:
        print(f"Installing {', '.join(missing)}")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", *missing])
        except (subprocess.CalledProcessError, OSError):
            print(red(f"pip install failed. Install manually: pip install {' '.join(missing)}"))
            sys.exit(1)

    # tkinter provides the file picker. It is part of Python but optional in the installer.
    if need_picker and importlib.util.find_spec("tkinter") is None:
        print(red("tkinter not found."))
        print("Run the Python installer, choose Modify, and enable \"tcl/tk and IDLE\".")
        sys.exit(1)


# --- shader lookup ---
def load_shader_index(folders):
    """Read 'technique Name' declarations from every .fx file in the given folders."""
    count = 0
    for folder in folders:
        if not os.path.isdir(folder):
            continue
        for root, _, names in os.walk(folder):
            for name in names:
                if not name.lower().endswith(".fx") or name.lower() in SHADER_INDEX:
                    continue
                try:
                    with open(os.path.join(root, name), "r", encoding="utf-8", errors="replace") as f:
                        m = TECHNIQUE_DECL.search(f.read())
                except OSError:
                    continue
                if m:
                    SHADER_INDEX[name.lower()] = m.group(1)
                    count += 1
    return count


# --- conversion ---
def map_technique(name):
    """Return (output, status). status: 'rule', 'shader', 'guessed', 'kept' or 'nvidia' (output is None)."""
    if "@" in name:
        return name, "kept"  # already ReShade format; never convert twice
    rule = RULES.get(name) or RULES_LOWER.get(name.lower())
    if rule:
        return f"{rule[0]}@{rule[1]}", "rule"
    technique = SHADER_INDEX.get(f"{name}.fx".lower())
    if technique:
        return f"{technique}@{name}.fx", "shader"
    if name.lower() in NVIDIA_ONLY:
        return None, "nvidia"
    return f"{name}@{name}.fx", "guessed"


def convert_text(text):
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = re.split(r"\r?\n", text)
    out, changed, techniques, found = [], [], {}, False

    for i, line in enumerate(lines):
        m = TECH_LINE.match(line)
        if not m:
            out.append(line)
            continue
        found = True
        mapped = []
        for name in (s.strip() for s in m.group(4).split(",")):
            if not name:
                continue
            result = map_technique(name)
            techniques.setdefault(name, result)
            if result[0]:
                mapped.append(result[0])
        new_line = f"{m.group(1)}{OUTPUT_KEY[m.group(2)]}={','.join(mapped)}"
        if new_line != line:
            changed.append((i + 1, line, new_line))
        out.append(new_line)

    return {"text": eol.join(out), "changed": changed, "techniques": techniques, "found": found}


def reshade_name(name):
    base, ext = os.path.splitext(name)
    return f"{base}_reshade{ext or '.ini'}"


def is_preset_name(name):
    return name.lower().endswith(".ini") and not os.path.splitext(name)[0].endswith("_reshade")


# --- input sources ---
class FileSource:
    """A preset on disk."""

    def __init__(self, path):
        self.path = path
        self.label = os.path.basename(path)
        self.key = os.path.normcase(os.path.abspath(path))

    def read(self):
        # newline="" keeps \r\n intact so the output uses the same line endings as the input.
        with open(self.path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
            return f.read()

    def target(self, out_dir=None):
        folder, name = os.path.split(self.path)
        return os.path.join(out_dir or folder, reshade_name(name))


class ZipSource:
    """A preset stored inside a .zip archive."""

    def __init__(self, zip_path, member):
        self.zip_path = zip_path
        self.member = member
        self.label = f"{os.path.basename(zip_path)} > {member}"
        self.key = (os.path.normcase(os.path.abspath(zip_path)), member)

    def read(self):
        with zipfile.ZipFile(self.zip_path) as z:
            return z.read(self.member).decode("utf-8-sig", errors="replace")

    def target(self, out_dir=None):
        folder, zip_name = os.path.split(self.zip_path)
        # Keep the zip's inner folders, dropping anything like ".." that could escape the output folder.
        parts = [p for p in self.member.replace("\\", "/").split("/") if p not in ("", ".", "..")]
        base_dir = os.path.join(out_dir or folder, os.path.splitext(zip_name)[0] + "_reshade")
        return os.path.join(base_dir, *parts[:-1], reshade_name(parts[-1]))


def zip_sources(zip_path):
    try:
        with zipfile.ZipFile(zip_path) as z:
            members = [
                m.filename for m in z.infolist()
                if not m.is_dir() and is_preset_name(m.filename) and not m.filename.startswith("__MACOSX/")
            ]
    except (zipfile.BadZipFile, OSError) as e:
        print(red(f"Can't open {os.path.basename(zip_path)}: {e}"))
        return []
    return [ZipSource(zip_path, m) for m in sorted(members)]


def expand_inputs(items):
    """Turn files, folders, zips and wildcards into a list of preset sources."""
    found = []
    for item in items:
        item = item.strip().strip('"\'')
        if not item:
            continue
        if os.path.isdir(item):
            for p in sorted(glob.glob(os.path.join(item, "*"))):
                if p.lower().endswith(".zip"):
                    found += zip_sources(p)
                elif os.path.isfile(p) and is_preset_name(p):
                    found.append(FileSource(p))
        elif any(c in item for c in "*?"):
            found += expand_inputs(glob.glob(item))
        elif item.lower().endswith(".zip") and os.path.isfile(item):
            found += zip_sources(item)
        else:
            found.append(FileSource(item))
    seen, unique = set(), []
    for s in found:
        if s.key not in seen:
            seen.add(s.key)
            unique.append(s)
    return unique


def ask(prompt):
    try:
        return input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return "q"


def pick(kind):
    """Show the Windows file or folder picker. Returns a list of paths, empty if cancelled."""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()                      # only the picker, no window of our own
    root.attributes("-topmost", True)    # keep the picker in front of the console
    try:
        if kind == "file":
            picked = filedialog.askopenfilenames(
                parent=root,
                title="Select presets",
                filetypes=[("professional vibecoder here", "*.ini *.zip"), ("All files", "*.*")],
            )
            return list(picked)
        folder = filedialog.askdirectory(parent=root, title="Select preset folder")
        return [folder] if folder else []
    finally:
        root.destroy()


def choose_paths():
    while True:
        print()
        print("  1  File")
        print("  2  Folder")
        print("  Q  Quit")
        choice = ask("Select: ")
        if choice in ("1", "f", "file"):
            kind = "file"
        elif choice in ("2", "d", "folder"):
            kind = "folder"
        elif choice in ("q", "quit", "exit"):
            return None
        else:
            print(red("Enter 1, 2 or Q."))
            continue

        picked = pick(kind)
        if not picked:
            print(dim("Cancelled."))
            continue
        paths = expand_inputs(picked)
        if not paths:
            print(yellow("No presets found (checked .ini files and .zip contents)."))
            continue
        return paths


# --- per-file processing ---
def process(source, args):
    print()
    print(bold(source.label))

    if isinstance(source, FileSource) and not os.path.isfile(source.path):
        print(red("  Not found: ") + source.path)
        return "error"

    try:
        text = source.read()
    except (OSError, zipfile.BadZipFile, KeyError) as e:
        print(red(f"  Read error: {getattr(e, 'strerror', None) or e}"))
        return "error"

    res = convert_text(text)

    if not res["found"]:
        print(dim("  No technique list, skipped."))
        return "skipped"

    width = max(len(n) for n in res["techniques"]) if res["techniques"] else 0
    for name, (out, status) in res["techniques"].items():
        label = {
            "rule": green("ok      "), "shader": green("shader  "), "guessed": yellow("guess   "),
            "kept": dim("as-is   "), "nvidia": red("removed "),
        }[status]
        print(f"  {label}{name.ljust(width)}  ->  {out or 'NVIDIA-only filter, no ReShade version'}")

    if not res["changed"]:
        print(dim("  Already in ReShade format, skipped."))
        return "skipped"

    if args.verbose or args.dry_run:
        for num, old, new in res["changed"]:
            print(dim(f"  line {num}"))
            print(red(f"    - {old}"))
            print(green(f"    + {new}"))

    guessed = [n for n, (_, s) in res["techniques"].items() if s == "guessed"]
    if guessed:
        print(yellow("  Unknown techniques. Verify these exist in reshade-shaders\\Shaders: "
                     + ", ".join(f"{n}.fx" for n in guessed)))

    target = source.target(args.out)
    if args.dry_run:
        print(dim(f"  Would write {target}"))
        return "converted"

    if os.path.exists(target) and not args.force:
        if args.interactive:
            if ask(f"  {os.path.basename(target)} exists. Overwrite? [y/N] ") not in ("y", "yes"):
                print(dim("  Skipped."))
                return "skipped"
        else:
            print(red(f"  {os.path.basename(target)} exists. Use -f to overwrite."))
            return "error"

    try:
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="") as f:
            f.write(res["text"])
    except OSError as e:
        print(red(f"  Write error: {e.strerror}"))
        return "error"

    print(green("  Written: ") + target)
    args.saved.append(target)
    return "converted"


def convert_batch(paths, args):
    args.saved = []
    counts = {"converted": 0, "skipped": 0, "error": 0}
    for p in paths:
        counts[process(p, args)] += 1
    print()
    summary = f"{counts['converted']} converted, {counts['skipped']} skipped, {counts['error']} failed"
    print(red(summary) if counts["error"] else summary)
    return counts


def main():
    parser = argparse.ArgumentParser(
        description="Convert Ansel / Freestyle presets to ReShade format.",
        epilog="Example: python ansel_to_reshade.py MyPreset.ini -n",
    )
    parser.add_argument("paths", nargs="*", help=".ini files, folders, or wildcards like *.ini")
    parser.add_argument("-o", "--out", metavar="FOLDER", help="write output here instead of next to the originals")
    parser.add_argument("-n", "--dry-run", action="store_true", help="preview changes without writing")
    parser.add_argument("-f", "--force", action="store_true", help="overwrite existing _reshade files")
    parser.add_argument("-v", "--verbose", action="store_true", help="show changed lines")
    parser.add_argument("-s", "--shaders", metavar="FOLDER", action="append", default=[],
                        help="extra folder of .fx files to read technique names from (repeatable)")
    args = parser.parse_args()
    args.interactive = not args.paths

    ensure_dependencies(need_picker=args.interactive)
    load_shader_index(args.shaders + DEFAULT_SHADER_DIRS)

    if not args.interactive:
        paths = expand_inputs(args.paths)
        if not paths:
            print(red("No input files."))
            return 1
        return 1 if convert_batch(paths, args)["error"] else 0

    print(bold("Ansel to ReShade preset converter"))
    while True:
        paths = choose_paths()
        if paths is None:
            return 0
        convert_batch(paths, args)

        if args.saved:
            folders = sorted({os.path.dirname(p) for p in args.saved})
            print()
            print(bold("Output"))
            for p in args.saved:
                print("  " + p)
            if ask("Open output folder? [y/N] ") in ("y", "yes"):
                for folder in folders:
                    os.startfile(folder)

        if ask("Convert more? [y/N] ") not in ("y", "yes"):
            return 0


if __name__ == "__main__":
    sys.exit(main())
