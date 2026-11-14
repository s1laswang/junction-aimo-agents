# Swarm C: product and Gemini experience (owner: Ivar)

**Gates:**
- A kitchen user can order **without typing** (photo or voice) and an approver approves in **one click**.
- The **Finnish UI** reads naturally.
- Multimodal extraction is **≥95% field-accurate** on 150 test images.
- The agent-mode **zero-correction rate** is measured on the full gold set (this feeds swarm A's gate).

**Your 100 cards:**
- `C-ux-*` (20): persona walkthroughs reporting friction with severity.
- `C-img-*` (30): 150 test images with expected extraction (handwritten orders, menus, invoices, delivery notes).
- `C-copy-*` (20): plain-language rewrites and fi/sv translation tables for every user-facing string.
- `C-eval-*` (30): **wave 2**. Run the buyer's eval harness on 30 gold shards once swarm A's gold files and `aimo/buyer/eval_gold.py` are on main.

**Claude leads:**
- frontend:
  - clarifying-question card;
  - "always allow this swap";
  - one-click re-quote;
  - photo intake;
  - CO2, reliability and label badges;
  - Valio Aimo ops tab, Recipes tab, blocked-attacks feed.
- buyer: `needs_clarification`, image intake, the eval harness.

**Ivar, human task (delegated by Silas):** book 15 minutes with a Valio mentor (Minni Ruhtinas for online sales, Riku Tapper or Ville Takanen for IT and data governance). Validate the approval matrix (auto vs needs approval vs reject), the good-better-best tiers and "learned approvals". Write their verdict and one quotable sentence into `swarm/A/out/A-mentor.md`.

**Rules:**
- No real personal data in images.
- Run servers with a scratch `AIMO_DATA_DIR`.
- Gemini calls go through `aimo.gemini`, at most 4 concurrent per agent.

## Eval row (C-eval)
`{"id": "<gold id>", "zero_correction": true, "escalation_match": true, "allergen_safe": true, "final_cart": [...], "outcome": "needs_approval", "latency_s": 31.2, "tokens_in": 27000, "transcript_path": "..."}`
