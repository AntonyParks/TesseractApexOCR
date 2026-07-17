import { api, cropUrl, AuditMatch, AuditKill } from "@/lib/api";

export const revalidate = 30;

// Admin/power-user audit surface: trace a player's ELO back to the source killfeed reads and crops.
// No auth — the whole site is open (read-only API); this is a diagnostic view, not a privileged one.
export default async function AuditPage({
  searchParams,
}: {
  searchParams: { player?: string };
}) {
  const player = (searchParams.player ?? "").trim();

  return (
    <>
      <div style={{ display: "flex", alignItems: "baseline", gap: "1rem", marginBottom: "0.4rem" }}>
        <h1 style={{ margin: 0, fontSize: "1.4rem" }}>Audit</h1>
        <span style={{ color: "#888", fontSize: "0.85rem" }}>OCR → ELO traceability</span>
      </div>
      <p style={{ color: "#777", fontSize: "0.8rem", marginTop: 0 }}>
        Trace a player&apos;s rating back to the exact killfeed reads that produced it — raw OCR vs.
        canonical name, the kill/knock icon vote, how many reads collapsed into each event, and the
        source crop image.
      </p>

      <form method="get" style={{ display: "flex", gap: "0.5rem", margin: "1rem 0 1.5rem" }}>
        <input
          type="text"
          name="player"
          defaultValue={player}
          placeholder="Exact player name (e.g. from the leaderboard)"
          style={{
            flex: "1 1 auto", maxWidth: 420, padding: "7px 10px", borderRadius: 4,
            border: "1px solid #333", background: "#111", color: "#eee", fontSize: "0.9rem",
          }}
        />
        <button type="submit" style={pageBtn}>Trace</button>
      </form>

      {player ? <PlayerTrace player={player} /> : (
        <p style={{ color: "#666", fontSize: "0.85rem" }}>Enter a player name above to load their audit trace.</p>
      )}
    </>
  );
}

async function PlayerTrace({ player }: { player: string }) {
  let data;
  try {
    data = await api.auditPlayer(player);
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    return (
      <p style={{ color: "#f87", fontSize: "0.9rem" }}>
        {msg.startsWith("404") ? `No rated player named "${player}".` : "Could not reach API."}
      </p>
    );
  }

  const p = data.player;
  return (
    <>
      <div style={{ display: "flex", gap: "1.5rem", alignItems: "baseline", marginBottom: "1rem",
                    borderBottom: "1px solid #222", paddingBottom: "0.6rem" }}>
        <span style={{ fontSize: "1.15rem", color: "#e8d5a3", fontWeight: 600 }}>{p.player}</span>
        <span style={{ color: "#7cf" }}>ELO {Math.round(p.elo)}</span>
        <span style={{ color: "#888", fontSize: "0.85rem" }}>
          {p.matches_played} matches · {p.total_kills} K / {p.total_deaths} D · peak {Math.round(p.peak_elo)}
        </span>
      </div>

      {data.matches.length === 0 && (
        <p style={{ color: "#666" }}>No match placements recorded for this player.</p>
      )}
      {data.matches.map((m) => <MatchCard key={m.match_id} m={m} player={player} />)}
    </>
  );
}

function MatchCard({ m, player }: { m: AuditMatch; player: string }) {
  const up = m.elo_change >= 0;
  return (
    <div style={{ border: "1px solid #222", borderRadius: 6, marginBottom: "1rem", overflow: "hidden" }}>
      <div style={{ display: "flex", gap: "1rem", alignItems: "baseline", flexWrap: "wrap",
                    background: "#141414", padding: "8px 12px", borderBottom: "1px solid #222" }}>
        <span style={{ color: "#ccc", fontSize: "0.85rem" }}>{new Date(m.start_time).toLocaleString()}</span>
        <span style={{ color: "#e8d5a3", fontSize: "0.85rem" }}>{m.streamer}</span>
        <span style={{ color: "#888", fontSize: "0.8rem" }}>
          {m.survived ? "survived (floor)" : "eliminated"} @ order {m.kill_order_out} of {m.kill_count}
        </span>
        <span style={{ marginLeft: "auto", fontSize: "0.85rem", color: "#aaa" }}>
          {Math.round(m.elo_before)} → {Math.round(m.elo_after)}{" "}
          <span style={{ color: up ? "#6c9" : "#f77", fontWeight: 600 }}>
            ({up ? "+" : ""}{Math.round(m.elo_change)})
          </span>
        </span>
      </div>
      <div>
        {m.kills.length === 0 && (
          <p style={{ color: "#666", fontSize: "0.8rem", padding: "8px 12px", margin: 0 }}>
            No contributing kills involving this player in this match.
          </p>
        )}
        {m.kills.map((k, i) => <KillRow key={i} k={k} player={player} />)}
      </div>
    </div>
  );
}

function KillRow({ k, player }: { k: AuditKill; player: string }) {
  const isAttacker = k.attacker === player;
  const crop = k.crop_streamer && k.crop_filename ? cropUrl(k.crop_streamer, k.crop_filename) : null;
  const nameMismatch = k.raw_text != null && k.canonical != null && k.raw_text !== k.canonical;
  return (
    <div style={{ display: "flex", gap: "0.9rem", padding: "9px 12px", borderTop: "1px solid #191919",
                  alignItems: "flex-start", fontSize: "0.82rem" }}>
      <div style={{ flex: "0 0 168px" }}>
        {crop ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={crop} alt="source crop" style={{ maxWidth: 168, maxHeight: 44, borderRadius: 3,
                 border: "1px solid #2a2a2a", display: "block", background: "#000" }} />
        ) : (
          <span style={{ color: "#555", fontStyle: "italic" }}>no crop</span>
        )}
      </div>
      <div style={{ flex: "1 1 auto", minWidth: 0 }}>
        <div style={{ color: "#ddd" }}>
          <span style={{ color: isAttacker ? "#6c9" : "#ccc", fontWeight: isAttacker ? 600 : 400 }}>
            {k.attacker || "—"}
          </span>
          <span style={{ color: "#666" }}> ▸ </span>
          <span style={{ color: !isAttacker ? "#f99" : "#ccc", fontWeight: !isAttacker ? 600 : 400 }}>
            {k.victim || "—"}
          </span>
          <span style={{ color: "#777", marginLeft: 8 }}>#{k.kill_order}</span>
        </div>
        {k.raw_text != null ? (
          <div style={{ color: "#8a8a8a", marginTop: 2, wordBreak: "break-word" }}>
            raw: <code style={code}>{k.raw_text}</code>
            {nameMismatch && (
              <> · canon: <code style={{ ...code, color: "#9cf" }}>{k.canonical}</code></>
            )}
          </div>
        ) : (
          <div style={{ color: "#555", marginTop: 2, fontStyle: "italic" }}>
            no source event linked (pre-backlink row)
          </div>
        )}
      </div>
      <div style={{ flex: "0 0 auto", textAlign: "right", color: "#888", whiteSpace: "nowrap" }}>
        {k.event_type && <Tag>{k.event_type}</Tag>}
        {k.icon_vote && <Tag color={k.icon_vote === "kill" ? "#f77" : "#7ac"}>vote:{k.icon_vote}</Tag>}
        {k.read_count != null && k.read_count > 1 && <Tag color="#c9a">×{k.read_count} reads</Tag>}
        <div style={{ marginTop: 3, color: "#666" }}>
          conf {k.attacker_conf.toFixed(2)}/{k.victim_conf.toFixed(2)}
        </div>
      </div>
    </div>
  );
}

function Tag({ children, color = "#888" }: { children: React.ReactNode; color?: string }) {
  return (
    <span style={{ display: "inline-block", marginLeft: 6, padding: "1px 6px", borderRadius: 3,
                   border: `1px solid ${color}44`, color, fontSize: "0.72rem" }}>
      {children}
    </span>
  );
}

const code: React.CSSProperties = {
  background: "#0d0d0d", padding: "1px 4px", borderRadius: 3, fontSize: "0.78rem", color: "#bbb",
};
const pageBtn: React.CSSProperties = {
  padding: "6px 16px", borderRadius: 4, border: "1px solid #333",
  color: "#e8d5a3", background: "#161616", fontSize: "0.85rem", cursor: "pointer",
};
