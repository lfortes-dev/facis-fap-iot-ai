#!/usr/bin/env bash
# Pin, and optionally mirror, the OCM W-Stack images and Helm charts that eclipse-xfsc/deployment uses.
#
# By default it only plans: it clones eclipse-xfsc/deployment at --ref, renders each wrapper chart
# of the selected Argo CD ApplicationSet(s), lists every container image and OCI chart it pulls from the
# XFSC Harbor (anonymous, public), resolves each tag to a digest, and writes a lock file. Nothing is copied
# and nothing is pushed.
#
# Copying needs both --apply and --target:
#   infrastructure/wstack/mirror-wstack-images.sh --apply --target <registry>/<prefix>
# copies each image by digest (all architectures, digests preserved) and each chart, then re-checks the digest.
# Log in to the target first (skopeo login / helm registry login). Cosign signatures (the
# sha256-<digest>.sig tags the deployment repo's Kyverno policy verifies) are not copied.
#
# Usage: infrastructure/wstack/mirror-wstack-images.sh [--ref main] [--appset ocm-wstack]... [--path 'PCM Cloud/*']...
#                                         [--lock infrastructure/wstack/wstack-images.lock.tsv] [--apply --target REGISTRY/PREFIX]
# Needs: git, helm (v3.8+ or v4), skopeo, python3 with PyYAML.
# Check the result: infrastructure/wstack/check-baseline.py (see the README next to this script).
set -euo pipefail

REPO=https://github.com/eclipse-xfsc/deployment.git
REF=main
APPSETS=()
EXTRA_PATHS=()
HERE=$(cd "$(dirname "$0")" && pwd)
LOCK="$HERE/wstack-images.lock.tsv"
APPLY=false
TARGET=""

while [ $# -gt 0 ]; do
  case "$1" in
    --ref) REF=$2; shift 2 ;;
    --appset) APPSETS+=("$2"); shift 2 ;;
    --path) EXTRA_PATHS+=("$2"); shift 2 ;;
    --lock) LOCK=$2; shift 2 ;;
    --apply) APPLY=true; shift ;;
    --target) TARGET=${2%/}; shift 2 ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done
[ ${#APPSETS[@]} -gt 0 ] || [ ${#EXTRA_PATHS[@]} -gt 0 ] || APPSETS=(ocm-wstack)

if $APPLY && [ -z "$TARGET" ]; then echo "--apply needs --target REGISTRY/PREFIX" >&2; exit 2; fi
if [ -n "$TARGET" ] && ! $APPLY; then echo "--target given without --apply: plan only, nothing will be copied"; fi
for t in git helm skopeo python3; do command -v "$t" >/dev/null || { echo "Missing tool: $t" >&2; exit 1; }; done
python3 -c 'import yaml' 2>/dev/null || { echo "Missing python module: PyYAML" >&2; exit 1; }

WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
echo "Cloning $REPO at $REF"
git clone -q --depth 1 --branch "$REF" "$REPO" "$WORK/deployment" 2>/dev/null || {
  git clone -q "$REPO" "$WORK/deployment" && git -C "$WORK/deployment" checkout -q "$REF"; }
COMMIT=$(git -C "$WORK/deployment" rev-parse --short HEAD)
echo "deployment @ $COMMIT"

# Chart folders: the ApplicationSet's path glob minus its excludes, plus any --path globs.
python3 - "$WORK/deployment" "$WORK/charts.txt" "${APPSETS[*]:-}" "${EXTRA_PATHS[@]:-}" <<'EOF'
import glob, os, sys, yaml
root, out, appsets, extra = sys.argv[1], sys.argv[2], sys.argv[3].split(), [p for p in sys.argv[4:] if p]
globs = []
for f in sorted(glob.glob(f"{root}/XFSC/Applicationsets/values/*.yaml")):
    v = yaml.safe_load(open(f)) or {}
    if v.get("name") in appsets:
        globs.append((v["path"], set(v.get("excludes") or []), f"appset {v['name']} (revision {v.get('tag')})"))
        appsets.remove(v["name"])
if appsets: sys.exit(f"ApplicationSet(s) not found: {' '.join(appsets)}")
globs += [(p, set(), "--path") for p in extra]
dirs = []
for pattern, excl, why in globs:
    for d in sorted(glob.glob(os.path.join(root, pattern))):
        rel = os.path.relpath(d, root)
        if rel in excl or not os.path.isfile(os.path.join(d, "Chart.yaml")): continue
        dirs.append(rel); print(f"  chart: {rel}   [{why}]")
open(out, "w").write("\n".join(dirs) + "\n")
EOF

: > "$WORK/refs.tsv"   # kind \t ref \t used-by
while IFS= read -r rel; do
  [ -n "$rel" ] || continue
  src="$WORK/deployment/$rel"; dst="$WORK/render/$rel"
  mkdir -p "$(dirname "$dst")"; cp -r "$src" "$dst"
  if ! helm dependency build "$dst" >"$WORK/dep.log" 2>&1; then
    echo "  WARN $rel: helm dependency build failed ($(tail -1 "$WORK/dep.log"))"; continue
  fi
  # OCI chart dependencies (what Argo CD pulls besides images).
  python3 - "$dst/Chart.yaml" "$rel" >>"$WORK/refs.tsv" <<'EOF'
import sys, yaml
c = yaml.safe_load(open(sys.argv[1])) or {}
for d in c.get("dependencies") or []:
    r = d.get("repository", "")
    if r.startswith("oci://"): print(f"chart\t{r[6:].rstrip('/')}/{d['name']}:{d['version']}\t{sys.argv[2]}")
EOF
  if helm template probe "$dst" >"$WORK/out.yaml" 2>"$WORK/tpl.log"; then
    grep -oE '^\s*image:\s*"?[^" ]+' "$WORK/out.yaml" | sed -E 's/^\s*image:\s*"?//' | sort -u \
      | sed "s|\$|\t$rel|; s|^|image\t|" >>"$WORK/refs.tsv"
  else
    echo "  WARN $rel: helm template failed, images not listed ($(tail -1 "$WORK/tpl.log"))"
  fi
done <"$WORK/charts.txt"

# Resolve digests (top-level manifest, so multi-arch indexes stay whole).
echo "Resolving digests"
{
  printf '# deployment @ %s (ref %s), generated %s\n' "$COMMIT" "$REF" "$(date -u +%FT%TZ)"
  printf 'kind\tsource\tdigest\tused_by\n'
  sort -u "$WORK/refs.tsv" | python3 -c '
import sys, collections
seen = collections.OrderedDict()
for l in sys.stdin:
    k, r, u = l.rstrip("\n").split("\t")
    seen.setdefault((k, r), []).append(u)
for (k, r), us in seen.items(): print(k, r, ";".join(sorted(set(us))), sep="\t")' |
  while IFS=$'\t' read -r kind ref used; do
    case "$ref" in *@sha256:*) digest=${ref#*@}; ;;
      *) if skopeo inspect --raw "docker://$ref" >"$WORK/m.json" 2>/dev/null; then
           digest=$(skopeo manifest-digest "$WORK/m.json")
         else digest=UNRESOLVED; fi ;;
    esac
    printf '%s\t%s\t%s\t%s\n' "$kind" "$ref" "$digest" "$used"
  done
} >"$WORK/lock.tsv"
mkdir -p "$(dirname "$LOCK")"; cp "$WORK/lock.tsv" "$LOCK"
total=$(grep -cvE '^(#|kind)' "$LOCK" || true); bad=$(grep -c UNRESOLVED "$LOCK" || true)
echo "Lock file: $LOCK ($total refs, $bad unresolved)"
grep -E '^(image|chart)' "$LOCK" | awk -F'\t' '{printf "  %-5s %-95s %s\n", $1, $2, substr($3,1,19)}'

if ! $APPLY; then
  echo "Plan only. Nothing was copied; re-run with --apply --target REGISTRY/PREFIX to mirror."
  exit 0
fi

# ---- Mirror (only with --apply --target) ----
case "$TARGET" in *ps-xaas.io*) echo "Refusing: target is the source registry" >&2; exit 1 ;; esac
chart_layer() { python3 -c 'import json,sys; m=json.load(sys.stdin); print(next(l["digest"] for l in m["layers"] if "chart.content" in l["mediaType"]))'; }
echo "Mirroring to $TARGET"
fail=0
while IFS=$'\t' read -r kind ref digest used; do
  [ "$kind" = image ] || [ "$kind" = chart ] || continue
  [ "$digest" != UNRESOLVED ] || { echo "  SKIP $ref (unresolved)"; fail=1; continue; }
  path=${ref#*/}                 # project/name:tag, registry host dropped
  repo=${path%%[:@]*}; tag=${path#"$repo"}; tag=${tag#:}; tag=${tag%%@*}
  dest="$TARGET/$repo:${tag:-mirrored}"
  if [ "$kind" = image ]; then
    skopeo copy -q --all --preserve-digests "docker://${ref%%[:@]*}@$digest" "docker://$dest"
  else
    name=${repo##*/}
    helm pull -q "oci://${ref%:*}" --version "$tag" -d "$WORK/charts-out" >/dev/null
    helm push "$WORK/charts-out/$name-$tag.tgz" "oci://$TARGET/${repo%/*}" >/dev/null 2>&1
  fi
  # Images keep their manifest digest. A re-pushed chart gets a new manifest, so compare its content layer.
  if [ "$kind" = image ]; then
    want=$digest; got=$(skopeo inspect --raw "docker://$dest" 2>/dev/null >"$WORK/t.json" && skopeo manifest-digest "$WORK/t.json" || echo none)
  else
    want=$(skopeo inspect --raw "docker://$ref" | chart_layer); got=$(skopeo inspect --raw "docker://$dest" 2>/dev/null | chart_layer || echo none)
  fi
  if [ "$want" = "$got" ]; then echo "  ok   $dest"; else echo "  DIFF $dest (expected $want, got $got)"; fail=1; fi
done < <(grep -E '^(image|chart)' "$LOCK")
exit $fail
