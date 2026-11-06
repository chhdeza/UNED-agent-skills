#!/usr/bin/env python3
"""
Extract .zip files inside a Moodle submission folder into _extracted/<safe_basename>/.

Safe against zip-slip; rejects archives exceeding max uncompressed size (zip bomb guard).
Run from workspace root before grading when the folder contains ZIP attachments.

Usage:
    python .claude/skills/moodle-grading/scripts/extract_submission_zip.py submissions/assignments/assignment_517559/user_3081
    python .../extract_submission_zip.py path/to/folder --max-mb 200
"""

from __future__ import annotations

import argparse
import stat
import sys
import zipfile
from pathlib import Path


def _safe_extract_target(dest_root: Path, member_name: str) -> Path | None:
    """Return absolute path for member if it stays under dest_root, else None."""
    dest_root = dest_root.resolve()
    # Normalize member path: reject absolute Windows/Unix paths
    if member_name.startswith(("/", "\\")) or ":" in member_name[:3]:
        return None
    parts = Path(member_name).parts
    if ".." in parts:
        return None
    out = (dest_root / member_name).resolve()
    try:
        out.relative_to(dest_root)
    except ValueError:
        return None
    return out


def extract_zip(
    zip_path: Path,
    dest_dir: Path,
    max_uncompressed_bytes: int,
    max_entries: int = 10_000,
) -> tuple[bool, str]:
    """
    Extract zip_path into dest_dir. Returns (ok, message).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_root = dest_dir.resolve()

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            entries = zf.infolist()
            if len(entries) > max_entries:
                return False, (
                    f"ZIP excede el máximo de entradas permitido "
                    f"({max_entries}; encontradas: {len(entries)})."
                )
            total = sum(info.file_size for info in entries)
            if total > max_uncompressed_bytes:
                return False, (
                    f"ZIP excede el tamaño máximo permitido ({max_uncompressed_bytes} bytes; "
                    f"declarado: {total})."
                )

            for info in entries:
                if _safe_extract_target(dest_root, info.filename) is None:
                    return False, f"Entrada ZIP rechazada (path no seguro): {info.filename!r}"
                unix_mode = (info.external_attr >> 16) & 0xFFFF
                if unix_mode and stat.S_ISLNK(unix_mode):
                    return False, f"Entrada ZIP rechazada (enlace simbólico): {info.filename!r}"

            extracted_bytes = 0
            for info in entries:
                target = _safe_extract_target(dest_root, info.filename)
                if target is None:  # Already validated; keeps type checkers satisfied.
                    return False, f"Entrada ZIP rechazada: {info.filename!r}"
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info, "r") as source, target.open("wb") as destination:
                    while chunk := source.read(1024 * 1024):
                        extracted_bytes += len(chunk)
                        if extracted_bytes > max_uncompressed_bytes:
                            destination.close()
                            target.unlink(missing_ok=True)
                            return False, (
                                "ZIP excedió el tamaño máximo permitido durante "
                                "la extracción."
                            )
                        destination.write(chunk)
    except zipfile.BadZipFile:
        return False, "Archivo ZIP corrupto o no válido."
    except RuntimeError as e:
        if "encrypted" in str(e).lower() or "password" in str(e).lower():
            return False, "ZIP cifrado o protegido con contraseña; no se puede extraer."
        return False, f"Error al extraer: {e}"
    except OSError as e:
        return False, f"Error de sistema al extraer: {e}"

    return True, f"Extraído correctamente en {dest_dir}"


def process_folder(
    folder: Path, max_mb: float, extracted_root: str, max_entries: int = 10_000
) -> int:
    """
    Find *.zip in folder (non-recursive), extract each to folder/<extracted_root>/<safe_stem>/.
    Returns exit code 0 if at least one zip processed or none found; 1 on fatal errors.
    """
    folder = folder.resolve()
    if not folder.is_dir():
        print(f"Error: no es un directorio: {folder}", file=sys.stderr)
        return 1

    max_bytes = int(max_mb * 1024 * 1024)
    unique = sorted(
        [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".zip"],
        key=lambda p: p.name.lower(),
    )

    if not unique:
        print(f"No hay archivos .zip en {folder}")
        return 0

    base_root = folder / extracted_root
    base_root.mkdir(parents=True, exist_ok=True)

    errors = 0
    for zp in unique:
        safe_name = "".join(c if c.isalnum() or c in "._- " else "_" for c in zp.stem)[:120].strip()
        if not safe_name:
            safe_name = "archive"
        dest = base_root / safe_name
        if dest.exists() and any(dest.iterdir()):
            print(f"Omitido (ya existe contenido): {zp.name} -> {dest}")
            continue
        print(f"Extrayendo {zp.name} -> {dest}")
        ok, msg = extract_zip(zp, dest, max_bytes, max_entries)
        print(msg)
        if not ok:
            errors += 1

    return 1 if errors else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extrae archivos ZIP dentro de una carpeta de entrega Moodle de forma segura."
    )
    parser.add_argument(
        "folder",
        type=Path,
        help="Carpeta de la entrega del estudiante (ej. submissions/assignments/assignment_XXX/user_YYY)",
    )
    parser.add_argument(
        "--max-mb",
        type=float,
        default=200.0,
        help="Tamaño máximo descomprimido total en MB (por defecto 200).",
    )
    parser.add_argument(
        "--extracted-dir",
        default="_extracted",
        help="Subcarpeta dentro de la entrega donde se crean los directorios (default: _extracted).",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=10_000,
        help="Número máximo de entradas por ZIP (por defecto 10000).",
    )
    args = parser.parse_args()
    code = process_folder(args.folder, args.max_mb, args.extracted_dir, args.max_files)
    sys.exit(code)


if __name__ == "__main__":
    main()
