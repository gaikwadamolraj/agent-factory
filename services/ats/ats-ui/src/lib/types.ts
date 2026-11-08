// API v1 contracts (camelCase, mirrors the orchestrator's schemas).

export interface Project {
  id: number;
  name: string;
  description?: string | null;
}

export interface Session {
  user: { id: number; username: string; role: string };
  client: { id: number; name: string };
  projects: Project[];
  expiresIn?: number | null;
}

export type AssignableRole = 'admin' | 'recruiter';

export interface AdminUser {
  id: number;
  username: string;
  email?: string | null;
  role: string;
  isActive: boolean;
  lastLoginAt?: string | null;
  createdAt: string;
}

export type DocumentKind = 'resume' | 'jd';

export interface DocumentInfo {
  kind: DocumentKind;
  filename: string;
  contentType: string;
  sizeBytes: number;
  pages: number;
  createdAt: string;
}

export interface DocumentText {
  kind: DocumentKind;
  filename: string;
  text: string;
}

export interface ChatSummary {
  id: string;
  title: string;
  hasJobDescription: boolean;
  hasResume: boolean;
  lastScore: number | null;
  createdAt: string;
  updatedAt: string;
}

export interface Requirement {
  name: string;
  importance: 'must' | 'nice';
  kind: string;
  jdQuote: string;
  grade: 'strong' | 'moderate' | 'adjacent' | 'claimed' | 'invalidated' | 'absent';
  matchedSkills: string[];
  evidence: string;
  years: number | null;
}

export interface Screening {
  skill: string;
  model: string;
  roleTitle: string;
  sector: string;
  score: number;
  band: string;
  recommendation: string;
  breakdown: Record<string, number | string | null>;
  totalYears: number | null;
  requiredYears: number | null;
  requirements: Requirement[];
  summary: string;
  recommendationReason: string;
  questions: { skill: string; question: string; expectedAnswer: string; redFlag: string }[];
  integrityNote: string;
  agentTrace: {
    stepsByAgent?: string[];
    stepsFallback?: string[];
    corrections?: string[];
    skill?: string;
    agentError?: string;
  };
}

export interface Message {
  id: number;
  role: 'user' | 'assistant';
  content: string;
  createdAt: string;
  data: {
    action?: string;
    // `onboardingRunId` is present on a resume when the upload started a durable onboarding run; without a
    // workflow engine the same work happens in the background and there is no run to follow.
    attachments?: { kind: DocumentKind; filename: string; pages: number; characters: number;
                    onboardingRunId?: string }[];
    jdUpdated?: boolean;
    screening?: Screening | null;
    toolsUsed?: string[];
    aborted?: boolean;
    errorCode?: string | null;
  };
}

export interface ChatDetail extends ChatSummary {
  jobDescription: string | null;
  resumeDocument: DocumentInfo | null;
  jdDocument: DocumentInfo | null;
  runningTurnId: string | null;
  messages: Message[];
}

export type RunStatus = 'queued' | 'running' | 'waiting' | 'completed' | 'failed' | 'cancelled';

/** One durable run, as the orchestrator's `workflow_runs` projection exposes it (GET /api/v1/runs/{id}). */
export interface Run {
  id: string;
  domain: string;
  journey: string;
  status: RunStatus;
  inputRef: Record<string, unknown>;
  resultSummary: {
    subjectRef?: string;
    stage?: string;
    chunks?: number;
    episodeId?: string;
    flagged?: boolean;
    notes?: string[];
  };
  errorCode: string | null;
  errorSummary: string | null;
  createdAt: string;
  startedAt: string | null;
  completedAt: string | null;
}

export const RUN_FINISHED: RunStatus[] = ['completed', 'failed', 'cancelled'];

export interface ApiErrorBody {
  code: string;
  message: string;
  hint: string;
  retryable: boolean;
  retryAfter?: number;
  requestId?: string;
}

export interface TurnEvent {
  type: 'turn_started' | 'user_message' | 'progress' | 'heartbeat' | 'message' | 'aborted' | 'error';
  turnId?: string;
  stage?: string;
  message?: any;
  progress?: number;
  elapsed?: number;
  chatTitle?: string;
  error?: ApiErrorBody;
}
