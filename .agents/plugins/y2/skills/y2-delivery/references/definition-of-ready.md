# Definition of Ready (DoR) Checklist

Before accepting any task card `T-nn` for implementation, verify:
- [ ] **Owner Role:** Exactly one assigned role (`y2-engineer`, `y2-researcher`, `y2-test-engineer`, `y2-azure-tester`).
- [ ] **Scope Boundaries:** Declared `scope.create`, `scope.modify`, and `scope.must_not_touch`.
- [ ] **Concise Behaviour:** Clear implementation description in ≤ 10 lines.
- [ ] **Traceability:** Explicit `covers:` naming one or more `AC-n`.
- [ ] **Done Criteria:** Explicit `done_when:` naming one or more `TP-nn` test items.
- [ ] **Size:** Estimated `S` or `M`.
- [ ] **Dependencies:** Explicit `depends_on:` list.
