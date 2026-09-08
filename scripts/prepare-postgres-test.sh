#!/usr/bin/env bash
# Default: extract signed PGDG Debian packages into a private test runtime.
# --runtime system: register already-installed PostgreSQL18 binaries; no download.
# Neither mode installs an apt source/package, initializes a global cluster or
# starts a global service. Run as the unprivileged development account.
set -euo pipefail
umask 077
pg_project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
pg_runtime_root="$pg_project_root/var/postgres-runtime"
pg_runtime_mode=download
if [[ $# == 1 && $1 == --help ]]; then
  printf 'Uso: bash scripts/prepare-postgres-test.sh [--runtime download|system]\n'
  printf 'system usa /usr/lib/postgresql/18/bin já instalado; download exige Debian12 amd64.\n'
  exit 0
fi
if [[ $# != 0 ]]; then
  if [[ $# != 2 || $1 != --runtime || ( $2 != download && $2 != system ) ]]; then
    printf 'Argumentos inválidos; use --runtime download ou --runtime system.\n' >&2; exit 1
  fi
  pg_runtime_mode=$2
fi
if [[ $(id -u) == 0 ]]; then printf 'Execute como usuário normal, sem sudo.\n' >&2; exit 1; fi
if [[ $pg_runtime_mode == system ]]; then
  exec python3 "$pg_project_root/scripts/run-postgres-tests.py" --prepare-system-runtime
fi
pg_arch=$(dpkg --print-architecture)
if [[ $pg_arch != amd64 || $(. /etc/os-release; printf '%s' "$VERSION_CODENAME") != bookworm ]]; then
  printf 'Este runtime isolado foi definido para Debian 12 bookworm amd64.\n' >&2; exit 1
fi
for pg_command in curl gpg apt-get apt-cache dpkg-deb python3 ldd; do command -v "$pg_command" >/dev/null; done
mkdir -p "$pg_runtime_root"
chmod 700 "$pg_runtime_root"
if [[ -f $pg_runtime_root/runtime.json ]]; then
  python3 - "$pg_runtime_root" <<'PY'
import hashlib,json,pathlib,sys
root=pathlib.Path(sys.argv[1]);manifest=json.loads((root/'runtime.json').read_text())
assert manifest['purpose']=='isolated-synthetic-postgresql-tests' and manifest['major']==18
for name,expected in manifest['binary_sha256'].items():
    with (root/name).open('rb') as stream:actual=hashlib.file_digest(stream,'sha256').hexdigest()
    if actual!=expected:raise SystemExit('Runtime existente diverge do manifesto; inspecione antes de repetir.')
print('Runtime PostgreSQL já preparado e hashes dos executáveis conferidos:',manifest['postgres_version'])
PY
  exit 0
fi
pg_apt_root="$pg_runtime_root/apt"
mkdir -p "$pg_apt_root/lists/partial" "$pg_apt_root/cache/archives/partial" "$pg_apt_root/empty" "$pg_apt_root/log" "$pg_runtime_root/gnupg" "$pg_runtime_root/packages" "$pg_runtime_root/root"
chmod 700 "$pg_runtime_root/gnupg"
curl --fail --show-error --silent --proto '=https' --tlsv1.2 \
  https://www.postgresql.org/media/keys/ACCC4CF8.asc -o "$pg_apt_root/pgdg.asc"
pg_fingerprint=$(gpg --homedir "$pg_runtime_root/gnupg" --batch --with-colons --show-keys "$pg_apt_root/pgdg.asc" | awk -F: '$1=="fpr" {print $10;exit}')
if [[ $pg_fingerprint != B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8 ]]; then
  printf 'Fingerprint da chave PGDG divergente; nenhum pacote foi extraído.\n' >&2; exit 1
fi
cat > "$pg_apt_root/sources.list" <<EOF
deb [arch=$pg_arch signed-by=$pg_apt_root/pgdg.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main
deb [arch=$pg_arch signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] https://deb.debian.org/debian bookworm main
EOF
cat > "$pg_apt_root/apt.conf" <<EOF
Dir::Etc::main "/dev/null";
Dir::Etc::parts "$pg_apt_root/empty";
Dir::Etc::sourcelist "$pg_apt_root/sources.list";
Dir::Etc::sourceparts "$pg_apt_root/empty";
Dir::State::lists "$pg_apt_root/lists";
Dir::State::status "/var/lib/dpkg/status";
Dir::Cache "$pg_apt_root/cache";
Dir::Cache::archives "$pg_apt_root/cache/archives";
Dir::Log "$pg_apt_root/log";
APT::Sandbox::User "$(id -un)";
APT::Get::AllowUnauthenticated "false";
Acquire::AllowInsecureRepositories "false";
Acquire::AllowDowngradeToInsecureRepositories "false";
Acquire::Check-Valid-Until "true";
Acquire::Languages "none";
APT::Install-Recommends "false";
EOF
printf 'Conferindo índices APT assinados em diretórios privados…\n'
APT_CONFIG="$pg_apt_root/apt.conf" apt-get update
pg_download() {
  local pg_package=$1 pg_version
  pg_version=$(APT_CONFIG="$pg_apt_root/apt.conf" apt-cache policy "$pg_package" | awk '/Candidate:/ {print $2;exit}')
  if [[ -n ${PG_TEST_VERSION:-} && $pg_package == postgresql*18 ]]; then pg_version=$PG_TEST_VERSION; fi
  if [[ -z $pg_version || $pg_version == '(none)' ]]; then printf 'Pacote indisponível: %s\n' "$pg_package" >&2; return 1; fi
  (cd "$pg_runtime_root/packages" && APT_CONFIG="$pg_apt_root/apt.conf" apt-get download "$pg_package=$pg_version")
}
pg_download postgresql-18
pg_download postgresql-client-18
pg_download libpq5
for pg_deb in "$pg_runtime_root/packages/"*.deb; do dpkg-deb --extract "$pg_deb" "$pg_runtime_root/root"; done
pg_local_libs="$pg_runtime_root/root/usr/lib/x86_64-linux-gnu:$pg_runtime_root/root/lib/x86_64-linux-gnu"
# Optional package dependencies need not load when their feature (e.g. JIT) is off.
# Resolve actual dynamic-link dependencies of the executables, without apt install.
for pg_attempt in 1 2 3; do
  pg_missing=$(for pg_binary in postgres initdb pg_ctl psql; do LD_LIBRARY_PATH="$pg_local_libs" ldd "$pg_runtime_root/root/usr/lib/postgresql/18/bin/$pg_binary"; done | awk '/not found/ {print $1}' | sort -u)
  if [[ -z $pg_missing ]]; then break; fi
  while IFS= read -r pg_library; do
    case "$pg_library" in
      liburing.so.2) pg_download liburing2 ;;
      libpq.so.5) pg_download libpq5 ;;
      libldap-2.5.so.0|liblber-2.5.so.0) pg_download libldap-2.5-0 ;;
      libsasl2.so.2) pg_download libsasl2-2 ;;
      *) printf 'Dependência não prevista: %s. Inspecione sem instalar globalmente.\n' "$pg_library" >&2; exit 1 ;;
    esac
  done <<< "$pg_missing"
  for pg_deb in "$pg_runtime_root/packages/"*.deb; do dpkg-deb --extract "$pg_deb" "$pg_runtime_root/root"; done
done
if [[ -n $pg_missing ]]; then printf 'Dependências não resolvidas.\n' >&2; exit 1; fi
PG_TEST_RUNTIME="$pg_runtime_root" LD_LIBRARY_PATH="$pg_local_libs" python3 - <<'PY'
import datetime,hashlib,json,os,pathlib,subprocess
root=pathlib.Path(os.environ['PG_TEST_RUNTIME'])
def sha(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
binary_root=root/'root/usr/lib/postgresql/18/bin'
version=subprocess.check_output([str(binary_root/'postgres'),'--version'],text=True).strip()
if not version.startswith('postgres (PostgreSQL) 18.'):raise SystemExit('Versão de PostgreSQL inesperada')
packages=[]
for archive in sorted((root/'packages').glob('*.deb')):
    metadata=subprocess.check_output(['dpkg-deb','-f',str(archive),'Package','Version','Architecture'],text=True)
    packages.append({'file':str(archive.relative_to(root)),'sha256':sha(archive),'metadata':metadata.strip()})
manifest={'purpose':'isolated-synthetic-postgresql-tests','major':18,'postgres_version':version,
 'prepared_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
 'verification':'APT authenticated InRelease and package SHA256; insecure/unauthenticated repositories disabled',
 'pgdg_key_fingerprint':'B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8',
 'sources':['https://apt.postgresql.org/pub/repos/apt','https://deb.debian.org/debian'],
 'packages':packages,'signed_indices':{str(p.relative_to(root)):sha(p) for p in (root/'apt/lists').glob('*InRelease')},
 'binary_sha256':{str((binary_root/name).relative_to(root)):sha(binary_root/name) for name in ['postgres','initdb','pg_ctl','psql']}}
(root/'runtime.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
os.chmod(root/'runtime.json',0o600)
print(version)
print('Pacotes apenas extraídos. Nenhum serviço, cluster ou pacote global foi criado.')
PY
