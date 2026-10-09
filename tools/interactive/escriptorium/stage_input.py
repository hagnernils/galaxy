#!/usr/bin/env python3
"""Stage one Galaxy input (dataset or collection element) into the import
workspace and append a manifest entry describing it.

Called once per selected dataset/collection element by the tool's <command>.
Each invocation copies the dataset into the workspace and records enough
metadata (ordering, display name, source kind, input category) for
galaxy_bootstrap.py to import it without re-reading Galaxy's own metadata
store.
"""

import argparse
import json
import os
import shutil

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff"}
PDF_EXTENSIONS = {".pdf"}
ANNOTATION_EXTENSIONS = {".xml"}
ARCHIVE_EXTENSIONS = {".zip"}
MODEL_EXTENSIONS = {".mlmodel", ".safetensors"}

CATEGORY_EXTENSIONS = {
    "image": IMAGE_EXTENSIONS,
    "pdf": PDF_EXTENSIONS,
    "annotation": ANNOTATION_EXTENSIONS,
    "annotation-archive": ARCHIVE_EXTENSIONS,
    "model": MODEL_EXTENSIONS,
}


def detect_category(extension):
    extension = extension.lower()
    if not extension.startswith("."):
        extension = f".{extension}"
    for category, extensions in CATEGORY_EXTENSIONS.items():
        if extension in extensions:
            return category
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--extension", required=True)
    parser.add_argument("--source-kind", required=True, choices=["datasets", "collection"])
    parser.add_argument("--category", required=True,
                         choices=["image", "pdf", "annotation", "annotation-archive", "model"],
                         help="Input category declared by the tool <when> branch that staged this file.")
    parser.add_argument("--model-job", default="",
                         choices=["", "segment", "recognize"],
                         help="For --category=model only: the kind of model being imported.")
    parser.add_argument("--index", required=True, type=int)
    args = parser.parse_args()

    if not args.name or "\n" in args.name or "\r" in args.name:
        parser.error("input names must be non-empty single-line strings")
    if not os.path.isfile(args.source):
        parser.error(f"input dataset does not exist: {args.source}")

    detected = detect_category(args.extension)
    if detected is not None and detected != args.category:
        parser.error(f"extension {args.extension!r} does not match declared category {args.category!r}")

    os.makedirs(os.path.dirname(args.manifest), exist_ok=True)
    filename = os.path.basename(args.name)
    extension = args.extension.lower()
    if not extension.startswith("."):
        extension = f".{extension}"
    if os.path.splitext(filename)[1].lower() != extension:
        filename += extension
    staged_name = f"{args.index:06d}_{args.category}{extension}"
    staged_path = os.path.join(os.path.dirname(args.manifest), staged_name)
    shutil.copyfile(args.source, staged_path)
    os.chmod(staged_path, 0o644)

    entry = {
        "index": args.index,
        "name": args.name,
        "filename": filename,
        "source_kind": args.source_kind,
        "category": args.category,
        "path": os.path.abspath(staged_path),
    }
    if args.category == "model":
        entry["model_job"] = args.model_job or "recognize"

    # The tool <command> truncates the manifest once before the first call;
    # every invocation (across all input categories) appends a line.
    with open(args.manifest, "a", encoding="utf-8") as manifest:
        json.dump(entry, manifest)
        manifest.write("\n")
    os.chmod(args.manifest, 0o644)


if __name__ == "__main__":
    main()
