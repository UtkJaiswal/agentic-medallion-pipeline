"""Generates the SVG diagrams in this folder: `python docs/diagrams/build_diagrams.py`.

Diagrams are code so they stay in sync with the system and are reviewable in diffs. No dependencies."""

from __future__ import annotations

from html import escape
from pathlib import Path

OUT = Path(__file__).parent
FONT = "Inter, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
STYLES = {  # fill, stroke, title colour
    "entry": ("#F1F5F9", "#64748B", "#0F172A"),
    "bronze": ("#FBEBDD", "#B8733A", "#6B3A12"),
    "silver": ("#EEF1F5", "#7C8796", "#1F2937"),
    "gate": ("#FDECEC", "#C24141", "#7F1D1D"),
    "gold": ("#FFF6D6", "#C9A227", "#6B5200"),
    "agent": ("#E7F0FF", "#3B6FD8", "#1E3A8A"),
    "review": ("#FFF1E6", "#E07B24", "#7C3A06"),
    "llm": ("#E8F6EE", "#2E8B57", "#14532D"),
    "ops": ("#F2EDFF", "#7A5AC8", "#3B2370"),
    "plain": ("#FFFFFF", "#94A3B8", "#0F172A"),
    "muted": ("#F8FAFC", "#CBD5E1", "#475569"),
}


class Svg:
    def __init__(self, width: int, height: int, title: str) -> None:
        self.w, self.h, self.parts = width, height, []
        self.parts.append(f'<rect width="{width}" height="{height}" fill="#FFFFFF"/>')
        self.text(24, 34, title, size=20, weight=700, color="#0F172A")

    def text(self, x: float, y: float, s: str, size: int = 13, weight: int = 400, color: str = "#334155",
             anchor: str = "start", italic: bool = False) -> None:
        style = ' font-style="italic"' if italic else ""
        self.parts.append(f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" font-weight="{weight}" '
                          f'fill="{color}" text-anchor="{anchor}"{style}>{escape(s)}</text>')

    def panel(self, x: float, y: float, w: float, h: float, label: str, style: str) -> None:
        fill, stroke, color = STYLES[style]
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" fill="{fill}" fill-opacity="0.45" '
                          f'stroke="{stroke}" stroke-dasharray="6 4"/>')
        self.text(x + 14, y + 22, label.upper(), size=11, weight=700, color=color)

    def box(self, x: float, y: float, w: float, h: float, title: str, lines: tuple[str, ...] = (),
            style: str = "plain") -> tuple[float, float, float, float]:
        fill, stroke, color = STYLES[style]
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{fill}" stroke="{stroke}" '
                          f'stroke-width="1.6"/>')
        self.text(x + w / 2, y + 22, title, size=14, weight=700, color=color, anchor="middle")
        for i, line in enumerate(lines):
            self.text(x + w / 2, y + 42 + i * 17, line, size=12, color="#334155", anchor="middle")
        return x, y, w, h

    def arrow(self, points: list[tuple[float, float]], label: str = "", color: str = "#475569",
              dashed: bool = False, label_at: int = 0, dx: float = 6, dy: float = -6) -> None:
        d = "M " + " L ".join(f"{x} {y}" for x, y in points)
        dash = ' stroke-dasharray="5 4"' if dashed else ""
        self.parts.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.8"{dash} '
                          f'marker-end="url(#arrow)"/>')
        if label:
            (x1, y1), (x2, y2) = points[label_at], points[label_at + 1]
            self.text((x1 + x2) / 2 + dx, (y1 + y2) / 2 + dy, label, size=11, color="#475569",
                      anchor="middle" if dx == 0 else "start", italic=True)

    def save(self, name: str) -> None:
        defs = ('<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
                'orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#475569"/></marker></defs>')
        body = "\n".join(self.parts)
        (OUT / name).write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
                                f'viewBox="0 0 {self.w} {self.h}">{defs}\n{body}\n</svg>\n')


# --------------------------------------------------------------------------------- 1. architecture
def architecture() -> None:
    s = Svg(1240, 760, "System architecture")
    s.text(24, 56, "Thin entry points drive one orchestrator; agents only ever propose, people (or a measured policy) "
                   "approve.", size=13)
    # entry points
    s.panel(24, 76, 1192, 96, "Entry points (thin adapters)", "entry")
    s.box(48, 104, 330, 56, "CLI  ·  medallion run | review | eval", (), "entry")
    s.box(398, 104, 380, 56, "HTTP API  ·  POST /v1/pipeline-runs", ("Idempotency-Key → 202 + poll GET",), "entry")
    s.box(798, 104, 394, 56, "Background worker", ("SKIP LOCKED claim · lease · retry + jitter",), "entry")
    s.arrow([(620, 172), (620, 196)])
    s.box(430, 196, 380, 44, "PipelineRunner  ·  advisory lock · trace_id · run_id", (), "plain")
    # medallion
    s.panel(24, 258, 1192, 176, "Medallion pipeline (PostgreSQL schemas)", "silver")
    s.box(48, 292, 230, 124, "BRONZE", ("raw_tickets.csv, as-is (jsonb)", "file sha256 · row hash",
                                         "schema drift · PII tags"), "bronze")
    s.box(322, 292, 290, 124, "SILVER", ("typed · cleaned · deduplicated", "tickets | quarantine | duplicates",
                                         "reconciliation asserted"), "silver")
    s.box(656, 292, 220, 124, "QUALITY GATE", ("12 approved checks", "critical failure keeps", "previous gold"), "gate")
    s.box(920, 292, 272, 124, "GOLD", ("star schema · 3 marts", "hazard view · knowledge graph", "atomic rebuild"),
          "gold")
    for x1, x2 in ((278, 322), (612, 656), (876, 920)):
        s.arrow([(x1, 354), (x2, 354)])
    s.arrow([(620, 240), (620, 258)])
    # agents
    s.panel(24, 452, 760, 150, "AI agents (propose only)", "agent")
    s.box(48, 484, 230, 102, "Classification", ("labels & templates →", "category, severity, hazard", "memory-aware"),
          "agent")
    s.box(298, 484, 230, 102, "Data Quality", ("profile → rules + SQL", "executed on real data", "repair loop"), "agent")
    s.box(548, 484, 214, 102, "Gold Design", ("schema + brief → marts", "EXPLAIN + read-only run", "repair loop"),
          "agent")
    s.box(808, 452, 408, 150, "Review gate  (ops.agent_proposals)", (
        "trust = human 0.6 · judge 0.3 · agent 0.1",
        "auto-approve only above threshold",
        "everything else → human review queue",
        "approved → ref.* (taxonomy, maps, checks)"), "review")
    s.arrow([(762, 560), (808, 560)])
    s.text(785, 552, "proposals", size=11, color="#475569", anchor="middle", italic=True)
    s.arrow([(1012, 452), (1012, 434), (467, 434), (467, 416)], "approved reference data feeds silver", label_at=1,
            dx=0, dy=-6, color="#E07B24")
    s.arrow([(163, 416), (163, 484)], "profile", dx=8, dy=0, dashed=True)
    # llm gateway + ops
    s.panel(24, 620, 760, 124, "LLM gateway (one entry point for every model call)", "llm")
    s.box(48, 652, 712, 78, "LLMRouter", (
        "guardrails (pre-flight, PII redaction) → cache → provider chain → schema validation + repair",
        "Anthropic · OpenAI · Gemini · OpenRouter (incl. Jev Router) · Ollama · vLLM · native Jev  —  or none"),
        "llm")
    s.arrow([(405, 602), (405, 652)])
    s.text(415, 615, "every model call", size=11, color="#475569", italic=True)
    s.box(808, 620, 408, 124, "ops schema", ("runs = job queue · stage metrics · lineage", "LLM calls & cost · cache",
                                             "proposals · DQ results · outbox", "→ Kafka relay (optional)"), "ops")
    s.save("architecture.svg")


# --------------------------------------------------------------------------------- 2. data flow
def data_flow() -> None:
    s = Svg(1240, 560, "Data flow: where every row goes (counts from data/raw_tickets.csv)")
    s.text(24, 56, "Bronze = silver + quarantine + duplicates is asserted on every run; nothing is dropped silently.",
           size=13)
    s.box(24, 90, 200, 110, "raw_tickets.csv", ("10,280 rows · 13 columns", "8 date formats", "115 category spellings"),
          "muted")
    s.arrow([(224, 145), (264, 145)])
    s.box(264, 80, 250, 130, "BRONZE  bronze.tickets_raw", ("10,280 rows, verbatim jsonb", "+ file sha256 · row hash",
                                                             "+ run_id · ingested_at", "re-run of same file: 0 rows"),
          "bronze")
    s.arrow([(514, 145), (560, 145)])
    s.box(560, 80, 290, 130, "SILVER transform (pure)", ("parse · normalise · flag (dq_flags[])",
                                                          "category via approved maps", "swap repair · entity extraction",
                                                          "dedup: ID + content fingerprint"), "silver")
    s.box(900, 70, 316, 64, "silver.tickets  10,055", ("typed, 45 columns, raw kept for audit",), "silver")
    s.box(900, 146, 316, 52, "silver.tickets_quarantine  30", ("invalid IDs · test / junk markers",), "gate")
    s.box(900, 210, 316, 52, "silver.ticket_duplicates  195", ("merged only if too rare to be chance",), "review")
    s.arrow([(850, 120), (900, 102)])
    s.arrow([(850, 150), (900, 172)])
    s.arrow([(850, 180), (900, 236)])
    s.arrow([(1216, 102), (1230, 102), (1230, 346), (1216, 346)])  # silver.tickets -> gate
    s.text(1224, 290, "tickets", size=11, color="#475569", anchor="end", italic=True)
    s.box(820, 300, 396, 92, "QUALITY GATE  ref.dq_checks", ("12 checks, agent-proposed + human-reviewed",
                                                              "critical over threshold → gold not published",
                                                              "warnings → alerts"), "gate")
    s.arrow([(820, 346), (740, 346)])
    s.panel(24, 290, 716, 250, "Gold (rebuilt atomically in one transaction)", "gold")
    s.box(44, 322, 210, 96, "Star schema", ("fct_tickets", "dim_category · dim_building", "dim_assignee · dim_date"),
          "gold")
    s.box(268, 322, 220, 96, "Business marts", ("mart_sla_performance", "mart_vendor_scorecard", "mart_open_backlog"),
          "gold")
    s.box(502, 322, 222, 96, "AI-enriched", ("v_underprioritised_hazards", "kg_edges (knowledge graph)",
                                             "gold_sandbox.* (agent marts)"), "gold")
    s.box(44, 432, 680, 92, "Lineage on every row", (
        "gold._run_id → silver._bronze_id / _row_hash / _run_id → bronze source_file / sha256 / row_number",
        "→ ops.ingestion_manifest;  every stage → ops.stage_runs (rows in/out/rejected, alerts) + ops.outbox events"),
          "ops")
    s.save("data-flow.svg")


# --------------------------------------------------------------------------------- 3. llm gateway
def llm_gateway() -> None:
    s = Svg(1240, 520, "LLM gateway: the path of every model call")
    s.text(24, 56, "One entry point (LLMRouter). Each step can stop a bad call early; the pipeline never fails "
                   "because a model did.", size=13)
    steps = [
        ("1. Input guardrails", ("unrendered placeholders?", "fits context window?", "PII redaction (hosted)",
                                 "injection screening"), "gate"),
        ("2. Cache", ("key = model + prompt", "version + memory + prompt", "only validated answers", "re-runs cost $0"),
         "ops"),
        ("3. Provider chain", ("in LLM_PROVIDERS order", "e.g. openrouter → ollama", "failure → next provider", ""),
         "llm"),
        ("4. Validate output", ("Pydantic schema", "enums = taxonomy keys", "1 repair round-trip", "invalid → next"),
         "agent"),
    ]
    x = 24
    for title, lines, style in steps:
        s.box(x, 84, 270, 132, title, lines, style)
        if x > 24:
            s.arrow([(x - 34, 150), (x, 150)])
        x += 304
    s.panel(24, 240, 1192, 150, "Inside each provider (decorators, outermost first)", "llm")
    inner = [("Circuit breaker", ("5 transient failures", "→ fail fast 30 s")),
             ("Retry", ("exponential backoff", "full jitter · Retry-After")),
             ("Rate limit", ("token bucket per", "provider (RPM)")),
             ("Metered", ("token + USD budget", "every attempt → ops.llm_calls")),
             ("Adapter", ("SDK call + timeout", "errors → transient | permanent"))]
    x = 48
    for title, lines in inner:
        s.box(x, 272, 208, 100, title, lines, "plain")
        if x > 48:
            s.arrow([(x - 22, 322), (x, 322)])
        x += 230
    s.arrow([(766, 216), (766, 240)])
    s.box(24, 412, 580, 90, "All providers failed or budget spent", ("NoProviderAvailable → the agent uses its",
                                                                      "deterministic strategy (keyword rules) and flags it"),
          "review")
    s.box(636, 412, 580, 90, "Providers (all from .env)", (
        "Anthropic · OpenAI · Gemini · OpenRouter (any model, incl. Jev Router)",
        "Ollama · vLLM (self-hosted)  ·  native Jev (typed choices, TypeSafe key)"), "muted")
    s.save("llm-gateway.svg")


# --------------------------------------------------------------------------------- 4. agents + HITL
def agent_lifecycle() -> None:
    s = Svg(1240, 600, "Agent lifecycle: propose → verify → trust → approve → learn")
    s.text(24, 56, "Agents never write to silver or gold. Approved proposals become reference data; reviews become "
                   "memory.", size=13)
    s.box(24, 90, 220, 116, "Agent proposes", ("classification: category,", "severity, hazard", "DQ: rule + SQL",
                                               "gold: mart SQL"), "agent")
    s.arrow([(244, 148), (284, 148)])
    s.box(284, 90, 240, 116, "Code verifies", ("schema + taxonomy enums", "SQL guard (read-only, timeout)",
                                               "executed on real data", "repair loop ≤ 2 rounds"), "gate")
    s.arrow([(524, 148), (564, 148)])
    s.box(564, 90, 300, 116, "Trust score", ("human 0.6  (similar past reviews)", "judge 0.3  (independent model)",
                                             "agent 0.1  (self-confidence)", "missing signals renormalised"), "review")
    s.arrow([(864, 120), (920, 104)])
    s.arrow([(864, 176), (920, 192)])
    s.text(892, 70, "trust ≥ threshold (classification only)", size=11, color="#475569", anchor="middle", italic=True)
    s.text(892, 240, "otherwise; DQ & gold always", size=11, color="#475569", anchor="middle", italic=True)
    s.box(920, 74, 296, 60, "Auto-approved", ("policy:auto, audited",), "llm")
    s.box(920, 162, 296, 60, "Human review queue", ("lowest trust first · approve / correct / reject",), "review")
    s.arrow([(1068, 222), (1068, 272)], "approve or correct", dx=8, dy=0)
    s.arrow([(1068, 134), (1180, 134), (1180, 272)])
    s.box(860, 272, 356, 96, "Reference data (ref.*)", ("category & template maps · DQ checks",
                                                         "ontology · gold_sandbox views", "source + approver recorded"),
          "gold")
    s.arrow([(860, 320), (764, 320)])
    s.text(812, 312, "every run", size=11, color="#475569", anchor="middle", italic=True)
    s.box(484, 280, 280, 80, "Pipeline (silver · gate · gold)", ("known values never re-asked",), "silver")
    s.panel(24, 400, 1192, 180, "Memory (feeds the next proposal)", "agent")
    s.box(48, 432, 360, 128, "Semantic memory", ("what the system knows:", "approved maps, taxonomy, ontology",
                                                 "→ skip the LLM for known values"), "plain")
    s.box(440, 432, 360, 128, "Episodic memory", ("what happened: human corrections", "similar ones recalled as precedents",
                                                  "(41 corrections from review)"), "plain")
    s.box(832, 432, 360, 128, "Procedural memory", ("how to do it: rules consolidated", "from reviews, versioned files",
                                                    "→ injected into the system prompt"), "plain")
    s.arrow([(1068, 368), (1068, 396), (620, 396), (620, 432)], "corrections become episodes", label_at=1, dx=0, dy=-4,
            dashed=True)
    s.arrow([(228, 432), (228, 400), (134, 400), (134, 206)], "", dashed=True)
    s.text(140, 300, "recall", size=11, color="#475569", italic=True)
    s.save("agent-lifecycle.svg")


# --------------------------------------------------------------------------------- 5. API job lifecycle
def job_lifecycle() -> None:
    s = Svg(1240, 420, "API run lifecycle: idempotent submit, background execution, polling")
    s.box(24, 84, 250, 112, "Client  (medallion submit)", ("POST + Idempotency-Key", "+ traceparent / X-Trace-Id",
                                                           "retries 429/5xx with backoff", "then polls with jitter"),
          "entry")
    s.arrow([(274, 120), (330, 120)], "POST", dx=-10, dy=-8)
    s.box(330, 70, 300, 140, "API", ("new key → INSERT run 'queued' → 202", "same key + same body → 200 replay",
                                     "same key + other body → 422", "rate limit → 429 + Retry-After",
                                     "X-Trace-Id echoed"), "entry")
    s.arrow([(630, 140), (690, 140)])
    s.box(690, 84, 250, 112, "ops.pipeline_runs", ("the job queue", "status · attempts", "not_before · lease"), "ops")
    s.arrow([(940, 140), (990, 140)], "claim", dx=-14, dy=-8)
    s.box(990, 70, 226, 140, "Worker(s)", ("FOR UPDATE SKIP LOCKED", "lease + heartbeat", "runs PipelineRunner",
                                          "idle poll with jitter"), "entry")
    s.box(330, 280, 260, 100, "succeeded", ("summary + stage metrics", "events → outbox"), "llm")
    s.box(620, 280, 290, 100, "transient failure", ("re-queued with exponential", "backoff + jitter (not_before)",
                                                    "up to max_attempts"), "review")
    s.box(940, 280, 276, 100, "crashed worker", ("lease expires", "another worker re-claims", "(pipeline is idempotent)"),
          "gate")
    s.arrow([(1103, 210), (1103, 280)])
    s.arrow([(1040, 210), (765, 280)])
    s.arrow([(1000, 210), (520, 280)])
    s.arrow([(149, 196), (149, 232), (480, 232), (480, 210)])
    s.text(160, 250, "poll GET /v1/pipeline-runs/{id}  (Retry-After hint while running)", size=11,
           color="#475569", italic=True)
    s.save("job-lifecycle.svg")


if __name__ == "__main__":
    architecture()
    data_flow()
    llm_gateway()
    agent_lifecycle()
    job_lifecycle()
