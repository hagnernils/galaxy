# eScriptorium Galaxy Interactive Tool

Integrates the eScriptorium container as a Galaxy 24.2+ Interactive Tool.
Each launch creates a fresh eScriptorium instance for the launching Galaxy
user: PostgreSQL, Redis, and application working media are disposable and
container-local; selected Galaxy inputs are imported into one default
project/document, and the user is logged in automatically with no admin
account ever exposed.

Local Podman development and validation infrastructure (disposable Galaxy
config, Podman job runner wiring, acceptance checks) lives on the
`galaxy-dev-infra` branch and is intentionally not included here.

## Files

- `interactivetool_escriptorium.xml` — the tool wrapper: inputs for selected
  images/PDFs (datasets or an ordered list collection), optional PAGE/ALTO
  annotation files or annotation archives, optional segmentation/recognition
  model files, and project/document name parameters; declared outputs for the
  session report, logs, and PAGE XML/ALTO XML/plain-text/model collections.
- `stage_input.py` — stages one Galaxy dataset or collection element into the
  job workspace and appends a manifest line, tagging it with its input
  category (image, pdf, annotation, annotation-archive, model).
- `galaxy_bootstrap.py` — deactivates any superuser account (defensive; this
  session never creates one), creates/reuses the Galaxy-mapped regular user,
  creates the default project/document, and imports staged images, rasterized
  PDF pages, PAGE/ALTO annotations (matched to pages by base filename), and
  Kraken models; writes the session report and records per-file import
  failures without aborting the session.
- `galaxy_finalize.py` — exports the latest saved PAGE XML, ALTO XML, and
  plain text for every document/transcription version, and collects models
  owned by the session user, publishing each into the corresponding output
  file atomically (temp file + rename); records failures in the session
  report and stderr instead of crashing.
- `galaxy_login.py` — the Galaxy-only Django login route and view, gated by
  `GALAXY_INTERACTIVE=true`; also serves as `ROOT_URLCONF`.
- `galaxy_settings.py` — a settings overlay pointing media/static/log paths
  at the container-local runtime directory and deriving cookie security from
  the Galaxy entry URL scheme.
- `start-escriptorium.sh` — container entrypoint for Galaxy mode: brings up a
  disposable container-local PostgreSQL/Redis, runs migrations and bootstrap
  under a temporary Supervisor instance, then execs the foreground
  application Supervisor instance. On SIGTERM (Galaxy Stop) it runs
  `galaxy_finalize.py` within a bounded grace period before stopping services.
- `supervisord-galaxy.conf`, `supervisord-bootstrap.conf` — Supervisor
  configurations for, respectively, the foreground application services and
  the temporary PostgreSQL/Redis-only bootstrap phase. Kept as two separate
  phases (rather than one combined supervisord instance) so nginx/uwsgi never
  start before the session's user/project/document exist, avoiding a race
  where Galaxy's one-shot entry point could hit the login view too early.
- `nginx-galaxy.conf` — nginx config binding to the container-local runtime
  directory and proxying the application/WebSocket upstreams on port 80.

## Input/output mapping

| Feature | Mechanism |
| --- | --- |
| PNG/JPEG/TIFF/PDF/PDF-collection images | `interactivetool_escriptorium.xml` `source` conditional (datasets or list collection) tagged `category=image`; `galaxy_bootstrap.py:import_pages` rasterizes PDFs to pages |
| Preserve names/order, supply missing extensions | `stage_input.py` records Galaxy's display name, datatype extension, and manifest index; `galaxy_bootstrap.py` imports in manifest order |
| PAGE/ALTO annotations and annotation archives | `annotations` input param (`xml,zip`); `galaxy_bootstrap.py:import_annotations` matches by base filename, expanding archives first |
| Kraken `.mlmodel`/`.safetensors` model import | `segmentation_models`/`recognition_models` input params; `galaxy_bootstrap.py:import_models` creates `OcrModel` rows |
| Model reuse/training within the session | Imported models are owned by the session user and visible in eScriptorium's normal model selector/training UI immediately |
| Default project/document, auto-login | `galaxy_bootstrap.py` creates Project/Document; `galaxy_login.py:galaxy_login` logs the mapped user in and opens the document |
| No admin account ever exposed | No superuser is ever created (standalone or Galaxy); `galaxy_bootstrap.py` defensively deactivates any superuser account found |
| Keep services running until Stop | `start-escriptorium.sh` execs the foreground `supervisord-galaxy.conf` and waits on it |
| Export to history on Stop | `start-escriptorium.sh` SIGTERM trap runs `galaxy_finalize.py`, which exports PAGE/ALTO/text/models into the declared outputs |
| Record import/export failures in job logs | Both bootstrap and finalize collect per-file errors into the session report and print them to stderr, which Galaxy captures in the job log |

## Documented defaults for deferred decisions

- **Stop mechanism/grace period**: `start-escriptorium.sh` runs
  `galaxy_finalize.py` under a 50-second `timeout` (`FINALIZE_GRACE_SECONDS`)
  on SIGTERM, chosen to fit under common ~60s job-kill grace periods. Target
  deployments with a different grace period should override this variable in
  the wrapper's environment.
- **PDF rasterization**: pages are rendered with `pypdfium2` (pure wheel, no
  system `poppler` package) rather than calling into eScriptorium's own PDF
  import code, whose exact entry point could not be verified without the
  eScriptorium source (cloned during the Docker build, not available in this
  environment). If `pypdfium2` is not present in the built image, PDF inputs
  fail per-file with a clear error rather than crashing the session; add
  `pypdfium2` to the image's Python dependencies to enable PDF support.
- **PAGE/ALTO import and model-field names**: `galaxy_bootstrap.py` and
  `galaxy_finalize.py` call `core.model_parsers` (`PagexmlParser`/
  `AltoParser`), `core.tasks.document_export`, and `core.models.OcrModel`
  following eScriptorium's documented conventions, also unverifiable here
  without the real source tree. Any mismatch surfaces as a reported per-file
  import/export error rather than a crash, so sessions remain usable even if
  these exact names need adjusting once validated against a running image.
- **Per-model job type**: Galaxy's input widgets cannot label an individual
  file within a multi-file select, so segmentation vs. recognition models use
  two separate optional input params (`segmentation_models`,
  `recognition_models`) instead of a single combined picker.

## Known limitations

- Working application state (PostgreSQL, Redis, media) is container-local and
  disposable; only the declared history outputs persist past Stop.
- Export at Stop covers the latest saved state; it does not replay specific
  manual export selections beyond what the existing export UI already wrote.
- See `galaxy-dev-infra` for the local Podman development/acceptance-test
  harness used to validate this integration end to end.
