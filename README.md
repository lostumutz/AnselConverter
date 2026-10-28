# Ansel to ReShade

## Introduction

Ansel to ReShade is a small Python script that turns NVIDIA Ansel / Freestyle presets into presets ReShade can load. Both use the same kind of `.ini` file and many of the same shaders, but they name the effects differently. Ansel lists an effect by its name alone (`Techniques=SMAA,CAS`), while ReShade wants the technique and the shader file it lives in (`Techniques=SMAA@SMAA.fx,ContrastAdaptiveSharpen@CAS.fx`). The script rewrites those lines and leaves the rest of the preset alone.

The original preset is never changed. The converted one is written next to it as `<name>_reshade.ini`. Presets inside `.zip` files are read straight from the archive, and their output goes to a `<zipname>_reshade` folder beside the zip.

> **Note** Only the `Techniques=` and `TechniqueSorting=` lines are rewritten. Effect settings are copied over as they are, so a shader whose settings were renamed between the Ansel and ReShade versions may come up with its defaults.

The workflow goes like this: the script starts → reads technique names from the `.fx` files Ansel installed → you pick presets (or pass them on the command line) → each technique name is looked up and rewritten → the converted preset is written beside the original.

Everything it uses ships with Python, so there is nothing to install.

## High-Level Overview

### Finding the technique lines

Ansel writes the sort order as `TechniquesSorting`, with an extra `s`. ReShade reads `TechniqueSorting`, so the key is renamed on the way through:

```python
# Ansel writes "TechniquesSorting"; ReShade reads "TechniqueSorting".
TECH_LINE = re.compile(r"^(\s*)(Techniques|TechniqueSorting|TechniquesSorting)(\s*)=(.*)$")
OUTPUT_KEY = {"Techniques": "Techniques", "TechniqueSorting": "TechniqueSorting", "TechniquesSorting": "TechniqueSorting"}
```

Every other line is copied exactly. The file is read with `newline=""`, so a preset saved with Windows line endings keeps them:

```python
eol = "\r\n" if "\r\n" in text else "\n"
lines = re.split(r"\r?\n", text)
```

### Turning a name into technique@file

Each name in the list goes through four checks, in order:

```python
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
```

1. **Already converted.** A name with `@` in it is ReShade format already and is left alone, so running the script twice does no harm.
2. **Known rules.** A table of common shaders whose technique name differs from the file name. `CAS` becomes `ContrastAdaptiveSharpen@CAS.fx`, `qUINT_dof` becomes `ADOF@qUINT_dof.fx`, and so on. Matching ignores case.
3. **The shader files themselves.** If the name matches a `.fx` file the script has read, it uses the technique that file really declares.
4. **NVIDIA-only filters.** Ansel's own filters (`Sharpen`, `Details`, `BlackAndWhite`, `DOF`, `GreenScreen` and the rest) are locked binaries with no ReShade version, so they are dropped from the list.

Anything left over is guessed as `Name@Name.fx`, which is right for most shaders, and flagged so you can check it.

### Reading real technique names

Before converting, the script walks the folders that hold `.fx` files and reads the first `technique` declared in each:

```python
TECHNIQUE_DECL = re.compile(r"^\s*technique\s+(\w+)", re.MULTILINE)
```

By default it looks in `C:\Program Files\NVIDIA Corporation\Ansel`, where the shaders Ansel ships with live. More folders can be added with `-s`, for example your game's `reshade-shaders\Shaders` folder. The first file found under a given name wins.

### Presets in zip files

Presets are often shared as zips. The script opens them read-only and picks out every `.ini` inside, skipping folders, macOS `__MACOSX` junk and files that are already `_reshade` output. The zip's inner folders are kept in the output, but any `..` in a path is dropped so nothing can be written outside the output folder:

```python
# Keep the zip's inner folders, dropping anything like ".." that could escape the output folder.
parts = [p for p in self.member.replace("\\", "/").split("/") if p not in ("", ".", "..")]
base_dir = os.path.join(out_dir or folder, os.path.splitext(zip_name)[0] + "_reshade")
```

### What it prints

Each preset gets a line per technique, showing what it was turned into and how:

| Label | Means |
|---|---|
| **ok** | Found in the known rules |
| **shader** | Read from the shader's own `.fx` file |
| **guess** | Not found anywhere; guessed as `Name@Name.fx` |
| **as-is** | Already in ReShade format |
| **removed** | NVIDIA-only filter, left out of the converted preset |

Guessed names are listed again at the end so you can check those `.fx` files exist in `reshade-shaders\Shaders`. Colours are turned off when the output is piped or `NO_COLOR` is set.

## Flowchart

```
┌──────────────────────────────┐
│ ansel_to_reshade.py starts   │
└──────────────┬───────────────┘
               │
               v
┌──────────────────────────────────────────────┐
│ Read technique names from .fx files          │
│  - Ansel's install folder                    │
│  - any folders given with -s                 │
└──────────────┬───────────────────────────────┘
               │
               v
┌──────────────────────────────────────────────┐
│ Collect presets                              │
│  - files, folders, wildcards, zips           │
│  - no arguments: pick with a file dialog     │
│  - skip _reshade files and duplicates        │
└──────────────┬───────────────────────────────┘
               │
               v
┌──────────────────────────────────────────────┐
│ For each preset                              │
│  - no technique list: skip                   │
│  - rewrite Techniques / TechniqueSorting     │
│  - nothing changed: skip                     │
└──────────────┬───────────────────────────────┘
               │
               v
┌──────────────────────────────────────────────┐
│ Map each name                                │
│  - has @: keep                               │
│  - known rule → shader file → NVIDIA-only    │
│  - otherwise guess Name@Name.fx              │
└──────────────┬───────────────────────────────┘
               │
               v
┌──────────────────────────────────────────────┐
│ Write <name>_reshade.ini                     │
│  - beside the original, or in -o FOLDER      │
│  - zips: into <zipname>_reshade\             │
│  - existing file: ask, or need -f            │
└──────────────────────────────────────────────┘
```

## Using it

1. Install [Python](https://www.python.org/downloads/) 3.8 or newer. Keep **tcl/tk and IDLE** ticked in the installer; the file picker needs it.
2. Double-click `ansel_to_reshade.py`, or run it from a terminal:

```bash
python ansel_to_reshade.py
```

3. Choose **1** to pick preset files (`.ini` or `.zip`) or **2** to pick a folder of them.
4. Copy the `_reshade.ini` files into your game's folder and select them in ReShade.

It can also be run without the menu, which is handy for converting a whole folder at once:

```bash
python ansel_to_reshade.py "C:\Presets" -n
```

| Option | Does |
|---|---|
| `paths` | `.ini` files, `.zip` files, folders, or wildcards like `*.ini` |
| `-n`, `--dry-run` | Shows what would change, writes nothing |
| `-o FOLDER` | Writes the output there instead of next to the originals |
| `-f`, `--force` | Overwrites existing `_reshade` files |
| `-v`, `--verbose` | Shows each changed line, before and after |
| `-s FOLDER` | Another folder of `.fx` files to read technique names from (can be repeated) |

From the command line the script exits with code 1 if any preset failed, so it can be used in a batch file.

## Known limitations

- **Settings aren't translated.** Only the technique lines change. If a shader's settings were renamed between versions, ReShade falls back to defaults for those.
- **NVIDIA filters are lost.** Ansel's built-in filters have no ReShade version and are removed, so the converted preset won't look exactly the same.
- **Guesses can be wrong.** A shader that isn't in the rules or the shader folders is guessed as `Name@Name.fx`. Check the ones marked **guess**.
- **One technique per file.** When reading `.fx` files, only the first technique in each is used. Shaders that declare several may need fixing by hand.
- **Folders aren't searched recursively.** Picking a folder converts the presets and zips directly inside it, not in its subfolders.
- **Windows first.** The menu's "open output folder" uses `os.startfile`, which is Windows only. The conversion itself works anywhere Python does.

## Disclaimer

This script isn't affiliated with NVIDIA or ReShade. It only reads preset files and writes new ones beside them; it never changes the originals, the zips they came in, or any game.

## Credits and licenses

- [ReShade](https://github.com/crosire/reshade) by crosire, BSD 3-Clause License. ReShade isn't included.
- NVIDIA Ansel and Freestyle are NVIDIA's. Their filters aren't included.
- Shaders belong to their authors and come under their own licenses. Get them through the ReShade installer.

NOTE: Claude was used to make this.
