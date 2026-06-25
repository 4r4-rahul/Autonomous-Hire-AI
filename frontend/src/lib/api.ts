const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000/api/v1";

async function req<T>(path: string, opts?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`API ${res.status}: ${text}`);
  }
  return res.json() as Promise<T>;
}

export interface Stats {
  total_jobs: number;
  linkedin_jobs: number;
  queued_jobs: number;
  total_applications: number;
  submitted: number;
  pending_review: number;
  pending_manual: number;
  blocked: number;
  avg_quality_score: number;
  pass_rate: number;
}

export interface PipelineStatus {
  running: boolean;
  pid: number | null;
  current_step: string | null;
  queue_depth: number;
  submitted_today: number;
  last_run_at: string | null;
}

export interface Job {
  id: string;
  source: string;
  title: string;
  company_name: string;
  location: string;
  remote: boolean;
  apply_url: string;
  ats_type: string | null;
  easy_apply: boolean;
  salary_min: number | null;
  salary_max: number | null;
  discovered_at: string;
  fit_score: number | null;
  passed_threshold: boolean | null;
}

export interface Application {
  id: string;
  job_id: string;
  status: string;
  quality_score: number | null;
  fit_score: number | null;
  quality_report: Record<string, unknown> | null;
  tailored_resume_path: string | null;
  ats_url: string | null;
  confirmation_url: string | null;
  rejection_reason: string | null;
  pipeline_step: string | null;
  submitted_at: string | null;
  updated_at: string;
  created_at: string | null;   // when the app entered the queue ("added X ago")
  company_name: string | null;
  role_title: string | null;
  posted_at: string | null;
  location: string | null;
  multi_location: boolean;
}

export interface PipelineRun {
  id: string;
  mode: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  jobs_scanned: number | null;
  jobs_applied: number | null;
  error_message: string | null;
}

export interface AppEvent {
  step: string;
  status: string;
  message: string | null;
  detail: Record<string, unknown> | null;
  at: string;
}

export interface AppProof {
  application_id: string;
  company: string | null;
  role: string | null;
  ats: string;
  ats_url: string | null;
  confirmation_url: string | null;
  status: string;
  submitted_at: string | null;
  quality_score: number | null;
  quality_report: Record<string, unknown> | null;
  resume_markdown: string;
  cover_letter_markdown: string;
  has_cover_letter: boolean;
  filled_screenshot_url: string | null;
  confirmation_screenshot_url: string | null;
  debug_screenshot_url: string | null;
  job_description: string;
}

export interface InsightCell {
  submitted: number;
  responded: number;
  positive: number;
  ghosted: number;
  resolved: number;
}

export interface Insights {
  available: boolean;
  generated_at: string | null;
  totals: {
    submitted: number;
    responded: number;
    interviews_or_offers: number;
    ghosted: number;
    pending: number;
    ghosted_this_run: number;
    response_rate: number;
    interview_rate: number;
    has_response_signal: boolean;
  };
  by_company: Record<string, InsightCell>;
  by_role: Record<string, InsightCell>;
  by_fit_band: Record<string, InsightCell>;
  by_source: Record<string, InsightCell>;
  by_ats: Record<string, InsightCell>;
  learnings_written: string[];
  interviews?: { company: string; role: string; status: string; at: string | null }[];
}

export const api = {
  stats:           ()                => req<Stats>("/stats"),
  insights:        ()                => req<Insights>("/insights"),
  pipelineStatus:  ()                => req<PipelineStatus>("/pipeline/status"),
  startPipeline:   (mode: string)    => req<{ ok: boolean; pid?: number }>("/pipeline/start", {
    method: "POST",
    body: JSON.stringify({ mode, auto_approve: false }),
  }),
  stopPipeline:    ()                => req<{ ok: boolean }>("/pipeline/stop", { method: "POST" }),
  listJobs:        (params?: string) => req<Job[]>(`/jobs/${params ? `?${params}` : ""}`),
  getQueue:        (limit = 40)      => req<Job[]>(`/jobs/queue?limit=${limit}`),
  listApps:        (params?: string) => req<Application[]>(`/applications/${params ? `?${params}` : ""}`),
  getApp:          (id: string)      => req<Application>(`/applications/${id}`),
  tailorApp:       (id: string)      => req<{ status: string; resume?: string; quality?: number }>(
    `/applications/${id}/tailor`, { method: "POST" }),
  referralKit:     (id: string, draft = false) => req<{
    company: string; role: string; job_url: string;
    search_urls: Record<string, string>;
    connect_note: string; referral_ask: string; why_me: string; mutual_intro: string;
    recruiter_note: string; hiring_manager_note: string;
  }>(`/applications/${id}/referral-kit?draft=${draft}`, { method: "POST" }),
  decideApp:       (id: string, action: "approve" | "reject", reason?: string) =>
    req<{ ok: boolean }>(`/applications/${id}/decide`, {
      method: "POST",
      body: JSON.stringify({ action, reason }),
    }),
  getAppEvents:    (id: string)      => req<AppEvent[]>(`/applications/${id}/events`),
  getAppProof:     (id: string)      => req<AppProof>(`/applications/${id}/proof`),
  getFormAnswers:  (id: string)      => req<{ count: number; answers: { question: string; answer: string; updated_at: string }[] }>(`/applications/${id}/form-answers`),
  pipelineRuns:    ()                => req<PipelineRun[]>("/pipeline/runs"),
};
