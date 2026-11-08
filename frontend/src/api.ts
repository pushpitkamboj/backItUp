export interface Config {
  supabase_url: string; supabase_publishable_key: string;
  source_install_url: string | null; recovery_install_url: string | null;
  workspace_storage_limit: number;
}
export interface Account {
  workspace_id: string; email: string; recovery_email_verified: boolean;
  connections: Partial<Record<'source' | 'recovery', { login: string; provider_user_id: number }>>;
}
export interface Job {
  id: string; repository_id: string; kind: string; state: string; attempt: number;
  due_at: string; started_at: string | null; finished_at: string | null;
  progress: { stage?: string; detail?: string }; error_code: string | null; error_message: string | null;
  cancel_requested: boolean;
}
export interface Overview {
  repositories: number; complete_snapshots: number; active_jobs: number;
  managed_used_bytes: number; managed_reserved_bytes: number; managed_limit_bytes: number;
  service_available: boolean; jobs: Job[];
}
export interface ManagedStorage {
  available: boolean;
  storage: { name: string; provider: string } | null;
  message: string | null;
}
export interface Plan {
  id: string; interval_hours: number; retention_days: number;
  categories: string[]; enabled: boolean; next_due: string | null;
}
export interface Repository {
  id: string; provider_id: number; owner: string; name: string; private: boolean;
  plan: Plan | null; last_attempt: Job | null; last_complete: string | null;
}
export interface AvailableRepository {
  provider_id: number; installation_id: number; owner: string; name: string; private: boolean;
  size_kb: number;
}
export interface Coverage { status: string; mode?: string; reason?: string; count?: number; ref_count?: number }
export interface Snapshot {
  id: string; repository_id: string; repository: string;
  created_at: string; expires_at: string; size_bytes: number; coverage: Record<string, Coverage>;
  summary: { collection_start: string; collection_end: string; git: { refs: Record<string, string>; default_branch: string }; lfs_count: number };
}
export interface Recovery {
  id: string; snapshot_id: string; target_owner: string; target_name: string; status: string;
  plan: { digest?: string; destination?: { identity: { login: string }; owner: { login: string } }; refs?: Record<string, string>; lfs_count?: number; archived_only?: string[]; collection_end?: string };
  report: { destination_url?: string; git?: { verified: boolean; actual: Record<string, string> }; lfs?: { count: number; status: string }; failures?: { code: string; message: string }[]; metadata?: Record<string, { download: string }> };
  checkpoint: { repository_id?: number; phase?: string }; created_at: string;
}
export interface Notice { id: string; kind: string; message: string; read: boolean; created_at: string }
export interface DashboardData {
  account: Account; overview: Overview; repositories: Repository[]; storage: ManagedStorage;
  snapshots: Snapshot[]; recoveries: Recovery[]; events: Notice[];
}

export const apiBase = (import.meta.env.VITE_API_URL || '').replace(/\/$/, '')

const friendlyCodes: Record<string, string> = {
  auth_setup: 'Sign-in is not configured yet. Please try again later.',
  unauthenticated: 'Your session has expired. Please sign in again.',
  github_setup: 'GitHub connection is being configured. Please try again later.',
  github_authorization: 'GitHub could not complete the authorization. Please try again.',
  github_revoked: 'GitHub access has expired or was revoked. Reconnect GitHub to continue.',
  github_permissions: 'GitHub denied this action. Check the app installation and permissions, then try again.',
  github_not_connected: 'Connect your source GitHub account once, then refresh the repository list.',
  github_rate_limit: 'GitHub is temporarily rate-limiting requests. Please try again shortly.',
  managed_storage_unavailable: 'BackItUp Cloud storage is temporarily unavailable. Please try again later.',
  internal_error: 'Sorry, something broke on our side. Please try again later.',
}

export function friendlyError(error: unknown, fallback = 'Sorry, something broke on our side. Please try again.') : string {
  const message = error instanceof Error ? error.message : typeof error === 'string' ? error : ''
  const code = typeof error === 'object' && error !== null && 'code' in error ? String((error as { code?: unknown }).code || '') : ''
  if (friendlyCodes[code]) return friendlyCodes[code]
  const normalized = message.toLowerCase()
  if (normalized.includes('unsupported provider') || normalized.includes('provider is not enabled')) {
    return 'GitHub sign-in is not enabled yet. Please ask the service operator to enable GitHub in Supabase Auth.'
  }
  if (normalized.includes('network') || normalized.includes('fetch')) {
    return 'We could not reach BackItUp. Check your connection and try again.'
  }
  if (normalized.includes('cancelled') || normalized.includes('canceled')) {
    return 'GitHub authorization was cancelled. You can try again when ready.'
  }
  return message && !/[{}\[\]]/.test(message) ? message : fallback
}

export async function api<T>(path: string, token?: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(apiBase + path, { ...options, headers: {
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers,
  } })
  if (!response.ok) {
    const error = await response.json().catch(() => ({}))
    const value = new Error(error?.error?.message || error?.detail?.[0]?.msg || `Request failed (${response.status})`)
    Object.assign(value, { code: error?.error?.code || error?.code })
    throw value
  }
  return response.json() as Promise<T>
}

export function mutate<T>(path: string, token: string, data?: unknown, method = 'POST'): Promise<T> {
  return api<T>(path, token, { method, headers: { 'Idempotency-Key': crypto.randomUUID() }, body: data === undefined ? undefined : JSON.stringify(data) })
}

export async function loadDashboard(token: string): Promise<DashboardData> {
  const [account, overview, repositories, storage, snapshots, recoveries, events] = await Promise.all([
    api<Account>('/api/v1/account', token), api<Overview>('/api/v1/overview', token),
    api<Repository[]>('/api/v1/repositories', token), api<ManagedStorage>('/api/v1/storage', token),
    api<Snapshot[]>('/api/v1/snapshots?limit=100', token), api<Recovery[]>('/api/v1/recoveries', token), api<Notice[]>('/api/v1/events', token),
  ])
  return { account, overview, repositories, storage, snapshots, recoveries, events }
}

export function bytes(value: number): string {
  if (value < 1000) return `${value} B`
  const unit = Math.min(3, Math.floor(Math.log(value) / Math.log(1000)))
  return `${(value / 1000 ** unit).toFixed(value < 1000 ** (unit + 1) / 10 ? 1 : 0)} ${['B', 'KB', 'MB', 'GB'][unit]}`
}
export function date(value: string | null | undefined): string {
  return value ? new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value)) : 'Not yet'
}
export function label(value: string): string { return value.replaceAll('_', ' ').replace(/^./, c => c.toUpperCase()) }
export const coreCategories = ['git', 'issues', 'pull_requests', 'comments', 'reviews', 'labels', 'milestones', 'releases', 'settings']
export const activeStates = ['queued', 'leased', 'running', 'committing', 'retry_wait']
