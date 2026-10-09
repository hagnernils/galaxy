#!/usr/bin/env python3
"""Export session results to Galaxy history on normal Stop.

Invoked by start-escriptorium.sh's SIGTERM handler, with the application
services (PostgreSQL, Django) still running. Exports the latest saved PAGE
XML, ALTO XML, and plain text for every document/transcription version, and
collects models trained or updated during the session, into Galaxy's
declared output files. Export is published atomically (write under a
temporary name, then os.replace) and individual failures are recorded in the
session report / stderr rather than aborting the shutdown, per the plan's
"record export failures in job logs" requirement.
"""

import argparse
import json
import os
import sys
import zipfile
from datetime import datetime, timezone


def atomic_zip(output_path, members):
    """Write `members` (list of (arcname, source_path)) into a zip at
    `output_path`, replacing it atomically once complete."""
    tmp_path = f"{output_path}.tmp"
    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for arcname, source_path in members:
            archive.write(source_path, arcname=arcname)
    os.replace(tmp_path, output_path)


def export_documents(report, errors):
    """Export PAGE XML, ALTO XML, and plain text for every document/
    transcription version owned by the session user, using eScriptorium's
    own export code path where available.

    The export entry point name (`core.tasks.document_export`) is a
    documented assumption following eScriptorium's existing manual-export
    feature; eScriptorium source is not present in this environment to
    verify it directly. Any mismatch is caught and reported per document
    rather than aborting finalize.
    """
    page_members, alto_members, text_members = [], [], []
    try:
        import django
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "galaxy_settings")
        django.setup()
        from core.models import Document, Transcription
        from core.tasks import document_export
    except ImportError as exc:
        errors.append({"stage": "export-setup", "error": str(exc)})
        return page_members, alto_members, text_members

    document_id = report.get("document", {}).get("id")
    if document_id is None:
        errors.append({"stage": "export-setup", "error": "session report has no document id"})
        return page_members, alto_members, text_members

    try:
        document = Document.objects.get(pk=document_id)
    except Document.DoesNotExist as exc:
        errors.append({"stage": "export-setup", "error": str(exc)})
        return page_members, alto_members, text_members

    transcriptions = list(Transcription.objects.filter(document=document))
    for file_format, members, extension in (
        ("pagexml", page_members, "xml"),
        ("alto", alto_members, "xml"),
        ("text", text_members, "txt"),
    ):
        for transcription in transcriptions or [None]:
            try:
                export_path = document_export(
                    document.pk,
                    file_format=file_format,
                    transcription_pk=getattr(transcription, "pk", None),
                )
            except Exception as exc:  # noqa: BLE001
                errors.append({
                    "stage": f"export-{file_format}",
                    "document": document.pk,
                    "transcription": getattr(transcription, "pk", None),
                    "error": str(exc),
                })
                continue
            if export_path and os.path.isfile(export_path):
                label = getattr(transcription, "name", "default")
                arcname = f"{document.name}_{label}.{extension}"
                members.append((arcname, export_path))

    return page_members, alto_members, text_members


def export_models(report, errors):
    """Collect models owned by the session user (imported or newly
    trained/updated) for return to history, in their stored format.
    """
    members = []
    try:
        import django
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "galaxy_settings")
        django.setup()
        from django.contrib.auth import get_user_model
        from core.models import OcrModel
    except ImportError as exc:
        errors.append({"stage": "models-setup", "error": str(exc)})
        return members

    galaxy_user_id = os.environ.get("GALAXY_USER_ID", "")
    username = f"galaxy_{galaxy_user_id}"
    User = get_user_model()
    try:
        user = User.objects.get(username=username)
    except User.DoesNotExist as exc:
        errors.append({"stage": "models-setup", "error": str(exc)})
        return members

    for model in OcrModel.objects.filter(owner=user):
        try:
            if not model.file or not os.path.isfile(model.file.path):
                continue
            members.append((os.path.basename(model.file.name), model.file.path))
        except Exception as exc:  # noqa: BLE001
            errors.append({"stage": "models-export", "model": model.pk, "error": str(exc)})
    return members


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace")
    parser.add_argument("--page-xml-out")
    parser.add_argument("--alto-xml-out")
    parser.add_argument("--text-out")
    parser.add_argument("--models-out")
    args = parser.parse_args()

    output_dir = os.path.join(args.workspace, "outputs")
    report_path = os.path.join(output_dir, "session-report.json")
    try:
        with open(report_path, encoding="utf-8") as report_file:
            report = json.load(report_file)
    except (FileNotFoundError, json.JSONDecodeError):
        report = {"status": "failed", "error": "bootstrap did not produce a valid report"}

    errors = list(report.get("import_errors", []))
    page_members, alto_members, text_members = export_documents(report, errors)
    model_members = export_models(report, errors)

    for output_path, members, kind in (
        (args.page_xml_out, page_members, "page"),
        (args.alto_xml_out, alto_members, "alto"),
        (args.text_out, text_members, "text"),
        (args.models_out, model_members, "models"),
    ):
        if not output_path:
            continue
        try:
            if members:
                atomic_zip(output_path, members)
            else:
                # No content to export (e.g. no models trained): publish an
                # empty archive so the declared output still exists.
                atomic_zip(output_path, [])
        except Exception as exc:  # noqa: BLE001
            errors.append({"stage": f"publish-{kind}", "error": str(exc)})

    if report.get("status") == "running":
        report["status"] = "stopped"
    report["stopped_at"] = datetime.now(timezone.utc).isoformat()
    report["export_errors"] = errors
    with open(report_path, "w", encoding="utf-8") as report_file:
        json.dump(report, report_file, indent=2)
        report_file.write("\n")

    for failure in errors:
        print(f"export error: {failure}", file=sys.stderr)


if __name__ == "__main__":
    main()
