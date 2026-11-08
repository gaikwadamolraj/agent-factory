// registry-service / auth-service API contracts (camelCase, mirrors the services' response models). Lists come back as
// { items: [...], total } unless noted. Ids exposed to the wire are UUID strings (external_id); user/project/client
// ids are integers.

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

export interface ApiErrorBody {
  code: string;
  message: string;
  hint: string;
  retryable: boolean;
  retryAfter?: number;
  requestId?: string;
  fields?: { field: string; issue: string }[];
}

export type VersionState = 'draft' | 'in_review' | 'published' | 'archived';

export interface Skill {
  id: string;
  domain: string;
  name: string;
  description?: string | null;
  activeVersionId?: number | null;
  resource: string;
}

export interface SkillVersion {
  id: string;
  skillId: number;
  versionNumber: number;
  state: VersionState;
  authorId?: number | null;
  rejectionReason?: string | null;
  files?: { path: string; contentHash: string }[];
  workflow?: Record<string, unknown>;
  history?: { fromState?: string; toState?: string; comment?: string | null; actorUserId?: number; createdAt?: string }[];
}

export interface SkillDetail extends Skill {
  versions: SkillVersion[];
}

export interface Policy {
  id: string;
  principalType: 'user' | 'group' | 'project' | 'client';
  principalId: number;
  name?: string | null;
  document: Record<string, unknown>;
  enabled: boolean;
}

export interface Group {
  id: string;
  name: string;
  description?: string | null;
  members?: number[];
}

export interface Notification {
  id: string;
  type: string;
  title: string;
  body: string;
  link?: string | null;
  isRead: boolean;
  actorUserId?: number | null;
  createdAt: string;
}

export interface Client {
  id: number;
  slug: string;
  name: string;
  isActive: boolean;
}

export interface CatalogueServer {
  name: string;
  audience: string;
}

export interface SimulateResult {
  userId: number;
  action: string;
  resource: string;
  allowed: boolean;
  effect: string;
  reason: string;
  matchedConditions?: string[];
}

export interface ListResult<T> {
  items: T[];
  total: number;
  unread?: number;
}
