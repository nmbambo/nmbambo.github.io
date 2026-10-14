// Decision Maturity Self-Check: the fifteen criteria of the Decision Maturity Assessment rubric v1.0,
// asked of one decision. The database scores answers; this file only words the questions.

export const STAGES = [
  ['F', 'Framing', 'Was the decision clearly defined before it was made?'],
  ['E', 'Evidence', 'Were the claims behind it sourced, and their kind made plain?'],
  ['J', 'Judgement', 'Did the right authority decide, between real options, with a stated expectation?'],
  ['X', 'Execution', 'Can you show what was implemented, under what conditions, and when it would stop?'],
  ['R', 'Review', 'Was the decision reviewed independently, on what was knowable at the time?'],
];

// [id, stage, label, question, [level 0, 1, 2, 3]]
export const CRITERIA = [
  ['F1', 'F', 'Answerable question', 'Was the decision stated as a question someone could answer?',
    ['Only a direction or a topic', 'The question existed informally, in an email or in conversation', 'A template requires a decision question', 'One sentence, dated before the decision, answerable by yes, no or an option']],
  ['F2', 'F', 'Scope and out-of-scope', 'Was it clear what the decision covered, and what it deliberately did not?',
    ['Not stated', 'Implied in discussion', 'A template asks for scope', 'Scope and the neighbouring questions it excluded are written down']],
  ['F3', 'F', 'Stakes and reversibility', 'Were the stakes, and how reversible the decision was, considered?',
    ['Not considered', 'Mentioned in passing', 'Required for some classes of decision', 'Reversibility and who is affected are stated, and they set how much rigour was applied']],
  ['E1', 'E', 'Sourced claims', 'Could someone else open the source of each material claim?',
    ['Figures without sources', 'Some sources, inconsistently', 'Papers must cite sources', 'Each material claim traces to a source someone else could open']],
  ['E2', 'E', 'Epistemic kind', 'Were measured, modelled, assumed and asserted claims kept apart?',
    ['Mixed together without distinction', 'Some caveats given', 'Standard caveats or confidence language are required', 'Each claim marked as observed, inferred, assumed or asserted, and unsupported assertions given no weight']],
  ['E3', 'E', 'Terms and meaning', 'Were the terms the decision turned on defined, as they are actually computed?',
    ['Key terms undefined', 'Definitions known to some people', 'A glossary, semantic layer or data dictionary exists', 'The terms it turned on are defined as computed, and known divergences are recorded']],
  ['J1', 'J', 'Authority and limits', 'Was the decider’s authority, and its limits, established?',
    ['The decider is unclear', 'The decider is known by custom', 'A delegation of authority framework exists', 'The mandate is identified by instrument, predates the decision, and its limits are stated']],
  ['J2', 'J', 'Options and rationale', 'Were real options compared, and the reasons for the choice kept?',
    ['A single option was presented', 'Alternatives were discussed but not recorded', 'A template requires options', 'Options including the status quo recorded, with reasons for the choice, the rejections, and any dissent and its answer']],
  ['J3', 'J', 'Predicted outcome', 'Was the expected outcome, and how it would be measured, stated beforehand?',
    ['No expectation stated', 'Hoped-for benefits described', 'A business case with targets is required', 'Expected outcome and its measure stated before execution']],
  ['X1', 'X', 'Conditions', 'Were the conditions the decision depended on recorded, each with an owner?',
    ['None', 'Informal watch-outs', 'Implementation plans require risks and controls', 'Preconditions and ongoing conditions recorded, each with an owner']],
  ['X2', 'X', 'Stop rule', 'Was there an agreed point at which execution would halt or reverse?',
    ['No point of reversal', 'An informal understanding about escalation', 'Escalation thresholds exist in general', 'Specific, pre-agreed conditions to halt or reverse, with an owner']],
  ['X3', 'X', 'Fidelity', 'Can you show what was implemented, and whether it matched the decision?',
    ['You cannot tell what was implemented', 'Implementation is known anecdotally', 'Change records exist', 'Evidence shows what was implemented and whether it matched the decision and its conditions']],
  ['R1', 'R', 'Review happened', 'Did a review take place when it was due?',
    ['No review', 'An informal look back', 'A post-implementation review is required', 'A dated review took place, or is scheduled with triggers, and was not skipped when due']],
  ['R2', 'R', 'Independence', 'Was the reviewer independent of the decider?',
    ['No review, so no reviewer', 'Reviewed by the decider', 'A review function exists', 'The reviewer was independent of the decider']],
  ['R3', 'R', 'Decision quality vs outcome', 'Was the decision judged on what was knowable at the time, not only on how it turned out?',
    ['Judged on outcome only', 'Some discussion of process', 'The review template covers process and outcome', 'Judged on what was knowable, against criteria set before execution, and fed back into practice']],
];

export const LEVELS = ['Absent', 'Ad hoc', 'Defined', 'Demonstrated'];

export const EVIDENCE = [
  ['artefact', 'A dated record from this decision', 'counts up to 3'],
  ['policy', 'A rule or template that requires it', 'counts up to 2'],
  ['testimony', 'People involved would say so', 'counts up to 1'],
  ['none', 'Nothing I could show', 'counts as 0'],
];
export const EVIDENCE_LABEL = Object.fromEntries(EVIDENCE.map(([k, l]) => [k, l]));

export const criterion = (id) => CRITERIA.find((c) => c[0] === id);
export const stageName = (s) => (STAGES.find((x) => x[0] === s) || [])[1] || s;

// Answers as the form holds them -> answers as submit_self_check takes them. Returns {answers, missing}.
export function collectAnswers(state) {
  const answers = {};
  const missing = [];
  for (const [id] of CRITERIA) {
    const a = state[id] || {};
    if (a.unknown) { answers[id] = { unknown: true }; continue; }
    const level = Number.isInteger(a.level) ? a.level : null;
    const evidence = EVIDENCE.some(([k]) => k === a.evidence) ? a.evidence : null;
    if (level === null || evidence === null) { missing.push(id); continue; }
    answers[id] = { level, evidence, unknown: false };
  }
  return { answers, missing };
}

// Server answers (from a stored payload) -> form state, for a rerun.
export function stateFromAnswers(answers = {}) {
  const state = {};
  for (const [id] of CRITERIA) {
    const a = answers[id];
    if (!a) continue;
    state[id] = a.unknown ? { unknown: true } : { level: a.level, evidence: a.evidence, unknown: false };
  }
  return state;
}
