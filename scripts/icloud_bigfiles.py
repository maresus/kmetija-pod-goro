#!/usr/bin/env python3
import argparse
import os
from pathlib import Path


def human_size(num_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(num_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def find_icloud_root() -> Path:
    candidates = [
        Path("~/Library/Mobile Documents/com~apple~CloudDocs/").expanduser(),
        Path("~/Library/Mobile Documents/").expanduser(),
    ]
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


def scan_big_files(root: Path, limit: int) -> list[tuple[int, Path]]:
    results: list[tuple[int, Path]] = []
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            file_path = Path(dirpath) / name
            try:
                size = file_path.stat().st_size
            except OSError:
                continue
            results.append((size, file_path))
    results.sort(key=lambda x: x[0], reverse=True)
    return results[:limit]


def main() -> None:
    parser = argparse.ArgumentParser(description="Izpiši največje datoteke v iCloud Drive.")
    parser.add_argument("--root", default=str(find_icloud_root()), help="iCloud root path")
    parser.add_argument("--limit", type=int, default=50, help="Število največjih datotek")
    args = parser.parse_args()

    root = Path(args.root).expanduser()
    if not root.exists():
        print(f"iCloud mapa ne obstaja: {root}")
        return

    print(f"Skeniram: {root}")
    files = scan_big_files(root, args.limit)
    if not files:
        print("Ni najdenih datotek.")
        return

    for size, path in files:
        print(f"{human_size(size):>10}  {path}")


if __name__ == "__main__":
    main()
