import Link from "next/link";
import { api, MatchKill, MatchPlacement } from "@/lib/api";
import { notFound } from "next/navigation";

export const revalidate = 60;

export default async function MatchDetailPage({ params }: { params: { id: string } }) {
  const id = decodeURIComponent(params.id);

  let data;
  try {
    data = await api.match(id);
  } catch (e: unknown) {
    if (e instanceof Error && e.message.startsWith("404")) notFound();
    return <p style={{ color: "#f87" }}>Could not reach API.</p>;
  }

  const m = data.match;

  return (
    <>
      <Link href="/matches" style={{ color: "#888", textDecoration: "none", fontSize: "0.85rem" }}>
        ← Matches
      </Link>

      <h1 style={{ marginTop: "0.75rem", fontSize: "1.4rem" }}>
        {new Date(m.start_time).toLocaleString()}
      </h1>
      <p style={{ color: "#888", marginTop: "0.25rem" }}>
        {m.streamer} · {m.kill_count} kills · {m.players_observed} players
      </p>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "2rem", marginTop: "1.5rem" }}>
        {/* ELO Changes */}
        <div>
          <h2 style={{ fontSize: "1rem", color: "#aaa", marginTop: 0 }}>ELO Changes</h2>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.85rem" }}>
            <thead>
              <tr style={{ borderBottom: "1px solid #333", color: "#888" }}>
                <th style={th}>Player</th>
                <th style={{ ...th, textAlign: "right" }}>+/−</th>
                <th style={{ ...th, textAlign: "right" }}>ELO After</th>
              </tr>
            </thead>
            <tbody>
              {[...data.placements]
                .sort((a, b) => b.elo_change - a.elo_change)
                .map((pl: MatchPlacement) => (
                  <tr key={pl.player} style={{ borderBottom: "1px solid #1a1a1a" }}>
                    <td style={td}>
                      <Link href={`/player/${encodeURIComponent(pl.player)}`} style={{ color: "#e8d5a3", textDecoration: "none" }}>
                        {pl.player}
                      </Link>
                    </td>
                    <td style={{ ...td, textAlign: "right", color: pl.elo_change >= 0 ? "#6f6" : "#f86" }}>
                      {pl.elo_change >= 0 ? "+" : ""}{Math.round(pl.elo_change)}
                    </td>
                    <td style={{ ...td, textAlign: "right", fontWeight: 600 }}>
                      {Math.round(pl.elo_after)}
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>

        {/* Kill Feed */}
        <div>
          <h2 style={{ fontSize: "1rem", color: "#aaa", marginTop: 0 }}>Kill Feed</h2>
          <div style={{ maxHeight: "480px", overflowY: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.8rem" }}>
              <thead>
                <tr style={{ borderBottom: "1px solid #333", color: "#888" }}>
                  <th style={th}>Time</th>
                  <th style={th}>Attacker</th>
                  <th style={th}>Victim</th>
                </tr>
              </thead>
              <tbody>
                {data.kills.map((k: MatchKill) => (
                  <tr key={k.kill_order} style={{ borderBottom: "1px solid #111" }}>
                    <td style={{ ...td, color: "#555", whiteSpace: "nowrap" }}>
                      {new Date(k.timestamp).toLocaleTimeString()}
                    </td>
                    <td style={td}>
                      <Link href={`/player/${encodeURIComponent(k.attacker)}`} style={{ color: "#7cf", textDecoration: "none" }}>
                        {k.attacker}
                      </Link>
                    </td>
                    <td style={td}>
                      <Link href={`/player/${encodeURIComponent(k.victim)}`} style={{ color: "#f87", textDecoration: "none" }}>
                        {k.victim}
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </>
  );
}

const th: React.CSSProperties = { padding: "6px 8px", fontWeight: 500, textAlign: "left" };
const td: React.CSSProperties = { padding: "6px 8px" };
