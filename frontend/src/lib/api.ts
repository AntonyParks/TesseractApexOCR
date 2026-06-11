const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8080";

// Matches player_ratings table columns
export interface PlayerRating {
  player: string;
  elo: number;
  matches_played: number;
  total_kills: number;
  total_deaths: number;
  peak_elo: number;
  last_updated: string;
}

export interface RankingsResponse {
  total: number;
  offset: number;
  limit: number;
  players: PlayerRating[];
}

// Matches match_kills table columns (returned by /matches/{id})
export interface MatchKill {
  kill_order: number;
  attacker: string;
  victim: string;
  timestamp: string;
  attacker_conf: number;
  victim_conf: number;
}

// Matches match_placements table columns
export interface MatchPlacement {
  player: string;
  kill_order_out: number;
  elo_before: number;
  elo_after: number;
  elo_change: number;
}

// Matches matches table columns
export interface Match {
  match_id: string;
  streamer: string;
  start_time: string;
  end_time: string;
  kill_count: number;
  players_observed: number;
}

// /matches/{id} returns { match: Match, kills: MatchKill[], placements: MatchPlacement[] }
export interface MatchDetail {
  match: Match;
  kills: MatchKill[];
  placements: MatchPlacement[];
}

// /rankings/{player} match history rows
export interface MatchHistory {
  match_id: string;
  kill_order_out: number;
  elo_before: number;
  elo_after: number;
  elo_change: number;
  streamer: string;
  start_time: string;
  kill_count: number;
}

export interface PlayerDetail {
  player: PlayerRating;
  match_history: MatchHistory[];
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { next: { revalidate: 60 } });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<T>;
}

export const api = {
  rankings: (params?: { min_matches?: number; limit?: number; offset?: number }) => {
    const q = new URLSearchParams();
    if (params?.min_matches) q.set("min_matches", String(params.min_matches));
    if (params?.limit) q.set("limit", String(params.limit));
    if (params?.offset) q.set("offset", String(params.offset));
    return get<RankingsResponse>(`/rankings?${q}`);
  },
  player: (name: string) => get<PlayerDetail>(`/rankings/${encodeURIComponent(name)}`),
  matches: (params?: { streamer?: string; limit?: number; offset?: number }) => {
    const q = new URLSearchParams();
    if (params?.streamer) q.set("streamer", params.streamer);
    if (params?.limit) q.set("limit", String(params.limit));
    if (params?.offset) q.set("offset", String(params.offset));
    return get<{ total: number; matches: Match[] }>(`/matches?${q}`);
  },
  match: (id: string) => get<MatchDetail>(`/matches/${encodeURIComponent(id)}`),
};
