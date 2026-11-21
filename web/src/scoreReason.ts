// Why a correctness score is lower than the judge model's own verdict (Issue #197): one line per downgraded fact, in words.
const WHY: Record<string, string> = {
  number_absent: "找不到要點裡的數字",
  number_other_entity: "要點裡的數字在答案中掛在別的對象上",
  entity_absent: "找不到要點裡的對象",
  opposite_direction: "方向（高／低、增／減）和要點相反",
  span_does_not_carry_fact: "評分模型引用的答案片段沒有帶出這個要點的內容",
  judge_span_not_in_answer: "評分模型引用的片段不在答案裡（或沒有引用）",
};

/** "fact_downgraded:f2:number_absent" -> "要點 2：評分模型判定有說到，但程式檢查「找不到要點裡的數字」而降級。" */
export function downgradeNotes(codes: string[] | undefined): string[] {
  const out: string[] = [];
  for (const c of codes ?? []) {
    const m = /^fact_downgraded:f(\d+):([a-z_]+)$/.exec(c);
    if (!m) continue;
    out.push(`要點 ${m[1]}：評分模型判定有說到，但程式檢查「${WHY[m[2]] ?? m[2]}」而降級。`);
  }
  return out;
}

export const REPORT_HINT = "如果這個降級看起來不對（答案其實說到了），請把這一題回報。";
