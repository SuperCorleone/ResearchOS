#!/usr/bin/env bash
# cleanup_old_artifacts.sh
# 全量清理所有实验产物，回到空白状态
# 覆盖范围：artifacts、decisions、scratch、data/raw、knowledge/raw、knowledge/wiki、
#           knowledge/proposals、runtime/candidates、runtime/pipelines、runtime/logs

set -euo pipefail

WDIR="/Users/adukopun/Documents/ClaudeCodeWorkspace/ResearchOS/.claude/worktrees/shiny-tickling-marble"
MDIR="/Users/adukopun/Documents/ClaudeCodeWorkspace/ResearchOS"

echo "=== ResearchOS 全量清理 ==="
echo ""

# ── 1. 所有 pipeline 产物目录 ────────────────────────────────────────────────
echo "[1] 清理 artifacts/pipeline_*..."
for dir in "$WDIR" "$MDIR"; do
    find "$dir/artifacts" -mindepth 1 -maxdepth 1 -type d -name "pipeline_*" \
        -exec rm -rf {} \; -print 2>/dev/null | sed 's/^/  Removed: /' || true
done

# ── 2. runtime/pipelines（提案 JSON）────────────────────────────────────────
echo "[2] 清理 runtime/pipelines/*.json..."
for dir in "$WDIR" "$MDIR"; do
    find "$dir/runtime/pipelines" -maxdepth 1 -name "*.json" \
        -exec rm -f {} \; -print 2>/dev/null | sed 's/^/  Removed: /' || true
    find "$dir/runtime/pipelines/archived" -maxdepth 1 -name "*.json" \
        -exec rm -f {} \; -print 2>/dev/null | sed 's/^/  Removed: /' || true
done

# ── 3. decisions/────────────────────────────────────────────────────────────
echo "[3] 清理 decisions/..."
for dir in "$WDIR" "$MDIR"; do
    find "$dir/decisions" -maxdepth 2 \( -name "*.yaml" -o -name "*.md" \) \
        -exec rm -f {} \; -print 2>/dev/null | sed 's/^/  Removed: /' || true
done

# ── 4. scratch/（所有 pipeline 工作区和 cloned repos）────────────────────────
echo "[4] 清理 scratch/..."
for dir in "$WDIR" "$MDIR"; do
    find "$dir/scratch" -mindepth 1 -maxdepth 1 -not -name ".gitkeep" -not -name ".DS_Store" \
        -exec rm -rf {} \; -print 2>/dev/null | sed 's/^/  Removed: /' || true
done

# ── 5. data/raw/（实验数据）──────────────────────────────────────────────────
echo "[5] 清理 data/raw/..."
for dir in "$WDIR" "$MDIR"; do
    find "$dir/data/raw" -mindepth 1 -maxdepth 1 -not -name ".gitkeep" -not -name ".DS_Store" \
        -exec rm -rf {} \; -print 2>/dev/null | sed 's/^/  Removed: /' || true
done

# ── 6. knowledge/raw/（导入的 PDF）──────────────────────────────────────────
echo "[6] 清理 knowledge/raw/..."
for dir in "$WDIR" "$MDIR"; do
    find "$dir/knowledge/raw" -maxdepth 1 \( -name "*.pdf" -o -name "*.md" -o -name "*.txt" \) \
        -exec rm -f {} \; -print 2>/dev/null | sed 's/^/  Removed: /' || true
    rm -f "$dir/knowledge/raw/raw_registry.yaml" 2>/dev/null && \
        echo "  Removed: $dir/knowledge/raw/raw_registry.yaml" || true
done

# ── 7. knowledge/wiki/（编译后 wiki 页面）────────────────────────────────────
echo "[7] 清理 knowledge/wiki/..."
for dir in "$WDIR" "$MDIR"; do
    for subdir in concepts methods experiments failures datasets; do
        find "$dir/knowledge/wiki/$subdir" -maxdepth 1 -name "*.md" \
            -exec rm -f {} \; -print 2>/dev/null | sed 's/^/  Removed: /' || true
    done
done

# ── 8. knowledge/proposals/（矿工提案）──────────────────────────────────────
echo "[8] 清理 knowledge/proposals/..."
for dir in "$WDIR" "$MDIR"; do
    find "$dir/knowledge/proposals" -maxdepth 1 \
        \( -name "evaluated_*.yaml" -o -name "new_idea_*.yaml" \) \
        -exec rm -f {} \; -print 2>/dev/null | sed 's/^/  Removed: /' || true
done

# ── 9. runtime/candidates/（候选列表）──────────────────────────────────────
echo "[9] 清理 runtime/candidates/..."
for dir in "$WDIR" "$MDIR"; do
    find "$dir/runtime/candidates" -maxdepth 1 \
        \( -name "candidate_*.json" -o -name "research_brief_*.md" \) \
        -exec rm -f {} \; -print 2>/dev/null | sed 's/^/  Removed: /' || true
done

# ── 10. runtime/logs/（pipeline 日志）────────────────────────────────────────
echo "[10] 清理 runtime/logs/..."
for dir in "$WDIR" "$MDIR"; do
    find "$dir/runtime/logs" -maxdepth 1 -name "*.log" \
        -exec rm -f {} \; -print 2>/dev/null | sed 's/^/  Removed: /' || true
done

# ── 11. runtime/checkpoints/────────────────────────────────────────────────
echo "[11] 清理 runtime/checkpoints/..."
for dir in "$WDIR" "$MDIR"; do
    find "$dir/runtime/checkpoints" -mindepth 1 -maxdepth 1 \
        -not -name ".gitkeep" -not -name ".DS_Store" \
        -exec rm -rf {} \; -print 2>/dev/null | sed 's/^/  Removed: /' || true
done

# ── 12. knowledge/archive/（rejected/expired ideas）──────────────────────────
echo "[12] 清理 knowledge/archive/..."
for dir in "$WDIR" "$MDIR"; do
    for subdir in rejected_ideas expired_ideas; do
        find "$dir/knowledge/archive/$subdir" -maxdepth 1 -name "*.yaml" \
            -exec rm -f {} \; -print 2>/dev/null | sed 's/^/  Removed: /' || true
    done
done

# ── 13. budget.json 全量重置 ────────────────────────────────────────────────
echo "[13] 重置 budget.json..."
for dir in "$WDIR" "$MDIR"; do
    bf="$dir/runtime/budget.json"
    if [ -f "$bf" ]; then
        python3 - "$bf" <<'PYEOF'
import json, sys
path = sys.argv[1]
with open(path) as f:
    d = json.load(f)
d["usage"] = {}
d["allocations"] = {}
with open(path, "w") as f:
    json.dump(d, f, indent=2)
print(f"  Reset: {path}")
PYEOF
    fi
done

echo ""
echo "=== 清理完成，系统已回到空白状态 ==="
echo ""
echo "下一步："
echo "  cd $WDIR"
echo "  unset ALL_PROXY all_proxy GRPC_PROXY grpc_proxy FTP_PROXY ftp_proxy RSYNC_PROXY"
echo "  researchos inject \\"
echo "    --base-paper '/Users/adukopun/Documents/ClaudeCodeWorkspace/papers/Taming model uncertainty in self-adaptive systems using Bayesian Model Averaging.pdf' \\"
echo "    --proposal '/Users/adukopun/Documents/ClaudeCodeWorkspace/papers/research proposal.txt' \\"
echo "    --ref-code 'https://github.com/matteocamilli/bma-package' \\"
echo "    --title 'STACK-SAS: Stacking-Based Ensemble for Model Uncertainty in Self-Adaptive Systems' \\"
echo "    --question 'Does replacing BMA with stacking-based ensemble learning in MAPE-K reduce uncertainty estimation error in self-adaptive systems?' \\"
echo "    --budget 20000"
echo "  python scripts/run_research.py run autonomous --max-ticks 50"
