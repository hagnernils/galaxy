#!/bin/bash
set -Eeuo pipefail

workspace=${1:?Usage: start-escriptorium.sh WORKSPACE PROJECT_NAME DOCUMENT_NAME REPORT_OUTPUT LOG_OUTPUT PAGE_XML_OUTPUT ALTO_XML_OUTPUT TEXT_OUTPUT MODELS_OUTPUT}
project_name=${2:?Project name is required}
document_name=${3:?Document name is required}
report_output=${4:?Report output is required}
logs_output=${5:?Log output is required}
page_xml_output=${6:?PAGE XML output is required}
alto_xml_output=${7:?ALTO XML output is required}
text_output=${8:?Plain text output is required}
models_output=${9:?Models output is required}
tool_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
app_dir=/home/escriptorium/escriptorium/app
supervisor_config="$tool_dir/supervisord-galaxy.conf"
bootstrap_supervisor_config="$tool_dir/supervisord-bootstrap.conf"
supervisor_pid=

# Bounded grace period for the Stop-time export (galaxy_finalize.py) before
# the application services are shut down. The plan explicitly defers the
# exact runner-specific Stop/grace-period mechanism to a later deployment
# decision; 50s is a documented default chosen to fit comfortably under
# common interactive-tool/job-kill grace periods (commonly 60s) while still
# giving export(s) a reasonable chance to finish.
FINALIZE_GRACE_SECONDS=${FINALIZE_GRACE_SECONDS:-50}

[[ ${GALAXY_INTERACTIVE:-} == true ]] || { printf 'Galaxy mode is required\n' >&2; exit 1; }
export GALAXY_RUNTIME_DIR=/run/escriptorium
export GALAXY_WORKSPACE=$workspace
export GALAXY_TOOL_DIR=$tool_dir
export PYTHONPATH="$tool_dir:$app_dir:${PYTHONPATH:-}"
export DJANGO_SETTINGS_MODULE=galaxy_settings
export POSTGRES_DB=escriptorium
export POSTGRES_USER=escriptorium
export POSTGRES_PASSWORD
POSTGRES_PASSWORD=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')
export SECRET_KEY
SECRET_KEY=$(python -c 'import secrets; print(secrets.token_urlsafe(64))')
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1
export SQL_HOST=127.0.0.1
export SQL_PORT=5432
export PGSSLCERT="$GALAXY_RUNTIME_DIR/tmp/postgresql.crt"
export PGHOST="$GALAXY_RUNTIME_DIR/socket"
export HOME=/home/escriptorium
export TMPDIR="$GALAXY_RUNTIME_DIR/tmp"

mkdir -p "$workspace"/{imports,logs,outputs}
chmod 755 "$workspace" "$workspace/imports"
chgrp escriptorium "$workspace"/{logs,outputs}
chmod 2775 "$workspace"/{logs,outputs}
mkdir -p "$GALAXY_RUNTIME_DIR"/{postgres,socket,media,static,tmp}
chmod 755 "$GALAXY_RUNTIME_DIR"
chmod 1777 "$TMPDIR"
chown postgres:postgres "$GALAXY_RUNTIME_DIR"/{postgres,socket}
chown escriptorium:escriptorium "$GALAXY_RUNTIME_DIR"/{media,static}

cleanup_bootstrap() {
    if [[ -n $supervisor_pid ]]; then
        kill -TERM "$supervisor_pid" 2>/dev/null || true
        wait "$supervisor_pid" || true
    fi
}
trap cleanup_bootstrap EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

runuser -u postgres -- /usr/lib/postgresql/15/bin/initdb \
    --pgdata="$GALAXY_RUNTIME_DIR/postgres" --auth-local=trust --auth-host=scram-sha-256

supervisord -c "$bootstrap_supervisor_config" &
supervisor_pid=$!

for _ in $(seq 1 60); do
    if runuser -u postgres -- pg_isready -q && redis-cli -h 127.0.0.1 ping >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
runuser -u postgres -- pg_isready -q
redis-cli -h 127.0.0.1 ping >/dev/null

runuser -u postgres -- psql -v ON_ERROR_STOP=1 \
    -c "CREATE USER escriptorium WITH PASSWORD '$POSTGRES_PASSWORD';"
runuser -u postgres -- createdb -O escriptorium escriptorium

runuser -u escriptorium --preserve-environment -- python "$app_dir/manage.py" migrate --noinput
runuser -u escriptorium --preserve-environment -- python "$tool_dir/galaxy_bootstrap.py" \
    --manifest "$workspace/imports/manifest.jsonl" \
    --project-name="$project_name" \
    --document-name="$document_name" \
    --report "$workspace/outputs/session-report.json"
runuser -u escriptorium --preserve-environment -- python "$app_dir/manage.py" collectstatic --noinput
runuser -u escriptorium --preserve-environment -- python "$app_dir/manage.py" check

supervisorctl -c "$bootstrap_supervisor_config" shutdown
wait "$supervisor_pid"
supervisor_pid=

cp "$workspace/outputs/session-report.json" "$report_output"
tar -czf "$logs_output" -C "$workspace" logs
cp "$logs_output" "$workspace/outputs/session-logs.tar.gz"

trap - EXIT TERM INT

# Run the foreground application supervisord directly (not backgrounded) so
# this script's own TERM handler below fires exactly on Galaxy Stop. The
# `wait` after `kill` gives galaxy_finalize.py a bounded window to run with
# the application services (PostgreSQL, Django) still up, satisfying the
# plan's "keep required application services available until final export
# completes" requirement, before the supervised services themselves stop.
finalize_and_stop() {
    timeout "${FINALIZE_GRACE_SECONDS}s" \
        runuser -u escriptorium --preserve-environment -- python "$tool_dir/galaxy_finalize.py" "$workspace" \
        --page-xml-out "$workspace/outputs/page-xml.zip" \
        --alto-xml-out "$workspace/outputs/alto-xml.zip" \
        --text-out "$workspace/outputs/text.zip" \
        --models-out "$workspace/outputs/models.zip" || true
    cp "$workspace/outputs/session-report.json" "$report_output" || true
    cp "$workspace/outputs/page-xml.zip" "$page_xml_output" || true
    cp "$workspace/outputs/alto-xml.zip" "$alto_xml_output" || true
    cp "$workspace/outputs/text.zip" "$text_output" || true
    cp "$workspace/outputs/models.zip" "$models_output" || true
    tar -czf "$logs_output" -C "$workspace" logs || true
    kill -TERM "$app_supervisor_pid" 2>/dev/null || true
}
trap finalize_and_stop TERM

supervisord -c "$supervisor_config" &
app_supervisor_pid=$!
wait "$app_supervisor_pid"
