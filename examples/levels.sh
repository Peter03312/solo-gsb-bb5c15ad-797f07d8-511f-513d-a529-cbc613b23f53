#!/usr/bin/env bash
# 本轮用法端到端示例: 论文评审保障等级 (高 / 中 / 普通)
#   录入 -> 设置等级 (携带所见修订号; 相同等级重试幂等, 有效变更推进修订号) ->
#   查询各论文等级 -> 制造容量不足 -> 普通预演无法完整覆盖时:
#     先完整总数最大, 再依次保高/中等级, 最后字典序; 返回各等级已分配/未分配数量 ->
#   无法完整分配仍拒绝发布 (当前发布版不变) ->
#   发布 + 评审人确认 -> 补位预演同样按等级择优 -> 删除/撤回论文后其等级不再参与求解
# 建议对新库运行: ./examples/levels.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
ORG="X-Organizer-Key: ${ORGANIZER_KEY:-dev-organizer-key}"
j() { python3 -c 'import sys, json; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$1"; }

echo "== 1. 录入 3 名不同机构评审人 (容量: R1=1, R2=1, R3=2) 与 3 篇论文 =="
for spec in 'R1 r1-secret 1' 'R2 r2-secret 1' 'R3 r3-secret 2'; do
  set -- $spec
  curl -s -o /dev/null -X POST "$BASE/reviewers" -H "$ORG" -H 'Content-Type: application/json' -d "{
    \"reviewer_id\": \"$1\", \"credential\": \"$2\", \"topics\": [\"AI\"],
    \"institution\": \"Inst-$1\", \"capacity\": $3, \"avoid_papers\": []}"
done
for pid in P1 P2 P3; do
  curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d "{
    \"paper_id\": \"$pid\", \"manuscript\": \"$pid 匿名稿全文...\", \"topics\": [\"AI\"],
    \"institutions\": [\"Author-$pid\"]}"
done

echo "== 2. 未设置等级时一律视为普通 =="
curl -s "$BASE/papers/P1/review-level" -H "$ORG" \
  | j "{'paper': d['paper_id'], 'level': d['level'], 'explicit': d['explicit']}"

echo "== 3. 设置等级: P3=高, P2=中 (P1 保持普通), 每次携带当前资料修订号 =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -X POST "$BASE/papers/P3/review-level" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P3\", \"base_revision\": $REV, \"level\": \"high\"}" \
  | j "{'paper': d['paper_id'], 'level': d['level'], 'changed': d['changed'], 'revision': d['revision']}"
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -X POST "$BASE/papers/P2/review-level" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P2\", \"base_revision\": $REV, \"level\": \"medium\"}" \
  | j "{'paper': d['paper_id'], 'level': d['level'], 'changed': d['changed'], 'revision': d['revision']}"

echo "== 4. 查询各论文等级与修订号 (含各等级计数) =="
curl -s "$BASE/papers/review-levels/all" -H "$ORG" \
  | j "{'revision': d['revision'], 'levels': d['levels'], 'counts': d['counts']}"

echo "== 5. 相同等级重试 -> changed=false 不推进修订号; 版本不符 409; 非法等级 422 =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -X POST "$BASE/papers/P3/review-level" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P3\", \"base_revision\": $REV, \"level\": \"high\"}" \
  | j "{'changed': d['changed'], 'revision': d['revision']}   # 幂等"
curl -s -o /dev/null -w '版本不符         -> HTTP %{http_code}\n' -X POST "$BASE/papers/P3/review-level" \
  -H "$ORG" -H 'Content-Type: application/json' \
  -d '{"paper_id": "P3", "base_revision": 0, "level": "high"}'
curl -s -o /dev/null -w '非法等级         -> HTTP %{http_code}\n' -X POST "$BASE/papers/P3/review-level" \
  -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P3\", \"base_revision\": $REV, \"level\": \"critical\"}"
curl -s -o /dev/null -w '未知论文         -> HTTP %{http_code}\n' -X POST "$BASE/papers/GHOST/review-level" \
  -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"GHOST\", \"base_revision\": $REV, \"level\": \"high\"}"

echo "== 6. 普通预演: 总容量 4 个槽位, 3 篇论文需要 6 个 -> 无法完整分配"
echo "   择优次序: 先完整总数最大 (=2), 再保高 (P3)、保中 (P2), 最后字典序"
curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" \
  | j "{'feasible': d['feasible'], 'plan': d['plan'], 'unassigned': d['unassigned'], 'level_counts': d['level_counts']}"

echo "== 7. 无法完整分配时发布仍被拒绝 (422), 当前发布版不变 (尚无发布版 -> 404) =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -o /dev/null -w '强行普通发布     -> HTTP %{http_code}\n' -X POST "$BASE/assignment/publish" \
  -H "$ORG" -H 'Content-Type: application/json' -d "{\"base_revision\": $REV}"

echo "== 8. 新增 R4 (容量 2) 使完整分配可行: 仍按容量比例 + 字典序优化 (等级不参与) =="
curl -s -o /dev/null -X POST "$BASE/reviewers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "reviewer_id": "R4", "credential": "r4-secret", "topics": ["AI"],
  "institution": "Inst-R4", "capacity": 2, "avoid_papers": []}'
curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" \
  | j "{'feasible': d['feasible'], 'ratio': d['max_used_capacity_ratio'], 'level_counts': d['level_counts']}"

echo "== 9. 发布当前完整方案 =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
PUB=$(curl -s -X POST "$BASE/assignment/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV}")
echo "$PUB" | j "{'ok': d['ok'], 'serial': d['serial']}"

echo "== 10. 补位预演: 等级择优同样适用 (完整可行时不受影响), 返回各等级数量 =="
curl -s -X POST "$BASE/assignment/backfill/dry-run" -H "$ORG" \
  | j "{'feasible': d['feasible'], 'level_counts': d['level_counts'], 'serial': d['serial']}"

echo "== 11. 高等级恢复为普通是有效变更 (推进修订号); 再提交普通则幂等 =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -X POST "$BASE/papers/P3/review-level" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P3\", \"base_revision\": $REV, \"level\": \"normal\"}" \
  | j "{'level': d['level'], 'changed': d['changed'], 'revision': d['revision']}"
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -X POST "$BASE/papers/P3/review-level" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P3\", \"base_revision\": $REV, \"level\": \"normal\"}" \
  | j "{'changed': d['changed']}   # 再次普通: 幂等"
curl -s "$BASE/papers/P3/review-level" -H "$ORG" \
  | j "{'level': d['level'], 'explicit': d['explicit']}"

echo "== 12. 删除 P1 后其等级不再参与求解; 查询列表也不再包含 =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -o /dev/null -X DELETE "$BASE/papers/P1" -H "$ORG"
curl -s "$BASE/papers/review-levels/all" -H "$ORG" \
  | j "{'levels': d['levels'], 'counts': d['counts']}"
echo "== 13. 撤回 P2: 撤回稿不可再设置等级 (404), 等级随撤回清除 =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -o /dev/null -X POST "$BASE/papers/P2/withdrawal" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P2\", \"base_revision\": $REV, \"reason\": \"作者申请撤稿\"}"
curl -s -o /dev/null -w '对撤回稿设置等级 -> HTTP %{http_code}\n' -X POST "$BASE/papers/P2/review-level" \
  -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"paper_id\": \"P2\", \"base_revision\": $(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]'), \"level\": \"high\"}"
curl -s "$BASE/papers/review-levels/all" -H "$ORG" \
  | j "{'levels': d['levels']}   # 仅剩未撤回论文"
