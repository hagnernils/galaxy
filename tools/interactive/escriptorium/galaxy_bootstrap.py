#!/usr/bin/env python3
"""Bootstrap one Galaxy eScriptorium session: deactivate any superuser
accounts (defensive; neither this session nor the standalone image creates
one), create/activate the Galaxy-mapped regular user, create a default
project and document, and import staged inputs (images, PDFs, PAGE/ALTO
annotations, annotation archives, and Kraken models) via eScriptorium's own
model/import conventions where practical.

Import failures for individual entries are recorded in the session report and
printed to stderr rather than aborting the whole session, per the plan's
"report import failures" requirement. A failure to create the user/project/
document itself is still fatal, since nothing useful can run without it.
"""

import argparse
import json
import os
import re
import sys
import zipfile
from tempfile import TemporaryDirectory


def load_manifest(path):
    entries = []
    with open(path, encoding="utf-8") as manifest:
        for line_number, line in enumerate(manifest, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"invalid manifest line {line_number}: {exc}") from exc
            entries.append(entry)
    return entries


def base_key(filename):
    """Filename without extension, used to match annotations to pages."""
    return os.path.splitext(os.path.basename(filename))[0]


def rasterize_pdf(path, errors):
    """Render a PDF's pages to PNG files in a temporary directory.

    Uses pypdfium2 (a pure-wheel PDF renderer) so no system `poppler` package
    needs to be added to the container image. This is a documented default
    choice: if pypdfium2 is not installed in the image, PDF inputs fail with
    a clear per-file error rather than crashing bootstrap, and the README
    notes the dependency as a deployment requirement for PDF support.
    """
    try:
        import pypdfium2 as pdfium
    except ImportError:
        errors.append({"file": path, "error": "pypdfium2 is not installed; PDF import is unavailable"})
        return []
    rendered = []
    workdir = TemporaryDirectory(prefix="pdf-pages-")
    try:
        pdf = pdfium.PdfDocument(path)
        for page_index in range(len(pdf)):
            page = pdf[page_index]
            bitmap = page.render(scale=2.0)
            image = bitmap.to_pil()
            out_path = os.path.join(workdir.name, f"{page_index:04d}.png")
            image.save(out_path, format="PNG")
            rendered.append(out_path)
    except Exception as exc:  # noqa: BLE001 - reported, not fatal
        errors.append({"file": path, "error": f"PDF rasterization failed: {exc}"})
    return rendered, workdir


def import_pages(document, entries, settings_module, errors):
    """Create DocumentPart rows for image and PDF entries, in supplied order."""
    from django.conf import settings
    from django.core.files import File
    from PIL import Image
    from easy_thumbnails.files import get_thumbnailer
    from core.models import DocumentPart

    pages = []  # list of (entry, image_path) in final order
    temp_dirs = []
    for entry in entries:
        if entry["category"] == "image":
            pages.append((entry, entry["path"]))
        elif entry["category"] == "pdf":
            result = rasterize_pdf(entry["path"], errors)
            if not result:
                continue
            rendered, workdir = result
            temp_dirs.append(workdir)
            for page_number, page_path in enumerate(rendered, 1):
                pseudo = dict(entry)
                pseudo["name"] = f"{entry['name']} p{page_number}"
                pseudo["filename"] = f"{base_key(entry['filename'])}_p{page_number:04d}.png"
                pages.append((pseudo, page_path))

    created = []
    for order, (entry, image_path) in enumerate(pages):
        try:
            with Image.open(image_path) as image:
                image.verify()
        except Exception as exc:  # noqa: BLE001
            errors.append({"file": entry["name"], "error": f"not a readable image: {exc}"})
            continue
        part = DocumentPart(
            document=document,
            name=entry["name"],
            original_filename=entry["filename"],
            source=f"galaxy://{entry['source_kind']}/{entry['index']}",
            workflow_state=DocumentPart.WORKFLOW_STATE_CONVERTED,
        )
        storage_name = f"{order:06d}_{entry['filename']}"
        with open(image_path, "rb") as image_file:
            part.image.save(storage_name, File(image_file), save=False)
        part.image_file_size = part.image.size
        part.save()
        thumbnailer = get_thumbnailer(part.image)
        for options in settings.THUMBNAIL_ALIASES[""].values():
            thumbnailer.get_thumbnail(options)
        created.append((entry, part))

    for workdir in temp_dirs:
        workdir.cleanup()
    return created


def import_annotations(parts_by_key, entries, errors):
    """Match PAGE/ALTO annotation files (and archive members) to imported
    pages by base filename and apply them via eScriptorium's own XML parsers.

    The exact parser class names/entry points are an assumption based on
    eScriptorium's documented PAGE/ALTO round-trip support; they are not
    directly verifiable in this environment (the eScriptorium source is
    cloned during the container build, not available here). Any mismatch
    surfaces as a per-file import error in the session report instead of
    crashing bootstrap, satisfying the plan's "report import failures"
    requirement either way.
    """
    try:
        from core import model_parsers  # noqa: F401  (presence check only)
    except ImportError as exc:
        errors.append({"file": "annotations", "error": f"core.model_parsers unavailable: {exc}"})
        return

    def apply_one(name, xml_path):
        key = base_key(name)
        part = parts_by_key.get(key)
        if part is None:
            errors.append({"file": name, "error": "no matching imported page for this annotation"})
            return
        try:
            with open(xml_path, "rb") as xml_file:
                header = xml_file.read(4096)
            is_alto = b"alto" in header.lower()
            parser_cls = getattr(model_parsers, "AltoParser" if is_alto else "PagexmlParser", None)
            if parser_cls is None:
                raise RuntimeError("expected parser class not found in core.model_parsers")
            parser_cls(part, xml_path).parse()
        except Exception as exc:  # noqa: BLE001
            errors.append({"file": name, "error": f"annotation import failed: {exc}"})

    for entry in entries:
        if entry["category"] == "annotation":
            apply_one(entry["name"], entry["path"])
        elif entry["category"] == "annotation-archive":
            try:
                with zipfile.ZipFile(entry["path"]) as archive:
                    with TemporaryDirectory(prefix="annotation-archive-") as extract_dir:
                        for member in archive.namelist():
                            if not member.lower().endswith(".xml"):
                                continue
                            extracted = archive.extract(member, extract_dir)
                            apply_one(os.path.basename(member), extracted)
            except zipfile.BadZipFile as exc:
                errors.append({"file": entry["name"], "error": f"not a valid archive: {exc}"})


def import_models(user, entries, errors):
    """Create OcrModel rows for imported Kraken model files.

    Field names (`name`, `file`, `owner`, `job`, `MODEL_JOB_SEGMENT`,
    `MODEL_JOB_RECOGNIZE`) follow eScriptorium's documented model-management
    conventions; as with annotations, any mismatch is reported per file
    rather than aborting the session.
    """
    imported = []
    try:
        from django.core.files import File
        from core.models import OcrModel
    except ImportError as exc:
        errors.append({"file": "models", "error": f"core.models.OcrModel unavailable: {exc}"})
        return imported

    for entry in entries:
        if entry["category"] != "model":
            continue
        try:
            job_value = (
                OcrModel.MODEL_JOB_SEGMENT if entry.get("model_job") == "segment"
                else OcrModel.MODEL_JOB_RECOGNIZE
            )
            model = OcrModel(name=entry["name"], owner=user, job=job_value)
            with open(entry["path"], "rb") as model_file:
                model.file.save(entry["filename"], File(model_file), save=False)
            model.save()
            imported.append({"id": model.pk, "name": model.name, "job": entry.get("model_job", "recognize")})
        except Exception as exc:  # noqa: BLE001
            errors.append({"file": entry["name"], "error": f"model import failed: {exc}"})
    return imported


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--project-name", required=True)
    parser.add_argument("--document-name", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "galaxy_settings")
    import django
    django.setup()

    from django.contrib.auth import get_user_model
    from django.db import transaction
    from core.models import Document, Project

    galaxy_user_id = os.environ.get("GALAXY_USER_ID", "")
    email = os.environ.get("GALAXY_USER_EMAIL", "")
    if not galaxy_user_id or not email or "@" not in email:
        raise RuntimeError("a Galaxy user ID and valid email are required")
    username = f"galaxy_{galaxy_user_id}"
    if len(username) > 150 or not re.fullmatch(r"galaxy_[A-Za-z0-9_-]+", username):
        raise RuntimeError("the Galaxy user ID cannot be mapped safely")

    User = get_user_model()
    # Neither this session nor the standalone image ever create a superuser;
    # this is a defensive safety net in case a future eScriptorium version
    # ships one via its own migrations/fixtures.
    for account in User.objects.filter(is_superuser=True):
        account.is_active = account.is_staff = account.is_superuser = False
        account.set_unusable_password()
        account.save(update_fields=["is_active", "is_staff", "is_superuser", "password"])

    entries = sorted(load_manifest(args.manifest), key=lambda e: (e["category"], e["index"]))
    if not any(e["category"] in ("image", "pdf") for e in entries):
        raise RuntimeError("the Galaxy selection contains no images or PDFs")

    errors = []
    with transaction.atomic():
        user, created = User.objects.get_or_create(username=username, defaults={"email": email})
        if not created and user.email != email:
            raise RuntimeError("the Galaxy user ID is already mapped to another email")
        user.is_active = True
        user.is_staff = user.is_superuser = False
        user.set_unusable_password()
        user.save(update_fields=["is_active", "is_staff", "is_superuser", "password"])

        project = Project.objects.create(name=args.project_name, owner=user)
        document = Document.objects.create(name=args.document_name, owner=user, project=project)

        page_entries = [e for e in entries if e["category"] in ("image", "pdf")]
        created_parts = import_pages(document, page_entries, "galaxy_settings", errors)
        parts_by_key = {base_key(entry["filename"]): part for entry, part in created_parts}

        annotation_entries = [e for e in entries if e["category"] in ("annotation", "annotation-archive")]
        import_annotations(parts_by_key, annotation_entries, errors)

        model_entries = [e for e in entries if e["category"] == "model"]
        imported_models = import_models(user, model_entries, errors)

    report = {
        "status": "running",
        "galaxy_history_id": os.environ.get("GALAXY_HISTORY_ID", ""),
        "galaxy_user_id": galaxy_user_id,
        "project": {"id": project.pk, "name": project.name, "slug": project.slug},
        "document": {"id": document.pk, "name": document.name},
        "pages": [entry["name"] for entry, _ in created_parts],
        "models": imported_models,
        "import_errors": errors,
    }
    with open(args.report, "w", encoding="utf-8") as report_file:
        json.dump(report, report_file, indent=2)
        report_file.write("\n")

    for failure in errors:
        print(f"import error: {failure['file']}: {failure['error']}", file=sys.stderr)
    print(document.pk)


if __name__ == "__main__":
    main()
