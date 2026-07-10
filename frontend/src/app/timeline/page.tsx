"use client";

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, Match, MatchDetail, MatchKill, MatchPlacement } from "@/lib/api";

// SQLite timestamps are "YYYY-MM-DD HH:MM:SS" — normalize for reliable Date parsing
function ts(s: string): number {
  return new Date(s.replace(" ", "T")).getTime();
}
function clock(s: string): string {
  return s.slice(11);
}
function fmtGap(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

interface PlayerStats {
  name: string;
  kills: number;
  deaths: number;
  placement?: MatchPlacement;
}

export default function TimelinePage() {
  const [matches, setMatches] = useState<Match[] | null>(null);
  const [filter, setFilter] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<MatchDetail | null>(null);
  const [selectedPlayer, setSelectedPlayer] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .matches({ limit: 200 })
      .then((d) => {
        setMatches(d.matches);
        if (d.matches.length > 0) setSelectedId(d.matches[0].match_id);
      })
      .catch(() => setError("Could not reach API — is api.py running on :8080?"));
  }, []);

  useEffect(() => {
    if (!selectedId) return;
    setDetail(null);
    setSelectedPlayer(null);
    api
      .match(selectedId)
      .then(setDetail)
      .catch(() => setError(`Could not load match ${selectedId}`));
  }, [selectedId]);

  if (error) return <p style={{ color: "#f87" }}>{error}</p>;
  if (!matches) return <p style={{ color: "#888" }}>Loading matches…</p>;

  const visible = filter
    ? matches.filter((m) => m.streamer.toLowerCase().includes(filter.toLowerCase()))
    : matches;

  return (
    <div style={{ display: "grid", gridTemplateColumns: "220px minmax(0,1fr) 220px", gap: "1rem", alignItems: "start" }}>
      <MatchRail
        matches={visible}
        filter={filter}
        onFilter={setFilter}
        selectedId={selectedId}
        onSelect={setSelectedId}
      />
      {detail ? (
        <Timeline detail={detail} selectedPlayer={selectedPlayer} onSelectPlayer={setSelectedPlayer} />
      ) : (
        <p style={{ color: "#888", paddingTop: "2rem" }}>Loading match…</p>
      )}
      {detail ? (
        <PlayerRail detail={detail} selectedPlayer={selectedPlayer} onSelectPlayer={setSelectedPlayer} />
      ) : (
        <div />
      )}
    </div>
  );
}

/* ------------------------------- match rail ------------------------------ */

function MatchRail({
  matches, filter, onFilter, selectedId, onSelect,
}: {
  matches: Match[];
  filter: string;
  onFilter: (v: string) => void;
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <div style={{ position: "sticky", top: "1rem" }}>
      <input
        value={filter}
        onChange={(e) => onFilter(e.target.value)}
        placeholder="Filter streamer…"
        style={{
          width: "100%", boxSizing: "border-box", padding: "6px 8px", marginBottom: "0.5rem",
          background: "#111", border: "1px solid #333", borderRadius: 4, color: "#e0e0e0",
          fontSize: "0.8rem", outline: "none",
        }}
      />
      <div style={{ maxHeight: "76vh", overflowY: "auto", border: "1px solid #1a1a1a", borderRadius: 4 }}>
        {matches.map((m) => {
          const active = m.match_id === selectedId;
          return (
            <button
              key={m.match_id}
              onClick={() => onSelect(m.match_id)}
              style={{
                display: "block", width: "100%", textAlign: "left", cursor: "pointer",
                padding: "7px 10px", border: "none", borderBottom: "1px solid #161616",
                background: active ? "#1c1910" : "#0f0f0f",
                borderLeft: active ? "2px solid #e8d5a3" : "2px solid transparent",
              }}
            >
              <span style={{ color: "#e8d5a3", fontSize: "0.8rem" }}>{m.streamer}</span>
              {m.merged_from ? (
                <span style={{
                  marginLeft: 6, fontSize: "0.62rem", color: "#0a0a0a", background: "#e8d5a3",
                  borderRadius: 3, padding: "1px 4px", verticalAlign: "1px", fontWeight: 600,
                }}>
                  MERGED
                </span>
              ) : null}
              <br />
              <span style={{ color: "#888", fontSize: "0.72rem", fontVariantNumeric: "tabular-nums" }}>
                {m.start_time.slice(5, 16)} · {m.kill_count} kills · {m.players_observed}p
              </span>
            </button>
          );
        })}
        {matches.length === 0 && (
          <p style={{ color: "#666", fontSize: "0.8rem", padding: "10px" }}>No matches.</p>
        )}
      </div>
    </div>
  );
}

/* -------------------------------- timeline ------------------------------- */

const GAP_MARKER_SECONDS = 90;

function Timeline({
  detail, selectedPlayer, onSelectPlayer,
}: {
  detail: MatchDetail;
  selectedPlayer: string | null;
  onSelectPlayer: (p: string | null) => void;
}) {
  const m = detail.match;
  const wrapRef = useRef<HTMLDivElement>(null);
  const [thread, setThread] = useState<{ path: string; dots: { x: number; y: number }[] } | null>(null);

  // Measure every chip belonging to the selected player (DOM order == kill order)
  // and connect their centers with a smooth vertical path.
  const drawThread = useCallback(() => {
    const wrap = wrapRef.current;
    if (!wrap || !selectedPlayer) {
      setThread(null);
      return;
    }
    const wrapRect = wrap.getBoundingClientRect();
    const chips = Array.from(wrap.querySelectorAll<HTMLElement>("[data-player]")).filter(
      (el) => el.dataset.player === selectedPlayer
    );
    if (chips.length === 0) {
      setThread(null);
      return;
    }
    const pts = chips.map((el) => {
      const r = el.getBoundingClientRect();
      return { x: r.left - wrapRect.left + r.width / 2, y: r.top - wrapRect.top + r.height / 2 };
    });
    let path = `M ${pts[0].x} ${pts[0].y}`;
    for (let i = 1; i < pts.length; i++) {
      const a = pts[i - 1];
      const b = pts[i];
      const dy = Math.min(60, Math.max(18, (b.y - a.y) / 2));
      path += ` C ${a.x} ${a.y + dy}, ${b.x} ${b.y - dy}, ${b.x} ${b.y}`;
    }
    setThread({ path, dots: pts });
  }, [selectedPlayer]);

  useLayoutEffect(() => {
    drawThread();
  }, [drawThread, detail]);

  useEffect(() => {
    window.addEventListener("resize", drawThread);
    return () => window.removeEventListener("resize", drawThread);
  }, [drawThread]);

  const start = ts(m.start_time);
  const end = ts(m.end_time);
  const durMin = Math.max(1, Math.round((end - start) / 60000));

  return (
    <div style={{ minWidth: 0 }}>
      <div style={{ marginBottom: "0.75rem" }}>
        <h1 style={{ margin: 0, fontSize: "1.15rem" }}>
          {m.streamer}
          <span style={{ color: "#888", fontWeight: 400, fontSize: "0.85rem", marginLeft: 10 }}>
            {m.start_time.slice(0, 16)} · {durMin} min · {m.kill_count} kills · {m.players_observed} players
          </span>
        </h1>
        {m.merged_from ? (
          <p style={{ margin: "4px 0 0", color: "#e8d5a3", fontSize: "0.72rem" }}>
            Merged view — also includes: {m.merged_from.split(",").join(", ")}
          </p>
        ) : null}
        <p style={{ margin: "4px 0 0", color: "#666", fontSize: "0.72rem" }}>
          {selectedPlayer
            ? <>Thread: <span style={{ color: "#e8d5a3" }}>{selectedPlayer}</span> — click the chip again to clear.</>
            : "Click any player to thread their mentions through the match."}
        </p>
      </div>

      <div
        ref={wrapRef}
        style={{
          position: "relative", border: "1px solid #1a1a1a", borderRadius: 4,
          background: "#0d0d0d", padding: "14px 10px", overflow: "hidden",
        }}
      >
        {thread && (
          <svg
            style={{ position: "absolute", inset: 0, width: "100%", height: "100%", pointerEvents: "none" }}
            aria-hidden="true"
          >
            <path d={thread.path} fill="none" stroke="#e8d5a3" strokeWidth={2} strokeOpacity={0.85} />
            {thread.dots.map((p, i) => (
              <circle key={i} cx={p.x} cy={p.y} r={3.5} fill="#e8d5a3" />
            ))}
          </svg>
        )}

        {detail.kills.map((k, i) => {
          const prev = i > 0 ? detail.kills[i - 1] : null;
          const gap = prev ? (ts(k.timestamp) - ts(prev.timestamp)) / 1000 : 0;
          return (
            <div key={k.kill_order}>
              {prev && gap > GAP_MARKER_SECONDS && (
                <div style={{ textAlign: "center", color: "#555", fontSize: "0.68rem", padding: "6px 0" }}>
                  ····· {fmtGap(gap)} later ·····
                </div>
              )}
              <KillRow kill={k} selectedPlayer={selectedPlayer} onSelectPlayer={onSelectPlayer} />
            </div>
          );
        })}
      </div>
    </div>
  );
}

function KillRow({
  kill, selectedPlayer, onSelectPlayer,
}: {
  kill: MatchKill;
  selectedPlayer: string | null;
  onSelectPlayer: (p: string | null) => void;
}) {
  const involved =
    !selectedPlayer || kill.attacker === selectedPlayer || kill.victim === selectedPlayer;
  return (
    <div
      style={{
        display: "flex", alignItems: "center", gap: 8, padding: "4px 0", flexWrap: "wrap",
        opacity: involved ? 1 : 0.3, transition: "opacity 120ms",
      }}
    >
      <span style={{
        color: "#555", fontSize: "0.7rem", width: 74, flexShrink: 0,
        fontVariantNumeric: "tabular-nums",
      }}>
        #{kill.kill_order} {clock(kill.timestamp)}
      </span>
      <PlayerChip name={kill.attacker} role="attacker" selectedPlayer={selectedPlayer} onSelectPlayer={onSelectPlayer} />
      <span style={{ color: "#666", fontSize: "0.75rem", flexShrink: 0 }} aria-label="eliminated">☠</span>
      <PlayerChip name={kill.victim} role="victim" selectedPlayer={selectedPlayer} onSelectPlayer={onSelectPlayer} />
    </div>
  );
}

function PlayerChip({
  name, role, selectedPlayer, onSelectPlayer,
}: {
  name: string | null;
  role: "attacker" | "victim";
  selectedPlayer: string | null;
  onSelectPlayer: (p: string | null) => void;
}) {
  if (!name) return <span style={{ color: "#444", fontSize: "0.78rem" }}>—</span>;
  const selected = name === selectedPlayer;
  return (
    <button
      data-player={name}
      onClick={() => onSelectPlayer(selected ? null : name)}
      title={selected ? "Clear selection" : `Thread ${name}`}
      style={{
        cursor: "pointer", fontSize: "0.78rem", padding: "2px 8px", borderRadius: 4,
        background: selected ? "#221d0f" : "#141414",
        border: selected ? "1px solid #e8d5a3" : "1px solid #262626",
        color: role === "attacker" ? "#7cf" : "#f87",
        whiteSpace: "nowrap",
      }}
    >
      {name}
    </button>
  );
}

/* ------------------------------- player rail ----------------------------- */

function PlayerRail({
  detail, selectedPlayer, onSelectPlayer,
}: {
  detail: MatchDetail;
  selectedPlayer: string | null;
  onSelectPlayer: (p: string | null) => void;
}) {
  const stats = new Map<string, PlayerStats>();
  const bump = (name: string | null, field: "kills" | "deaths") => {
    if (!name) return;
    const s = stats.get(name) ?? { name, kills: 0, deaths: 0 };
    s[field] += 1;
    stats.set(name, s);
  };
  detail.kills.forEach((k) => {
    bump(k.attacker, "kills");
    bump(k.victim, "deaths");
  });
  detail.placements.forEach((p) => {
    const s = stats.get(p.player) ?? { name: p.player, kills: 0, deaths: 0 };
    s.placement = p;
    stats.set(p.player, s);
  });
  const players = Array.from(stats.values()).sort(
    (a, b) => b.kills - a.kills || b.deaths - a.deaths || a.name.localeCompare(b.name)
  );

  return (
    <div style={{
      border: "1px solid #1a1a1a", borderRadius: 4, maxHeight: "76vh", overflowY: "auto",
      position: "sticky", top: "1rem",
    }}>
      <div style={{
        padding: "6px 10px", color: "#888", fontSize: "0.7rem", letterSpacing: "0.06em",
        textTransform: "uppercase", borderBottom: "1px solid #1a1a1a", position: "sticky",
        top: 0, background: "#0f0f0f",
      }}>
        Players ({players.length})
      </div>
      {players.map((p) => {
        const selected = p.name === selectedPlayer;
        const pl = p.placement;
        return (
          <button
            key={p.name}
            onClick={() => onSelectPlayer(selected ? null : p.name)}
            style={{
              display: "block", width: "100%", textAlign: "left", cursor: "pointer",
              padding: "6px 10px", border: "none", borderBottom: "1px solid #141414",
              background: selected ? "#1c1910" : "transparent",
              borderLeft: selected ? "2px solid #e8d5a3" : "2px solid transparent",
            }}
          >
            <span style={{ color: selected ? "#e8d5a3" : "#ccc", fontSize: "0.78rem" }}>{p.name}</span>
            <br />
            <span style={{ color: "#777", fontSize: "0.68rem", fontVariantNumeric: "tabular-nums" }}>
              {p.kills}K / {p.deaths}D
              {pl && (
                <>
                  {" · "}
                  {pl.survived ? `alive past #${pl.kill_order_out}` : `out #${pl.kill_order_out}`}
                  {" · "}
                  <span style={{ color: pl.elo_change >= 0 ? "#6f6" : "#f86" }}>
                    {pl.elo_change >= 0 ? "+" : ""}{Math.round(pl.elo_change)}
                  </span>
                </>
              )}
            </span>
          </button>
        );
      })}
    </div>
  );
}
