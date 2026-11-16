#!/usr/bin/env bash
# Build the KiCad 10 EDA-bench images and a copy of the dataset that points at them.
#
#   bench/edabench/prepare.sh [EDA_BENCH_CHECKOUT]
#
# Without an argument, EDA-bench is cloned into bench/edabench/.work/eda-bench at the pinned commit.
# Output: images $AGENT_IMAGE / $VERIFIER_IMAGE and the dataset at bench/edabench/.work/dataset-kicad10.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
work="$here/.work"
EDA_BENCH_REPO="https://github.com/EDA-bench/eda-bench.git"
EDA_BENCH_REF="5d65bfffd6975f44f42914ec15a917d3aacb0298"   # the revision this adapter was written against
AGENT_IMAGE="${AGENT_IMAGE:-copper-edabench-agent:kicad10.0.8}"
VERIFIER_IMAGE="${VERIFIER_IMAGE:-copper-edabench-verifier:kicad10.0.8}"
HARBOR="${HARBOR:-harbor}"
command -v "$HARBOR" >/dev/null 2>&1 || HARBOR="uvx --from harbor==0.19.0 harbor"

src="${1:-$work/eda-bench}"
if [ ! -d "$src/dataset" ]; then
  echo "== cloning EDA-bench @ ${EDA_BENCH_REF:0:12} into $src"
  git clone --quiet "$EDA_BENCH_REPO" "$src"
  git -C "$src" checkout --quiet "$EDA_BENCH_REF"
fi
rev="$(git -C "$src" rev-parse HEAD 2>/dev/null || echo unknown)"
[ "$rev" = "$EDA_BENCH_REF" ] || echo "!! EDA-bench is at $rev, not the pinned $EDA_BENCH_REF" >&2

echo "== building $AGENT_IMAGE"
docker build -t "$AGENT_IMAGE" -f "$here/images/agent.Dockerfile" "$here/images"

echo "== building $VERIFIER_IMAGE"
docker build --build-arg "AGENT_IMAGE=$AGENT_IMAGE" \
  -t "$VERIFIER_IMAGE" -f "$here/images/verifier.Dockerfile" "$src"

echo "== kicad-cli in the images"
docker run --rm "$AGENT_IMAGE" kicad-cli version
docker run --rm "$VERIFIER_IMAGE" kicad-cli version

out="$work/dataset-kicad10"
echo "== writing $out"
rm -rf "$out"
mkdir -p "$work"
cp -R "$src/dataset" "$out"
python3 - "$out" "$AGENT_IMAGE" "$VERIFIER_IMAGE" <<'PY'
import sys
from pathlib import Path

root, agent, verifier = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
swap = {
    '"ghcr.io/eda-bench/eda-bench-agent:harbor-v1"': f'"{agent}"',
    '"ghcr.io/eda-bench/eda-bench-verifier:harbor-v1"': f'"{verifier}"',
}
n = 0
for toml in sorted(root.glob("*/task.toml")):
    text = toml.read_text(encoding="utf-8")
    new = text
    for old, rep in swap.items():
        new = new.replace(old, rep)
    if new.count(agent) != 1 or new.count(verifier) != 1:
        sys.exit(f"{toml}: expected exactly one agent and one verifier image reference")
    toml.write_text(new, encoding="utf-8")
    n += 1
print(f"rewrote image references in {n} tasks")
PY

echo "== refreshing the dataset manifest"
$HARBOR sync "$out"

cat <<EOF

Ready. Check the grader on KiCad 10 with the reference designs first:
  $HARBOR run --path "$out" --agent oracle
Then run Copper (from the repository root):
  PYTHONPATH=bench/edabench $HARBOR run --path "$out" --agent copper_agent:CopperClaudeCode --model anthropic/<model>
EOF
