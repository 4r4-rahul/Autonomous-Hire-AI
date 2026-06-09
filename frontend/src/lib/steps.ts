// Shared pipeline-step definitions (was duplicated in page.tsx + linkedin/page.tsx with drift —
// one had `key`, the other didn't, and the tailor step mislabeled the model).
export const STEPS = [
  { n: 1, name: "Scan LinkedIn",  desc: "10 searches/hr",       key: "scan" },
  { n: 2, name: "Extract JD",     desc: "Navigate job page",    key: "extract" },
  { n: 3, name: "Detect ATS URL", desc: "Skip Easy Apply",      key: "detect" },
  { n: 4, name: "Filter junk",    desc: "Skip agencies",        key: "filter" },
  { n: 5, name: "Score fit",      desc: "Haiku ≥ 65",           key: "score" },
  { n: 6, name: "Tailor resume",  desc: "Opus rewrite",         key: "tailor" },
  { n: 7, name: "QA review",      desc: "Haiku checks ATS",     key: "qa" },
  { n: 8, name: "Submit ATS",     desc: "Extension form fill",  key: "submit" },
];
