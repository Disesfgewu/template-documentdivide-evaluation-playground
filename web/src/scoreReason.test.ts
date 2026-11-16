import { describe, expect, it } from "vitest";
import { downgradeNotes } from "./scoreReason";

describe("downgradeNotes", () => {
  it("says in words which fact was downgraded and why", () => {
    const lines = downgradeNotes(["judge_overruled_by_number_check", "fact_downgraded:f1:number_absent", "fact_downgraded:f2:number_other_entity"]);
    expect(lines).toEqual([
      "要點 1：評分模型判定有說到，但程式檢查「找不到要點裡的數字」而降級。",
      "要點 2：評分模型判定有說到，但程式檢查「要點裡的數字在答案中掛在別的對象上」而降級。",
    ]);
  });
  it("has a sentence for every code the judge can produce and ignores everything else", () => {
    for (const code of ["number_absent", "number_other_entity", "entity_absent", "opposite_direction", "span_does_not_carry_fact", "judge_span_not_in_answer"]) {
      expect(downgradeNotes([`fact_downgraded:f3:${code}`])[0]).not.toContain(code);
    }
    expect(downgradeNotes(["contradicted_fact", "abstained", "fact_downgraded:bad", "fact_downgraded:f1:"])).toEqual([]);
    expect(downgradeNotes(undefined)).toEqual([]);
  });
  it("shows an unknown code as it is rather than hiding the downgrade", () => {
    expect(downgradeNotes(["fact_downgraded:f1:some_new_code"])[0]).toContain("some_new_code");
  });
});
